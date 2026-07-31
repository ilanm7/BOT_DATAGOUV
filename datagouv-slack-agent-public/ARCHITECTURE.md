# 🧠 CHOIX D'ARCHITECTURE — DATAGOUV-AGENT

> Document de référence pour répondre à toute question technique en peer-review.
> Chaque section = un choix, les alternatives, et la justification.

---

## 1. POURQUOI PYTHON 3.12 + ASYNCIO ?

**Choix :** Python 3.12 avec `async`/`await` sur toute la stack.

**Alternatives rejetées :**
- **Node.js** — Bon pour les bots Slack, mais l'écosystème data.gouv/data science est 100% Python. Pas de pandas, pas de matplotlib natif.
- **Go** — Excellent pour la concurrence, mais pas adapté à un projet solo étudiant (courbe d'apprentissage, pas d'écosystème data).
- **Python sync** — Plus simple à écrire, mais le bot fait des appels réseau partout (Slack, OpenAI, MCP, data.gouv). En sync, un appel lent bloque tout le bot pour tous les utilisateurs.

**Pourquoi asyncio :**
- Le bot passe son temps à attendre des réponses réseau (API OpenAI = 2-5s, MCP = 1-3s, Slack = 200ms).
- Avec `async`, pendant qu'un utilisateur attend le LLM, le bot peut recevoir et traiter les messages des 4 autres utilisateurs.
- Le sémaphore à 5 limite la charge sans bloquer Slack (les events sont toujours reçus).

**Question probable :** *"Pourquoi pas un worker pool avec multiprocessing ?"*
**Réponse :** Le GIL de Python n'est pas un problème ici car le bottleneck est réseau, pas CPU. `asyncio` est plus léger que des processus. Pour du CPU lourd (génération de graphique matplotlib), c'est synchrone mais rapide (<1s). Si on scale, on externalisera les workers.

---

## 2. POURQUOI SLACK BOLT EN SOCKET MODE ?

**Choix :** `slack-bolt` avec `AsyncSocketModeHandler`.

**Alternatives rejetées :**
- **HTTP Events (bolt sans socket)** — Nécessite un serveur web public (Flask/FastAPI) avec une URL fixe et un certificat SSL. Impossible à faire tourner sur un laptop sans tunnel (ngrok).
- **API Slack bas niveau** (`slack_sdk` brut) — Trop verbeux. Bolt gère la signature des requêtes, le retry, le parsing des events.
- **Mattermost / Teams** — L'entreprise utilise Slack. Zéro adoption si on change l'outil de communication.

**Pourquoi Socket Mode :**
- Connexion WebSocket temps réel depuis n'importe où (laptop, VM, container).
- Pas besoin d'IP publique, de reverse proxy, ni de ngrok.
- Le bot se reconnecte automatiquement (logique dans `main.py` avec backoff exponentiel).
- Un seul token (`xapp-`) à gérer.

**Question probable :** *"Et si le firewall bloque le WebSocket ?"*
**Réponse :** Socket Mode utilise HTTPS (port 443) en fallback. C'est du traffic WebSocket standard, rarement bloqué en entreprise.

---

## 3. POURQUOI UNE BOUCLE AGENTIQUE (ORCHESTRATEUR) ?

**Choix :** Pattern ReAct (Reasoning + Acting) avec boucle LLM ↔ outils.

**Alternatives rejetées :**
- **LLM seul sans outils** — Hallucine sur les données data.gouv. Le LLM n'a pas accès au catalogue en temps réel (cutoff 2024).
- **Code déterministe sans LLM** — Trop rigide. Impossible de comprendre "trouve-moi un dataset surprenant" ou "l'autre".
- **LangChain / LlamaIndex** — Trop magiques, trop d'abstraction. Pour un projet pédagogique et un débutant, il faut comprendre chaque étape. L'orchestrateur maison fait 200 lignes, on contrôle tout.

**Pourquoi ReAct maison :**
- Le LLM reçoit une liste d'outils (MCP + locaux) avec leur description.
- Il décide lui-même quel outil appeler, avec quels paramètres.
- L'orchestrateur exécute, renvoie le résultat, et le LLM réfléchit à nouveau.
- Boucle jusqu'à réponse finale ou limite d'itérations (8 max).

**Question probable :** *"Pourquoi pas LangChain ? C'est le standard."*
**Réponse :** LangChain ajoute 10 couches d'abstraction pour un gain nul ici. Notre boucle fait exactement ce que fait LangChain Agents en 200 lignes au lieu de 2000. On maîtrise le prompt, les retries, la troncature. C'est un choix pédagogique et de contrôle.

