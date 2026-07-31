"""Client MCP data.gouv.fr.

Se connecte au serveur MCP officiel (https://mcp.data.gouv.fr/mcp) via le
transport HTTP streamable, expose les tools disponibles et permet de les
appeler. L'URL est configurable pour rester compatible avec une future
version self-hostée.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


class MCPToolError(Exception):
    """Erreur lors de l'appel d'un tool MCP."""


class DataGouvMCPClient:
    """[OUTILS MCP] Client MCP data.gouv.fr — découvre et appelle les tools du serveur MCP."""
    def __init__(self, url: str):
        self.url = url

    @asynccontextmanager
    async def session(self):
        """Ouvre une session MCP pour la durée d'un traitement de message."""
        async with streamablehttp_client(self.url) as (read_stream, write_stream, _), ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)
                yield session

    @staticmethod
    async def list_tools_openai_format(session: ClientSession) -> list[dict[str, Any]]:
        """[OUTILS MCP] Liste les tools MCP et les convertit au format tools d'OpenAI."""
        result = await session.list_tools()
        tools = []
        for tool in result.tools:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        logger.info("Tools MCP disponibles: %s", [t["function"]["name"] for t in tools])
        return tools

    @staticmethod
    async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> str:
        """Appelle un tool MCP et retourne le contenu textuel concaténé."""
        try:
            result = await session.call_tool(name, arguments=arguments)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Échec de l'appel MCP %s", name)
            raise MCPToolError(f"Erreur lors de l'appel du tool MCP '{name}': {exc}") from exc

        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        output = "\n".join(parts) if parts else "(réponse vide du serveur MCP)"
        MAX_MCP_OUTPUT = 8000
        if len(output) > MAX_MCP_OUTPUT:
            output = output[:MAX_MCP_OUTPUT] + "\n\n[... résultat MCP tronqué ...]"

        if getattr(result, "isError", False):
            raise MCPToolError(f"Le tool MCP '{name}' a retourné une erreur: {output[:500]}")
        return output
