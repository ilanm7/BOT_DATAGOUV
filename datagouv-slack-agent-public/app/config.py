"""Configuration centralisée de l'application, chargée depuis les variables d'environnement."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Slack
    slack_bot_token: str
    slack_app_token: str  # xapp-... requis pour le Socket Mode
    slack_signing_secret: str = ""

    # LLM OpenAI-compatible
    # [MODÈLE LLM] gpt-4o-mini = modèle le moins cher et suffisant pour du routing d'outils
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "bge-m3"
    # [TEMPÉRATURE] 0.2 = assez déterministe sans être bloqué sur les ties
    openai_temperature: float = 0.2
    openai_max_tokens: int = 6000

    # MCP data.gouv.fr
    datagouv_mcp_url: str = "https://mcp.data.gouv.fr/mcp"

    # API data.gouv.fr directe (fallback, semaine 6)
    datagouv_api_base_url: str = "https://www.data.gouv.fr/api/1"

    # Limites
    cache_dir: str = "./cache"
    max_download_mb: int = 1_000  # [LIMITE TÉLÉCHARGEMENT] 1 Go pour les gros fichiers DVF
    max_scan_rows: int = 100_000
    max_sample_rows: int = 30
    llm_context_max_chars: int = 60_000
    agent_max_iterations: int = 5

    # API Tavily (recherche web)
    tavily_api_key: str = ""

    # API Sirene INSEE (https://api.insee.fr)
    insee_consumer_key: str = ""
    insee_consumer_secret: str = ""

    # API Legifrance (https://www.piste.gouv.fr — compte gratuit)
    legifrance_client_id: str = ""
    legifrance_client_secret: str = ""

    # Canal Slack dédié aux alertes Iguane (usage interne)
    iguane_alert_channel: str = ""

    log_level: str = "INFO"

    @field_validator("max_download_mb")
    @classmethod
    def _clamp_max_download_mb(cls, v: int) -> int:
        return max(1, min(v, 2_000))

    @field_validator("agent_max_iterations")
    @classmethod
    def _clamp_agent_max_iterations(cls, v: int) -> int:
        return max(1, min(v, 20))

    @field_validator("llm_context_max_chars")
    @classmethod
    def _clamp_llm_context_max_chars(cls, v: int) -> int:
        return max(1_000, min(v, 200_000))


@lru_cache
def get_settings() -> Settings:
    return Settings()
