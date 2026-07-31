"""Géocodage d'adresses via l'API Adresse de data.gouv.fr (gratuite, sans clé)."""

import logging
import sqlite3
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

API_URL = "https://api-adresse.data.gouv.fr/search/"
CACHE_DB = "geocode_cache.db"


def _init_cache(cache_dir: str) -> Path:
    db_path = Path(cache_dir) / CACHE_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS geocache (
                address TEXT PRIMARY KEY,
                lat REAL,
                lon REAL,
                timestamp REAL DEFAULT (strftime('%s', 'now'))
            )
            """
        )
        conn.commit()
    return db_path


def _cleanup_geocache(cache_dir: str, days: int = 90) -> None:
    """Supprime les entrées de géocodage de plus de N jours."""
    db_path = _init_cache(cache_dir)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "DELETE FROM geocache WHERE timestamp < strftime('%s', 'now', ?)",
                (f"-{days} days",),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Erreur cleanup géocodage")


def _get_cached(cache_dir: str, address: str) -> tuple[float, float] | None:
    db_path = _init_cache(cache_dir)
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT lat, lon FROM geocache WHERE address = ?",
                (address,),
            ).fetchone()
            if row:
                return row[0], row[1]
    except sqlite3.Error:
        logger.exception("Erreur lecture cache géocodage")
    return None


def _save_cache(cache_dir: str, address: str, lat: float, lon: float) -> None:
    db_path = _init_cache(cache_dir)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO geocache (address, lat, lon) VALUES (?, ?, ?)",
                (address, lat, lon),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Erreur sauvegarde cache géocodage")


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def geocode_address(address: str, cache_dir: str = "./cache") -> tuple[float, float] | None:
    """Géocode une adresse et retourne (latitude, longitude) ou None."""
    if not address or not address.strip():
        return None

    addr = address.strip()
    cached = _get_cached(cache_dir, addr)
    if cached:
        logger.debug("Géocodage cache hit: %s", addr)
        return cached

    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
            response = await client.get(
                API_URL,
                params={"q": addr, "limit": 1},
            )
            response.raise_for_status()
            data = response.json()

        features = data.get("features", [])
        if not features:
            return None

        coords = features[0].get("geometry", {}).get("coordinates", [])
        if len(coords) >= 2:
            lat, lon = coords[1], coords[0]
            _save_cache(cache_dir, addr, lat, lon)
            return lat, lon
    except Exception:
        logger.exception("Erreur géocodage pour %s", addr)
    finally:
        # Cleanup différé : une chance sur 50
        import random
        if random.randint(1, 50) == 1:
            _cleanup_geocache(cache_dir)

    return None
