"""Gestion des clés API pour l'accès programmatique."""

import logging
import secrets
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"


def _get_db_path(cache_dir: str) -> Path:
    return Path(cache_dir) / DB_NAME


def _init_api_keys(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                user_id TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def generate_api_key(cache_dir: str, user_id: str) -> str:
    """Génère une nouvelle clé API pour l'utilisateur (écrase l'ancienne)."""
    _init_api_keys(cache_dir)
    key = "dgv_" + secrets.token_urlsafe(32)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_keys (user_id, api_key, created_at) VALUES (?, ?, datetime('now'))",
            (user_id, key),
        )
        conn.commit()
    return key


def get_api_key(cache_dir: str, user_id: str) -> str | None:
    """Retourne la clé API existante d'un utilisateur, ou None."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT api_key FROM api_keys WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else None


def get_user_id_from_key(cache_dir: str, api_key: str) -> str | None:
    """Valide une clé API et retourne le user_id associé."""
    if not api_key:
        return None
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM api_keys WHERE api_key = ?", (api_key,)
        ).fetchone()
    return row[0] if row else None
