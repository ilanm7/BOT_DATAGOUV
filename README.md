# datagouv-slack-agent

Conversational AI agent for Slack that answers natural-language questions about French open data (data.gouv.fr) — powered by a custom agentic loop, the official data.gouv.fr MCP server, and local Python analysis (DuckDB). No raw file ever reaches the LLM.

> 🇫🇷 Version française : [README.fr.md](README.fr.md)


![Tests](https://img.shields.io/badge/tests-41%20passing-brightgreen) ![Python](https://img.shields.io/badge/python-3.11%2B-blue) ![Ruff](https://img.shields.io/badge/linting-ruff-orange)

## What it does

Ask the bot a question in a Slack thread — *"Find the average property price in the 16th arrondissement of Paris since 2020"* — and it will:

1. Search data.gouv.fr through the **official MCP server** (10 tools, with direct-API fallback)
2. Download the relevant datasets (up to 1 GB, e.g. DVF property records)
3. Analyze them **locally with DuckDB / pandas** — raw files are never sent to the LLM
4. Reply in the thread with figures, charts (matplotlib) and enriched CSV/Excel exports

It also handles company lookups (INSEE Sirene, Recherche d'Entreprises), user-uploaded files (CSV/Excel/JSON/Parquet/ZIP), hourly dataset alerts, and keeps a 30-day per-thread memory (SQLite).

## Architecture

![Architecture](schema_bot.png)

The core is a **hand-built agentic loop** (`app/agent/orchestrator.py`): the LLM receives both MCP tools and local analysis tools, requests calls, the orchestrator routes each call to the right executor, feeds results back, and loops until a final answer (bounded iterations, forced conclusion, concurrency semaphore to cap API costs).

Key design choices (full rationale in [ARCHITECTURE.md](ARCHITECTURE.md)):
- **Socket Mode** — no public endpoint needed, single `xapp-` token
- **Local-only data analysis** — privacy + token-cost control: the LLM writes SQL, DuckDB executes it
- **Typed configuration** (pydantic-settings) with validators and safe clamping
- **Retry with tenacity**, WAL-mode SQLite, structured logging

## Quickstart

```bash
git clone https://github.com/ilanm7/BOT_DATAGOUV && cd BOT_DATAGOUV/datagouv-slack-agent-public
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env   # fill in your Slack + LLM keys
python launch_bot.py
```

Slack app manifest: [`slack_manifest.json`](datagouv-slack-agent-public/slack_manifest.json). Step-by-step setup (French): [`README.fr.md`](datagouv-slack-agent-public/README.fr.md). A Dockerfile is included but has not been validated yet.

## Tests

```bash
pytest   # 41 tests, ~2s
```

Covers config validation, download pipeline, local analyzer, LLM client and bot logic (mocked with respx).

## Known limitations & roadmap

Honest status — this is a working system with open issues:

- Real-estate (DVF) and AI tender queries sometimes fail at the retrieval step: the MCP search ranks the wrong datasets first. Fix in progress: query rewriting + curated dataset shortlist.
- Test coverage is concentrated on the data layer; the orchestrator loop needs dedicated tests.
- `app/slack/bot.py` has grown to ~2,500 lines and is being split into handlers.

## Context

Built end-to-end during my AI automation internship at **Iguane Solutions** (Paris), under two hard constraints: pure Python (no low-code) and zero raw data sent to the LLM. Published with permission.

## License

MIT
