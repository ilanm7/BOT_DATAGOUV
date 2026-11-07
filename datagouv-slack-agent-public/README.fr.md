# datagouv-slack-agent

Agent IA conversationnel Slack pour exploiter data.gouv.fr, via le MCP officiel, un LLM OpenAI-compatible, et une analyse locale Python (DuckDB).

## Table des matières

- [État actuel](#état-actuel)
- [Installation locale](#installation-locale)
- [Configuration Slack](#configuration-slack)
- [Configuration du LLM](#configuration-du-llm)
- [Configuration MCP](#configuration-mcp)
- [Intégration du Skill data.gouv.fr](#intégration-du-skill-datagouvfr)
- [Variables d'environnement](#variables-denvironnement)
- [Lancement avec Docker](#lancement-avec-docker)
- [Exemples d'utilisation](#exemples-dutilisation)
- [Limites connues](#limites-connues)
- [Architecture interne](#architecture-interne)
- [Pistes d'amélioration](#pistes-damélioration)
- [Tests](#tests)
- [Problèmes connus](#problèmes-connus)

---

## État actuel

Projet fonctionnel en production :
- 🤖 Bot Slack en Socket Mode (mentions + threads)
- 🔍 MCP data.gouv.fr (10 tools) + API directe fallback
- 🏢 Recherche d'entreprises (API Recherche d'Entreprises)
- 📇 Dirigeants, téléphones, emails (API officielle + Tavily web search)
- 🧠 Mémoire persistante par thread (SQLite, 30 jours)
- 📊 Export CSV/Excel enrichi + graphiques automatiques (matplotlib)
- 🔔 Alertes data.gouv.fr automatiques (scheduler toutes les heures)
- 📎 Analyse de fichiers uploadés (CSV/Excel/JSON/Parquet/ZIP)
- 🗄️ Analyse SQL locale (DuckDB) avec filtres via `sql_query`
- 41 tests pytest passent en ~2 secondes

---

## Installation locale

### Prérequis

- Python 3.11+
- `uv` (recommandé) ou `pip`
- Un workspace Slack (pour le livrable bot)
- Une clé API pour un LLM OpenAI-compatible

### Avec uv (recommandé)

```bash
cd ~/dev/datagouv-slack-agent
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Avec pip

```bash
cd ~/dev/datagouv-slack-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Configuration

```bash
cp .env.example .env
# Édite .env avec tes vraies clés (voir section Variables d'environnement)
```

---

## Configuration Slack

1. Va sur https://api.slack.com/apps et crée une nouvelle app ("From scratch").
2. **Socket Mode** : dans "Socket Mode", active-le. Génère un App-Level Token avec le scope `connections:write`. C'est ton `SLACK_APP_TOKEN` (xapp-...).
3. **Scopes du bot** : dans "OAuth & Permissions" > Bot Token Scopes, ajoute :
   - `app_mentions:read`
   - `channels:history`
   - `groups:history`
   - `chat:write`
   - `reactions:write`
4. **Events** : dans "Event Subscriptions", active et souscris aux bot events :
   - `app_mention`
   - `message.channels`
   - `message.groups`
5. **Installation** : "Install to Workspace". Récupère le Bot User OAuth Token (`SLACK_BOT_TOKEN`, xoxb-...).
6. Invite le bot dans un canal : `/invite @ton-bot`.

---

## Configuration du LLM

L'agent utilise un LLM compatible OpenAI (API REST `/v1/chat/completions`). Cela inclut :
- OpenAI (GPT-4, GPT-4o-mini...)
- LiteLLM (proxy interne)
- vLLM, Ollama, etc.

Dans `.env` :
```bash
OPENAI_BASE_URL=https://api.internal.ig1.ai  # ou https://api.openai.com/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL=kimi-k26-thinking                 # ou gpt-4o-mini
OPENAI_TEMPERATURE=0.2
OPENAI_MAX_TOKENS=3000
```

**Pourquoi temperature=0.2 ?** Pour limiter la créativité : l'agent doit suivre des instructions précises (format `_refs`, discipline tools) plutôt que d'être inventif.

---

## Configuration MCP

Le projet utilise le **MCP officiel data.gouv.fr** : `https://mcp.data.gouv.fr/mcp`

Le protocole MCP (Model Context Protocol) expose des tools que le LLM peut appeler :
- `search_datasets`, `search_organizations`, `search_dataservices`
- `get_dataset_info`, `list_dataset_resources`, `get_resource_info`
- `query_resource_data` (Tabular API)
- `get_dataservice_info`, `get_dataservice_openapi_spec`
- `get_metrics`

**Self-host futur** : l'URL est configurable via `DATAGOUV_MCP_URL`.

---

## Intégration du Skill data.gouv.fr

Le **Skill** est un ensemble de 6 documents Markdown dans `app/knowledge/datagouv_skill/` :
- `overview.md` : présentation de data.gouv.fr
- `catalog_api.md` : fonctionnement du catalogue
- `tabular_api.md` : utilisation de l'API tabulaire
- `datasets_pieges.md` : pièges réels des datasets (DVF, DPE, etc.)
- `mcp_usage.md` : guide d'utilisation du MCP
- `examples.md` : exemples de requêtes

**Fonctionnement** : le `SkillRetriever` fait un scoring lexical sur la requête utilisateur (keywords frontmatter + tokens contenu) et injecte les 2 documents les plus pertinents dans le prompt système du LLM. Pas d'embeddings : inutile pour 6 documents.

**Exemple** : une question sur IRVE injecte `datasets_pieges` et `overview`. Une question DVF injecte `datasets_pieges` (Paris par arrondissement, valeur foncière = mutation entière).

---

## Variables d'environnement

| Variable | Description | Exemple |
|----------|-------------|---------|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (xoxb-...) | `xoxb-1234...` |
| `SLACK_APP_TOKEN` | App-Level Token Socket Mode (xapp-...) | `xapp-1-...` |
| `SLACK_SIGNING_SECRET` | Secret de signature (optionnel en Socket Mode) | ` ` |
| `OPENAI_BASE_URL` | URL de l'API LLM | `https://api.internal.ig1.ai` |
| `OPENAI_API_KEY` | Clé API LLM | `sk-...` |
| `OPENAI_MODEL` | Nom du modèle | `kimi-k26-thinking` |
| `OPENAI_TEMPERATURE` | Créativité (0 = déterministe) | `0.2` |
| `OPENAI_MAX_TOKENS` | Longueur max des réponses | `3000` |
| `DATAGOUV_MCP_URL` | URL du serveur MCP | `https://mcp.data.gouv.fr/mcp` |
| `DATAGOUV_API_BASE_URL` | URL API directe (fallback) | `https://www.data.gouv.fr/api/1` |
| `CACHE_DIR` | Répertoire de cache local | `./cache` |
| `MAX_DOWNLOAD_MB` | Taille max de téléchargement | `500` |
| `MAX_SCAN_ROWS` | Lignes scannées pour l'analyse | `100000` |
| `MAX_SAMPLE_ROWS` | Lignes d'exemple dans le résumé | `30` |
| `LLM_CONTEXT_MAX_CHARS` | Taille max du contexte LLM | `60000` |
| `AGENT_MAX_ITERATIONS` | Itérations max de la boucle | `10` |
| `TAVILY_API_KEY` | Clé API Tavily (recherche web, téléphones, emails) | `tvly-...` |
| `LOG_LEVEL` | Niveau de log | `INFO` |

---

## Lancement avec Docker

```bash
docker compose up --build
```

> **Note :** Docker Desktop peut rencontrer des problèmes de virtualisation sur certains Mac Apple Silicon (erreur Rosetta / macOS Sequoia). Le projet fonctionne parfaitement sans Docker via `python -m app.main`.

---

## Exemples d'utilisation

### CLI (question unique)

```bash
python -m scripts.chat_cli --list-tools
python -m scripts.chat_cli "Trouve les datasets sur les bornes de recharge"
```

### REPL (conversation multi-tours)

```bash
python -m scripts.chat_repl
```

Exemple de session :
```
> Trouve le dataset IRVE
[agent liste 3 datasets avec _refs]
> liste les ressources du premier
[agent appelle list_dataset_resources avec l'ID du premier]
> télécharge et analyse le premier CSV
[agent download_and_analyze_resource]
> combien de points de charge à Paris ?
[agent query_cached_resource avec SQL SELECT ...]
```

### Slack

```bash
python -m app.main
```

Puis dans Slack : `@ton-bot trouve les datasets sur les bornes de recharge`

---

## Limites connues

1. **Le LLM ne voit jamais le fichier brut** — seul un résumé structuré est injecté. Certains détails fins peuvent être manqués.
2. **MAX_SCAN_ROWS=100000** — l'analyse descriptive est approximative sur les très gros fichiers (mais les requêtes SQL portent sur le fichier entier).
3. **DuckDB et les dates** — nécessite `pytz` pour les fuseaux horaires.
4. **Docker non testé** — Dockerfile écrit mais jamais exécuté avec succès (problème Docker Desktop / macOS Sequoia).
5. **Carte PNG abandonnée** — `staticmap` est trop lent/instable pour la production.

---

## Architecture interne

```
Slack (Socket Mode)
 └── app/slack/bot.py              réception events, réponse en thread
      └── app/slack/thread_context.py   reconstruction historique
           └── app/agent/orchestrator.py  boucle agentique
                ├── app/agent/prompts.py      prompt système + discipline
                ├── app/agent/local_tools.py  2 tools locaux
                ├── app/llm/client.py         client OpenAI-compatible
                ├── app/mcp/datagouv_mcp_client.py   10 tools MCP
                ├── app/datagouv/api_client.py       API directe (fallback)
                ├── app/knowledge/skill_retriever.py scoring lexical
                └── app/data/
                     ├── downloader.py    streaming + cache
                     ├── analyzer.py      DuckDB (types, stats, geo/date)
                     └── query.py         SQL sécurisé (SELECT only)
```

**Flux d'une requête :**
1. `SkillRetriever` injecte les docs pertinents dans le prompt système
2. Session MCP ouverte, tools MCP + locaux exposés au LLM
3. Boucle : LLM → tool_calls → dispatch (MCP ou local) → résultats plafonnés → réinjection
4. Fallback API automatique si MCP échoue
5. Réponse finale avec ligne `_refs: dataset_1=..., local=...` pour les tours suivants

---

## Pistes d'amélioration

1. **Hébergement cloud** — passer sur Render/Railway pour tourner 24/7
2. **Tests d'intégration Slack** — tester `bot.py` et `thread_context.py` avec des mocks
3. **CI/CD** — GitHub Actions pour les tests + build Docker
4. **Embeddings du Skill** — si le Skill passe à 20+ documents, envisager des embeddings légers
5. **Boutons interactifs Slack** — Blocks avec boutons "Exporter" / "Graphique"

---

## Tests

```bash
pytest
```

41 tests, ~20 secondes. Mocks réseau via `respx`.

---

## Problèmes connus

Voir [TROUBLESHOOTING.md](TROUBLESHOOTING.md) pour les bugs courants (Docker, dépendances, tokens Slack).
