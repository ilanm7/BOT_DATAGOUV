"""Tests du module de configuration."""


import pytest

from app.config import Settings, get_settings


class TestSettings:
    """Tests de la classe Settings."""

    def test_default_values(self, monkeypatch):
        """Les valeurs par défaut doivent être appliquées quand les vars optionnelles ne sont pas définies."""
        # On efface les variables d'env qui pourraient interférer
        for key in ["OPENAI_BASE_URL", "OPENAI_MODEL", "CACHE_DIR", "LOG_LEVEL"]:
            monkeypatch.delenv(key, raising=False)

        # Vars obligatoires minimales
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        # _env_file=None = on ignore le fichier .env pour ce test
        settings = Settings(_env_file=None)

        assert settings.openai_base_url == "https://api.openai.com/v1"
        assert settings.openai_model == "gpt-4o-mini"
        assert settings.openai_temperature == 0.2
        assert settings.openai_max_tokens == 6000
        assert settings.cache_dir == "./cache"
        assert settings.max_download_mb == 1_000
        assert settings.max_scan_rows == 100_000
        assert settings.agent_max_iterations == 5
        assert settings.log_level == "INFO"

    def test_env_override(self, monkeypatch):
        """Les variables d'environnement doivent écraser les valeurs par défaut."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
        monkeypatch.setenv("MAX_DOWNLOAD_MB", "1000")
        monkeypatch.setenv("AGENT_MAX_ITERATIONS", "12")

        settings = Settings(_env_file=None)

        assert settings.openai_model == "gpt-4o"
        assert settings.max_download_mb == 1000
        assert settings.agent_max_iterations == 12

    def test_required_fields_missing(self, monkeypatch):
        """Sans les champs obligatoires, pydantic doit lever une ValidationError."""
        for key in ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "OPENAI_API_KEY"]:
            monkeypatch.delenv(key, raising=False)

        with pytest.raises(Exception):  # pydantic lève une ValidationError
            Settings(_env_file=None)


class TestGetSettings:
    """Tests du getter mis en cache."""

    def test_cache_returns_same_instance(self, monkeypatch):
        """get_settings() doit retourner la même instance à chaque appel (cache LRU)."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        # On vide le cache entre les tests pour être sûr
        get_settings.cache_clear()

        s1 = get_settings()
        s2 = get_settings()

        assert s1 is s2  # 'is' vérifie que c'est le MÊME objet en mémoire
