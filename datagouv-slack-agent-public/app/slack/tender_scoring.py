"""Scoring de pertinence des AO par IA avec cache SQLite."""

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_scores(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tender_scores (
                ao_id TEXT NOT NULL,
                profile_hash TEXT NOT NULL,
                score INTEGER NOT NULL,
                reason TEXT NOT NULL,
                scored_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (ao_id, profile_hash)
            )
            """
        )
        conn.commit()


def _profile_hash(profile: dict[str, Any]) -> str:
    """Hash déterministe d'un profil pour le cache."""
    canonical = json.dumps(profile, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def get_cached_score(cache_dir: str, ao_id: str, profile: dict[str, Any]) -> tuple[int, str] | None:
    """Retourne (score, reason) si déjà en cache, sinon None."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None
    ph = _profile_hash(profile)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT score, reason FROM tender_scores WHERE ao_id = ? AND profile_hash = ?",
            (ao_id, ph),
        ).fetchone()
    if row:
        return row[0], row[1]
    return None


def _cache_score(cache_dir: str, ao_id: str, profile: dict[str, Any], score: int, reason: str) -> None:
    _init_scores(cache_dir)
    db_path = _get_db_path(cache_dir)
    ph = _profile_hash(profile)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tender_scores (ao_id, profile_hash, score, reason, scored_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (ao_id, ph, score, reason),
        )
        conn.commit()


_SCORING_PROMPT = """Tu es un expert en marchés publics français. Tu évalues la pertinence d'un appel d'offre pour une entreprise donnée.

Appel d'offre :
- ID : {idweb}
- Objet : {objet}
- Acheteur : {org}
- Départements : {depts}
- Descripteurs : {descripteurs}
- Date limite : {deadline}

Profil de l'entreprise :
- Activité principale : {activity}
- Taille : {company_size}
- Secteurs visés : {sectors}
- Zones géographiques : {zones}
- Mots-clés : {keywords}
- Fourchette de montant (€) : {min_amount} - {max_amount}

INSTRUCTIONS :
1. Attribue un score de 0 à 100 (100 = parfaitement adapté).
2. Rédige UNE SEULE PHRASE de justification (max 140 caractères) expliquant pourquoi ça matche ou pas.
3. Réponds UNIQUEMENT au format JSON strict : {{"score": <int>, "reason": "<phrase>"}}
"""


async def score_tender(
    ao: dict[str, Any],
    profile: dict[str, Any],
    llm: LLMClient,
    cache_dir: str,
) -> tuple[int, str]:
    """
    Score un AO pour un profil donné.
    Retourne (score 0-100, reason).
    Utilise le cache SQLite si disponible.
    """
    ao_id = ao.get("idweb", "")
    cached = get_cached_score(cache_dir, ao_id, profile)
    if cached is not None:
        return cached

    # Fallbacks pour les champs manquants
    def _fmt(val: Any) -> str:
        if isinstance(val, list):
            return ", ".join(str(v) for v in val if v) or "Non précisé"
        return str(val) if val else "Non précisé"

    prompt = _SCORING_PROMPT.format(
        idweb=_fmt(ao.get("idweb")),
        objet=_fmt(ao.get("objet")),
        org=_fmt(ao.get("org")),
        depts=_fmt(ao.get("departements")),
        descripteurs=_fmt(ao.get("descripteurs")),
        deadline=_fmt(ao.get("datelimitereponse")),
        activity=_fmt(profile.get("activity")),
        company_size=_fmt(profile.get("company_size")),
        sectors=_fmt(profile.get("sectors")),
        zones=_fmt(profile.get("zones")),
        keywords=_fmt(profile.get("keywords")),
        min_amount=_fmt(profile.get("min_amount")),
        max_amount=_fmt(profile.get("max_amount")),
    )

    try:
        msg = await llm.chat(
            messages=[
                {"role": "system", "content": "Tu réponds uniquement en JSON strict."},
                {"role": "user", "content": prompt},
            ]
        )
        content = (msg.content or "").strip()
        # Extraire le JSON de la réponse (s'il est dans un bloc markdown)
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        data = json.loads(content)
        score = max(0, min(100, int(data.get("score", 0))))
        reason = str(data.get("reason", "")).strip() or "Correspondance évaluée par IA"
    except Exception:
        logger.exception("Erreur scoring LLM pour AO %s", ao_id)
        score = 50
        reason = "Évaluation par défaut (erreur IA)"

    _cache_score(cache_dir, ao_id, profile, score, reason)
    return score, reason


def format_ao_push(ao: dict[str, Any], score: int, reason: str, rank: int) -> str:
    """Formate un AO pour un push Slack avec badge de score."""
    badge = "🔥" if score >= 85 else "✅" if score >= 70 else "📌"
    lines = [
        f"*{rank}. {badge} {score}/100 — {ao.get('objet', 'Sans titre')}*",
        f"• Acheteur : {ao.get('org', 'Inconnu')}",
    ]
    if ao.get("dateparution"):
        lines.append(f"• Publié le : {ao['dateparution']}")
    if ao.get("datelimitereponse"):
        dl_raw = str(ao["datelimitereponse"])
        try:
            from datetime import datetime
            dl_date = datetime.fromisoformat(dl_raw.replace("Z", "+00:00"))
            dl_str = dl_date.strftime("%d/%m/%Y")
        except Exception:
            dl_str = dl_raw
        lines.append(f"• Date limite : {dl_str}")
    if ao.get("departements"):
        lines.append(f"• Département(s) : {', '.join(str(d) for d in ao['departements'])}")
    lines.append(f"• 💡 {reason}")
    if ao.get("_history"):
        for hl in ao["_history"].splitlines():
            lines.append(f"• {hl}")
    if ao.get("url_avis"):
        lines.append(f"• <{ao['url_avis']}|Voir l'annonce {ao.get('idweb', '')}>")
    return "\n".join(lines)
