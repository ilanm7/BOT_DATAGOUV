"""Point d'entrée : lance le bot Slack en Socket Mode + le dashboard web."""

import asyncio
import contextlib
import logging
import signal

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from app.config import get_settings
from app.data.sau import _download_csv as _download_sau_csv
from app.data.sncf import _download_csv as _download_sncf_csv
from app.data.traffic import _download_csv as _download_traffic_csv
from app.health import heartbeat_task
from app.slack.ao_alerts import ensure_iguane_alert
from app.slack.bot import create_app
from app.slack.buyer_history import fetch_buyer_history
from app.web.dashboard import create_dashboard_app


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _run_web_server(cache_dir: str, port: int = 8080) -> None:
    """Lance le serveur FastAPI en parallèle du bot."""
    import uvicorn
    dashboard_app = create_dashboard_app(cache_dir)
    config = uvicorn.Config(dashboard_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    load_dotenv()
    settings = get_settings()
    setup_logging(settings.log_level)

    # Validation rapide des tokens critiques
    if not settings.slack_bot_token.startswith("xoxb-"):
        logging.getLogger(__name__).error("SLACK_BOT_TOKEN invalide (doit commencer par xoxb-)")
        raise SystemExit(1)
    if not settings.slack_app_token.startswith("xapp-"):
        logging.getLogger(__name__).error("SLACK_APP_TOKEN invalide (doit commencer par xapp-)")
        raise SystemExit(1)

    app, handler = create_app(settings)
    await app._resolve_bot_user_id()

    # Démarrer le dashboard web en tâche de fond
    web_task = asyncio.create_task(_run_web_server(settings.cache_dir))
    logging.getLogger(__name__).info("Dashboard web démarré sur http://localhost:8080")

    # Démarrer le heartbeat de monitoring en tâche de fond
    hb_task = asyncio.create_task(heartbeat_task(settings, app.client, interval_minutes=5))
    logging.getLogger(__name__).info("Heartbeat healthcheck démarré (toutes les 5 min)")

    # Pré-chauffer les caches en arrière-plan (garder les références pour éviter
    # le garbage collection prématuré des tâches orphelines)
    _background_tasks: set[asyncio.Task] = set()

    async def _warm_decp_cache() -> None:
        try:
            await fetch_buyer_history("Métropole de Lyon", "2024", settings, max_results=5)
            logging.getLogger(__name__).info("Pré-warm cache DECP terminé")
        except Exception:
            logging.getLogger(__name__).exception("Pré-warm cache DECP échoué (non bloquant)")

    t1 = asyncio.create_task(_warm_decp_cache())
    _background_tasks.add(t1)

    async def _warm_traffic_cache() -> None:
        try:
            await _download_traffic_csv(settings.cache_dir, "2024")
            logging.getLogger(__name__).info("Pré-warm cache TMJA 2024 terminé")
        except Exception:
            logging.getLogger(__name__).exception("Pré-warm cache TMJA échoué (non bloquant)")

    t2 = asyncio.create_task(_warm_traffic_cache())
    _background_tasks.add(t2)

    async def _warm_sncf_cache() -> None:
        try:
            await _download_sncf_csv(settings.cache_dir)
            logging.getLogger(__name__).info("Pré-warm cache SNCF terminé")
        except Exception:
            logging.getLogger(__name__).exception("Pré-warm cache SNCF échoué (non bloquant)")

    t3 = asyncio.create_task(_warm_sncf_cache())
    _background_tasks.add(t3)

    async def _warm_sau_cache() -> None:
        try:
            await _download_sau_csv(settings.cache_dir)
            logging.getLogger(__name__).info("Pré-warm cache SAU terminé")
        except Exception:
            logging.getLogger(__name__).exception("Pré-warm cache SAU échoué (non bloquant)")

    t4 = asyncio.create_task(_warm_sau_cache())
    _background_tasks.add(t4)

    # Créer l'alerte Iguane interne si un canal est configuré
    if settings.iguane_alert_channel:
        try:
            ensure_iguane_alert(settings.cache_dir, settings.iguane_alert_channel)
            logging.getLogger(__name__).info(
                "Alerte Iguane configurée sur le canal %s", settings.iguane_alert_channel
            )
        except Exception:
            logging.getLogger(__name__).exception("Échec création alerte Iguane (non bloquant)")

    loop = asyncio.get_running_loop()

    def _signal_handler(sig: int, current_task: asyncio.Task) -> None:
        logger = logging.getLogger(__name__)
        logger.info("Signal %s reçu, arrêt en cours...", signal.Signals(sig).name)
        current_task.cancel()
        web_task.cancel()
        hb_task.cancel()

    retries = 0
    max_retries = 10
    # [SOCKET MODE] Connexion WebSocket persistante avec Slack (pas besoin d'URL publique)
    while retries < max_retries:
        task = asyncio.create_task(handler.start_async())
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig, t=task: _signal_handler(s, t))

        logging.getLogger(__name__).info("Démarrage du bot en Socket Mode...")
        try:
            await task
        except asyncio.CancelledError:
            logging.getLogger(__name__).info("Bot arrêté proprement.")
            break
        except Exception:
            logger = logging.getLogger(__name__)
            logger.exception("Déconnexion inattendue de Slack")
            retries += 1
            # [RECONNEXION] Backoff exponentiel : 2^retries secondes, plafonné à 60
            wait = min(2 ** retries, 60)
            logger.info("Reconnexion dans %d secondes... (%d/%d)", wait, retries, max_retries)
            await asyncio.sleep(wait)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            with contextlib.suppress(Exception):
                await handler.close_async()
            # Recréer SEULEMENT le handler, pas l'app (évite fuite mémoire et duplication d'events)
            handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    else:
        logging.getLogger(__name__).error("Nombre maximal de reconnections atteint. Arrêt.")

    # Arrêter proprement les tâches de fond
    for task in (web_task, hb_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    asyncio.run(main())
