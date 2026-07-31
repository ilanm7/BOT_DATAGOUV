"""Alertes proactives sur les appels d'offre (AO) avec scoring IA."""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.agent.local_tools import fetch_boamp_tenders
from app.config import Settings
from app.llm.client import LLMClient
from app.slack.profiles import get_profile
from app.slack.tender_scoring import format_ao_push, score_tender

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_db(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ao_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                query TEXT NOT NULL,
                threshold INTEGER DEFAULT 70,
                created_at TEXT DEFAULT (datetime('now')),
                last_check TEXT,
                last_seen_ids TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ao_push_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                ao_id TEXT NOT NULL,
                objet TEXT,
                score INTEGER,
                reason TEXT,
                pushed_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ao_push_user ON ao_push_history(user_id)"
        )
        conn.commit()


def save_ao_push(
    cache_dir: str,
    user_id: str,
    channel: str,
    ao_id: str,
    objet: str,
    score: int,
    reason: str,
) -> None:
    """Enregistre un AO poussé dans l'historique pour le dashboard."""
    _init_db(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ao_push_history
            (user_id, channel, ao_id, objet, score, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, channel, ao_id, objet, score, reason),
        )
        conn.commit()


def add_ao_alert(
    cache_dir: str,
    user_id: str,
    channel: str,
    query: str,
    threshold: int = 70,
) -> int:
    """Ajoute une alerte AO et retourne son ID."""
    _init_db(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO ao_alerts (user_id, channel, query, threshold) VALUES (?, ?, ?, ?)",
            (user_id, channel, query, threshold),
        )
        conn.commit()
        return cursor.lastrowid or 0


def ensure_iguane_alert(cache_dir: str, channel: str) -> None:
    """Crée l'alerte Iguane par défaut si elle n'existe pas déjà."""
    if not channel:
        return
    _init_db(cache_dir)
    db_path = _get_db_path(cache_dir)
    query_iguane = (
        "IA automatisation agents conversationnels RPA n8n data "
        "intelligence artificielle LLM workflow"
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM ao_alerts WHERE user_id = ? AND query = ?",
            ("__iguane__", query_iguane),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO ao_alerts (user_id, channel, query, threshold) VALUES (?, ?, ?, ?)",
                ("__iguane__", channel, query_iguane, 65),
            )
            conn.commit()
            logger.info("Alerte Iguane créée pour le canal %s", channel)


