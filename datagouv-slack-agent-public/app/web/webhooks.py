"""Gestion des webhooks pour notifications push AO vers des systèmes tiers."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"


def _get_db_path(cache_dir: str) -> Path:
    return Path(cache_dir) / DB_NAME


def _init_webhooks(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                url TEXT NOT NULL,
                secret TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def add_webhook(cache_dir: str, user_id: str, url: str, secret: str | None = None) -> int:
    _init_webhooks(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO webhooks (user_id, url, secret) VALUES (?, ?, ?)",
            (user_id, url, secret),
        )
        conn.commit()
        return cursor.lastrowid or 0


def list_webhooks(cache_dir: str, user_id: str) -> list[dict]:
    _init_webhooks(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, url, secret, active, created_at FROM webhooks WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def remove_webhook(cache_dir: str, webhook_id: int, user_id: str) -> bool:
    _init_webhooks(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM webhooks WHERE id = ? AND user_id = ?",
            (webhook_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_active_webhooks(cache_dir: str, user_id: str) -> list[dict]:
    _init_webhooks(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, url, secret FROM webhooks WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


async def send_webhook_notification(
    url: str,
    payload: dict[str, Any],
    secret: str | None = None,
) -> bool:
    """Envoie un POST JSON vers un webhook avec signature HMAC optionnelle."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "datagouv-agent/0.1.0",
    }
    if secret:
        import hashlib
        import hmac
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info("Webhook envoyé à %s (HTTP %s)", url, resp.status_code)
            return True
    except Exception:
        logger.exception("Échec envoi webhook à %s", url)
        return False


async def notify_webhooks(
    cache_dir: str,
    user_id: str,
    notification: dict[str, Any],
) -> None:
    """Envoie une notification à tous les webhooks actifs d'un utilisateur."""
    hooks = get_active_webhooks(cache_dir, user_id)
    if not hooks:
        return
    for h in hooks:
        await send_webhook_notification(
            h["url"],
            notification,
            secret=h.get("secret"),
        )