---

## 4. POURQUOI MCP (MODEL CONTEXT PROTOCOL) ?

**Choix :** Connexion à data.gouv.fr via un serveur MCP externe.

**Alternatives rejetées :**
- **API data.gouv brute** — Il faut écrire à la main les appels HTTP pour chaque endpoint (search, dataset info, resource query, etc.). 500 lignes de glue code.
- **SDK Python data.gouv** — N'existe pas en officiel stable.
- **Hardcoder les URLs des datasets** — Non maintenable, le catalogue change chaque jour.

**Pourquoi MCP :**
- C'est un standard ouvert (proposé par Anthropic) pour connecter des outils à des LLM.
- Le serveur MCP data.gouv expose 10 outils avec leurs schemas JSON.
- L'orchestrateur demande la liste des outils au serveur, les présente au LLM sous format OpenAI Functions.
- Zero glue code : le LLM sait tout seul quand appeler `search_datasets` vs `get_dataset_info`.

**Question probable :** *"Et si le serveur MCP data.gouv tombe ?"*
**Réponse :** Dans `orchestrator.py`, l'appel MCP est wrappé dans un `try/except`. Si le serveur MCP est down, le bot retourne : *"Le service data.gouv est temporairement indisponible"*. On a aussi l'API directe en fallback (`datagouv_api_base_url`) pour la recherche programmatique.

---

## 5. POURQUOI GPT-4O-MINI ET PAS UN MODÈLE OPEN SOURCE ?

**Choix :** `gpt-4o-mini` via API OpenAI-compatible.