def list_ao_alerts(cache_dir: str, user_id: str) -> list[dict]:
    """Liste les alertes AO d'un utilisateur."""
    _init_db(cache_dir)
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, query, threshold, created_at FROM ao_alerts WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def remove_ao_alert(cache_dir: str, alert_id: int, user_id: str) -> bool:
    """Supprime une alerte AO si elle appartient à l'utilisateur."""
    _init_db(cache_dir)
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM ao_alerts WHERE id = ? AND user_id = ?",
            (alert_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


async def fetch_ao_by_idweb(idweb: str) -> dict[str, Any] | None:
    """Récupère un AO unique sur BOAMP par son idweb."""
    try:
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}
        ) as client:
            resp = await client.get(
                "https://boamp.fr/api/records/1.0/search/",
                params={
                    "dataset": "boamp",
                    "q": f"idweb:{idweb}",
                    "rows": 1,
                    "refine.nature": "APPEL_OFFRE",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("records", []):
                fields = r.get("fields", {})
                if fields.get("idweb") == idweb:
                    return {
                        "idweb": idweb,
                        "objet": (fields.get("objet") or "").strip(),
                        "org": (fields.get("nomacheteur") or "").strip(),
                        "dateparution": fields.get("dateparution"),
                        "datelimitereponse": fields.get("datelimitereponse"),
                        "url_avis": fields.get("url_avis", ""),
                        "departements": fields.get("code_departement") if isinstance(fields.get("code_departement"), list) else ([fields.get("code_departement")] if fields.get("code_departement") else []),
                        "descripteurs": fields.get("descripteur_libelle") if isinstance(fields.get("descripteur_libelle"), list) else ([fields.get("descripteur_libelle")] if fields.get("descripteur_libelle") else []),
                    }
    except Exception:
        logger.exception("Échec récupération AO par idweb")
    return None


async def check_new_tenders(cache_dir: str, llm: LLMClient, settings: Settings) -> list[dict]:
    """
    Vérifie s'il y a de nouveaux AO pertinents pour chaque alerte.
    Retourne une liste de notifications à envoyer sur Slack.
    """
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []

    notifications: list[dict] = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        alerts = conn.execute("SELECT * FROM ao_alerts").fetchall()

    for alert in alerts:
        user_id = alert["user_id"]
        channel = alert["channel"]
        query = alert["query"]
        threshold = alert["threshold"] or 70

        last_seen_raw = alert["last_seen_ids"] or "[]"
        try:
            last_seen = set(json.loads(last_seen_raw))
        except json.JSONDecodeError:
            last_seen = set()

        # Récupérer le profil du client
        profile = get_profile(cache_dir, user_id) or {
            "activity": "",
            "company_size": "",
            "keywords": [],
            "sectors": [],
            "zones": [],
            "min_amount": 0,
            "max_amount": 0,
        }

        try:
            # On cherche les AO des 7 derniers jours pour éviter de scorer des milliers de résultats
            results, _, _ = await fetch_boamp_tenders(
                query=query,
                published_since="7d",
            )
        except Exception:
            logger.exception("BOAMP fetch échoué pour alerte %s", alert["id"])
            continue

        new_results = [r for r in results if r.get("idweb") and r["idweb"] not in last_seen]
        if not new_results:
            # Mettre à jour last_check même s'il n'y a rien de nouveau
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE ao_alerts SET last_check = ? WHERE id = ?",
                    (datetime.now().isoformat(), alert["id"]),
                )
                conn.commit()
            continue

        # Scorer chaque nouveau AO (cache interne)
        scored: list[tuple[dict, int, str]] = []
        for ao in new_results:
            try:
                s, reason = await score_tender(ao, profile, llm, cache_dir)
                if s >= threshold:
                    scored.append((ao, s, reason))
            except Exception:
                logger.exception("Scoring échoué pour AO %s", ao.get("idweb"))
                continue

        # Enrichissement historique acheteur pour les scores ≥ 80
        if scored:
            from app.slack.buyer_history import fetch_buyer_history, format_buyer_history
            for _i, (ao, s, _reason) in enumerate(scored):
                if s >= 80 and ao.get("org"):
                    try:
                        hist = await asyncio.wait_for(
                            fetch_buyer_history(ao["org"], "2025", settings),
                            timeout=10,
                        )
                        if hist:
                            ao["_history"] = format_buyer_history(hist)
                    except Exception:
                        logger.exception("Enrichissement acheteur échoué pour AO %s", ao.get("idweb"))

        # Trier par score décroissant, garder les 5 meilleurs
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:5]

        if top:
            notifications.append({
                "user_id": user_id,
                "channel": channel,
                "query": query,
                "top": top,
            })

        # Mettre à jour les IDs vus
        all_ids = last_seen | {r["idweb"] for r in results if r.get("idweb")}
        # Garder seulement les 500 derniers IDs pour ne pas gonfler la DB
        all_ids_list = list(all_ids)[-500:]
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE ao_alerts SET last_check = ?, last_seen_ids = ? WHERE id = ?",
                (datetime.now().isoformat(), json.dumps(all_ids_list), alert["id"]),
            )
            conn.commit()

    return notifications


def build_ao_notification(query: str, top: list[tuple[dict, int, str]]) -> str:
    """Construit le texte Slack d'une notification AO."""
    lines = [f"🔔 *{len(top)} appel(s) d'offre très pertinent(s) pour toi* — _{query}_", ""]
    for i, (ao, score, reason) in enumerate(top, 1):
        lines.append(format_ao_push(ao, score, reason, i))
        lines.append("")
    lines.append("_Pour configurer ton profil :_ `/datagouv profil`")
    return "\n".join(lines)
