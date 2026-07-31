"""Client LLM OpenAI-compatible (OpenAI, LiteLLM, vLLM, etc.)."""

import logging
from typing import Any

from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings

logger = logging.getLogger(__name__)

RETRYABLE_ERRORS = (APIError, APIConnectionError, RateLimitError, TimeoutError)


class LLMClient:
    def __init__(self, settings: Settings):
        self._client = AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            timeout=60,
        )
        self._model = settings.openai_model
        self._embedding_model = settings.openai_embedding_model
        self._temperature = settings.openai_temperature
        self._max_tokens = settings.openai_max_tokens

    # [RETRY LLM] 3 tentatives avec backoff exponentiel (2s, 4s, 10s) sur erreurs réseau
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        reraise=True,
    )
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        """Un tour de chat completion, avec tools si fournis."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        if not response.choices:
            raise APIError("Réponse LLM vide (pas de choices)", request=None, body=None)
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            logger.warning("Réponse LLM tronquée (finish_reason=length)")
        return choice.message

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        reraise=True,
    )
    async def embed(self, text: str) -> list[float]:
        """Génère un embedding vectoriel pour un texte donné."""
        response = await self._client.embeddings.create(
            model=self._embedding_model,
            input=text,
        )
        if not response.data:
            raise APIError("Réponse embedding vide", request=None, body=None)
        return response.data[0].embedding
