"""RAG sur les DCE PDF — indexation et question-réponse par similarité cosinus."""

import asyncio
import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.data.downloader import download_resource
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"
_MAX_PAGES = 100
_CHUNK_SIZE = 800
_CHUNK_OVERLAP = 100
_MAX_PDF_MB = 50


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_dce_rag(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dce_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ao_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding TEXT NOT NULL,
                indexed_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dce_chunks_ao_id ON dce_chunks(ao_id)"
        )
        conn.commit()


def _extract_text_from_pdf(path: Path) -> str:
    try:
        import fitz
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF (fitz) est requis pour lire les PDF DCE. "
            "Installe-le avec : uv add pymupdf  (ou  pip install pymupdf)"
        ) from exc

    text_parts: list[str] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= _MAX_PAGES:
                logger.warning("PDF tronqué à %s pages", _MAX_PAGES)
                break
            text_parts.append(page.get_text())
    return "\n".join(text_parts)


def _chunk_text(
    text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP
) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    step = chunk_size - overlap
    i = 0
    while i < len(words):
        chunk = words[i : i + chunk_size]
        chunks.append(" ".join(chunk))
        i += step
        if len(chunk) < chunk_size:
            break
    return chunks


async def _embed_chunks(chunks: list[str], llm: LLMClient) -> list[list[float]]:
    """Embedde les chunks en parallèle (max 5 simultanés) pour ne pas surcharger l'API."""
    semaphore = asyncio.Semaphore(5)
    embeddings: list[list[float]] = [[] for _ in chunks]

    async def _embed_one(i: int, chunk: str) -> None:
        async with semaphore:
            try:
                emb = await asyncio.wait_for(llm.embed(chunk), timeout=15)
                embeddings[i] = emb
            except Exception:
                logger.exception("Échec embedding chunk %s", i)

    await asyncio.gather(*[_embed_one(i, c) for i, c in enumerate(chunks)])
    return embeddings


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def is_dce_indexed(cache_dir: str, ao_id: str) -> bool:
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM dce_chunks WHERE ao_id = ? LIMIT 1", (ao_id,)
        ).fetchone()
    return row is not None


async def index_dce_pdf(
    ao_id: str,
    pdf_url: str,
    settings: Settings,
    llm: LLMClient,
) -> dict[str, Any]:
    """Télécharge un DCE PDF, l'extrait, le découpe, embed et stocke."""
    _init_dce_rag(settings.cache_dir)

    # Nettoyer les anciens chunks pour cet AO
    db_path = _get_db_path(settings.cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM dce_chunks WHERE ao_id = ?", (ao_id,))
        conn.commit()

    # Télécharger
    try:
        path = await download_resource(
            pdf_url,
            settings.cache_dir,
            settings.max_download_mb,
            max_mb_override=_MAX_PDF_MB,
        )
    except Exception as exc:
        logger.exception("Échec téléchargement DCE")
        return {"success": False, "error": f"Téléchargement échoué : {exc}"}

    # Extraire le texte (dans un thread pour ne pas bloquer l'évent loop)
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_extract_text_from_pdf, path),
            timeout=20,
        )
    except Exception as exc:
        logger.exception("Échec extraction PDF")
        return {"success": False, "error": f"Extraction PDF échouée : {exc}"}

    if not text.strip():
        return {"success": False, "error": "Aucun texte extractible dans ce PDF."}

    # Découper
    chunks = _chunk_text(text)
    if not chunks:
        return {"success": False, "error": "Texte trop court pour être découpé."}

    # Embed
    try:
        embeddings = await asyncio.wait_for(
            _embed_chunks(chunks, llm),
            timeout=120,
        )
    except Exception as exc:
        logger.exception("Échec embeddings")
        return {"success": False, "error": f"Embeddings échoués : {exc}"}

    # Stocker
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        for idx, (chunk, emb) in enumerate(zip(chunks, embeddings, strict=False)):
            if emb:
                conn.execute(
                    """
                    INSERT INTO dce_chunks
                    (ao_id, chunk_index, chunk_text, embedding, indexed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ao_id, idx, chunk, json.dumps(emb), now),
                )
        conn.commit()

    return {
        "success": True,
        "chunks": len(chunks),
        "pages": min(_MAX_PAGES, len(text) // 2000 + 1),
    }


async def query_dce_rag(
    ao_id: str,
    question: str,
    llm: LLMClient,
    cache_dir: str,
    top_k: int = 5,
) -> str | None:
    """Interroge le DCE indexé d'un AO par RAG. Retourne None si non indexé."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT chunk_text, embedding FROM dce_chunks WHERE ao_id = ?",
            (ao_id,),
        ).fetchall()

    if not rows:
        return None

    # Embed la question
    try:
        q_emb = await asyncio.wait_for(llm.embed(question), timeout=15)
    except Exception:
        logger.exception("Échec embedding de la question")
        return None

    # Cosine similarity
    scored: list[tuple[float, str]] = []
    for chunk_text, emb_json in rows:
        try:
            emb = json.loads(emb_json)
        except json.JSONDecodeError:
            continue
        score = _cosine_similarity(q_emb, emb)
        scored.append((score, chunk_text))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    if not top:
        return "Je n'ai pas trouvé de passage pertinent dans le DCE."

    context = "\n\n---\n\n".join(
        f"[Extrait {i + 1}]\n{text}" for i, (_, text) in enumerate(top)
    )

    messages = [
        {
            "role": "system",
            "content": (
                "Tu es un assistant expert en marchés publics français. "
                "Tu réponds à la question de l'utilisateur UNIQUEMENT à partir des extraits "
                "du cahier des charges (DCE) fournis ci-dessous. "
                "Si la réponse n'est pas dans les extraits, dis-le clairement. "
                "Sois précis, concis, et cite les passages pertinents quand c'est possible. "
                "N'invente jamais d'information qui ne figure pas dans les extraits."
            ),
        },
        {
            "role": "user",
            "content": f"Extraits du DCE :\n\n{context}\n\n---\n\nQuestion : {question}",
        },
    ]

    try:
        response = await asyncio.wait_for(llm.chat(messages), timeout=30)
        return response.content or "(réponse vide du modèle)"
    except Exception:
        logger.exception("Échec génération réponse RAG")
        return "❌ Désolé, je n'ai pas pu générer la réponse à partir du DCE."
