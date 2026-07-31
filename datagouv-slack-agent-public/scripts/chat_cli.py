"""Test de l'agent en ligne de commande, sans Slack.

Usage :
    python -m scripts.chat_cli "Trouve les datasets sur les DPE"
    python -m scripts.chat_cli --list-tools
"""

import asyncio
import logging
import sys

from dotenv import load_dotenv

from app.config import get_settings
from app.mcp.datagouv_mcp_client import DataGouvMCPClient


async def list_tools() -> None:
    settings = get_settings()
    client = DataGouvMCPClient(settings.datagouv_mcp_url)
    async with client.session() as session:
        tools = await DataGouvMCPClient.list_tools_openai_format(session)
        for tool in tools:
            fn = tool["function"]
            print(f"- {fn['name']}: {fn['description'][:120]}")


async def chat(prompt: str) -> None:
    from app.agent.orchestrator import AgentOrchestrator

    settings = get_settings()
    orchestrator = AgentOrchestrator(settings)
    answer = await orchestrator.run([{"role": "user", "content": prompt}])
    print("\n=== Réponse de l'agent ===\n")
    print(answer)


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level="INFO")
    if len(sys.argv) > 1 and sys.argv[1] == "--list-tools":
        asyncio.run(list_tools())
    elif len(sys.argv) > 1:
        asyncio.run(chat(" ".join(sys.argv[1:])))
    else:
        print(__doc__)