**Alternatives rejetées :**
- **GPT-4o** — 15x plus cher. Pour un bot d'exploration de données, le raisonnement de 4o-mini suffit largement.
- **Claude 3.5 Sonnet** — Excellent, mais plus cher et moins rapide pour ce use case.
- **Llama 3 / Mistral local** — Nécessite un GPU dédié (coûteux, bruyant, consommateur d'énergie). Un MacBook Pro M3 fait tourner Llama 3 8B mais c'est lent (10-20s par réponse) et moins fiable sur le tool-calling.
- **Ollama en local** — Prévu dans la roadmap, mais pas pour la V1. L'API externe garantit la rapidité et la fiabilité.

**Pourquoi gpt-4o-mini :**
- Tool-calling natif et très fiable (le LLM comprend quand appeler `search_datasets` vs `get_dataset_info`).
- Rapide (2-4s par tour).
- Bon marché (~0,15€ / 1M tokens input, ~0,60€ / 1M output).
- Une conversation complète (3-4 tours) coûte ~0,005€.

**Question probable :** *"Vous envoyez nos données à OpenAI ?"*
**Réponse :** On utilise l'API OpenAI, pas ChatGPT. Les données ne servent pas à entraîner les modèles (policy OpenAI API). De plus, on ne logue pas de données sensibles : les inputs sont nettoyés (`_sanitize_user_input`), les SIRET/téléphones sont masqués dans les logs (`_safe_args`).

---

## 6. POURQUOI SQLITE ET PAS REDIS ?

**Choix :** SQLite avec `PRAGMA journal_mode=WAL`.

**Alternatives rejetées :**
- **Redis** — Nécessite un serveur Redis supplémentaire (infra, maintenance, coût). Overkill pour un prototype.
- **JSON files** — Pas de requêtes SQL, pas d'index, corruption possible si écriture concurrente.
- **PostgreSQL** — Trop lourd à déployer pour un bot Slack.

**Pourquoi SQLite :**
- Zero configuration (un fichier `.db` dans le dossier cache).
- WAL (Write-Ahead Logging) permet plusieurs lecteurs simultanés.
- Suffisant pour la charge actuelle (< 1000 messages/jour).
- Chaque message ouvre une connexion courte (`with sqlite3.connect(...)`), donc pas de problème de connexion persistante.

**Question probable :** *"SQLite tiendra la charge si 50 personnes parlent en même temps ?"*
**Réponse :** SQLite gère très bien les lectures concurrentes en WAL. L'écriture est sérialisée, mais une écriture de message fait ~5ms. Avec le sémaphore à 5, le pire cas c'est 5 écritures en file indienne = 25ms. C'est invisible. Si on passe à 500 users simultanés, là oui il faudra PostgreSQL ou aiosqlite.

---

## 7. POURQUOI UN SÉMAPHORE À 5 ?

**Choix :** `asyncio.Semaphore(5)` dans `orchestrator.py`.

**Alternatives rejetées :**
- **Aucune limite** — Si 20 personnes parlent en même temps, 20 appels LLM partent en parallèle. L'API OpenAI rate-limite, les réponses deviennent lentes, le bot plante.
- **Limite à 1** — File d'attente interminable. Le 6ème user attend 2 minutes.
- **Limite par user** — Plus complexe à implémenter. Un user spammeur peut quand même saturer le bot.

**Pourquoi 5 :**
- C'est le sweet spot entre réactivité et protection.
- gpt-4o-mini accepte facilement 5 req/s sans ralentir.
- En dessous de 5, on gaspille du temps d'attente. Au-dessus, on risque le rate limit.

**Question probable :** *"Pourquoi pas un rate limit par user ?"*
**Réponse :** C'est prévu dans la roadmap. Pour la V1, le sémaphore global suffit. Si un user spamme, il bloque les autres 5 slots maximum. On peut ajouter un cooldown par user (ex: max 1 msg/10s) en 20 lignes de code.

---

## 8. POURQUOI DES RÉPONSES SOCIALES SANS LLM ?

**Choix :** Regex (`_SOCIAL_RE.fullmatch`) pour détecter "salut", "merci", "ça va ?" et répondre instantanément sans appeler l'IA.

**Alternatives rejetées :**
- **Tout passer au LLM** — Coûteux et lent. "Salut" = 1 appel API = 0,005€ et 2 secondes d'attente.
- **Ignorer les messages sociaux** — Le bot paraît froid et robotique.

**Pourquoi :**
- Économie : 80% des interactions Slack commencent par "Salut @bot".
- UX : réponse instantanée (< 10ms).
- Le message est quand même sauvegardé en base pour la mémoire.

**Question probable :** *"Et si quelqu'un dit 'Salut, cherche des auto-écoles' ?"*
**Réponse :** La regex utilise `fullmatch`, pas `search`. "Salut, cherche..." ne matchera pas. Seul "Salut" exact match. Le message partira au LLM.

---

## 9. POURQUOI COURT-CIRCUITER EXPORT/STATS/GRAPHIQUE DANS BOT.PY ?

**Choix :** Détection de mots-clés rapides dans `bot.py` avant l'orchestrateur.

**Alternatives rejetées :**
- **Tout passer par l'orchestrateur** — Le LLM coûte de l'argent et du temps. Pour "exporte en CSV", pas besoin de réfléchir.
- **Commandes slash uniquement** — Moins naturel que "exporte en Excel" dans la conversation.

**Pourquoi :**
- "Exporte en CSV" → 0 appel LLM, 0,00€, réponse en 500ms.
- "Stats" → calcul local, 0 appel LLM.
- "Graphique" → génération matplotlib locale.
- C'est un pattern "fast path" standard dans les bots conversationnels.

**Le bug qu'on a corrigé :** Le court-circuit graphique ne vérifiait pas si le sujet avait changé. Maintenant il compare les mots du message avec la requête en cache. Si c'est un nouveau sujet, il passe par l'orchestrateur.

---

## 10. POURQUOI MATPLOTLIB ET PAS UN OUTIL DE VISU EN LIGNE ?

**Choix :** `matplotlib` en mode non-interactif (`Agg`).

**Alternatives rejetées :**
- **Plotly / Bokeh** — Génèrent du HTML/JS interactif. Plus lourd, nécessite un navigateur. Slack affiche mal l'interactivité.
- **Tableau / PowerBI** — Trop lourds, coûteux, nécessitent une licence.
- **API externe de graphiques** — Dépendance supplémentaire, rate limits, coût.

**Pourquoi matplotlib :**
- Standard de Python, zero config.
- Génère du PNG propre que Slack affiche nativement.
- Pas d'interactivité mais c'est ce qu'on veut pour un export rapide.
- Non-interactif (`Agg`) = pas de fenêtre graphique, fonctionne sur un serveur sans écran.

---

## 11. POURQUOI TAVILY POUR L'ENRICHISSEMENT ?

**Choix :** API Tavily pour chercher téléphones, emails, dirigeants sur le web.

**Alternatives rejetées :**
- **Scraping maison avec BeautifulSoup** — Fragile (les sites changent), illégal sur certains sites (CGU), lent.
- **API Pappers / Societe.com** — Payantes, pas toujours à jour.
- **Ne pas enrichir** — Le dataset officiel n'a pas de téléphones. L'export serait inutilisable pour un commercial.

**Pourquoi Tavily :**
- API de recherche web conçue pour les LLM. Elle retourne des résultats structurés.
- Plus fiable que du scraping maison.
- Clé API gratuite à l'essai, puis payante mais raisonnable.
- L'enrichissement est optionnel (ne se déclenche que si la clé est configurée).

---

## 12. POURQUOI PYDANTIC-SETTINGS ?

**Choix :** `pydantic-settings` pour la config.

**Alternatives rejetées :**
- **os.environ direct** — Pas de validation de type, pas de valeurs par défaut, erreurs silencieuses.
- **YAML/JSON de config** — Fichier à versionner = risque de commit de secrets.
- **argparse** — Pas adapté à une app longue durée.

**Pourquoi pydantic-settings :**
- Charge automatiquement depuis `.env` + variables d'environnement.
- Validation de type native (`int`, `float`, `str`).
- Clamping automatique (`max_download_mb` entre 1 et 2000, `agent_max_iterations` entre 1 et 20).
- `extra="ignore"` → si un jour on ajoute une variable inutile dans `.env`, ça ne plante pas.

---

## 13. POURQUOI DES TESTS AVEC PYTEST ET RUFF ?

**Choix :** `pytest` pour les tests, `ruff` pour le linting.

**Alternatives rejetées :**
- **unittest** — Verbeux, moins de plugins.
- **flake8 + black + isort** — 3 outils à configurer. Ruff fait les 3 en un seul binaire (Rust = rapide).
- **Pas de tests** — Impossible de refactorer sans casser.

**Pourquoi :**
- 37 tests qui valident les fonctions critiques (extraction d'IDs, nettoyage de texte, génération de graphiques).
- Ruff vérifie en 50ms ce que flake8 faisait en 3s.
- Le code est typé (PEP 484) pour la documentation et la détection d'erreurs.

---

## 14. POURQUOI PAS D'AUTHENTIFICATION UTILISATEUR ?

**Choix :** Pas de login, pas de rôles, pas de permissions.

**Alternatives rejetées :**
- **OAuth 2.0 complet** — Complexe à implémenter. Nécessite une base de users, une gestion de tokens, une interface admin.
- **Système de rôles** — Overkill pour un prototype interne.

**Pourquoi :**
- Le bot est dans un workspace Slack interne. Seuls les employés ont accès.
- Slack fournit déjà l'identité (`user_id`). Pas besoin de recréer un système de login.
- Pour la V1, on fait confiance au workspace. Pour la V2, on ajoutera des rôles (admin/user).

---

## 15. POURQUOI LE CACHE EST LOCAL (FICHIER) ?

**Choix :** Dossier `./cache` sur le disque local.

**Alternatives rejetées :**
- **S3 / Cloud Storage** — Nécessite des credentials AWS, coût, latence réseau.
- **Base de données blob** — Overkill pour des fichiers temporaires.

**Pourquoi :**
- Les exports CSV/Excel et graphiques PNG sont temporaires (nettoyés après 7 jours par `_cleanup_cache_files`).
- Un dossier local est instantané (pas de latence réseau).
- Si le bot redémarre, le cache persiste (pas de perte de données).
- Cleanup automatique : supprime les fichiers de +7 jours et si le dossier dépasse 2 Go.

---

## 16. CE QU'ON A VOLONTAIREMENT SIMPLIFIÉ

| Simplification | Pourquoi | Quand on complexifiera |
|---|---|---|
| SQLite au lieu de PostgreSQL | Prototype | > 100 users simultanés |
| Cache fichier au lieu de S3 | Prototype | Multi-instance du bot |
| Sémaphore global au lieu de rate limit par user | Prototype | Si un user spamme |
| Pas de CI/CD | Prototype | Déploiement automatique |
| Pas de monitoring (Sentry/DataDog) | Prototype | Production externe |
| Pas de tests d'intégration Slack | Difficile à mocker | Quand le bot sera stable |

---

## 🎯 RÉSUMÉ POUR L'ÉQUIPE

> "On a fait des choix pragmatiques pour livrer rapidement un bot fonctionnel et compréhensible. Python async + Slack Bolt Socket Mode = déploiement instantané. MCP = pas de glue code data.gouv. SQLite + cache fichier = zero infra. Le sémaphore à 5 protège les coûts LLM. Chaque choix est justifiable et réversible quand le bot passera à l'échelle."
