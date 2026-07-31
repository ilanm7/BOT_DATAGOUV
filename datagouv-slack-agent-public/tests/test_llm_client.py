"""Tests du client LLM OpenAI-compatible."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.llm.client import LLMClient


@pytest.fixture
def mock_settings():
    from app.config import Settings
    return Settings(
        _env_file=None,
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        openai_api_key="sk-test",
        openai_base_url="https://api.test.com/v1",
        openai_model="gpt-4o-test",
        openai_temperature=0.5,
        openai_max_tokens=2000,
    )


class TestInit:
    def test_init_creates_openai_client(self, mock_settings):
        with patch("app.llm.client.AsyncOpenAI") as MockAsyncOpenAI:
            client = LLMClient(mock_settings)
            MockAsyncOpenAI.assert_called_once_with(
                base_url="https://api.test.com/v1",
                api_key="sk-test",
                timeout=60,
            )
            assert client._model == "gpt-4o-test"
            assert client._temperature == 0.5
            assert client._max_tokens == 2000


class TestChat:
    @pytest.mark.asyncio
    async def test_chat_without_tools(self, mock_settings):
        mock_openai_instance = MagicMock()
        mock_message = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        mock_create = AsyncMock(return_value=mock_response)
        mock_openai_instance.chat.completions.create = mock_create
        
        with patch("app.llm.client.AsyncOpenAI", return_value=mock_openai_instance):
            client = LLMClient(mock_settings)
            result = await client.chat([{"role": "user", "content": "Hello"}])
            
            mock_create.assert_awaited_once_with(
                model="gpt-4o-test",
                messages=[{"role": "user", "content": "Hello"}],
                temperature=0.5,
                max_tokens=2000,
            )
            assert result is mock_message

    @pytest.mark.asyncio
    async def test_chat_with_tools(self, mock_settings):
        mock_openai_instance = MagicMock()
        mock_message = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        mock_create = AsyncMock(return_value=mock_response)
        mock_openai_instance.chat.completions.create = mock_create
        
        tools = [{"type": "function", "function": {"name": "test_tool"}}]
        
        with patch("app.llm.client.AsyncOpenAI", return_value=mock_openai_instance):
            client = LLMClient(mock_settings)
            result = await client.chat(
                [{"role": "user", "content": "Hello"}],
                tools=tools,
            )
            
            mock_create.assert_awaited_once_with(
                model="gpt-4o-test",
                messages=[{"role": "user", "content": "Hello"}],
                temperature=0.5,
                max_tokens=2000,
                tools=tools,
                tool_choice="auto",
            )
            assert result is mock_message
