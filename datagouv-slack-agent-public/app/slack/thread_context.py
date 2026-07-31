"""Reconstruction du contexte conversationnel à partir d'un thread Slack."""

import logging

from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)


async def build_conversation(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    bot_user_id: str,
    current_text: str,
    current_ts: str,
    is_dm: bool = False,
) -> list[dict[str, str]]:
    """Récupère l'historique du thread et le convertit en messages LLM.

    Les messages du bot deviennent role=assistant, les autres role=user.
    Les messages des autres bots sont ignorés. Le message courant est
    toujours inclus en dernier. Gère la pagination pour les threads longs.
    En DM, utilise conversations_history au lieu de conversations_replies.
    """
    conversation: list[dict[str, str]] = []
    messages: list[dict] = []
    try:
        if is_dm:
            # En DM, on charge UNIQUEMENT le message le plus récent.
            # Si c'est le message actuel de l'utilisateur, il est filtré plus bas
            # et la conversation reste vide (seul le message courant est envoyé
            # au LLM). Si une race condition fait que le message le plus récent
            # est la réponse précédente du bot, elle est ignorée via le filtre
            # is_dm + bot_user_id. Ainsi, en DM, le LLM ne reçoit JAMAIS de
            # contexte historique depuis Slack — il ne voit que le message actuel.
            response = await client.conversations_history(
                channel=channel, limit=1, request_timeout=15
            )
            messages = response.get("messages", [])
        else:
            cursor = None
            # Récupérer jusqu'à 100 messages (2 pages de 50) pour limiter le rate limit sous charge
            for _ in range(2):
                kwargs = {"channel": channel, "ts": thread_ts, "limit": 50, "request_timeout": 15}
                if cursor:
                    kwargs["cursor"] = cursor
                response = await client.conversations_replies(**kwargs)
                batch = response.get("messages", [])
                messages.extend(batch)
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

        for msg in messages:
            text = (msg.get("text") or "").strip()
            if not text or msg.get("ts") == current_ts:
                continue
            if msg.get("bot_id") and msg.get("user") != bot_user_id:
                continue  # autres bots ignorés
            # [FIX DM RACE CONDITION] En DM, on ignore les messages du bot pour
            # éviter que le LLM continue un sujet obsolète quand la réponse
            # précédente du bot est retournée par erreur à cause d'une race
            # condition entre l'événement Slack et la persistance du message.
            if is_dm and msg.get("user") == bot_user_id:
                continue
            role = "assistant" if msg.get("user") == bot_user_id else "user"
            conversation.append({"role": role, "content": text})
    except Exception:  # noqa: BLE001
        logger.exception(
            "Impossible de récupérer l'historique du thread %s (is_dm=%s)", thread_ts, is_dm
        )

    conversation.append({"role": "user", "content": current_text})
    return conversation
