# Troubleshooting — Problèmes connus et solutions

## Installation

### `ModuleNotFoundError: No module named 'pytz'`
**Cause :** DuckDB a besoin de `pytz` pour parser les dates avec fuseaux horaires dans les CSV.  
**Solution :** `pytz>=2024.1` est maintenant dans `pyproject.toml`. Réinstalle :
```bash
uv pip install -e ".[dev]"
```

### `ModuleNotFoundError: No module named 'aiohttp'`
**Cause :** `slack-bolt` en mode asynchrone Socket Mode nécessite `aiohttp`.  
**Solution :** `aiohttp>=3.9` est maintenant dans `pyproject.toml`. Réinstalle comme ci-dessus.

## Docker

### Docker Desktop ne démarre pas sur macOS Apple Silicon
**Symptôme :** `Docker Engine stopped`, erreur Rosetta, ou socket absent.  
**Cause :** Bug de Docker Desktop 4.81+ sur macOS Sequoia (15.x) avec le Virtualization Framework.  
**Solution :** Le projet fonctionne parfaitement sans Docker. Utilise `python -m app.main`.

## Tests

### `pytest` trouve 0 tests
**Cause :** Le dossier `tests/` est vide ou manquant.  
**Solution :** Vérifie que `tests/` contient les fichiers `test_*.py`. La version complète est sur le Desktop si le Downloads est incomplet.

## Slack

### `invalid_auth` au démarrage du bot
**Symptôme :** `slack_sdk.errors.SlackApiError: invalid_auth`  
**Cause :** Les tokens Slack dans `.env` sont des placeholders.  
**Solution :** Crée une app Slack, récupère les vrais tokens, et remplace-les dans `.env`.

## Données

### DuckDB ne lit pas un CSV français
**Symptôme :** Erreur de parsing ou types incorrects.  
**Cause :** Séparateur `;`, nombres avec virgule, accents dans les headers.  
**Solution :** `read_csv_auto` avec `ignore_errors=true` gère la plupart des cas. Pour les requêtes SQL sur des nombres français : `CAST(replace(col, ',', '.') AS DOUBLE)`.
