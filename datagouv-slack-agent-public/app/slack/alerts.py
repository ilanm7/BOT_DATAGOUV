"""Système d'alertes data.gouv.fr avec stockage SQLite."""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx

from app.config import Settings
from app.llm.client import LLMClient
from app.slack.ao_alerts import check_new_tenders, save_ao_push
from app.slack.tender_scoring import format_ao_push
from app.slack.tender_tracking import check_deadline_reminders, save_ao_message
from app.web.webhooks import notify_webhooks

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
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                query TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                last_check TEXT,
                last_results TEXT
            )
            """
        )
        conn.commit()


def add_alert(cache_dir: str, user_id: str, channel: str, query: str) -> int:
    """Ajoute une alerte et retourne son ID."""
    _init_db(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO alerts (user_id, channel, query) VALUES (?, ?, ?)",
            (user_id, channel, query),
        )
        conn.commit()
        return cursor.lastrowid or 0


def list_alerts(cache_dir: str, user_id: str) -> list[dict]:
    """Liste les alertes d'un utilisateur."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, query, created_at FROM alerts WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def remove_alert(cache_dir: str, alert_id: int, user_id: str) -> bool:
    """Supprime une alerte si elle appartient à l'utilisateur."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM alerts WHERE id = ? AND user_id = ?",
            (alert_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


async def check_new_datasets(cache_dir: str) -> list[dict]:
    """Vérifie s'il y a de nouveaux datasets pour chaque alerte.
    Retourne une liste de notifications à envoyer."""
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return []

    notifications = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        alerts = conn.execute("SELECT * FROM alerts").fetchall()

    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
        for alert in alerts:
            query = alert["query"]
            last_results_raw = alert["last_results"] or "[]"
            try:
                last_results = set(json.loads(last_results_raw))
            except json.JSONDecodeError:
                last_results = set()

            # Recherche sur data.gouv.fr
            try:
                response = await client.get(
                    "https://www.data.gouv.fr/api/1/datasets/",
                    params={"q": query, "page_size": 5},
                )
                response.raise_for_status()
                data = response.json()
            except Exception:
                continue

            current_ids = {d.get("id", "") for d in data.get("data", [])}
            new_ids = current_ids - last_results

            if new_ids and last_results:  # Éviter de notifier à la création de l'alerte
                new_datasets = [d for d in data.get("data", []) if d.get("id") in new_ids]
                notifications.append({
                    "user_id": alert["user_id"],
                    "channel": alert["channel"],
                    "query": query,
                    "new_datasets": new_datasets,
                })

            # Mettre à jour last_check et last_results pour CETTE alerte
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE alerts SET last_check = ?, last_results = ? WHERE id = ?",
                    (datetime.now().isoformat(), json.dumps(list(current_ids)), alert["id"]),
                )
                conn.commit()

    return notifications


async def alert_scheduler(cache_dir: str, client, settings: Settings, interval_minutes: int = 60):
    """Tâche de fond qui vérifie les alertes périodiquement."""
    llm = LLMClient(settings)
    while True:
        await asyncio.sleep(interval_minutes * 60)
        # --- Alertes datasets ---
        try:
            notifications = await asyncio.wait_for(
                check_new_datasets(cache_dir), timeout=300
            )
            for notif in notifications:
                safe_query = notif['query'].replace("*", "\\*")
                lines = [f"🔔 *Nouveau(x) dataset(s) pour \"{safe_query}\"* :"]
                for ds in notif["new_datasets"][:3]:
                    title = ds.get("title", "Sans titre")
                    url = ds.get("page", f"https://www.data.gouv.fr/fr/datasets/{ds.get('id', '')}/")
                    lines.append(f"• <{url}|{title}>")
                await client.chat_postMessage(
                    channel=notif["channel"],
                    text="\n".join(lines),
                )
        except TimeoutError:
            logger.warning("Timeout global sur le check des alertes datasets")
        except Exception:
            logger.exception("Erreur dans le scheduler d'alertes datasets")

        # --- Alertes AO (appels d'offre) ---
        try:
            ao_notifications = await asyncio.wait_for(
                check_new_tenders(cache_dir, llm, settings), timeout=300
            )
            for notif in ao_notifications:
                channel = notif["channel"]
                user_id = notif["user_id"]
                is_iguane = user_id == "__iguane__"

                # Pour le canal Iguane : message groupé récapitulatif
                if is_iguane and len(notif["top"]) > 1:
                    lines = [
                        f"🔔 *{len(notif['top'])} appels d'offre cette semaine pour Iguane*",
                        "_Mots-clés : IA, automatisation, agents conversationnels, RPA, n8n, data, LLM, workflow_\n",
                    ]
                    for _i, (ao, score, reason) in enumerate(notif["top"], 1):
                        badge = "🔥" if score >= 85 else "✅" if score >= 70 else "📌"
                        lines.append(
                            f"{badge} *{ao.get('objet', 'Sans titre')[:80]}*"
                        )
                        lines.append(f"   Score : *{score}/100* — {reason}")
                        lines.append(
                            f"   <https://www.boamp.fr/avis/detail/{ao.get('idweb', '')}|Voir l'annonce>"
                        )
                        lines.append("")
                    lines.append("_Réagis avec ✅ ou 🔥 sur un AO pour le suivre (rappels J-15/J-7/J-2)_")
                    try:
                        resp = await client.chat_postMessage(
                            channel=channel,
                            text="\n".join(lines),
                        )
                        ts = resp.get("ts", "")
                        for ao, score, reason in notif["top"]:
                            if ts:
                                save_ao_message(
                                    cache_dir,
                                    message_ts=ts,
                                    channel=channel,
                                    ao_id=ao.get("idweb", ""),
                                    objet=ao.get("objet", ""),
                                    deadline=str(ao.get("datelimitereponse", "")),
                                )
                            save_ao_push(
                                cache_dir,
                                user_id=user_id,
                                channel=channel,
                                ao_id=ao.get("idweb", ""),
                                objet=ao.get("objet", ""),
                                score=score,
                                reason=reason,
                            )
                    except Exception:
                        logger.exception("Échec envoi résumé Iguane")
                    continue

                # Comportement normal (push individuel)
                for ao, score, reason in notif["top"]:
                    text = format_ao_push(ao, score, reason, 1)
                    try:
                        resp = await client.chat_postMessage(
                            channel=channel,
                            text=text,
                        )
                        ts = resp.get("ts", "")
                        if ts:
                            save_ao_message(
                                cache_dir,
                                message_ts=ts,
                                channel=channel,
                                ao_id=ao.get("idweb", ""),
                                objet=ao.get("objet", ""),
                                deadline=str(ao.get("datelimitereponse", "")),
                            )
                        save_ao_push(
                            cache_dir,
                            user_id=user_id,
                            channel=channel,
                            ao_id=ao.get("idweb", ""),
                            objet=ao.get("objet", ""),
                            score=score,
                            reason=reason,
                        )
                        # Notifier les webhooks tiers enregistrés
                        try:
                            await notify_webhooks(
                                cache_dir,
                                user_id,
                                {
                                    "event": "ao.push",
                                    "ao_id": ao.get("idweb", ""),
                                    "objet": ao.get("objet", ""),
                                    "org": ao.get("org", ""),
                                    "score": score,
                                    "reason": reason,
                                    "deadline": str(ao.get("datelimitereponse", "")),
                                    "departements": ao.get("departements", []),
                                    "descripteurs": ao.get("descripteurs", []),
                                    "url_avis": ao.get("url_avis", ""),
                                    "pushed_at": datetime.now().isoformat(),
                                },
                            )
                        except Exception:
                            logger.exception("Échec notification webhook")
                    except Exception:
                        logger.exception("Échec envoi push AO")
        except TimeoutError:
            logger.warning("Timeout global sur le check des alertes AO")
        except Exception:
            logger.exception("Erreur dans le scheduler d'alertes AO")

        # --- Rappels deadline AO ---
        try:
            await asyncio.wait_for(
                check_deadline_reminders(cache_dir, client), timeout=300
            )
        except TimeoutError:
            logger.warning("Timeout global sur les rappels deadline")
        except Exception:
            logger.exception("Erreur dans les rappels deadline")
