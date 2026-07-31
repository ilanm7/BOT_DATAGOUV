"""Classe de base pour les clients API externes — cache SQLite, retry, timeout."""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DB_NAME = "external_apis.db"

RETRYABLE = (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_cache(cache_dir: str, table_name: str, key_col: str = "cache_key", data_col: str = "data") -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                {key_col} TEXT PRIMARY KEY,
                {data_col} TEXT,
                cached_at INTEGER
            )
            """
        )
        conn.commit()


def _get_cached(cache_dir: str, table_name: str, key: str, ttl_days: int = 7) -> Any | None:
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None
    cutoff = int(time.time()) - (ttl_days * 86400)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT data FROM {table_name} WHERE cache_key = ? AND cached_at > ?",
            (key, cutoff),
        ).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None
    return None


def _save_cache(cache_dir: str, table_name: str, key: str, data: Any) -> None:
    _init_cache(cache_dir, table_name)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {table_name} (cache_key, data, cached_at) VALUES (?, ?, ?)",
            (key, json.dumps(data, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


class ExternalAPIClient:
    """Client HTTP extensible avec cache SQLite intégré."""

    def __init__(
        self,
        cache_dir: str,
        base_url: str,
        table_name: str,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        ttl_days: int = 7,
    ):
        self.cache_dir = cache_dir
        self.base_url = base_url.rstrip("/")
        self.table_name = table_name
        self.headers = headers or {}
        self.timeout = timeout
        self.ttl_days = ttl_days

    def _cache_key(self, endpoint: str, params: dict | None = None) -> str:
        key = f"{endpoint}?{json.dumps(params or {}, sort_keys=True)}"
        return key

    def _get(self, endpoint: str, params: dict | None = None) -> Any | None:
        """GET avec cache. Retourne None si échec."""
        cache_key = self._cache_key(endpoint, params)
        cached = _get_cached(self.cache_dir, self.table_name, cache_key, self.ttl_days)
        if cached is not None:
            return cached

        url = f"{self.base_url}{endpoint}"
        try:
            with httpx.Client(timeout=self.timeout, headers=self.headers) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Échec requête %s", url)
            return None

        _save_cache(self.cache_dir, self.table_name, cache_key, data)
        return data

    async def _aget(self, endpoint: str, params: dict | None = None) -> Any | None:
        """Async GET avec cache."""
        cache_key = self._cache_key(endpoint, params)
        cached = _get_cached(self.cache_dir, self.table_name, cache_key, self.ttl_days)
        if cached is not None:
            return cached

        url = f"{self.base_url}{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Échec requête async %s", url)
            return None

        _save_cache(self.cache_dir, self.table_name, cache_key, data)
        return data
