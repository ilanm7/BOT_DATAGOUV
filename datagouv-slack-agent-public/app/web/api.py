"""Router API publique v1 — intégrations tierces (CRM, outils internes)."""

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from app.slack.ao_alerts import _init_db as _init_ao_alerts
from app.slack.ao_alerts import add_ao_alert, list_ao_alerts, remove_ao_alert
from app.slack.dce_rag import _init_dce_rag
from app.slack.profiles import _init_profiles, get_profile, upsert_profile
from app.slack.tender_tracking import _init_tracking, list_tracked
from app.web.api_keys import _init_api_keys, get_user_id_from_key
from app.web.webhooks import _init_webhooks, add_webhook, list_webhooks, remove_webhook

logger = logging.getLogger(__name__)


def _get_db_path(cache_dir: str):
    from pathlib import Path
    return Path(cache_dir) / "alerts.db"


def create_api_router(cache_dir: str) -> APIRouter:
    """Factory qui retourne le router API configuré avec le cache_dir."""
    # S'assurer que toutes les tables existent
    _init_api_keys(cache_dir)
    _init_ao_alerts(cache_dir)
    _init_tracking(cache_dir)
    _init_profiles(cache_dir)
    _init_dce_rag(cache_dir)
    _init_webhooks(cache_dir)
    router = APIRouter(prefix="/api/v1")

    async def _require_auth(x_api_key: str = Header(alias="X-API-Key")) -> str:
        if not x_api_key:
            raise HTTPException(status_code=401, detail="Header X-API-Key manquant")
        user_id = get_user_id_from_key(cache_dir, x_api_key)
        if not user_id:
            raise HTTPException(status_code=401, detail="Clé API invalide")
        return user_id

    # -----------------------------------------------------------------------
    # Alertes AO
    # -----------------------------------------------------------------------
    @router.get("/alerts")
    def api_list_alerts(user_id: str = Depends(_require_auth)):
        return list_ao_alerts(cache_dir, user_id)

    @router.post("/alerts")
    def api_create_alert(
        user_id: str = Depends(_require_auth),
        body: dict[str, Any] | None = None,
    ):
        body = body or {}
        query = body.get("query", "")
        threshold = body.get("threshold", 70)
        if not query:
            raise HTTPException(status_code=400, detail="Champ 'query' requis")
        alert_id = add_ao_alert(cache_dir, user_id, "", query, threshold=threshold)
        return {"id": alert_id, "query": query, "threshold": threshold}

    @router.delete("/alerts/{alert_id}")
    def api_delete_alert(
        alert_id: int,
        user_id: str = Depends(_require_auth),
    ):
        if remove_ao_alert(cache_dir, alert_id, user_id):
            return {"deleted": True}
        raise HTTPException(status_code=404, detail="Alerte introuvable ou pas à vous")

    # -----------------------------------------------------------------------
    # AO suivis
    # -----------------------------------------------------------------------
    @router.get("/tracked")
    def api_list_tracked(user_id: str = Depends(_require_auth)):
        return list_tracked(cache_dir, user_id)

    # -----------------------------------------------------------------------
    # Historique des pushes
    # -----------------------------------------------------------------------
    @router.get("/pushes")
    def api_list_pushes(
        user_id: str = Depends(_require_auth),
        limit: int = 20,
    ):
        db_path = _get_db_path(cache_dir)
        if not db_path.exists():
            return []
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ao_id, objet, score, reason, pushed_at
                FROM ao_push_history
                WHERE user_id = ?
                ORDER BY pushed_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [
            {
                "ao_id": r["ao_id"],
                "objet": r["objet"],
                "score": r["score"],
                "reason": r["reason"],
                "pushed_at": r["pushed_at"],
            }
            for r in rows
        ]

    # -----------------------------------------------------------------------
    # Profil
    # -----------------------------------------------------------------------
    @router.get("/profile")
    def api_get_profile(user_id: str = Depends(_require_auth)):
        profile = get_profile(cache_dir, user_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profil non trouvé")
        return profile

    @router.put("/profile")
    def api_update_profile(
        user_id: str = Depends(_require_auth),
        body: dict[str, Any] | None = None,
    ):
        body = body or {}
        upsert_profile(
            cache_dir,
            user_id,
            activity=body.get("activity", ""),
            company_size=body.get("company_size", ""),
            keywords=body.get("keywords", []),
            sectors=body.get("sectors", []),
            zones=body.get("zones", []),
            min_amount=body.get("min_amount", 0),
            max_amount=body.get("max_amount", 0),
        )
        return {"updated": True}

    # -----------------------------------------------------------------------
    # Webhooks
    # -----------------------------------------------------------------------
    @router.get("/webhooks")
    def api_list_webhooks(user_id: str = Depends(_require_auth)):
        return list_webhooks(cache_dir, user_id)

    @router.post("/webhooks")
    def api_create_webhook(
        user_id: str = Depends(_require_auth),
        body: dict[str, Any] | None = None,
    ):
        body = body or {}
        url = body.get("url", "").strip()
        if not url or not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Champ 'url' requis et doit commencer par http:// ou https://")
        webhook_id = add_webhook(cache_dir, user_id, url, secret=body.get("secret"))
        return {"id": webhook_id, "url": url, "active": True}

    @router.delete("/webhooks/{webhook_id}")
    def api_delete_webhook(
        webhook_id: int,
        user_id: str = Depends(_require_auth),
    ):
        if remove_webhook(cache_dir, webhook_id, user_id):
            return {"deleted": True}
        raise HTTPException(status_code=404, detail="Webhook introuvable ou pas à vous")

    # -----------------------------------------------------------------------
    # Stats personnalisées
    # -----------------------------------------------------------------------
    @router.get("/stats")
    def api_user_stats(user_id: str = Depends(_require_auth)):
        db_path = _get_db_path(cache_dir)
        stats = {
            "alerts": 0,
            "tracked": 0,
            "pushes": 0,
            "avg_score": 0,
            "dce_indexed": 0,
        }
        if not db_path.exists():
            return stats
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM ao_alerts WHERE user_id = ?", (user_id,)
            ).fetchone()
            stats["alerts"] = row[0] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) FROM tracked_tenders WHERE user_id = ?", (user_id,)
            ).fetchone()
            stats["tracked"] = row[0] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) FROM ao_push_history WHERE user_id = ?", (user_id,)
            ).fetchone()
            stats["pushes"] = row[0] if row else 0

            row = conn.execute(
                "SELECT AVG(score) FROM ao_push_history WHERE user_id = ?", (user_id,)
            ).fetchone()
            stats["avg_score"] = round(row[0], 1) if row and row[0] else 0

            row = conn.execute("SELECT COUNT(DISTINCT ao_id) FROM dce_chunks").fetchone()
            stats["dce_indexed"] = row[0] if row else 0
        return stats

    return router
