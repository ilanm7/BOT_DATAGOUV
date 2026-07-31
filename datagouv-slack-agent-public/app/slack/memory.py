"""Mémoire persistante des conversations en SQLite.

Chaque message utilisateur et réponse assistant est stocké pour permettre
au bot de conserver un contexte à long terme, même après redémarrage.
"""

import logging
import random
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_NAME = "bot_memory.db"


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_db(db_path: Path) -> None:
    with sqlite3.connect(db_path, timeout=10) as conn:
        # [WAL MODE] Write-Ahead Logging = lecture concurrente possible pendant l'écriture
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL DEFAULT (strftime('%s', 'now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversations_user
            ON conversations(user_id, timestamp DESC)
            """
        )
        conn.commit()
        # Checkpoint WAL pour éviter la fuite disque sur les fichiers .wal/.shm
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _cleanup_old_messages(db_path: Path, days: int = 30) -> None:
    """[NETTOYAGE CACHE] Supprime les messages de plus de N jours et compacte le WAL."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(
                "DELETE FROM conversations WHERE timestamp < strftime('%s', 'now', ?)",
                (f"-{days} days",),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        logger.exception("Erreur cleanup mémoire")


def save_message(
    cache_dir: str,
    user_id: str,
    channel: str,
    thread_ts: str,
    role: str,
    content: str,
) -> None:
    """[MÉMOIRE SQLITE] Sauvegarde un message dans la base SQLite avec WAL mode."""
    db_path = _get_db_path(cache_dir)
    _init_db(db_path)
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO conversations (user_id, channel, thread_ts, role, content) VALUES (?, ?, ?, ?, ?)",
                (user_id, channel, thread_ts, role, content),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Erreur sauvegarde message en base")
    # Cleanup différé : une chance sur 20 pour ne pas ralentir chaque message
    if random.randint(1, 20) == 1:
        _cleanup_old_messages(db_path)


def get_user_memory(
    cache_dir: str,
    user_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """[MÉMOIRE CROSS-THREAD] Récupère les derniers messages d'un utilisateur (tous threads confondus)."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT role, content, thread_ts, datetime(timestamp, 'unixepoch') as date
                FROM conversations
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
    except sqlite3.Error:
        logger.exception("Erreur lecture mémoire utilisateur")
        return []


def get_thread_memory(
    cache_dir: str,
    channel: str,
    thread_ts: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """[MÉMOIRE THREAD] Récupère l'historique d'un thread spécifique."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT role, content, user_id, datetime(timestamp, 'unixepoch') as date
                FROM conversations
                WHERE channel = ? AND thread_ts = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (channel, thread_ts, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
    except sqlite3.Error:
        logger.exception("Erreur lecture mémoire thread")
        return []
