"""Monitoring du bot — heartbeat et vérification des services."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


async def health_check(settings: Settings) -> dict[str, Any]:
    """Vérifie l'état de tous les services critiques."""

    status = {
        "timestamp": datetime.now(UTC).isoformat(),
        "slack_connected": False,
        "llm_connected": False,
        "mcp_reachable": False,
        "web_server": False,
        "overall": "degraded",
    }

    # 1. LLM
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {}
            if settings.openai_api_key:
                headers["Authorization"] = f"Bearer {settings.openai_api_key}"
            resp = await client.get(settings.openai_base_url + "/models", headers=headers)
            status["llm_connected"] = resp.status_code == 200
    except Exception:
        pass

    # 2. MCP data.gouv.fr
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(settings.datagouv_api_base_url + "/datasets/")
            status["mcp_reachable"] = resp.status_code == 200
    except Exception:
        pass

    # 3. Web server local
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("http://localhost:8080/api/health")
            status["web_server"] = resp.status_code == 200
    except Exception:
        pass

    # Slack est implicitement connecté si ce code s'exécute
    status["slack_connected"] = True

    # Overall
    if status["llm_connected"] and status["mcp_reachable"]:
        status["overall"] = "healthy"
    elif status["llm_connected"]:
        status["overall"] = "degraded"
    else:
        status["overall"] = "critical"

    return status


async def heartbeat_task(settings: Settings, _client, interval_minutes: int = 5) -> None:
    """Tâche de fond qui envoie un heartbeat toutes les N minutes."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        status = await health_check(settings)
        if status["overall"] == "critical":
            logger.critical(
                "HEALTHCHECK CRITICAL — LLM:%s MCP:%s Web:%s",
                "✅" if status["llm_connected"] else "❌",
                "✅" if status["mcp_reachable"] else "❌",
                "✅" if status["web_server"] else "❌",
            )
        else:
            logger.info("Heartbeat OK — %s", status["overall"])
