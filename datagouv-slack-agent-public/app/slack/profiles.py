"""Profils clients pour le scoring des appels d'offre."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_profiles(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_profiles (
                user_id TEXT PRIMARY KEY,
                channel TEXT,
                activity TEXT,
                keywords TEXT,
                company_size TEXT,
                sectors TEXT,
                zones TEXT,
                min_amount INTEGER,
                max_amount INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def get_profile(cache_dir: str, user_id: str) -> dict[str, Any] | None:
    """Récupère le profil d'un utilisateur."""
    _init_profiles(cache_dir)
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM client_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "channel": row["channel"],
        "activity": row["activity"] or "",
        "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
        "company_size": row["company_size"] or "",
        "sectors": json.loads(row["sectors"]) if row["sectors"] else [],
        "zones": json.loads(row["zones"]) if row["zones"] else [],
        "min_amount": row["min_amount"] or 0,
        "max_amount": row["max_amount"] or 0,
    }


def upsert_profile(
    cache_dir: str,
    user_id: str,
    channel: str = "",
    activity: str = "",
    keywords: list[str] | None = None,
    company_size: str = "",
    sectors: list[str] | None = None,
    zones: list[str] | None = None,
    min_amount: int = 0,
    max_amount: int = 0,
) -> None:
    """Crée ou met à jour un profil client."""
    _init_profiles(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO client_profiles
            (user_id, channel, activity, keywords, company_size, sectors, zones, min_amount, max_amount, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                channel=excluded.channel,
                activity=excluded.activity,
                keywords=excluded.keywords,
                company_size=excluded.company_size,
                sectors=excluded.sectors,
                zones=excluded.zones,
                min_amount=excluded.min_amount,
                max_amount=excluded.max_amount,
                updated_at=datetime('now')
            """,
            (
                user_id,
                channel,
                activity,
                json.dumps(keywords or [], ensure_ascii=False),
                company_size,
                json.dumps(sectors or [], ensure_ascii=False),
                json.dumps(zones or [], ensure_ascii=False),
                min_amount,
                max_amount,
            ),
        )
        conn.commit()


def list_all_profiles(cache_dir: str) -> list[dict[str, Any]]:
    """Liste tous les profils clients."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM client_profiles ORDER BY updated_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        result.append({
            "user_id": row["user_id"],
            "channel": row["channel"] or "",
            "activity": row["activity"] or "",
            "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
            "company_size": row["company_size"] or "",
            "sectors": json.loads(row["sectors"]) if row["sectors"] else [],
            "zones": json.loads(row["zones"]) if row["zones"] else [],
            "min_amount": row["min_amount"] or 0,
            "max_amount": row["max_amount"] or 0,
            "updated_at": row["updated_at"] or "",
        })
    return result


def format_profile(profile: dict[str, Any]) -> str:
    """Formatte un profil pour affichage Slack."""
    lines = ["*Ton profil AO* :"]
    if profile.get("activity"):
        lines.append(f"• Activité : {profile['activity']}")
    if profile.get("company_size"):
        lines.append(f"• Taille : {profile['company_size']}")
    if profile.get("keywords"):
        lines.append(f"• Mots-clés : {', '.join(profile['keywords'])}")
    if profile.get("sectors"):
        lines.append(f"• Secteurs : {', '.join(profile['sectors'])}")
    if profile.get("zones"):
        lines.append(f"• Zones : {', '.join(profile['zones'])}")
    if profile.get("min_amount"):
        lines.append(f"• Montant min : {profile['min_amount']:,} €".replace(",", " "))
    if profile.get("max_amount"):
        lines.append(f"• Montant max : {profile['max_amount']:,} €".replace(",", " "))
    if len(lines) == 1:
        lines.append("_Aucune info renseignée._")
    lines.append("\n_Modifie avec_ `/datagouv profil activité=BTP taille=PME zones=75,93`")
    return "\n".join(lines)
