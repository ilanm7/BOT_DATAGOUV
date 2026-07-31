"""Suivi des deadlines d'AO — réaction emoji → rappels J-15 / J-7 / J-2."""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"

_ACCEPTED_REACTIONS = frozenset(
    (
        "white_check_mark",
        "heavy_check_mark",
        "ballot_box_with_check",
        "ok",
        "thumbsup",
        "thumbsup_all",
        "fire",
        "star",
        "eyes",
        "rocket",
    )
)


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_tracking(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ao_messages (
                message_ts TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                ao_id TEXT NOT NULL,
                objet TEXT,
                deadline TEXT,
                sent_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_tenders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                message_ts TEXT NOT NULL,
                ao_id TEXT NOT NULL,
                objet TEXT,
                deadline TEXT,
                reminders_sent TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def is_accepted_reaction(reaction: str) -> bool:
    return reaction in _ACCEPTED_REACTIONS


def save_ao_message(
    cache_dir: str,
    message_ts: str,
    channel: str,
    ao_id: str,
    objet: str,
    deadline: str,
) -> None:
    """Mappe un message Slack vers l'AO qu'il contient (pour retrouver l'AO au moment de la réaction)."""
    _init_tracking(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ao_messages (message_ts, channel, ao_id, objet, deadline) VALUES (?, ?, ?, ?, ?)",
            (message_ts, channel, ao_id, objet, deadline),
        )
        conn.commit()


def get_ao_from_message(cache_dir: str, channel: str, message_ts: str) -> dict | None:
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM ao_messages WHERE channel = ? AND message_ts = ?",
            (channel, message_ts),
        ).fetchone()
    if row:
        return dict(row)
    return None


def track_tender(
    cache_dir: str,
    user_id: str,
    channel: str,
    message_ts: str,
    ao_id: str,
    objet: str,
    deadline: str,
) -> None:
    """Ajoute un AO dans la liste des suivis (idempotente)."""
    _init_tracking(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tracked_tenders
            (user_id, channel, message_ts, ao_id, objet, deadline)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, channel, message_ts, ao_id, objet, deadline),
        )
        conn.commit()


def untrack_tender(cache_dir: str, track_id: int, user_id: str) -> bool:
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM tracked_tenders WHERE id = ? AND user_id = ?",
            (track_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_tracked(cache_dir: str, user_id: str | None = None) -> list[dict]:
    _init_tracking(cache_dir)
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if user_id:
            rows = conn.execute(
                "SELECT * FROM tracked_tenders WHERE user_id = ? ORDER BY deadline ASC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tracked_tenders ORDER BY deadline ASC").fetchall()
        return [dict(r) for r in rows]


def update_reminders(cache_dir: str, track_id: int, reminders: list[str]) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tracked_tenders SET reminders_sent = ? WHERE id = ?",
            (json.dumps(reminders), track_id),
        )
        conn.commit()


async def check_deadline_reminders(cache_dir: str, client) -> None:
    """Envoie les rappels J-15 / J-7 / J-2 sur les AO suivis."""
    tracked = list_tracked(cache_dir)
    if not tracked:
        return

    today = datetime.now().date()
    thresholds = [("J-2", 2), ("J-7", 7), ("J-15", 15)]

    for t in tracked:
        deadline_str = t.get("deadline")
        if not deadline_str:
            continue

        try:
            if "T" in deadline_str or " " in deadline_str:
                deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00")).date()
            else:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
        except Exception:
            continue

        days_left = (deadline - today).days
        if days_left < 0:
            continue

        reminders = json.loads(t.get("reminders_sent") or "[]")
        for label, threshold_days in thresholds:
            if days_left <= threshold_days and label not in reminders:
                try:
                    await client.chat_postMessage(
                        channel=t["channel"],
                        thread_ts=t["message_ts"],
                        text=(
                            f"⏰ *{label}* — Deadline de l'AO : *{t['objet']}*\n"
                            f"📅 Date limite : {deadline.strftime('%d/%m/%Y')}\n"
                            f"🔗 <https://www.boamp.fr/avis/detail/{t['ao_id']}|Voir l'annonce>"
                        ),
                    )
                    reminders.append(label)
                    update_reminders(cache_dir, t["id"], reminders)
                except Exception:
                    logger.exception("Échec envoi rappel deadline")
                break
