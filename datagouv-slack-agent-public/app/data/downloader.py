"""Téléchargement de ressources avec cache local et limites de taille.

Principe : streaming httpx, vérification du Content-Length avant téléchargement,
coupure si la taille réelle dépasse la limite, cache indexé par hash de l'URL.
Le LLM ne voit jamais ces fichiers : ils sont analysés localement.
"""

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Erreur contrôlée de téléchargement (taille, réseau, statut HTTP)."""


def _detect_suffix(url: str, content_type: str | None = None) -> str:
    """Détecte l'extension du fichier à partir de l'URL ou du Content-Type."""
    parsed = urlparse(url)
    path_suffix = Path(parsed.path).suffix.lower()
    if path_suffix:
        return path_suffix

    # Détection par le dernier segment du path (ex: /exports/csv, /api/json)
    last_segment = Path(parsed.path).name.lower()
    if "csv" in last_segment:
        return ".csv"
    elif "json" in last_segment:
        return ".json"
    elif "parquet" in last_segment:
        return ".parquet"
    elif "xls" in last_segment or "excel" in last_segment or "spreadsheet" in last_segment:
        return ".xlsx"
    elif "zip" in last_segment:
        return ".zip"
    elif "gz" in last_segment:
        return ".gz"

    if content_type:
        ct = content_type.lower()
        if "csv" in ct:
            return ".csv"
        elif "json" in ct:
            return ".json"
        elif "parquet" in ct:
            return ".parquet"
        elif "zip" in ct:
            return ".zip"
        elif "gzip" in ct or "gz" in ct:
            return ".gz"
        elif "excel" in ct or "spreadsheet" in ct or "xls" in ct:
            return ".xlsx"

    return ".bin"


def _cache_path(cache_dir: Path, url: str, suffix: str | None = None) -> Path:
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    if suffix is None:
        suffix = _detect_suffix(url)
    return cache_dir / f"{url_hash}{suffix}"


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def download_resource(
    url: str, cache_dir: str, max_mb: int, max_mb_override: int | None = None
) -> Path:
    """Télécharge une ressource et retourne le chemin local (cache si déjà présent).

    `max_mb_override` permet d'outrepasser la limite globale pour des fichiers
    spécifiques connus (ex: DECP 2024 = 532 Mo).
    """
    cache = Path(cache_dir)
    await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True)

    # 1. Deviner le suffixe initial depuis l'URL
    initial_suffix = _detect_suffix(url)
    target = _cache_path(cache, url, initial_suffix)

    # 2. Vérifier le cache avec le suffixe initial (jamais .bin, on veut le vrai format)
    if target.exists() and target.stat().st_size > 0 and initial_suffix != ".bin":
        logger.info("Cache hit pour %s -> %s", url, target)
        return target

    effective_max_mb = max_mb_override if max_mb_override is not None else max_mb
    max_bytes = effective_max_mb * 1024 * 1024

    # Nettoyer les fichiers .part orphelins du même target (crash précédent)
    for orphan in await asyncio.to_thread(lambda: list(cache.glob(f"{target.stem}.part_*"))):
        try:
            orphan.unlink(missing_ok=True)
            logger.info("Orphelin nettoyé: %s", orphan.name)
        except OSError:
            pass

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30, read=30),
        headers={"User-Agent": "datagouv-slack-agent/0.1.0"},
    ) as client, client.stream("GET", url) as response:
        if response.status_code >= 400:
            raise DownloadError(
                f"HTTP {response.status_code} en téléchargeant {url}"
            )

        declared = response.headers.get("content-length")
        if declared:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > max_bytes:
                raise DownloadError(
                    f"Fichier trop volumineux: {declared_bytes / 1e6:.0f} Mo "
                    f"(limite {max_mb} Mo). URL: {url}"
                )

        # 3. Si le suffixe initial était .bin, essayer de deviner avec Content-Type
        content_type = response.headers.get("content-type", "")
        if initial_suffix == ".bin":
            real_suffix = _detect_suffix(url, content_type)
            if real_suffix != ".bin":
                new_target = _cache_path(cache, url, real_suffix)
                if new_target.exists() and new_target.stat().st_size > 0:
                    logger.info("Cache hit (format détecté) pour %s -> %s", url, new_target)
                    return new_target
                target = new_target

        tmp = target.with_suffix(target.suffix + f".part_{uuid.uuid4().hex[:8]}")
        written = 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in response.aiter_bytes(chunk_size=1 << 20):
                    written += len(chunk)
                    if written > max_bytes:
                        raise DownloadError(
                            f"Téléchargement interrompu: taille réelle "
                            f"> {max_mb} Mo. URL: {url}"
                        )
                    fh.write(chunk)
            tmp.rename(target)
        finally:
            tmp.unlink(missing_ok=True)

    # [MAGIC NUMBER] Détection post-téléchargement : certains serveurs renvoient
    # des ZIP avec une extension trompeuse (ex: _CSV_FR sur Mélodi).
    if target.suffix == ".csv":
        with target.open("rb") as fh:
            magic = fh.read(4)
        if magic == b"PK\x03\x04":
            new_target = target.with_suffix(".zip")
            target.rename(new_target)
            target = new_target
            logger.info("Magic number ZIP détecté, fichier renommé -> %s", target)

    logger.info("Téléchargé %s (%.1f Mo) -> %s", url, written / 1e6, target)
    return target
