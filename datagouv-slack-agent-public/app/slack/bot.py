"""Bot Slack (Socket Mode).

Deux points d'entrée :
- @mention du bot dans un canal : démarre une conversation, répond en thread ;
- message dans un thread où le bot a déjà répondu : poursuit la conversation.
"""

import asyncio
import contextlib
import datetime
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from app.agent.local_tools import _deep_enrich, execute_local_tool
from app.agent.orchestrator import AgentOrchestrator
from app.config import Settings
from app.data.charts import (
    generate_bar_chart,
    generate_categorical_chart,
    generate_interactive_map,
    generate_pie_chart,
    generate_scatter_map,
)
from app.data.exporter import (
    load_enrich_state,
    load_search_results,
    save_enrich_state,
    save_search_results,
)
from app.health import health_check
from app.llm.client import LLMClient
from app.slack.alerts import add_alert, alert_scheduler, list_alerts, remove_alert
from app.slack.ao_alerts import add_ao_alert, fetch_ao_by_idweb, list_ao_alerts, remove_ao_alert
from app.slack.dce_rag import index_dce_pdf, is_dce_indexed, query_dce_rag
from app.slack.demo import run_demo
from app.slack.file_handler import analyze_slack_files
from app.slack.memory import get_thread_memory, get_user_memory, save_message
from app.slack.onboarding import has_been_welcomed, mark_welcomed, send_welcome
from app.slack.profiles import format_profile, get_profile, upsert_profile
from app.slack.tender_response import generate_response_skeleton
from app.slack.tender_tracking import (
    get_ao_from_message,
    is_accepted_reaction,
    list_tracked,
    track_tender,
)
from app.slack.thread_context import build_conversation
from app.web.api_keys import generate_api_key, get_api_key

logger = logging.getLogger(__name__)


async def _fetch_dataset_title(dataset_id: str, api_base: str) -> str:
    """Récupère le titre d'un dataset via l'API data.gouv.fr."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{api_base}/datasets/{dataset_id}/",
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("title", "")
    except Exception:
        logger.exception("Erreur récupération titre dataset %s", dataset_id)
    return ""


async def _search_datagouv_alternatives(query: str, api_base: str, page_size: int = 10) -> str:
    """Recherche programmatique de datasets alternatifs via API data.gouv.fr."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{api_base}/datasets/",
                params={"q": query, "page_size": page_size},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            datasets = data.get("data", [])
            if not datasets:
                return "Aucune alternative trouvée."
            lines = []
            for i, ds in enumerate(datasets, 1):
                slug = ds.get("slug") or ds.get("id", "")
                ds_id = ds.get("id", "")
                lines.append(
                    f"{i}. {ds.get('title', 'Sans titre')} — "
                    f"https://www.data.gouv.fr/fr/datasets/{slug}/ "
                    f"(id: {ds_id})"
                )
            return "\n".join(lines)
    except Exception:
        logger.exception("Erreur recherche programmatique alternatives")
        return "Recherche d'alternatives indisponible."


BOT_ONBOARDING_TEXT_TEMPLATE = """👋 *Coucou ! Je suis {bot_name}, votre assistant data & entreprises.*

Je peux vous aider à :
• 🔍 *Rechercher des données* sur data.gouv.fr
• 🏢 *Trouver des entreprises* par métier et localisation
• 📊 *Exporter* vos résultats en CSV/Excel enrichis
• 📈 *Générer des graphiques* automatiques
• 📎 *Analyser* vos fichiers uploadés
• 🔔 *Créer des alertes* sur les nouveaux datasets
• 📋 *Surveiller les appels d'offre* qui matchent ton profil

Mentionnez-moi (`@{bot_name}`) pour discuter, ou tapez `/datagouv help` pour le détail complet de mes commandes.
"""

BOT_HELP_TEXT = """🤖 *DATAGOUV-AGENT — Aide complète*

Voici tout ce que je sais faire :

*📊 Recherche de données publiques*
• `@DATAGOUV--AGENT trouve les datasets sur [sujet]` — Recherche sur data.gouv.fr
• `@DATAGOUV--AGENT analyse ce dataset [URL]` — Télécharge et analyse un fichier (CSV, Excel, JSON, Parquet, ZIP)
• `@DATAGOUV--AGENT donne-moi un aperçu des données` — Résumé statistique

*🏢 Recherche d'entreprises*
• `@DATAGOUV--AGENT trouve les [métier] du [code postal]` — Recherche par métier + localisation
• `@DATAGOUV--AGENT qui est le dirigeant de [entreprise]` — Dirigeants via API officielle + web
• `@DATAGOUV--AGENT trouve le téléphone/email de [entreprise]` — Coordonnées via recherche web

*📎 Fichiers uploadés*
• Glissez un fichier Excel/CSV/JSON/Parquet/ZIP dans le thread — Je l'analyse automatiquement !

*📥 Export & Graphiques*
• `exporte en Excel` ou `exporte en CSV` — Export des derniers résultats
• `exporte tout avec téléphones et dirigeants` — Export enrichi via web
• `graphique` ou `stats` — Graphique automatique (histogramme, camembert, répartition par ville/NAF...)

*🔔 Alertes datasets*
• `/datagouv alerte [mot-clé]` — Créer une alerte data.gouv.fr
• `/datagouv alertes` — Lister vos alertes actives
• `/datagouv stop [numéro]` — Supprimer une alerte

*🎬 Démo*
• `/datagouv demo` — Démo guidée : recherche AO, scoring, historique acheteur, DCE

*📋 Alertes appels d'offre (AO)*
• `/datagouv ao alerte [mot-clé]` — Créer une alerte AO (ex: `ao alerte cybersécurité`)
• `/datagouv ao alertes` — Lister vos alertes AO
• `/datagouv ao stop [numéro]` — Supprimer une alerte AO
• `/datagouv ao suivis` — Voir les AO que tu suis (deadlines)
• `/datagouv ao trame [idweb]` — Générer un squelette de mémoire technique

*✍️ Réponse aux AO*
• Réponds *trame* ou *mémoire technique* dans le thread d'un push AO — Je génère un plan structuré adapté à ton profil

*📄 RAG sur les DCE (cahiers des charges)*
• `/datagouv ao dce [idweb] [url]` — Indexer le PDF du DCE d'un AO pour pouvoir l'interroger
• `/datagouv ao question [idweb] [ta question]` — Poser une question sur un DCE déjà indexé
• Réponds dans le thread d'un push AO avec des mots-clés comme *critères*, *notation*, *pénalités*… — Je réponds directement depuis le DCE s'il est indexé

*📊 Dashboard & API*
• <http://localhost:8080|Dashboard web> — Stats globales, deadlines, profils clients, historique des pushes
• `/datagouv apikey` — Voir ta clé API d'intégration
• `/datagouv apikey reset` — Régénérer ta clé API
• API REST : `http://localhost:8080/api/v1/{alerts,tracked,pushes,profile,stats}` — Auth par header `X-API-Key`

*🔌 APIs externes branchées (toutes gratuites)*
• France Travail (offres d'emploi) • Hub'Eau (nappes phréatiques, qualité eau)
• Legifrance (codes, lois, décrets) • Météo France (prévisions 5 jours)
• RNB (bâtiments, parcelles) • INSEE Sirene • Base Adresse Nationale

_💡 Astuce : mets un emoji ✅ ou 🔥 sur un push AO pour le suivre et recevoir les rappels J-15 / J-7 / J-2._

*👤 Profil entreprise (pour le scoring AO)*
• `/datagouv profil` — Voir ton profil
• `/datagouv profil activité=BTP taille=PME zones=75,93` — Mettre à jour

*💡 Conseils*
• Je réponds toujours dans un *thread* pour garder le contexte
• J'ai une *mémoire persistante* : je me souviens de nos conversations précédentes
• Posez-moi des questions en français, je comprends !

*Besoin d'aide ?* Mentionnez-moi avec "aide" ou "help".
"""

MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+>")
# Capture les URLs dataset (slug ou ID) et les URLs de ressources directes (/r/...)
DATASET_URL_RE = re.compile(
    r"https?://(?:www\.)?data\.gouv\.fr/fr/datasets/(?:r/)?([^/\s?#]+)"
)
_SOCIAL_RE = re.compile(
    r"^(bonjour|salut|hello|hey|coucou|bonsoir|yo|wesh|slt|"
    r"ça va|ca va|comment ça va|"
    r"tu vas bien|vous allez bien|comment vas-tu|comment allez-vous|"
    r"merci|thanks|thx|merci beaucoup|au revoir|bye|ciao|à plus|a plus)"
    r"[\s!?.]*$",
    re.IGNORECASE,
)
_SOCIAL_REPLY = (
    "Salut ! 😊 Je suis prêt à t'aider sur les données publiques. "
    "Qu'est-ce que tu cherches ?"
)


def _extract_dataset_ids(text: str) -> list[str]:
    """Extrait les slugs/IDs de datasets depuis les URLs data.gouv présentes dans le texte."""
    return DATASET_URL_RE.findall(text)


_REFS_RE = re.compile(r"dataset_\d+=([a-f0-9]+)")


def _extract_refs_ids(text: str) -> list[str]:
    """Extrait les IDs data.gouv depuis les lignes _refs: dataset_1=xxx, ..."""
    return _REFS_RE.findall(text)


_SIREN_RE = re.compile(r"\d[\d\s]{7,}\d")


def _extract_single_siren(text: str) -> str | None:
    """Extrait le SIREN principal du texte (ex: tableau markdown SIRENE).

    Privilégie les SIREN/SIRET explicitement labellés (| SIREN | ... |)
    pour éviter de confondre avec un SIREN mentionné en passant dans le texte.
    Retourne le SIREN (9 chiffres) si exactement un est trouvé, sinon None.
    """
    candidates: set[str] = set()

    # 1. SIREN/SIRET explicitement labellés (tableau markdown ou SIREN: xxx)
    label_re = re.compile(
        r"(?:^|\|)\s*(?:SIREN|SIRET)\b[^|\n]*?(?:\||:)\s*(\d[\d\s]{7,}\d)",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in label_re.findall(text):
        cleaned = re.sub(r"\s+", "", match)
        if len(cleaned) == 9:
            candidates.add(cleaned)
        elif len(cleaned) == 14:
            candidates.add(cleaned[:9])
    if len(candidates) == 1:
        return candidates.pop()

    # 2. Fallback : tout SIREN/SIRET trouvé dans le texte
    for raw in _SIREN_RE.findall(text):
        cleaned = re.sub(r"\s+", "", raw)
        if len(cleaned) == 9:
            candidates.add(cleaned)
        elif len(cleaned) == 14:
            candidates.add(cleaned[:9])
    return candidates.pop() if len(candidates) == 1 else None


def _clean_answer(text: str) -> str:
    """Nettoie les réponses du LLM avant envoi Slack.

    Préserve la ligne `_refs: ...` qui permet au LLM de référencer les datasets
    dans les tours suivants ("le premier", "le deuxième", "l'autre").
    Convertit aussi le markdown standard en mrkdwn Slack.
    """
    if not text or not text.strip():
        return text

    # [RÉSULTAT BRUT] Les résultats bruts (search_enterprises, tableaux markdown)
    # ne doivent PAS être nettoyés agressivement — seule la conversion
    # markdown → mrkdwn est appliquée.
    if "[RÉSULTAT BRUT" in text:
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)
        cleaned = re.sub(r"(?m)^\*\s+", "• ", cleaned)
        cleaned = re.sub(r"(?m)^-\s+", "• ", cleaned)
        # Préserver les _refs si présents
        refs_line = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("_refs:") or stripped.startswith("refs:"):
                ids = re.findall(r"dataset_\d+=[a-f0-9]+", stripped)
                if ids:
                    refs_line = "_refs: " + ", ".join(ids)
        if refs_line:
            cleaned = cleaned + "\n\n" + refs_line
        return cleaned

    # 1. Extraire la ligne _refs: ou refs: AVANT tout nettoyage pour la préserver
    refs_line = ""
    remaining_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("_refs:") or stripped.startswith("refs:"):
            refs_line = stripped
        else:
            remaining_lines.append(line)

    # 1b. Nettoyer la ligne refs pour ne garder que les IDs dataset valides
    # (supprime le texte collé comme "data.gouv.frPlateforme...")
    if refs_line:
        ids = re.findall(r"dataset_\d+=[a-f0-9]+", refs_line)
        refs_line = "_refs: " + ", ".join(ids) if ids else ""

    lines = remaining_lines
    # 2. Patterns de spam en fin de message (liens bruts, unfurl, refs malformés)
    spam_patterns = (
        "data.gouv.fr",
        "plateforme des données publiques",
        "consulter, télécharger et intégrer",
        "données cadastrales ouvertes",
        "cadastre.data.gouv.fr",
        "legal notices",
        "terms of service",
    )
    while lines:
        stripped = lines[-1].strip().lower()
        if stripped == "" or any(p in stripped for p in spam_patterns):
            lines.pop()
        else:
            break

    # 2b. Supprimer les lignes où un domaine est collé directement à du texte
    # (ex: "datavaccin-covid.ameli.frVaccination...", "WikipediaCampagne...",
    #  "paris.frÉtat...", "aqicn.orgPollution...", "BRGMNappes...")
    #  Capture aussi les caractères accentués (À-ÿ) après le TLD.
    _domain_glued = re.compile(r"[a-z0-9-]+\.[a-z]{2,}[A-ZÀ-ÿ]")
    #  Capture aussi les noms de sites/acronymes majuscules collés à du texte
    #  (ex: BRGMNappes). On exige une minuscule après l'acronyme pour éviter
    #  les faux positifs sur les mots entièrement en majuscules (FORMATION, BOAMP, etc.)
    _site_glued = re.compile(r"\b[A-Z]{3,}[a-zà-ÿ]")
    lines = [
        line for line in lines
        if line.strip().startswith("|")
        or line.strip().startswith("•")              # Préserver les puces structurées (Achetteur, Publié le, etc.)
        or re.match(r"^(\*{0,2})?\d+\.", line.strip())  # Préserver les lignes numérotées (1., *1., **1., ...)
        or not (_domain_glued.search(line) or _site_glued.search(line))
    ]

    cleaned = "\n".join(lines)
    # 3. Si le nettoyage a tout supprimé, on garde l'original (réponse courte ou hors sujet)
    if not cleaned.strip():
        cleaned = text.strip()

    # 4. Convertir markdown standard → mrkdwn Slack
    # **gras** → *gras* (mais pas dans les liens <...>)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", cleaned)
    # Lignes commençant par "*   " ou "* " → bullet Slack "• "
    cleaned = re.sub(r"(?m)^\*\s+", "• ", cleaned)
    # Lignes commençant par "- " → bullet Slack "• "
    cleaned = re.sub(r"(?m)^-\s+", "• ", cleaned)

    # 5. Remettre les _refs: à la fin si présents
    if refs_line:
        cleaned = cleaned + "\n\n" + refs_line
    return cleaned


def _try_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _generate_ascii_chart(data: list[dict], title: str = "Graphique") -> str:
    """Génère un tableau de bord ASCII à partir de données structurées (AO, etc.).

    Pour les AO : génère plusieurs analyses pertinentes (timeline, urgence, acheteurs, geo).
    """
    if not data:
        return ""
    # Détection type de données — AO (appels d'offre)
    is_ao = any("idweb" in str(r.keys()) for r in data[:1])
    if not is_ao:
        return ""

    from datetime import datetime

    lines: list[str] = [f"📊 *{title}*", ""]

    # ── 1. TIMELINE DES PUBLICATIONS ──
    dates: dict[str, int] = {}
    for item in data:
        dp = item.get("dateparution")
        if dp:
            try:
                d = datetime.strptime(str(dp), "%Y-%m-%d")
                key = d.strftime("%d/%m")
                dates[key] = dates.get(key, 0) + 1
            except Exception:
                pass
    if dates:
        sorted_dates = sorted(dates.items(), key=lambda x: datetime.strptime(x[0], "%d/%m"))[-10:]
        max_d = max(v for _, v in sorted_dates)
        max_label = max(len(str(k)) for k, _ in sorted_dates)
        lines.append("📅 *Publications dans le temps (10 derniers jours)*")
        for key, val in sorted_dates:
            bar_len = int((val / max_d) * 15) if max_d > 0 else 0
            bar = "█" * bar_len
            lines.append(f"{str(key):<{max_label}} | {bar} {val}")
        lines.append("")

    # ── 2. URGENCE — DÉLAI PUBLICATION → DEADLINE ──
    delays: list[int] = []
    for item in data:
        dp = item.get("dateparution")
        dl = item.get("datelimitereponse")
        if dp and dl:
            try:
                d1 = datetime.strptime(str(dp), "%Y-%m-%d")
                d2 = datetime.strptime(str(dl), "%Y-%m-%d")
                delta = (d2 - d1).days
                if 0 < delta < 365:
                    delays.append(delta)
            except Exception:
                pass
    if delays:
        avg_delay = sum(delays) // len(delays)
        urgent = sum(1 for d in delays if d <= 30)
        lines.append(f"⏱️ *Urgence des marchés* — Délai moyen : {avg_delay} jours")
        lines.append(f"   • {urgent} marché(s) ferment dans ≤30 jours")
        lines.append(f"   • {len(delays) - urgent} marché(s) ont plus de 30 jours")
        # Distribution par tranches
        buckets = {"≤15j": 0, "16-30j": 0, "31-60j": 0, "61-90j": 0, ">90j": 0}
        for d in delays:
            if d <= 15:
                buckets["≤15j"] += 1
            elif d <= 30:
                buckets["16-30j"] += 1
            elif d <= 60:
                buckets["31-60j"] += 1
            elif d <= 90:
                buckets["61-90j"] += 1
            else:
                buckets[">90j"] += 1
        max_b = max(v for v in buckets.values() if v > 0) if any(v > 0 for v in buckets.values()) else 1
        max_bl = max(len(k) for k in buckets)
        for key, val in buckets.items():
            if val > 0:
                bar_len = int((val / max_b) * 12)
                bar = "█" * bar_len
                lines.append(f"{str(key):<{max_bl}} | {bar} {val}")
        lines.append("")

    # ── 3. TOP ACHETEURS ──
    buyers: dict[str, int] = {}
    for item in data:
        org = item.get("org", "").strip()
        if org and len(org) > 3:
            # Tronquer les noms trop longs pour l'affichage
            short = org[:35] + "…" if len(org) > 35 else org
            buyers[short] = buyers.get(short, 0) + 1
    if buyers:
        top_buyers = sorted(buyers.items(), key=lambda x: x[1], reverse=True)[:5]
        max_bval = max(v for _, v in top_buyers)
        max_blabel = max(len(str(k)) for k, _ in top_buyers)
        lines.append("🏛️ *Top acheteurs*")
        for key, val in top_buyers:
            bar_len = int((val / max_bval) * 12) if max_bval > 0 else 0
            bar = "█" * bar_len
            lines.append(f"{str(key):<{max_blabel}} | {bar} {val}")
        lines.append("")

    # ── 4. RÉPARTITION GÉOGRAPHIGUE ──
    depts: dict[str, int] = {}
    for item in data:
        for d in item.get("departements", []):
            depts[str(d)] = depts.get(str(d), 0) + 1
    if depts:
        top_depts = sorted(depts.items(), key=lambda x: x[1], reverse=True)[:8]
        max_dval = max(v for _, v in top_depts)
        max_dlabel = max(len(str(k)) for k, _ in top_depts)
        lines.append("📍 *Top départements*")
        for key, val in top_depts:
            bar_len = int((val / max_dval) * 12) if max_dval > 0 else 0
            bar = "█" * bar_len
            lines.append(f"{str(key):<{max_dlabel}} | {bar} {val}")
        lines.append("")

    return "\n".join(lines)


def _escape_slack_mrkdwn(text: str) -> str:
    """Échappe les caractères spéciaux mrkdwn de Slack pour éviter le formatting cassé."""
    if not text:
        return text
    # Échapper le backslash d'abord, puis les métacaractères mrkdwn
    text = text.replace("\\", "\\\\")
    for ch in ("*", "_", "~", "`", ">"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _sanitize_user_input(text: str) -> str:
    """[INJECTION PROMPT] Neutralise les tentatives d'injection de prompt via des balises système.
    Supprime aussi les caractères de contrôle (C0, C1, bidirectionnels) pour éviter
    la corruption JSON/DB et les attaques d'injection invisibles."""
    if not text:
        return text
    # Autorisés : espace, tab, retour à la ligne, retour chariot, et tout caractère >= 0x20 sauf DEL (0x7F)
    # On filtre aussi les C1 controls (0x80-0x9F) et les caractères bidirectionnels invisibles
    _bidi = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
    def _ok(ch: str) -> bool:
        cp = ord(ch)
        if ch in "\n\t\r":
            return True
        return bool(cp >= 32 and cp != 127 and not 128 <= cp <= 159 and ch not in _bidi)
    text = "".join(ch for ch in text if _ok(ch))
    # Remplace les balises qui pourraient tromper le LLM en les cassant visuellement
    replacements = {
        "[CONSIGNE SYSTÈME]": "(CONSIGNE SYSTÈME)",
        "[CONTEXTE PRÉCÉDENT]": "(CONTEXTE PRÉCÉDENT)",
        "[MESSAGE ACTUEL]": "(MESSAGE ACTUEL)",
        "[CONSIGNE SYSTEME]": "(CONSIGNE SYSTEME)",
        "[SYSTEM]": "(SYSTEM)",
        "[/SYSTEM]": "(/SYSTEM)",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def _extract_geo_points_from_history(texts: list[str]) -> list[dict]:
    """[EXTRACTION GÉO] Parse l'historique du thread pour retrouver des points lat/lon
    générés par le bot (ex: blocs CSV dans ses réponses précédentes)."""
    import csv
    import io

    points: list[dict] = []
    for text in texts:
        # Cherche un bloc ```csv ... ```
        csv_blocks = re.findall(r"```csv\s*(.*?)```", text, re.DOTALL)
        # Sinon, cherche des lignes qui ressemblent à un header CSV lat/lon
        if not csv_blocks:
            lines = text.splitlines()
            header_idx = None
            for i, line in enumerate(lines):
                low = line.lower()
                if "latitude" in low and "longitude" in low and ("nom" in low or "name" in low):
                    header_idx = i
                    break
            if header_idx is not None:
                csv_blocks.append("\n".join(lines[header_idx:]))

        for block in csv_blocks:
            try:
                reader = csv.DictReader(io.StringIO(block.strip()))
                for row in reader:
                    nom = row.get("nom") or row.get("Nom") or row.get("name") or "Point"
                    lat = row.get("latitude") or row.get("lat")
                    lon = row.get("longitude") or row.get("lon")
                    if lat and lon:
                        try:
                            points.append({"nom": nom, "lat": float(lat), "lon": float(lon)})
                        except ValueError:
                            continue
            except Exception:
                continue
    return points


def _is_recent_search(data: dict, thread_ts: str, max_seconds: int = 600) -> bool:
    """[CACHE THREAD] Vérifie que last_search.json correspond au thread actuel et est récent."""
    if not data or not data.get("results"):
        return False
    stored_ts = data.get("thread_ts")
    # Si thread_ts est stocké, il doit matcher ; si None (fichier global), on accepte
    if stored_ts is not None and stored_ts != thread_ts:
        return False
    timestamp = data.get("timestamp", 0)
    return not (not timestamp or time.time() - timestamp > max_seconds)


def _cleanup_cache_files(cache_dir: str, max_age_days: int = 7, max_total_mb: int = 2_000) -> None:
    """[NETTOYAGE CACHE] Supprime les fichiers temporaires de plus de N jours. Si le cache dépasse la taille totale autorisée, supprime les plus vieux en premier."""
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return
    cutoff = time.time() - (max_age_days * 86400)
    extensions = {".png", ".csv", ".xlsx", ".html", ".json"}
    candidates: list[tuple[Path, float]] = []
    for path in cache_path.iterdir():
        if not path.is_file():
            continue
        is_temp = path.suffix.lower() in extensions or path.name.startswith(".last_export_")
        if is_temp:
            mtime = path.stat().st_mtime
            if mtime < cutoff:
                try:
                    path.unlink()
                    logger.info("Cache nettoyé (âge): %s", path.name)
                except OSError:
                    pass
            else:
                candidates.append((path, mtime))
    # Si le cache dépasse la taille max, supprimer les plus vieux
    total_bytes = sum(p.stat().st_size for p, _ in candidates)
    max_bytes = max_total_mb * 1024 * 1024
    if total_bytes > max_bytes:
        candidates.sort(key=lambda x: x[1])
        for path, _ in candidates:
            if total_bytes <= max_bytes:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total_bytes -= size
                logger.info("Cache nettoyé (taille): %s", path.name)
            except OSError:
                pass


def _detect_new_jobs(text: str, current_query: str) -> list[str]:
    """Détecte si l'utilisateur demande un métier différent de celui déjà en cache."""
    jobs = [
        "plombier", "électricien", "electricien", "dératiseur", "deratiseur",
        "menuisier", "serrurier", "vitrier", "chauffagiste", "couvreur",
        "maçon", "macon", "peintre", "carreleur",
    ]
    found = []
    text_lower = text.lower()
    current_lower = (current_query or "").lower()
    for job in jobs:
        if job in text_lower and job not in current_lower:
            found.append(job)
    return found


async def _combine_searches(
    settings: Settings,
    cache_dir: str,
    thread_ts: str,
    current_data: dict,
    new_jobs: list[str],
) -> dict:
    """Combine les résultats du cache avec une nouvelle recherche entreprise."""
    if not current_data or not current_data.get("results"):
        return current_data

    combined = list(current_data["results"])
    for r in combined:
        r["_category"] = current_data.get("query", "résultat précédent")

    # Récupérer le département de la recherche initiale (défaut Paris 75)
    dept = current_data.get("departement", "75") or "75"

    for job in new_jobs:
        logger.info("[COMBINE] Recherche du métier '%s' (dépt %s) pour combiner avec le cache", job, dept)
        _ = await execute_local_tool(
            "search_enterprises",
            {"query": job, "departement": dept, "max_results": 25},
            settings,
            {"thread_ts": thread_ts},
        )
        new_data = load_search_results(cache_dir, thread_ts)
        if new_data and new_data.get("results"):
            for r in new_data["results"]:
                r["_category"] = job
            combined.extend(new_data["results"])

    combined_query = f"{current_data.get('query', '')} + {', '.join(new_jobs)}"
    save_search_results(
        cache_dir, combined_query, combined,
        metadata={"thread_ts": thread_ts, "timestamp": int(time.time()), "departement": dept}
    )
    save_search_results(
        cache_dir, combined_query, combined,
        metadata={"timestamp": int(time.time()), "departement": dept}
    )
    return {"query": combined_query, "results": combined, "departement": dept}


def _dm_threads_path(cache_dir: str) -> Path:
    return Path(cache_dir) / "dm_threads.json"


def _load_dm_thread(cache_dir: str, channel: str) -> dict | None:
    path = _dm_threads_path(cache_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get(channel)
    except Exception:
        return None


def _save_dm_thread(cache_dir: str, channel: str, thread_ts: str, last: float) -> None:
    path = _dm_threads_path(cache_dir)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    data[channel] = {"thread_ts": thread_ts, "last": last}
    path.write_text(json.dumps(data, ensure_ascii=False))


# Stopwords pour la comparaison sémantique entre la question utilisateur et le cache
_GRAPH_STOPWORDS = {
    "fais", "moi", "un", "une", "le", "la", "les", "des", "par", "et", "ou",
    "avec", "sans", "pour", "sur", "dans", "de", "du", "ce", "cet", "cette",
    "mon", "ton", "son", "ma", "ta", "sa", "mes", "tes", "ses", "notre",
    "votre", "leur", "nos", "vos", "leurs", "je", "tu", "il", "elle", "nous",
    "vous", "ils", "elles", "est", "sont", "a", "ont", "être", "avoir",
    "faire", "plus", "moins", "très", "peu", "trop", "assez", "bien", "mal",
    "oui", "non", "merci", "svp", "s'il", "plaît", "stp", "voici", "voilà",
    "y", "on", "n'", "ne", "pas", "ni", "car", "que", "qui",
    "quoi", "dont", "où", "quand", "comment", "pourquoi", "quel", "quelle",
    "quels", "quelles", "au", "aux", "en", "à", "vers", "sous", "chez",
    "graphique", "graphiqu", "chart", "histogramme", "camembert", "diagramme", "tableau", "tableaux",
    "stats", "statistiques", "statistique", "résumé", "chiffré", "export", "csv", "excel",
}


def _significant_words(s: str) -> set[str]:
    """Extrait les mots significatifs d'une chaîne pour comparaison sémantique."""
    return {
        w.strip(".,;:!?")
        for w in s.lower().split()
        if len(w.strip(".,;:!?")) > 3 and w.strip(".,;:!?") not in _GRAPH_STOPWORDS
    }


def create_app(settings: Settings) -> tuple[AsyncApp, AsyncSocketModeHandler]:
    app = AsyncApp(token=settings.slack_bot_token)

    @app.middleware
    async def log_all_events(body, next):
        logger.info("EVENT REÇU: %s", body.get("event", {}).get("type"))
        await next()

    orchestrator = AgentOrchestrator(settings)
    state: dict[str, str] = {}  # bot_user_id, rempli au démarrage

    async def _handle(event: dict, client, say, is_dm: bool = False) -> None:
        # [MODE RÉFLEXION] Les circuits rapides (stats, graphique, carte, export)
        # sont DÉSACTIVÉS pour forcer le passage par le LLM à chaque fois.
        # Le LLM doit réfléchir et décider lui-même des actions à entreprendre.
        DISABLE_AUTO_CIRCUITS = True

        if not event.get("channel") or not event.get("ts"):
            logger.warning("Event Slack malformé, ignoré: %s", event)
            return
        channel = event["channel"]
        ts = event["ts"]
        if is_dm:
            # En DM, on maintient un thread continu par canal pour structurer la conversation
            now = time.time()
            dm_data = _load_dm_thread(settings.cache_dir, channel)
            last = dm_data.get("last", 0) if dm_data else 0
            # Reset le thread après 30 min d'inactivité
            if now - last > 1800:
                dm_data = None
            # Premier DM jamais reçu de cet utilisateur → welcome
            user_id = event.get("user", "")
            if dm_data is None and user_id and not has_been_welcomed(settings.cache_dir, user_id):
                try:
                    await send_welcome(client, channel, user_id)
                    mark_welcomed(settings.cache_dir, user_id)
                except Exception:
                    logger.exception("Échec envoi welcome en DM")
            if event.get("thread_ts"):
                # L'utilisateur a répondu explicitement dans un thread existant
                slack_thread_ts = event["thread_ts"]
            else:
                # Message principal en DM : on répond directement sous ce message
                # pour que la réponse soit visible dans le flux principal.
                # Le contexte conversationnel est conservé via la mémoire SQLite
                # (conversation_id = channel), pas via le thread Slack.
                slack_thread_ts = ts
                _save_dm_thread(settings.cache_dir, channel, ts, now)
                dm_data = {"thread_ts": ts, "last": now}
            # Met à jour le dernier accès
            if dm_data:
                _save_dm_thread(settings.cache_dir, channel, dm_data["thread_ts"], now)
            conversation_id = channel
            thread_ts = event.get("thread_ts") or ts
        else:
            thread_ts = event.get("thread_ts") or ts
            conversation_id = thread_ts
            slack_thread_ts = thread_ts
        raw_text = event.get("text") or ""
        # Limiter la taille du message pour protéger le contexte LLM
        MAX_USER_TEXT = 10_000
        if len(raw_text) > MAX_USER_TEXT:
            raw_text = raw_text[:MAX_USER_TEXT] + "\n... [message tronqué par le bot, trop long] ..."
        text = MENTION_PATTERN.sub("", raw_text).strip()

        # Injection forcée des IDs datasets détectés dans les liens
        # pour empêcher le LLM de faire une recherche générique à côté
        dataset_ids = _extract_dataset_ids(text)
        if dataset_ids:
            ids_str = ", ".join(dataset_ids)
            text = f"{text}\n\n[IDs datasets détectés dans les liens : {ids_str}]"

        if text.lower() in ("aide", "help", "menu"):
            await say(text=BOT_HELP_TEXT, thread_ts=slack_thread_ts)
            return

        # [EXPORT EXCEL] Détection directe d'export CSV/Excel (uniquement si recherche récente dans ce thread)
        if not DISABLE_AUTO_CIRCUITS and any(kw in text.lower() for kw in ("exporte", "export", "csv", "excel", "fichier")):
            from app.data.exporter import (
                enrich_results_with_tavily,
                export_to_csv,
                export_to_excel,
            )

            data = load_search_results(settings.cache_dir, conversation_id)
            if _is_recent_search(data, conversation_id):
                fmt = "excel" if "excel" in text.lower() else "csv"
                enriched = any(kw in text.lower() for kw in (
                    "complémentaire", "téléphone", "tel", "email", "mail",
                    "dirigeant", "tout", "complet", "contact",
                ))

                try:
                    results = data["results"]
                    if enriched and settings.tavily_api_key:
                        await say(
                            text="🔍 Je recherche les téléphones, emails et dirigeants... ça peut prendre 20-30 secondes.",
                            thread_ts=slack_thread_ts,
                        )
                        results = await enrich_results_with_tavily(
                            results, settings.tavily_api_key
                        )
                        save_search_results(
                            settings.cache_dir, data["query"], results,
                            metadata={"thread_ts": conversation_id, "timestamp": int(time.time())}
                        )

                    if fmt == "excel":
                        path = export_to_excel(settings.cache_dir, thread_ts=conversation_id)
                    else:
                        path = export_to_csv(settings.cache_dir, thread_ts=conversation_id)

                    await say(text=f"📊 Export {fmt.upper()} prêt !", thread_ts=slack_thread_ts)
                    try:
                        await client.files_upload_v2(
                            channel=channel,
                            thread_ts=slack_thread_ts,
                            file=str(path),
                            title=f"Résultats exportés.{fmt}",
                            initial_comment=f"Voici l'export {fmt.upper()} avec {len(results)} lignes {'(enrichi)' if enriched else ''} 📎",
                        )
                    except Exception:
                        logger.exception("Échec upload fichier export")
                        await say(text="❌ L'export a été généré mais je n'ai pas pu l'envoyer sur Slack.", thread_ts=slack_thread_ts)
                    return
                except Exception as exc:
                    logger.exception("Export échoué")
                    await say(text=f"❌ Désolé, l'export a échoué : {exc}", thread_ts=slack_thread_ts)
                    return

        # Détection directe de stats texte (uniquement si recherche récente dans ce thread)
        if not DISABLE_AUTO_CIRCUITS and any(kw in text.lower() for kw in ("stats", "statistiques", "statistique", "résumé chiffré")):
            data = load_search_results(settings.cache_dir, conversation_id)
            if _is_recent_search(data, conversation_id):
                # [GARDE SÉMANTIQUE] Vérifier que le sujet demandé correspond au cache
                cached_query = (data.get("query") or "").lower()
                msg_words = _significant_words(text)
                query_words = _significant_words(cached_query)
                if msg_words and query_words and not (msg_words & query_words):
                    logger.info(
                        "Sujet stats (%s) différent du cache (%s), passage par orchestrateur",
                        text,
                        cached_query,
                    )
                else:
                    try:
                        results = data["results"]
                        safe_query = _escape_slack_mrkdwn(data.get('query', 'la recherche'))
                        lines = [f"📊 Stats pour *{safe_query}* — {len(results)} résultat(s)"]

                        # Répartition par ville
                        villes: dict[str, int] = {}
                        nafs: dict[str, int] = {}
                        effectifs: list[int] = []
                        for r in results:
                            v = r.get("ville") or "Inconnue"
                            villes[v] = villes.get(v, 0) + 1
                            n = r.get("activite_naf") or "Inconnu"
                            nafs[n] = nafs.get(n, 0) + 1
                            eff = r.get("effectif", "")
                            if isinstance(eff, int) or (isinstance(eff, str) and eff.isdigit()):
                                effectifs.append(int(eff))

                        if len(villes) > 1:
                            lines.append("\n*Top villes* :")
                            for v, c in sorted(villes.items(), key=lambda x: x[1], reverse=True)[:5]:
                                lines.append(f"• {_escape_slack_mrkdwn(v)} : {c}")

                        if len(nafs) > 1:
                            lines.append("\n*Top codes NAF* :")
                            for n, c in sorted(nafs.items(), key=lambda x: x[1], reverse=True)[:5]:
                                lines.append(f"• {_escape_slack_mrkdwn(n)} : {c}")

                        if effectifs:
                            lines.append(
                                f"\n*Effectif* — min: {min(effectifs)}, max: {max(effectifs)}, "
                                f"moy: {sum(effectifs) // len(effectifs)}"
                            )

                        await say(text="\n".join(lines), thread_ts=slack_thread_ts)
                        return
                    except Exception as exc:
                        logger.exception("Stats échouées")
                        await say(text=f"❌ Désolé, les stats n'ont pas pu être générées : {exc}", thread_ts=slack_thread_ts)
                        return

        # [GRAPHIQUE] Détection directe de graphique (uniquement si recherche récente dans ce thread)
        graph_kw = ("graphique", "graphiqu", "chart", "histogramme", "camembert", "diagramme", "tableau", "tableaux")
        has_graph_kw = any(kw in text.lower() for kw in graph_kw)
        if not DISABLE_AUTO_CIRCUITS and has_graph_kw:
            data = load_search_results(settings.cache_dir, conversation_id)
            if _is_recent_search(data, conversation_id):
                # Vérifier que le sujet demandé correspond au sujet en cache
                # pour éviter de générer un graphique sur de vieilles données
                # quand l'utilisateur demande un nouveau sujet dans le même thread
                cached_query = (data.get("query") or "").lower()
                msg_words = _significant_words(text)
                query_words = _significant_words(cached_query)
                if msg_words and query_words and not (msg_words & query_words):
                    logger.info(
                        "Sujet graphique (%s) différent du cache (%s), passage par orchestrateur",
                        text,
                        cached_query,
                    )
                    # Ne pas court-circuiter : laisser l'orchestrateur gérer la nouvelle recherche
                    pass
                elif not msg_words:
                    # L'utilisateur n'a pas précisé de sujet (ex: "fais un graphique").
                    # Si des données AO structurées sont en cache, générer un graphique ASCII directement.
                    cache_results = data.get("results", [])
                    has_ao_data = cache_results and any("idweb" in str(r.keys()) for r in cache_results[:1])
                    logger.info("Graphique sans sujet — cache_results=%s has_ao_data=%s", len(cache_results), has_ao_data)
                    if has_ao_data:
                        ascii_chart = _generate_ascii_chart(
                            cache_results, data.get("query", "Répartition")
                        )
                        if ascii_chart:
                            await say(text=ascii_chart, thread_ts=slack_thread_ts)
                            return
                    # Sinon, laisser l'orchestrateur gérer (il n'a pas de données structurées à visualiser)
                    logger.info(
                        "Sujet graphique non précisé (%s), passage par orchestrateur",
                        text,
                    )
                    pass
                else:
                    try:
                        results = data["results"]
                        chart_type = "pie" if "camembert" in text.lower() or "pie" in text.lower() else "bar"

                        # Détecter si l'utilisateur a précisé une colonne
                        explicit_col = None
                        for col in ["activite_naf", "naf", "ville", "rue", "code_postal", "departement"]:
                            if col.replace("_", " ") in text.lower() or col in text.lower():
                                explicit_col = col
                                break

                        # Si explicit_col demandée, faire un graphique catégoriel
                        if explicit_col and any(explicit_col in str(r.keys()).lower() for r in results[:1]):
                            actual_key = next(
                                (k for k in results[0] if explicit_col in k.lower()),
                                explicit_col
                            )
                            path = generate_categorical_chart(
                                results, actual_key,
                                title=f"Répartition par {actual_key}",
                                cache_dir=settings.cache_dir,
                            )
                        else:
                            # Sinon, essayer de trouver une colonne numérique intéressante
                            numeric_cols = ["effectif", "effectif_salarie", "salaries", "nb_employes"]
                            value_key = None
                            for col in numeric_cols:
                                matching_keys = [k for k in results[0] if col in k.lower()]
                                if matching_keys:
                                    non_zero = sum(
                                        1 for r in results
                                        if _try_float(r.get(matching_keys[0])) > 0
                                    )
                                    if non_zero >= max(3, int(len(results) * 0.3)):
                                        value_key = matching_keys[0]
                                        break

                            if value_key and chart_type == "pie":
                                path = generate_pie_chart(
                                    results, "nom", value_key,
                                    title=data.get("query", "Graphique"),
                                    cache_dir=settings.cache_dir,
                                )
                            elif value_key:
                                path = generate_bar_chart(
                                    results, "nom", value_key,
                                    title=data.get("query", "Graphique"),
                                    cache_dir=settings.cache_dir,
                                )
                            else:
                                # Fallback : graphique catégoriel par NAF ou par ville
                                cat_key = None
                                for k in results[0]:
                                    if "naf" in k.lower() or "activite" in k.lower():
                                        cat_key = k
                                        break
                                if not cat_key:
                                    cat_key = next((k for k in results[0] if "ville" in k.lower()), None)
                                if not cat_key:
                                    cat_key = list(results[0].keys())[0]

                                path = generate_categorical_chart(
                                    results, cat_key,
                                    title=f"Répartition par {cat_key}",
                                    cache_dir=settings.cache_dir,
                                )

                        await say(text="📊 Graphique généré !", thread_ts=slack_thread_ts)
                        try:
                            await client.files_upload_v2(
                                channel=channel,
                                thread_ts=slack_thread_ts,
                                file=str(path),
                                title="Graphique",
                                initial_comment="Voici le graphique demandé 📊",
                            )
                        except Exception:
                            logger.exception("Échec upload graphique")
                            await say(text="❌ Le graphique a été généré mais je n'ai pas pu l'envoyer sur Slack.", thread_ts=slack_thread_ts)
                        return
                    except Exception as exc:
                        logger.exception("Génération graphique échouée")
                        await say(text=f"❌ Désolé, le graphique n'a pas pu être généré : {exc}", thread_ts=slack_thread_ts)
                        return

        # [CARTE] Détection directe de carte / map (uniquement si recherche récente dans ce thread)
        # Court-circuite si le message est court (ex: "fais-moi une carte")
        # OU si le message mentionne un nouveau métier à combiner avec le cache existant.
        map_kw = ("carte", "map", "cartographie", "géographique", "géolocalisation", "localisation")
        has_map_kw = any(kw in text.lower() for kw in map_kw)
        # Détecter si l'utilisateur demande explicitement d'ajouter/coupler un nouveau métier
        combine_kw = ("couple", "ajoute", "ajouter", "mets", "avec", "plus", "et")
        has_combine_kw = any(kw in text.lower() for kw in combine_kw)
        is_short_map_request = has_map_kw and len(text) < 35
        is_map_combine_request = has_map_kw and has_combine_kw
        if not DISABLE_AUTO_CIRCUITS and (is_short_map_request or is_map_combine_request):
            data = load_search_results(settings.cache_dir, conversation_id)
            results = []
            if _is_recent_search(data, conversation_id):
                results = data.get("results", [])

            # [CACHE GLOBAL FALLBACK] Si le thread est vide, chercher dans le cache global récent
            if not results:
                global_data = load_search_results(settings.cache_dir, "")
                if global_data and global_data.get("results"):
                    ts = global_data.get("timestamp", 0)
                    if ts and (time.time() - ts) < 600:
                        results = global_data["results"]

            # [EXTRACTION GÉO FALLBACK] Si pas de données structurées, parser l'historique
            if not results:
                past = get_thread_memory(settings.cache_dir, channel, conversation_id)
                assistant_texts = [m["content"] for m in past if m.get("role") == "assistant"]
                results = _extract_geo_points_from_history(assistant_texts)

            # [COMBINE] Si l'utilisateur demande un nouveau métier, chercher et fusionner
            if data and results:
                new_jobs = _detect_new_jobs(text, data.get("query", ""))
                if new_jobs:
                    data = await _combine_searches(
                        settings, settings.cache_dir, conversation_id, data, new_jobs
                    )
                    results = data.get("results", [])

            if results:
                lat_candidates = ["latitude", "lat", "y", "geo_point_2d_lat", "coordonnees_lat"]
                lon_candidates = ["longitude", "lon", "x", "geo_point_2d_lon", "coordonnees_lon"]
                lat_key = next((k for k in results[0] if any(c in k.lower() for c in lat_candidates)), None)
                lon_key = next((k for k in results[0] if any(c in k.lower() for c in lon_candidates)), None)
                # Fallback : si une clé contient un dict avec lat/lon (ex: geo_point_2d)
                if not lat_key or not lon_key:
                    for k, v in results[0].items():
                        if isinstance(v, dict) and "lat" in v and "lon" in v:
                            for r in results:
                                if isinstance(r.get(k), dict):
                                    r["lat_flat"] = r[k].get("lat")
                                    r["lon_flat"] = r[k].get("lon")
                            lat_key = "lat_flat"
                            lon_key = "lon_flat"
                            break
                if lat_key and lon_key:
                    # Par défaut on génère une carte HTML interactive ; PNG seulement sur demande explicite
                    is_static = any(kw in text.lower() for kw in ("png", "image", "screenshot", "photo", "jpg"))
                    # Si les résultats ont des catégories (_category), on les utilise pour colorer par métier
                    cat_key = "_category" if results and any("_category" in r for r in results) else None
                    try:
                        if is_static:
                            path = generate_scatter_map(
                                results, lat_key=lat_key, lon_key=lon_key,
                                label_key="nom" if "nom" in results[0] else None,
                                title=data.get("query", "Carte"),
                                cache_dir=settings.cache_dir,
                            )
                            await client.files_upload_v2(
                                channel=channel,
                                thread_ts=slack_thread_ts,
                                file=str(path),
                                title="Carte",
                                initial_comment="🗺️ Carte générée.",
                            )
                        else:
                            path = generate_interactive_map(
                                results, lat_key=lat_key, lon_key=lon_key,
                                label_key="nom" if "nom" in results[0] else None,
                                category_key=cat_key,
                                title=data.get("query", "Carte interactive"),
                                cache_dir=settings.cache_dir,
                            )
                            await client.files_upload_v2(
                                channel=channel,
                                thread_ts=slack_thread_ts,
                                file=str(path),
                                title="Carte interactive",
                                initial_comment="📎 Carte interactive (HTML/Leaflet) — télécharge-la et ouvre-la dans ton navigateur.",
                            )
                        return
                    except Exception as exc:
                        logger.exception("Génération carte échouée")
                        await say(text=f"❌ Désolé, la carte n'a pas pu être générée : {exc}", thread_ts=slack_thread_ts)
                        return

        raw_files = event.get("files", [])
        files = raw_files[:5] if isinstance(raw_files, list) else []
        if files:
            try:
                file_summary = await analyze_slack_files(files, settings.slack_bot_token)
                if file_summary:
                    text = f"{text}\n\nFichiers joints :\n{file_summary}".strip()
            except Exception:
                logger.exception("Échec analyse des fichiers joints, on continue sans")

        # [TRAME MÉMOIRE TECHNIQUE] Détection dans un thread d'AO pushé
        trame_kw = ("trame", "mémoire technique", "memoire technique", "squelette", "comment répondre", "comment postuler")
        if any(kw in text.lower() for kw in trame_kw):
            ao_data = get_ao_from_message(settings.cache_dir, channel, slack_thread_ts)
            if not ao_data and event.get("thread_ts"):
                ao_data = get_ao_from_message(settings.cache_dir, channel, event["thread_ts"])
            if ao_data:
                profile = get_profile(settings.cache_dir, user_id) or {
                    "activity": "", "company_size": "", "keywords": [],
                    "sectors": [], "zones": [], "min_amount": 0, "max_amount": 0,
                }
                with contextlib.suppress(Exception):
                    await client.reactions_add(channel=channel, timestamp=ts, name="memo")
                try:
                    llm = LLMClient(settings)
                    ao = {
                        "idweb": ao_data.get("ao_id", ""),
                        "objet": ao_data.get("objet", ""),
                        "org": "", "departements": [], "descripteurs": [],
                    }
                    skeleton = await generate_response_skeleton(ao, profile, llm)
                    await say(text=skeleton, thread_ts=slack_thread_ts)
                    with contextlib.suppress(Exception):
                        await client.reactions_remove(channel=channel, timestamp=ts, name="memo")
                    return
                except Exception:
                    logger.exception("Erreur génération trame")
                    await say(text="❌ Désolé, la génération de la trame a échoué.", thread_ts=slack_thread_ts)
                    return

        # [RAG DCE] Questions sur le cahier des charges dans un thread d'AO
        dce_q_kw = (
            "critère", "critères", "notation", "pénalité", "pénalités",
            "garantie", "clause", "cahier des charges", "cdc", "règlement",
            "modalité", "modalités", "délai", "delai", "technique", "financier",
            "offre", "soumission", "visite", "chantier", "mémoire technique",
            "dce", "consultation", "référence", "reference", "expérience requise",
            "experience requise", "qualification", "agrément", "agrement",
            "certification", "dossier", "candidature", "soumissionnaire",
            "attestation", "déclaration", "declaration", "engagement",
        )
        if any(kw in text.lower() for kw in dce_q_kw):
            ao_data = get_ao_from_message(settings.cache_dir, channel, slack_thread_ts)
            if not ao_data and event.get("thread_ts"):
                ao_data = get_ao_from_message(settings.cache_dir, channel, event["thread_ts"])
            if ao_data:
                ao_id = ao_data.get("ao_id", "")
                if ao_id:
                    if is_dce_indexed(settings.cache_dir, ao_id):
                        with contextlib.suppress(Exception):
                            await client.reactions_add(channel=channel, timestamp=ts, name="mag")
                        try:
                            answer = await query_dce_rag(ao_id, text, LLMClient(settings), settings.cache_dir)
                            if answer:
                                await say(text=answer, thread_ts=slack_thread_ts)
                            else:
                                await say(
                                    text="Je n'ai pas trouvé de réponse dans le DCE indexé.",
                                    thread_ts=slack_thread_ts,
                                )
                        except Exception:
                            logger.exception("Erreur RAG DCE")
                            await say(
                                text="❌ Désolé, l'interrogation du DCE a échoué.",
                                thread_ts=slack_thread_ts,
                            )
                        return
                    else:
                        await say(
                            text=(
                                f"📄 Ce DCE n'est pas encore indexé. "
                                f"Envoie `/datagouv ao dce {ao_id} [URL_DCE]` pour l'indexer."
                            ),
                            thread_ts=slack_thread_ts,
                        )
                        return

        # [OUI IMPLICITE] Si l'utilisateur répond "oui" / "ok" / "go" sans autre contexte,
        # et que le dernier message du bot proposait une recherche approfondie ou une carte,
        # on traite directement sans passer par le LLM pour éviter de le surcharger.
        text_lower_stripped = text.lower().strip().rstrip(".!?:")
        if text_lower_stripped in ("oui", "ok", "go", "yes", "d'accord", "vas-y", "tous", "tout"):
            try:
                past_mem = get_thread_memory(settings.cache_dir, channel, conversation_id, limit=5)
                assistant_msgs = [m for m in past_mem if m.get("role") == "assistant"]
                if assistant_msgs:
                    last_assistant = assistant_msgs[-1].get("content", "")
                    # --- COURT-CIRCUIT RECHERCHE APPROFONDIE ---
                    if any(kw in last_assistant.lower() for kw in ("recherche approfondie", "enrichis", "enrichir", "dirigeants", "téléphones", "emails", "[résultat brut")):
                        # [FIX ROBUSTESSE] On charge les résultats MÊME SI enrich_state est manquant.
                        # search_enterprises sauvegarde en global, donc on tente d'abord le thread
                        # puis le fallback global.
                        target_siren = _extract_single_siren(last_assistant)
                        search_data = load_search_results(settings.cache_dir, conversation_id)
                        # [VALIDATION TYPE] Le cache thread peut contenir des AO/dataset anciens.
                        # Si aucun résultat n'a de champ siren, ce n'est pas une recherche entreprise.
                        has_siren = search_data and any(r.get("siren") for r in search_data.get("results", []))
                        if not has_siren:
                            search_data = load_search_results(settings.cache_dir, "")
                        # [VALIDATION CIBLAGE] Si le SIREN du dernier message n'est pas dans ce cache,
                        # c'est une recherche entreprise obsolète (ex: Orange puis Astek) → fallback global.
                        if search_data and search_data.get("results") and target_siren and not any(
                            r.get("siren", "").replace(" ", "") == target_siren for r in search_data["results"]
                        ):
                            search_data = load_search_results(settings.cache_dir, "")
                        if not search_data or not search_data.get("results"):
                            search_data = load_search_results(settings.cache_dir, "")
                        if search_data and search_data.get("results"):
                            with contextlib.suppress(Exception):
                                await client.reactions_add(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
                            enrich_state = load_enrich_state(settings.cache_dir, conversation_id)
                            offset = enrich_state.get("next_offset", 0) if enrich_state else 0
                            limit = 20
                            try:
                                # [FILTRE ENTITÉ UNIQUE] Si le dernier message ne présentait
                                # qu'une seule entreprise (ex: SIRENE ciblé), on n'enrichit
                                # que celle-là au lieu de tout le lot brut.
                                enrich_results = search_data["results"]
                                if target_siren:
                                    filtered = [
                                        r for r in search_data["results"]
                                        if r.get("siren", "").replace(" ", "") == target_siren
                                    ]
                                    if filtered:
                                        enrich_results = filtered
                                result_text = await _deep_enrich(
                                    enrich_results,
                                    search_data.get("query", ""),
                                    settings,
                                    offset=offset,
                                    limit=limit,
                                )
                                new_offset = min(limit, len(enrich_results))
                                save_enrich_state(
                                    settings.cache_dir, conversation_id,
                                    search_data.get("query", ""),
                                    next_offset=new_offset,
                                    total=len(enrich_results),
                                )
                                # Persister les enrichissements pour que la carte / export les voient
                                save_search_results(
                                    settings.cache_dir,
                                    search_data.get("query", ""),
                                    enrich_results,
                                    metadata={"timestamp": int(time.time()), "thread_ts": conversation_id, "departement": ""},
                                )
                                await say(text=result_text, thread_ts=slack_thread_ts)
                                with contextlib.suppress(Exception):
                                    await client.reactions_remove(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
                                return
                            except Exception:
                                logger.exception("Échec _deep_enrich")
                                await say(
                                    text="❌ L'enrichissement a échoué. Réessaie ou demande-moi directement les coordonnées.",
                                    thread_ts=slack_thread_ts,
                                )
                                with contextlib.suppress(Exception):
                                    await client.reactions_remove(channel=channel, timestamp=ts, name="hourglass_flowing_sand")
                                return
                        # Si on a les mots-clés mais aucune donnée en cache, on prévient
                        # l'utilisateur plutôt que de polluer le LLM avec un historier long.
                        await say(
                            text="❌ Je n'ai pas trouvé les résultats à enrichir en mémoire. "
                                 "Recommence la recherche entreprise, puis demande l'enrichissement.",
                            thread_ts=slack_thread_ts,
                        )
                        return
                    elif any(kw in last_assistant.lower() for kw in ("carte", "générer une carte", "visualiser")):
                        text = "Oui, génère la carte interactive avec les résultats précédents."
                        logger.info("[OUI IMPLICITE] transformé en demande de carte")
            except Exception:
                logger.exception("Erreur détection oui implicite")
                await say(
                    text="❌ Je n'ai pas pu traiter ta confirmation. Réessaie ou reformule ta demande.",
                    thread_ts=slack_thread_ts,
                )
                return

        safe_text = _sanitize_user_input(text)

        # --- Tracker le dataset principal par thread ---
        # RÈGLE : dans un DM, le dataset principal ne vient QUE du message actuel.
        # On ne récupère jamais un dataset d'une conversation précédente dans un DM
        # car conversation_id = channel (tous les messages partagent le même ID).
        principal_dataset_id: str | None = None
        if dataset_ids:
            principal_dataset_id = dataset_ids[0]
            thread_principal_dataset = state.setdefault("thread_principal_dataset", {})
            thread_principal_dataset[conversation_id] = principal_dataset_id
            # [MEMOIRE] Limiter la taille du dict pour éviter une fuite mémoire (LRU max 500)
            if len(thread_principal_dataset) > 500:
                oldest_key = next(iter(thread_principal_dataset))
                thread_principal_dataset.pop(oldest_key)
        elif not is_dm:
            # Channels / threads : récupération depuis le state ou la mémoire SQLite
            principal_dataset_id = state.get("thread_principal_dataset", {}).get(conversation_id)
            if not principal_dataset_id:
                try:
                    past_mem = get_thread_memory(settings.cache_dir, channel, conversation_id, limit=20)
                    for m in past_mem:
                        ids = _extract_dataset_ids(m.get("content", ""))
                        if ids:
                            principal_dataset_id = ids[0]
                            thread_principal_dataset = state.setdefault("thread_principal_dataset", {})
                            thread_principal_dataset[conversation_id] = principal_dataset_id
                            # [MEMOIRE] Limiter la taille du dict pour éviter une fuite mémoire (LRU max 500)
                            if len(thread_principal_dataset) > 500:
                                oldest_key = next(iter(thread_principal_dataset))
                                thread_principal_dataset.pop(oldest_key)
                            break
                except Exception:
                    logger.exception("Erreur recherche dataset principal dans mémoire thread")

        if principal_dataset_id:
            safe_text = f"[DATASET_PRINCIPAL: {principal_dataset_id}]\n\n{safe_text}"

        # Réaction pour signaler la prise en compte
        with contextlib.suppress(Exception):
            await client.reactions_add(channel=channel, timestamp=ts, name="hourglass_flowing_sand")

        # Construire la conversation Slack d'abord
        conversation = await build_conversation(
            client, channel, thread_ts, state.get("bot_user_id", ""), safe_text, ts, is_dm=is_dm
        )

        # Injection mémoire à long terme (par thread, pas par utilisateur)
        user_id = event.get("user", "unknown")
        past = get_thread_memory(settings.cache_dir, channel, conversation_id, limit=10)

        # [DÉTECTION APPELS D'OFFRE] Forcer l'utilisation du tool local fetch_boamp_tenders
        tender_kw = (
            "appel d'offre", "appels d'offre", "marché public", "marches publics",
            "marchés publics", "consultation ouverte", "ao en", "ao ia", "ao sur",
            "consultation ", "marchés pub", "appels d'offre",
        )
        historical_kw = (
            "attribué", "attribuée", "attribués", "attribuées", "attribution",
            "notifié", "notifiée", "notifiés", "notifiées", "notification",
            "passé", "passée", "passés", "passées", "historique", "historiques",
            "déjà", "deja", "attributaire", "titulaire", "titulaires",
        )
        tender_context_kw = ("prix", "montant", "budget", "coût", "euros", "€")
        is_tender_request = any(kw in safe_text.lower() for kw in tender_kw)
        is_historical_request = any(kw in safe_text.lower() for kw in historical_kw)
        # Si le message est un suivi court sur prix/montant dans un thread avec AO
        if not is_tender_request and any(kw in safe_text.lower() for kw in tender_context_kw):
            # Vérifier si l'historique du thread mentionne des appels d'offre
            for msg in conversation:
                if any(kw in (msg.get("content") or "").lower() for kw in tender_kw):
                    is_tender_request = True
                    break
        if is_tender_request and not is_historical_request:
            # [PRIORITÉ AO] Les appels d'offre priment sur tout dataset principal :
            # on retire le tag DATASET_PRINCIPAL pour éviter que le LLM soit distrait
            # par l'analyse d'un dataset avant de répondre sur les AO.
            safe_text = safe_text.replace(f"[DATASET_PRINCIPAL: {principal_dataset_id}]\n\n", "")
            safe_text = (
                "[CONSIGNE SYSTÈME ABSOLUE — PRIORITÉ MAXIMALE] L'utilisateur demande des appels d'offre, "
                "marchés publics, consultations, ou des détails sur leurs montants/prix. "
                "Tu DOIS OBLIGATOIREMENT appeler le tool local 'fetch_boamp_tenders' "
                "AVANT de répondre. Ne réponds JAMAIS de mémoire. N'utilise JAMAIS "
                "'search_datasets', 'web_search', 'query_resource_data' ni 'search_public_tenders' "
                "pour cette demande. Recherche les vraies annonces sur le BOAMP. "
                "Le tool 'fetch_boamp_tenders' inclut DÉJÀ automatiquement les synonymes "
                "courants pour les domaines techniques (cybersécurité, IA, cloud, etc.). "
                "Ne relance PAS cet outil avec 'related_terms' sauf si l'utilisateur demande "
                "explicitement un terme très spécifique non couvert. "
                "CRITIQUE : les résultats de fetch_boamp_tenders sont déjà formatés en tableau markdown. "
                "Tu DOIS les présenter TELS QUELS sans les reformater, sans ajouter de sections, "
                "sans supprimer de colonnes et sans déplacer les liens. Recopie le tableau exactement. "
                "Si la question porte sur un classement ou des stats sur les appels d'offre "
                "publiés, agrège toi-même les résultats BOAMP. N'appelle PAS "
                "'query_notified_contracts' pour les appels d'offre en cours.\n\n" + safe_text
            )
            if conversation:
                conversation[-1]["content"] = safe_text
                # Retirer aussi le tag des messages précédents de la conversation
                for msg in conversation:
                    if "[DATASET_PRINCIPAL:" in (msg.get("content") or ""):
                        msg["content"] = re.sub(r"\[DATASET_PRINCIPAL: [^\]]+\]\n\n", "", msg["content"])
                        msg["content"] = re.sub(r"\[DATASET_PRINCIPAL: [^\]]+\]\n", "", msg["content"])
                        msg["content"] = re.sub(r"\[DATASET_PRINCIPAL: [^\]]+\]", "", msg["content"])

        if is_historical_request:
            year_match = re.search(r"\b(20\d{2})\b", safe_text)
            year = year_match.group(1) if year_match else "2024"
            safe_text = (
                f"[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande des marchés publics "
                f"déjà attribués, notifiés, ou historiques. Tu DOIS OBLIGATOIREMENT appeler "
                f"le tool local 'query_notified_contracts' avec year='{year}' et les autres "
                f"paramètres pertinents extraits de la question (region, departement, pme_only). "
                f"N'utilise JAMAIS 'get_dataset_info', 'search_datasets', "
                f"'list_dataset_resources' ou 'query_resource_data' pour cette demande. "
                f"Appelle DIRECTEMENT 'query_notified_contracts'.\n\n" + safe_text
            )
            if conversation:
                conversation[-1]["content"] = safe_text

        # [DÉTECTION ENTREPRISE SPÉCIFIQUE] Forcer web_search + search_enterprises filtré
        company_specific_kw = (
            "infos sirene de", "infos sirene d'", "sirene de", "sirene d'",
            "donne-moi les infos de", "donne-moi les infos d'", "infos de", "infos d'",
            "fiche entreprise", "fiche de", "recherche entreprise", "recherche de",
            "qui est", "donne moi", "parle-moi de", "parle moi de",
        )
        is_listing_request = any(kw in safe_text.lower() for kw in (
            "trouver des", "liste de", "tous les", "toutes les", "chercher des",
            "entreprises à", "entreprises de", "sociétés de", "artisans",
        ))
        is_company_specific = any(kw in safe_text.lower() for kw in company_specific_kw) and not is_listing_request
        if is_company_specific and not is_tender_request and not is_historical_request:
            safe_text = (
                "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande les informations d'UNE "
                "entreprise précise par son nom. Tu ne dois PAS te contenter de 'search_enterprises' "
                "qui retournerait tous les homonymes. Stratégie obligatoire : "
                "1) Faire 'web_search' pour trouver le site officiel, le SIREN, le siège social, "
                "le secteur d'activité ou tout lien avec Iguane/IG1. "
                "2) Puis appeler 'search_enterprises' avec les filtres discriminants trouvés "
                "(département, code postal, NAF). "
                "3) Si 'search_enterprises' retourne PLUSIEURS entités avec le même nom, "
                "tu DOIS analyser chaque entité et éliminer celles clairement non pertinentes "
                "(holdings, filiales financières, activités sans lien avec la demande). "
                "Tu ne dois présenter qu'UNE SEULE entreprise dans ta réponse finale. "
                "Si l'ambiguïté persiste entre 2 entités légitimes après analyse, "
                "présente ta meilleure hypothèse avec justification et pose UNE question de clarification. "
                "Ne présente JAMAIS une liste brute de 10+ homonymes sans analyse. "
                "Ne présente JAMAIS un tableau comparatif de plusieurs entités. "
                "FORMAT OBLIGATOIRE de ta réponse — tu dois respecter cette structure EXACTE : "
                "1) Commence par un tableau markdown avec DEUX colonnes (Champ | Valeur) contenant obligatoirement : "
                "   SIREN, SIRET siège, Dénomination, Adresse, Code NAF, Effectif, Dirigeant, Téléphone. "
                "   NE OMIT JAMAIS le SIREN et le SIRET siège — ils sont dans les résultats du tool. "
                "2) Puis une phrase expliquant pourquoi les autres entités ont été écartées. "
                "3) Termine TOUJOURS par : 'C'est bien cette entité que vous cherchiez ? Je peux vous donner les emails et détails si vous confirmez.'\n\n" + safe_text
            )
            if conversation:
                conversation[-1]["content"] = safe_text

        # [DÉTECTION DVF] Forcer l'utilisation des tools locaux DVF
        dvf_kw = (
            "prix au m²", "prix au m2", "prix m²", "prix m2",
            "transaction immobilière", "transactions immobilières",
            "vente immobilière", "ventes immobilières",
            "valeur foncière", "valeurs foncières", "dvf",
            "demandes de valeurs foncières", "prix immobilier",
            "nombre de ventes", "nombre de transactions",
            "combien de transactions", "combien de ventes",
            "évolution des prix", "évolution du prix", "tendance",
            "sur 5 ans", "sur 10 ans", "sur 3 ans", "sur plusieurs années",
            "prix des maisons", "prix des appartements",
            "marché immobilier", "immobilier en ", "immobilier à ",
            # [EXTENSION] Capturer les requêtes sur les ventes d'appartements/maisons
            "dernières ventes", "derniere vente", "derniers achats", "dernier achat",
            "ventes d'appartements", "vente d'appartement", "vente d'appartements",
            "ventes d'appartement", "ventes de maisons", "vente de maison",
            "achat immobilier", "achat appartement", "achat maison",
            "achat d'appartement", "achat de maison", "achat d'appartements",
            "prix appartement", "prix maison", "prix appartements", "prix maisons",
            "appartements à ", "appartement à ", "maisons à ", "maison à ",
            "appartements dans ", "appartement dans ", "maisons dans ", "maison dans ",
        )
        is_dvf_request = any(kw in safe_text.lower() for kw in dvf_kw)

        # Détection spécifique requête TREND (plusieurs années / région)
        dvf_trend_kw = ("évolution", "tendance", "sur 5 ans", "sur 10 ans", "sur 3 ans", "sur plusieurs années", "comparer")
        is_dvf_trend = is_dvf_request and any(kw in safe_text.lower() for kw in dvf_trend_kw)

        # [FOLLOW-UP DVF] Si le message est court et qu'un contexte DVF existe en cache,
        # forcer le traitement DVF avec les paramètres géographiques précédents.
        dvf_context_path = Path(settings.cache_dir) / f"dvf_context_{conversation_id}.json"
        dvf_context = None
        is_dvf_followup = False
        if not is_dvf_request and await asyncio.to_thread(dvf_context_path.exists):
            try:

                def _load_ctx(path):
                    with open(path) as f:
                        return json.load(f)

                dvf_context = await asyncio.to_thread(_load_ctx, dvf_context_path)
            except Exception:
                dvf_context = None
            # [GARDE-FOU] Ne pas forcer le follow-up DVF si le message pourrait
            # être une demande sur un autre topic (graphique, SIRENE, AO, export, etc.)
            followup_exclude_kw = (
                "pourquoi", "comment", "ok", "merci", "super", "génial", "genial",
                "cool", "parfait", "top", "bravo", "nice", "bien", "d'accord", "vas-y",
                "graphique", "graphiqu", "chart", "histogramme", "camembert", "diagramme",
                "tableau", "tableaux", "stats", "statistiques", "export", "csv", "excel",
                "sirene", "siren", "siret", "entreprise", "boite", "societe", "société",
                "appel d'offre", "appels d'offre", "marché public", "consultation", "ao ",
                "compare", "comparaison", "versus", "vs ", "différence", "carte", "map",
                "donne-moi", "donne moi", "liste", "montre", "affiche", "cherche",
                # [FIX] Vague/open-ended questions that should NEVER trigger DVF follow-up.
                # NOTE: broad interrogatives (combien, quelle, quand, etc.) are deliberately
                # NOT listed here because they can appear in legitimate DVF follow-ups
                # (e.g. "combien de ventes?", "quelle tendance?"). The weak-keyword
                # whitelist below is the primary guard; this list is a safety net only.
                "conseil", "conseilles", "conseiller", "aide", "aider", "aides",
                "sais pas", "ne sais pas", "sait pas",
                "qu'est-ce que", "quest ce que", "qu est ce que",
                "peux-tu", "peux tu", "pouvez-vous", "pouvez vous", "pourrais-tu", "pourrais tu",
                "suggère", "suggestion", "idée", "propose", "proposition",
                "parle-moi", "parle moi", "parles-moi", "parles moi",
                "raconte", "explique", "décris", "decris",
                "capacité", "capacites", "peux", "pouvoir", "peut", "peuvent",
                "hello", "salut", "bonjour", "hey", "coucou", "yo",
                "ca va", "ça va", "comment vas-tu", "comment ca va",
                "test", "testing", "essai", "essaye",
                "désolé", "desole", "excuse", "pardon",
                "bisous", "ciao", "bye", "au revoir",
                "lol", "mdr", "ptdr", "haha", "ha ha",
                "oui", "non", "peut-être", "peut etre", "probablement", "certainement",
                "d'accord", "d accord", "accord", "entendu",
                "excellent", "parfait", "top", "cool", "nice",
                "bravo", "félicitations", "felicitations", "chapeau",
                "remercie", "gratitude",
                "problème", "probleme", "bug", "erreur", "plante", "planté",
                "cassé", "casse", "coincé", "coince",
                "attends", "wait", "pause", "stop", "arrete", "arrête",
            )
            is_generic_reaction = any(kw in safe_text.lower() for kw in followup_exclude_kw)
            # [FIX] Require at least one weak DVF keyword for follow-up to trigger.
            # Prevents vague messages like "Je sais pas qu'est-ce que tu me conseilles"
            # from forcing DVF with a stale cached location.
            dvf_weak_kw = (
                "prix", "vente", "achat", "vendre", "acheter", "achet", "vendu",
                "appartement", "maison", "loft", "duplex", "triplex", "villa",
                "parking", "garage", "cave", "terrain", "bureau", "local", "commerce",
                "habitation", "logement", "résidence", "immeuble", "bâtiment",
                "m²", "m2", "metre", "mètre", "carre", "carré", "surface",
                "pièce", "pièces", "chambre", "chambres", "piece", "pieces",
                "foncier", "immobilier", "immobilière", "propriété",
                "notaire", "hypothèque", "crédit", "prêt", "loyer", "bail", "charges",
                "taxe foncière", "taxe d'habitation", "pinel", "lmnp",
                "quartier", "arrondissement", "adresse", "rue", "boulevard", "avenue",
                "allée", "place", "impasse", "chemin", "passage", "faubourg", "quai",
                "marché", "tendance", "évolution", "augmentation", "baisse", "hausse",
                "cote", "côte", "estimation", "évaluer", "valeur",
                "2020", "2021", "2022", "2023", "2024", "2025", "2026",
                "euro", "euros", "€", "million", "millions",
                "neuf", "ancien", "rénové", "renové", "moderne",
                "copropriété", "syndic", "promoteur", "agence",
                "jardin", "piscine", "terrasse", "balcon", "ascenseur",
                "dpe", "diagnostic", "ges", "énergie",
                "locataire", "propriétaire", "colocation", "sous-location",
                "cher", "chère", "abordable", "détail", "detail", "exemple", "exemples",
            )
            has_dvf_weak_kw = any(kw in safe_text.lower() for kw in dvf_weak_kw)
            if dvf_context and len(safe_text) < 60 and not is_tender_request and not is_generic_reaction and has_dvf_weak_kw:
                is_dvf_request = True
                is_dvf_followup = True

        if is_dvf_request:
            voie = ""
            is_dvf_detail = False
            if is_dvf_followup and dvf_context:
                # En follow-up DVF, récupérer les paramètres géographiques du contexte précédent
                cp = dvf_context.get("cp", "")
                dept = dvf_context.get("dept", "")
                voie = dvf_context.get("voie", "")
                year_match = re.search(r"\b(20\d{2})\b", safe_text)
                annee = year_match.group(1) if year_match else dvf_context.get("annee", str(datetime.datetime.now().year - 1))
                is_dvf_detail = dvf_context.get("is_dvf_detail", False)
                is_dvf_trend = False
            else:
                cp_match = re.search(r"\b(\d{5})\b", safe_text)
                cp = cp_match.group(1) if cp_match else ""
                dept_match = re.search(r"\bdept\s+(\d{1,3})\b|\bdépartement\s+(\d{1,3})\b", safe_text.lower())
                dept = (dept_match.group(1) or dept_match.group(2)) if dept_match else ""
                year_match = re.search(r"\b(20\d{2})\b", safe_text)
                annee = year_match.group(1) if year_match else str(datetime.datetime.now().year - 1)

            # [FALLBACK VILLE] Si aucun CP/dept explicite, mapper les grandes villes
            if not cp and not dept:
                city_to_dept = {
                    "paris": "75", "lyon": "69", "marseille": "13", "toulouse": "31",
                    "nice": "06", "nantes": "44", "strasbourg": "67", "montpellier": "34",
                    "bordeaux": "33", "lille": "59", "rennes": "35", "reims": "51",
                    "saint-etienne": "42", "toulon": "83", "grenoble": "38", "dijon": "21",
                    "angers": "49", "villeurbanne": "69", "le havre": "76", "nimes": "30",
                    "aix-en-provence": "13", "brest": "29", "perpignan": "66", "limoges": "87",
                    "tours": "37", "amiens": "80", "metz": "57", "besancon": "25",
                    "boulogne-billancourt": "92", "argenteuil": "95", "rouen": "76",
                    "mulhouse": "68", "caen": "14", "nancy": "54", "roubaix": "59",
                    "tourcoing": "59", "nanterre": "92", "vitry-sur-seine": "94",
                    "creteil": "94", "avignon": "84", "poitiers": "86",
                    "aubervilliers": "93", "asnieres-sur-seine": "92", "aulnay-sous-bois": "93",
                    "rueil-malmaison": "92", "pau": "64", "champigny-sur-marne": "94",
                    "antibes": "06", "saint-maur-des-fosses": "94", "cannes": "06",
                    "beziers": "34", "colmar": "68", "bourges": "18", "dunkerque": "59",
                    "quimper": "29", "valence": "26", "drancy": "93", "merignac": "33",
                    "issy-les-moulineaux": "92", "levallois-perret": "92",
                    "noisy-le-grand": "93", "cergy": "95", "courbevoie": "92",
                    "la seyne-sur-mer": "83", "pessac": "33", "venissieux": "69",
                    "clichy": "92", "ivry-sur-seine": "94", "troyes": "10",
                    "neuilly-sur-seine": "92", "antony": "92", "pantin": "93",
                    "chambery": "73", "niort": "79", "sarcelles": "95",
                    "le blanc-mesnil": "93", "lorient": "56", "beauvais": "60",
                    "saint-quentin": "02", "hyeres": "83", "epinay-sur-seine": "93",
                    "chelles": "77", "meaux": "77", "la roche-sur-yon": "85",
                    "cholet": "49", "bondy": "93", "clermont-ferrand": "63",
                    "fontenay-sous-bois": "94", "frejus": "83", "villejuif": "94",
                    "saint-nazaire": "44", "arles": "13", "bayonne": "64",
                    "talence": "33", "angouleme": "16", "castres": "81",
                    "bastia": "2B", "belfort": "90", "charleville-mezieres": "08",
                    "suresnes": "92", "martigues": "13", "corbeil-essonnes": "91",
                    "saint-ouen-sur-seine": "93", "cagnes-sur-mer": "06", "sevran": "93",
                    "montrouge": "92", "massy": "91", "bobigny": "93", "evry": "91",
                    "saint-malo": "35", "albi": "81", "brive-la-gaillarde": "19",
                    "vaulx-en-velin": "69", "gennevilliers": "92", "malakoff": "92",
                    "thionville": "57", "athis-mons": "91", "bagneux": "92",
                    "chatillon": "92", "bron": "69", "rosny-sous-bois": "93",
                    "salon-de-provence": "13", "saint-germain-en-laye": "78",
                    "noisy-le-sec": "93", "puteaux": "92", "chalons-en-champagne": "51",
                    "mantes-la-jolie": "78", "sete": "34", "alfortville": "94",
                    "grasse": "06", "livry-gargan": "93", "chateauroux": "36",
                    "saint-laurent-du-var": "06", "saint-brieuc": "22",
                    "bourg-en-bresse": "01", "gap": "05", "melun": "77",
                    "annecy": "74", "orleans": "45", "laon": "02", "tarbes": "65",
                    "agen": "47", "biarritz": "64", "carcassonne": "11",
                    "colombes": "92",
                    "versailles": "78", "saint-denis": "93", "levallois": "92",
                }
                text_lower = safe_text.lower()
                for city, d in city_to_dept.items():
                    if city in text_lower:
                        dept = d
                        break

            # Détecter une région dans le texte
            regions_detected = []
            region_names = {
                "bretagne": "Bretagne", "ile de france": "Île-de-France",
                "ile-de-france": "Île-de-France", "provence": "Provence-Alpes-Côte d'Azur",
                "auvergne": "Auvergne-Rhône-Alpes", "occitanie": "Occitanie",
                "hauts de france": "Hauts-de-France", "grand est": "Grand Est",
                "pays de la loire": "Pays de la Loire", "normandie": "Normandie",
                "bourgogne": "Bourgogne-Franche-Comté", "centre": "Centre-Val de Loire",
                "corse": "Corse",
            }
            for key, name in region_names.items():
                if key in safe_text.lower():
                    regions_detected.append(name)
                    break

            if is_dvf_trend:
                # Requête de tendance multi-années
                loc = regions_detected[0] if regions_detected else (dept if dept else (cp[:2] if cp else ""))
                safe_text = (
                    "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande une TENDANCE / ÉVOLUTION "
                    "des prix immobiliers sur plusieurs années. Tu DOIS OBLIGATOIREMENT "
                    f"appeler le tool local 'query_dvf_trend' avec location='{loc or '[extrait du texte]'}' et "
                    f"annees=['2020','2021','2022','2023','2024'] (ou la plage adaptée) "
                    f"et type_local='[Maison ou Appartement selon la question]'. "
                    "Ne réponds JAMAIS de mémoire. N'utilise JAMAIS 'web_search'.\n\n" + safe_text
                )
            else:
                # Requête ponctuelle (une année, un lieu)
                if not is_dvf_followup:
                    # Extraire un nom de voie si présent (mot après boulevard/rue/avenue/allée/place/impasse/chemin)
                    voie_match = re.search(
                        r"\b(boulevard|rue|avenue|allée|place|impasse|chemin|passage|quai|faubourg)\s+([^,;\.\d]{3,60}?)"
                        r"(?:\s+(?:à|sur|dans|au|en|et|de|du|des)\s+|$)",
                        safe_text.lower(),
                    )
                    voie = voie_match.group(2).strip() if voie_match else ""
                    # Détection demande de DÉTAIL (dernières ventes individuelles) vs STATS (agrégats)
                    detail_kw = ("dernières ventes", "dernieres ventes", "liste des ventes", "détail", "detail",
                                 "exemples", "transactions individuelles", "vente d'appartement", "vente d'appartements",
                                 "ventes d'appartements", "ventes d'appartement", "dernières transactions",
                                 "biens vendus", "appartements vendus", "maisons vendues")
                    is_dvf_detail = any(kw in safe_text.lower() for kw in detail_kw)
                if is_dvf_detail:
                    safe_text = (
                        "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande les DERNIÈRES VENTES INDIVIDUELLES "
                        "(dates, adresses, prix, surfaces, pièces). Tu DOIS OBLIGATOIREMENT "
                        f"appeler le tool local 'query_dvf_details' avec code_postal='{cp or ''}' "
                        f"et departement='{dept or ''}' et annee='{annee or '[extrait du texte]'}' "
                        f"et voie='{voie or ''}' et type_local='Appartement' (ou 'Maison' selon la question) "
                        "AVANT de répondre. "
                        "RÈGLE CRITIQUE sur le paramètre 'voie' : il doit contenir UNIQUEMENT "
                        "le nom propre de la voie SANS le type (pas de 'boulevard', 'rue', 'avenue', 'allée', 'place'). "
                        "Exemples corrects : voie='des batignolles', voie='rivoli', voie='faubourg saint-honoré'. "
                        "INTERDICTION ABSOLUE : tu ne dois JAMAIS utiliser 'search_datasets', "
                        "'get_dataset_info', 'list_dataset_resources', 'download_and_analyze_resource' "
                        "ou 'query_resource_data' pour les données DVF. Utilise UNIQUEMENT 'query_dvf_details'. "
                        f"CRITIQUE : tu DOIS appeler le tool avec l'année EXACTE indiquée ({annee}). "
                        "Si le tool retourne des résultats vides pour cette année, dis EXPLICITEMENT : "
                        f"'Les données DVF pour {annee} ne sont pas encore disponibles.' "
                        "Ne montre JAMAIS des données d'une autre année sans prévenir. "
                        "Ne réponds JAMAIS de mémoire. N'utilise JAMAIS 'web_search' pour cette demande.\n\n" + safe_text
                    )
                else:
                    safe_text = (
                        "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande des données "
                        "immobilières (prix au m², transactions, DVF). Tu DOIS OBLIGATOIREMENT "
                        f"appeler le tool local 'query_dvf_stats' avec code_postal='{cp or ''}' "
                        f"et departement='{dept or ''}' et annee='{annee or '[extrait du texte]'}' "
                        f"et voie='{voie or ''}' "
                        "AVANT de répondre. "
                        "RÈGLE CRITIQUE sur le paramètre 'voie' : il doit contenir UNIQUEMENT "
                        "le nom propre de la voie SANS le type (pas de 'boulevard', 'rue', 'avenue', 'allée', 'place'). "
                        "Exemples corrects : voie='des batignolles', voie='rivoli', voie='faubourg saint-honoré'. "
                        "INTERDICTION ABSOLUE : tu ne dois JAMAIS utiliser 'search_datasets', "
                        "'get_dataset_info', 'list_dataset_resources', 'download_and_analyze_resource' "
                        "ou 'query_resource_data' pour les données DVF. Utilise UNIQUEMENT 'query_dvf_stats'. "
                        f"CRITIQUE : tu DOIS appeler le tool avec l'année EXACTE indiquée ({annee}). "
                        "Si le tool retourne des résultats vides pour cette année, dis EXPLICITEMENT : "
                        f"'Les données DVF pour {annee} ne sont pas encore disponibles.' "
                        "Ne montre JAMAIS des données d'une autre année sans prévenir. "
                        "Ne réponds JAMAIS de mémoire. N'utilise JAMAIS 'web_search' pour cette demande.\n\n" + safe_text
                    )
            if conversation:
                conversation[-1]["content"] = safe_text
            # Sauvegarder le contexte DVF pour les follow-ups futurs
            def _save_ctx(path, data):
                with open(path, "w") as f:
                    json.dump(data, f)

            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    _save_ctx,
                    dvf_context_path,
                    {"cp": cp, "dept": dept, "voie": voie, "annee": annee, "is_dvf_detail": is_dvf_detail, "question": safe_text[:200]},
                )

        # [DÉTECTION STATS / CHIFFRES GÉNÉRAUX] web_search en premier pour les données récentes
        stats_kw = (
            "taux de", "taux d'", "statistique", "statistiques", "pourcentage",
            "chiffre", "chiffres", "évolution du", "évolution de la",
            "démographie", "indicateur", "indicateurs", "combien de", "nombre de",
        )
        # NOTE : les questions sur les entreprises (créations, nombre, actives)
        # sont gérées par le SYSTEM_PROMPT qui cite le dataset INSEE de référence.
        # Ne pas injecter de consigne web_search ici pour ne pas écraser la règle dataset.
        is_stats_request = any(kw in safe_text.lower() for kw in stats_kw)
        if is_stats_request and not is_tender_request and not is_historical_request and not is_dvf_request:
            stats_instruction = (
                "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande des chiffres, statistiques, taux ou indicateurs généraux. "
                "Tu dois OBLIGATOIREMENT commencer par `web_search` pour trouver des données récentes et sourcées sur le web "
                "(INSEE, Wikipédia, sites officiels). Si le premier web_search retourne un résumé mais pas le tableau détaillé demandé, "
                "utilise immédiatement `extract_web_tables` sur l'URL officielle trouvée (ex: page INSEE ou Wikipédia) pour extraire les chiffres complets. "
                "Si l'extraction échoue, fais alors un second `web_search` avec une requête plus ciblée. "
                "Présente tous les chiffres trouvés immédiatement en citant la source et l'année. "
                "Si web_search échoue, analyse alors le premier dataset pertinent trouvé sur data.gouv.fr. "
                "Si le dataset trouvé est trop volumineux (> 1000 Mo) ou inaccessible (Mélodi), n'essaie pas de le télécharger : rebascule immédiatement sur `web_search`. "
                "Ne demande JAMAIS à l'utilisateur 'souhaites-tu que j'inspecte...' — fais-le directement."
            )
            conversation.insert(0, {"role": "system", "content": stats_instruction})

        # [DÉTECTION POPULATION PAR ÂGE — MARSEILLE] Données récentes via web_search, fallback PACA 2015
        pop_age_kw = ("population", "habitants", "âge", "tranche d'âge", "pyramide des âges")
        is_pop_age_request = any(kw in safe_text.lower() for kw in pop_age_kw)
        if is_pop_age_request and "marseille" in safe_text.lower():
            marseille_instruction = (
                "[CONSIGNE SYSTÈME ABSOLUE] L'utilisateur demande la population de Marseille par tranche d'âge. "
                "Tu dois OBLIGATOIREMENT chercher les données les plus récentes dans cet ordre : "
                "1) Appelle d'abord `web_search` avec la requête 'Marseille population tranche âge recensement 2020 INSEE' pour trouver des chiffres sourcés sur le web (Wikipedia, INSEE, etc.). "
                "2) Si web_search retourne des chiffres précis par tranche d'âge, présente-les immédiatement en citant la source et l'année. "
                "3) Si web_search échoue ou ne donne pas de chiffres exploitable, utilise alors le dataset régional PACA (ID: 668d09f8a847603d391cbec0) comme fallback. "
                "Pour le dataset PACA, appelle `list_dataset_resources` puis `download_and_analyze_resource` avec un `sql_query` filtrant sur Marseille. "
                "Ne perds PAS de temps sur les datasets INSEE nationaux (POP1, Évolution et structure de la population) — leurs ressources sont sur l'API Mélodi inaccessible."
            )
            conversation.insert(0, {"role": "system", "content": marseille_instruction})

        # [MESSAGES VAGUES] Empêcher la répétition automatique de recherches précédentes
        _VAGUE_WORDS = {
            "oui", "non", "ok", "okay", "ook", "ooo", "kk", "k", "oki", "okidoki", "oké", "oke", "okok",
            "d'accord", "dac", "yes", "no", "yep", "nope", "sure",
            "hello", "salut", "bonjour", "hey", "coucou", "yo", "hi", "hola",
            "test", "testing", "tst",
            "super", "génial", "cool", "nickel", "parfait", "top", "bien", "nice", "great",
            "merci", "thx", "thanks", "thank you",
            "👍", "👋", "🙏", "✅", "🙂", "😊", "🚀", "💪", "🎉",
        }
        clean_text_vague = text.strip().lower().rstrip("!?.,:;")
        if clean_text_vague in _VAGUE_WORDS:
            if len(conversation) > 1:
                safe_text = (
                    f"[DIRECTIVE SYSTÈME — MESSAGE VAGUE] L'utilisateur a répondu \"{text.strip()}\". "
                    f"Ce message ne contient AUCUNE nouvelle demande de données, de recherche, de graphique, d'export ou d'analyse. "
                    f"Tu dois répondre en UNE SEULE PHRASE CONCISE (salutation, remerciement, ou acquiescement). "
                    f"Tu ne dois JAMAIS relancer un tool (fetch_boamp_tenders, search_enterprises, query_dvf_stats, etc.) "
                    f"ni répéter les résultats précédents. Réponds brièvement puis attends."
                )
                if conversation:
                    conversation[-1]["content"] = safe_text
            else:
                await say(
                    text="Salut ! Je suis prêt — qu'est-ce que tu cherches ? 🔍",
                    thread_ts=slack_thread_ts,
                )
                return

        # [GÉOLOCALISATION] L'utilisateur demande une carte — le bot n'en génère pas
        geo_kw = (
            "carte", "carte interactive", "map", "géolocalisation",
            "géographique", "coordonnées gps", "latitude", "longitude",
            "localisation sur carte", "visualiser sur carte",
        )
        if any(kw in safe_text.lower() for kw in geo_kw):
            safe_text = (
                "[CONSIGNE SYSTÈME] L'utilisateur demande une carte ou une "
                "visualisation géographique. Si tu disposes de coordonnées GPS "
                "(latitude/longitude), tu DOIS utiliser l'outil local 'generate_scatter_map' "
                "(carte PNG) ou 'generate_interactive_map' (carte HTML/Leaflet) pour "
                "générer la carte toi-même. Ne propose JAMAIS d'importer un CSV dans "
                "Google My Maps ou QGIS. Si tu n'as pas de coordonnées, tu peux donner "
                "des statistiques par région/département ou proposer un géocodage.\n\n" + safe_text
            )
            if conversation:
                conversation[-1]["content"] = safe_text

        if not safe_text:
            await say(text="Dis-moi ce que tu cherches sur data.gouv.fr !", thread_ts=slack_thread_ts)
            return

        # [PASSAGE PAR LLM] Les salutations passent par l'orchestrateur pour une
        # réponse contextualisée. Les fixes sur build_conversation (limit=1 en DM)
        # et sur l'injection mémoire empêchent le mashup des sessions précédentes.
        # if _SOCIAL_RE.fullmatch(text.strip()):
        #     await say(text=_SOCIAL_REPLY, thread_ts=slack_thread_ts)
        #     user_id = event.get("user", "unknown")
        #     save_message(settings.cache_dir, user_id, channel, conversation_id, "user", safe_text)
        #     save_message(settings.cache_dir, user_id, channel, conversation_id, "assistant", _SOCIAL_REPLY)
        #     return

        # Si l'utilisateur fait référence à "l'autre", "le deuxième", etc.,
        # chercher aussi dans l'historique cross-thread pour retrouver les datasets
        ambiguous_refs = (
            "l'autre", "l'autres", "les autres", "un autre", "une autre",
            "le deuxième", "la deuxième", "le 2e", "la 2e", "le 2ème", "la 2ème",
            "le troisième", "la troisième", "le 3e", "la 3e", "le 3ème", "la 3ème",
            "le premier", "la première", "le 1er", "le 1ère", "la 1ère",
            "celui d'avant", "celle d'avant", "celui d'après", "celle d'après",
        )
        if any(ref in safe_text.lower() for ref in ambiguous_refs):
            try:
                user_past = get_user_memory(settings.cache_dir, user_id, limit=20)
                # Ne garder que les messages assistant contenant des _refs: (datasets mentionnés)
                ref_msgs = [
                    m for m in user_past
                    if m.get("role") == "assistant" and "_refs:" in m.get("content", "")
                ]
                # Compter les datasets UNIQUES mentionnés dans l'historique
                # On parse à la fois les URLs et les lignes _refs: dataset_1=xxx
                unique_ids = set()
                for m in ref_msgs:
                    content = m.get("content", "")
                    unique_ids.update(_extract_dataset_ids(content))
                    unique_ids.update(_extract_refs_ids(content))

                if len(unique_ids) >= 2:
                    ref_text = "\n".join(
                        f"- [{m['date']}] Dataset(s) mentionné(s): "
                        f"{_extract_dataset_ids(m['content']) + _extract_refs_ids(m['content'])}"
                        for m in ref_msgs[-3:]
                    )
                    safe_text = f"[RAPPEL DES DATASETS DÉJÀ MENTIONNÉS]\n{ref_text}\n\n{safe_text}"
                    if conversation:
                        conversation[-1]["content"] = safe_text
                else:
                    # Moins de 2 datasets uniques : recherche programmatique d'alternatives
                    # pour ne plus dépendre du hasard du LLM
                    if conversation:
                        query = ""
                        if principal_dataset_id:
                            title = await _fetch_dataset_title(
                                principal_dataset_id, settings.datagouv_api_base_url
                            )
                            # Utiliser les 4 premiers mots pour un query plus générique
                            query = " ".join(title.split()[:4]) if title else ""
                        if not query:
                            query = "données publiques"
                        alt_results = await _search_datagouv_alternatives(
                            query, settings.datagouv_api_base_url
                        )
                        conversation.insert(
                            -1,
                            {
                                "role": "system",
                                "content": (
                                    f"L'utilisateur fait référence à 'l'autre' dataset mais seul "
                                    f"le dataset principal a été mentionné jusqu'à présent. "
                                    f"Voici des alternatives trouvées par recherche programmatique "
                                    f"sur data.gouv.fr (query: {query}) :\n{alt_results}\n"
                                    f"Présente-les numérotées avec leurs _refs:."
                                ),
                            },
                        )
            except Exception:
                logger.exception("Erreur récupération mémoire cross-thread, on continue sans")

        if past and conversation and len(conversation) <= 4 and not is_dm:
            # [MÉMOIRE CROSS-THREAD] On injecte UNIQUEMENT les messages UTILISATEUR
            # pour rappeler les sujets abordés sans polluer le LLM avec les réponses
            # assistant précédentes (souvent des tableaux markdown de 500+ lignes).
            # [FIX DM] En DM, build_conversation est volontairement limité à 1 pour
            # éviter la pollution de session Slack. L'injection de past contredirait
            # ce choix et provoquerait un mashup des conversations précédentes.
            slack_texts = {msg["content"] for msg in conversation[:-1]}
            filtered = [
                m for m in past
                if m.get("role") == "user"
                and m["content"] not in slack_texts
                and m["content"] != safe_text
            ]
            if filtered:
                memory_text = "\n".join(
                    f"- [{m['date']}] {m['role']}: {_sanitize_user_input(m['content'])[:200]}"
                    for m in filtered[-3:]  # max 3 messages utilisateur historiques
                )
                # On enrichit le dernier message de la conversation (le message actuel)
                conversation[-1]["content"] = (
                    f"[CONTEXTE PRÉCÉDENT — sujets abordés par l'utilisateur]\n{memory_text}\n\n[MESSAGE ACTUEL]\n{safe_text}"
                )



        try:
            # [BOUCLE AGENTIQUE] Appel à l'orchestrateur (boucle agentique LLM → tools)
                answer = await orchestrator.run(conversation, context={"thread_ts": conversation_id})
        except Exception:  # noqa: BLE001
            logger.exception("Erreur de l'agent")
            # [FALLBACK ERREUR] Fallback ultime si l'orchestrateur plante
            answer = (
                "Désolé, une erreur est survenue pendant le traitement de ta demande. "
                "Réessaie dans un instant."
            )

        # [COURT-CIRCUIT AO] Désactivé en mode réflexion (DISABLE_AUTO_CIRCUITS=True)
        # pour forcer le passage par le LLM et éviter les réponses préformatées.
        if not DISABLE_AUTO_CIRCUITS:
            ao_data = load_search_results(settings.cache_dir, conversation_id)
            if ao_data and ao_data.get("formatted_text"):
                ft = ao_data["formatted_text"]
                ts = ao_data.get("timestamp", 0)
                # Timeout court (30s) pour éviter d'intercepter les questions suivantes non liées
                if ft and (time.time() - ts) < 30:
                    # Condition 1 : la question actuelle parle d'AO
                    tender_kw = (
                        "appel d'offre", "appels d'offre", "marché public", "marches publics",
                        "marchés publics", "consultation ouverte", "ao en", "ao ia", "ao sur",
                        "consultation ", "marchés pub", "appels d'offre",
                    )
                    is_ao_question = any(kw in safe_text.lower() for kw in tender_kw)

                    # Condition 2 : la réponse LLM semble incomplète pour un contexte AO
                    numbered_lines = [
                        line for line in (answer or "").splitlines()
                        if re.match(r"^(\*\*)?\d+\.", line.strip())
                    ]
                    llm_incomplete = len(numbered_lines) < 5

                    if is_ao_question or llm_incomplete:
                        logger.info(
                            "[COURT-CIRCUIT AO] Remplacement de la réponse LLM par le formatted_text (raison: %s)",
                            "question AO" if is_ao_question else "réponse LLM incomplète",
                        )
                        # Supprimer le tag technique [RÉSULTAT BRUT] avant affichage
                        answer = re.sub(r"^\[RÉSULTAT BRUT[^\n]*\]\n?", "", ft).strip()
                        # Nettoyer formatted_text pour ne pas réutiliser sur la question suivante
                        clean_meta = {k: v for k, v in ao_data.items() if k not in ("query", "results", "formatted_text")}
                        save_search_results(
                            settings.cache_dir,
                            ao_data.get("query", ""),
                            ao_data.get("results", []),
                            metadata=clean_meta,
                        )

        # Sauvegarder dans la mémoire persistante
        user_id = event.get("user", "unknown")
        save_message(settings.cache_dir, user_id, channel, conversation_id, "user", safe_text)
        # [TRONCATURE MÉMOIRE] Les très longues réponses (listes de 70+ entreprises)
        # ne doivent pas saturer le contexte LLM des messages suivants.
        # EXCEPTION : les résultats bruts de search_enterprises doivent rester intacts
        # pour permettre l'enrichissement par lots (l'utilisateur répond "oui" pour la suite).
        MAX_MEMORY_CHARS = 4_000
        if "[RÉSULTAT BRUT" in answer:
            answer_to_save = answer  # On garde l'intégralité pour les SIREN
        elif len(answer) > MAX_MEMORY_CHARS:
            answer_to_save = answer[:MAX_MEMORY_CHARS] + "\n\n... [réponse tronquée dans la mémoire] ..."
        else:
            answer_to_save = answer
        save_message(settings.cache_dir, user_id, channel, conversation_id, "assistant", answer_to_save)

        # [RÉPLICATION THREAD] Copier les résultats globaux vers le thread UNIQUEMENT
        # si le thread n'a pas déjà des données récentes (pour ne pas écraser AO/DVF/etc.)
        global_data = load_search_results(settings.cache_dir, "")
        thread_data = load_search_results(settings.cache_dir, conversation_id)
        thread_is_fresh = (
            thread_data
            and thread_data.get("results")
            and (time.time() - thread_data.get("timestamp", 0)) < 600
        )
        if (
            global_data
            and global_data.get("results")
            and not global_data.get("thread_ts")
            and not thread_is_fresh
        ):
            save_search_results(
                settings.cache_dir,
                global_data["query"],
                global_data["results"],
                metadata={"thread_ts": conversation_id, "timestamp": int(time.time())}
            )

        # [UPLOAD FICHIERS GÉNÉRÉS] Si des tools ont généré des fichiers (ex: carte)
        file_paths = re.findall(r"\[FICHIER_GÉNÉRÉ\]\s+(\S+)", answer)
        if file_paths:
            for fp in file_paths:
                if await asyncio.to_thread(Path(fp).exists):
                    try:
                        await client.files_upload_v2(
                            channel=channel,
                            thread_ts=slack_thread_ts,
                            file=fp,
                            title="Fichier généré",
                            initial_comment="Voici le fichier généré 📎",
                        )
                    except Exception:
                        logger.exception("Échec upload fichier généré")
                        await say(text=f"❌ Le fichier {Path(fp).name} a été généré mais je n'ai pas pu l'envoyer.", thread_ts=slack_thread_ts)
            # Supprimer les lignes de marqueur du texte
            answer = re.sub(r"\[FICHIER_GÉNÉRÉ\]\s+\S+\n?", "", answer).strip()

        answer = _clean_answer(answer)
        # Fallback ultime : jamais de message vide vers Slack
        if not answer or answer == "(réponse vide du modèle)":
            answer = (
                "Je n'ai pas compris ta demande. "
                "Peux-tu reformuler ou préciser ce que tu cherches ?"
            )
        # Tronquer si le message dépasse la limite conservative de Slack (40k max, on tronque à 15k)
        MAX_SLACK_CHARS = 15_000
        if len(answer) > MAX_SLACK_CHARS:
            answer = answer[:MAX_SLACK_CHARS] + "\n\n... [message tronqué, trop long pour Slack] ..."
        await say(text=answer, thread_ts=slack_thread_ts)

        # Détecter et uploader les fichiers exportés (thread-safe)
        marker_path = Path(settings.cache_dir) / f".last_export_{conversation_id}"
        if await asyncio.to_thread(marker_path.exists):
            try:
                file_path = await asyncio.to_thread(marker_path.read_text)
                file_path = file_path.strip()
                if await asyncio.to_thread(Path(file_path).exists):
                    try:
                        await client.files_upload_v2(
                            channel=channel,
                            thread_ts=slack_thread_ts,
                            file=file_path,
                            title="Résultats exportés",
                            initial_comment="Voici l'export demandé 📎",
                        )
                    except Exception:
                        logger.exception("Échec upload fichier Slack")
                        await say(text="❌ L'export existe mais je n'ai pas pu l'envoyer sur Slack.", thread_ts=slack_thread_ts)
                marker_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Échec traitement marker upload")
                marker_path.unlink(missing_ok=True)

    @app.event("app_mention")
    async def on_mention(event, client, say):
        try:
            await asyncio.wait_for(_handle(event, client, say), timeout=300)
        except TimeoutError:
            logger.warning("Timeout sur le traitement d'un message (mention)")
            await say(
                text="⏱️ *Timeout* — le traitement a dépassé la minute autorisée.\n"
                "Causes possibles : fichier très gros, API lente, ou requête trop complexe.\n"
                "Conseil : reformule avec des mots-clés plus précis, ou demande une étape à la fois.",
                thread_ts=event.get("thread_ts") or event.get("ts"),
            )

    @app.event("message")
    async def on_message(event, client, say):
        # Ignorer les bots (y compris nous-même)
        if event.get("bot_id"):
            logger.debug("Message ignoré : provient d'un bot (bot_id=%s)", event.get("bot_id"))
            return
        # Gérer les messages édités : extraire le nouveau texte
        subtype = event.get("subtype")
        if subtype == "message_changed":
            edited = event.get("message", {})
            event["text"] = edited.get("text", "")
            event["user"] = edited.get("user", "")
            event["ts"] = edited.get("ts", event.get("ts"))
            logger.debug("Message édité détecté, nouveau texte traité")
        elif subtype:
            # Ignorer les autres subtypes (suppression, join, etc.)
            logger.debug("Message ignoré : subtype='%s' non géré", subtype)
            return
        # En DM (channel_type=im), il n'y a pas de thread_ts — on traite quand même
        is_dm = event.get("channel_type") == "im"
        channel = event.get("channel", "?")
        user = event.get("user", "?")
        raw_text = event.get("text") or ""
        # En DM, traiter manuellement les slash commands car Slack ne les envoie pas
        # toujours comme events `command` en Socket Mode.
        clean = raw_text.strip().strip("`").strip()
        if is_dm and clean.startswith("/datagouv "):
            cmd_text = clean[len("/datagouv "):].strip().lower()
            logger.info("DM slash command manuelle : %s", cmd_text)
            if cmd_text == "demo":
                resp = await client.chat_postMessage(
                    channel=channel, text="🎬 Lancement de la démo guidée…"
                )
                demo_thread_ts = resp.get("ts") if resp else None
                await run_demo(settings, say, user, demo_thread_ts)
                return
            # Autres commandes : on laisse tomber (help/status/apikey fonctionnent via mention)
            return
        if clean.startswith("/"):
            logger.info("Message ignoré : slash command (%s)", raw_text[:40])
            return
        logger.info("Message reçu — user=%s channel=%s is_dm=%s text='%s...'", user, channel, is_dm, raw_text[:60])
        # Ne traiter que les réponses dans un thread existant.
        # On ignore seulement si c'est une vraie mention directe (app_mention gère ça),
        # pas si le texte contient une mention ancienne copiée-collée.
        is_direct_mention = bool(MENTION_PATTERN.search(raw_text)) and event.get("type") == "app_mention"
        if not is_dm and (not event.get("thread_ts") or is_direct_mention):
            logger.info("Message ignoré : pas en DM et hors thread (thread_ts=%s, is_direct_mention=%s)", event.get("thread_ts"), is_direct_mention)
            return
        # En DM, on traite directement (pas de thread à vérifier)
        if is_dm:
            logger.info("DM détecté — traitement direct pour user=%s", user)
            dm_thread_ts = event.get("thread_ts") or event.get("ts")
            try:
                await asyncio.wait_for(_handle(event, client, say, is_dm=True), timeout=180)
            except TimeoutError:
                logger.warning("Timeout sur le traitement d'un message (DM)")
                await say(
                    text="⏱️ *Timeout* — le traitement a dépassé la minute autorisée.\n"
                    "Causes possibles : fichier très gros, API lente, ou requête trop complexe.\n"
                    "Conseil : reformule avec des mots-clés plus précis, ou demande une étape à la fois.",
                    thread_ts=dm_thread_ts,
                )
            return
        # Vérifier que le bot participe déjà à ce thread
        try:
            replies = await client.conversations_replies(
                channel=event["channel"], ts=event["thread_ts"], limit=50
            )
        except Exception:
            logger.exception("Impossible de récupérer le thread %s", event["thread_ts"])
            return
        bot_user_id = state.get("bot_user_id")
        bot_in_thread = bot_user_id is not None and any(
            m.get("user") == bot_user_id for m in replies.get("messages", [])
        )
        if bot_in_thread:
            try:
                await asyncio.wait_for(_handle(event, client, say), timeout=180)
            except TimeoutError:
                logger.warning("Timeout sur le traitement d'un message (thread)")
                await say(
                    text="⏱️ *Timeout* — le traitement a dépassé la minute autorisée.\n"
                    "Causes possibles : fichier très gros, API lente, ou requête trop complexe.\n"
                    "Conseil : reformule avec des mots-clés plus précis, ou demande une étape à la fois.",
                    thread_ts=event.get("thread_ts") or event.get("ts"),
                )

    @app.command("/datagouv")
    async def datagouv_command(ack, command, say):
        await ack()
        text = (command.get("text") or "").strip().lower()
        user_id = command.get("user_id", "")
        channel_id = command.get("channel_id", "")

        if text.startswith("alerte ") or text.startswith("alert "):
            query = text.split(" ", 1)[1] if " " in text else ""
            if not query:
                await say("Usage: `/datagouv alerte [mot-clé]`")
                return
            alert_id = add_alert(settings.cache_dir, user_id, channel_id, query)
            safe_query = _escape_slack_mrkdwn(query)
            await say(f"✅ Alerte #{alert_id} créée pour *{safe_query}*. Je te notifierai quand un nouveau dataset sera publié.")
            return

        elif text in ("alertes", "list"):
            alerts = list_alerts(settings.cache_dir, user_id)
            if not alerts:
                await say("Tu n'as aucune alerte active.")
            else:
                lines = ["🔔 *Tes alertes actives* :"]
                for a in alerts:
                    lines.append(f"• #{a['id']} — {a['query']} (depuis {a['created_at']})")
                await say("\n".join(lines))
            return

        elif text.startswith("ao "):
            ao_text = text[3:].strip()
            if ao_text.startswith("alerte ") or ao_text.startswith("alert "):
                query = ao_text.split(" ", 1)[1] if " " in ao_text else ""
                if not query:
                    await say("Usage: `/datagouv ao alerte [mot-clé]`")
                    return
                alert_id = add_ao_alert(settings.cache_dir, user_id, channel_id, query)
                safe_query = _escape_slack_mrkdwn(query)
                await say(f"✅ Alerte AO #{alert_id} créée pour *{safe_query}*. Je te pousserai les nouveaux appels d'offre pertinents chaque matin.")
                return
            elif ao_text in ("alertes", "list"):
                alerts = list_ao_alerts(settings.cache_dir, user_id)
                if not alerts:
                    await say("Tu n'as aucune alerte AO active.")
                else:
                    lines = ["📋 *Tes alertes AO actives* :"]
                    for a in alerts:
                        lines.append(f"• #{a['id']} — {a['query']} (seuil {a['threshold']}/100)")
                    await say("\n".join(lines))
                return
            elif ao_text.startswith("stop ") or ao_text.startswith("remove "):
                try:
                    alert_id = int(ao_text.split(" ")[1])
                    if remove_ao_alert(settings.cache_dir, alert_id, user_id):
                        await say(f"✅ Alerte AO #{alert_id} supprimée.")
                    else:
                        await say(f"❌ Alerte AO #{alert_id} introuvable ou pas à toi.")
                except (ValueError, IndexError):
                    await say("Usage: `/datagouv ao stop [numéro]`")
                return
            elif ao_text in ("suivis", "followed", "tracked"):
                tracked = list_tracked(settings.cache_dir, user_id)
                if not tracked:
                    await say("Tu ne suis aucun AO pour l'instant. Réagis avec ✅ ou 🔥 sur un push AO pour le suivre.")
                    return
                lines = ["📋 *AO suivis* :"]
                for t in tracked:
                    dl = t.get("deadline") or "?"
                    rem = ", ".join(json.loads(t.get("reminders_sent") or "[]")) or "aucun"
                    lines.append(f"• #{t['id']} — {t['objet']} (deadline {dl}, rappels {rem})")
                await say("\n".join(lines))
                return
            elif ao_text.startswith("trame ") or ao_text == "trame":
                idweb = ao_text[6:].strip() if ao_text.startswith("trame ") else ""
                ao = None
                if idweb:
                    ao = await fetch_ao_by_idweb(idweb)
                if not ao:
                    await say("Usage: `/datagouv ao trame [idweb]` ou réponds *trame* dans le thread d'un push AO.")
                    return
                profile = get_profile(settings.cache_dir, user_id) or {
                    "activity": "", "company_size": "", "keywords": [],
                    "sectors": [], "zones": [], "min_amount": 0, "max_amount": 0,
                }
                await say("✍️ Je génère le squelette de mémoire technique...")
                from app.llm.client import LLMClient
                llm = LLMClient(settings)
                skeleton = await generate_response_skeleton(ao, profile, llm)
                await say(skeleton)
                return
            elif ao_text.startswith("dce ") or ao_text == "dce":
                rest = ao_text[4:].strip() if ao_text.startswith("dce ") else ""
                parts = rest.split(None, 1)
                if len(parts) < 2:
                    await say("Usage: `/datagouv ao dce [idweb] [url_du_pdf]`")
                    return
                idweb, url = parts[0], parts[1]
                await say(f"📄 Indexation du DCE pour AO *{idweb}* en cours…")
                llm = LLMClient(settings)
                result = await index_dce_pdf(idweb, url, settings, llm)
                if result.get("success"):
                    await say(
                        f"✅ DCE indexé ! {result['chunks']} extraits du document.\n"
                        f"Tu peux maintenant poser des questions avec `/datagouv ao question {idweb} …` "
                        f"ou répondre directement dans le thread avec des mots-clés comme *critères*, *notation*…"
                    )
                else:
                    await say(f"❌ Échec de l'indexation : {result.get('error', 'erreur inconnue')}")
                return
            elif ao_text.startswith("question ") or ao_text == "question":
                rest = ao_text[9:].strip() if ao_text.startswith("question ") else ""
                parts = rest.split(None, 1)
                if len(parts) < 2:
                    await say("Usage: `/datagouv ao question [idweb] [ta question]`")
                    return
                idweb, question = parts[0], parts[1]
                if not is_dce_indexed(settings.cache_dir, idweb):
                    await say(
                        f"📄 Ce DCE n'est pas encore indexé. "
                        f"Envoie `/datagouv ao dce {idweb} [URL_DCE]` pour l'indexer."
                    )
                    return
                await say("🔍 Je cherche dans le DCE…")
                llm = LLMClient(settings)
                answer = await query_dce_rag(idweb, question, llm, settings.cache_dir)
                await say(answer or "❌ Aucune réponse trouvée dans le DCE.")
                return

        elif text == "canal":
            await say(
                f"🆔 *ID de ce canal :* `{channel_id}`\n"
                f"_Copie cette valeur dans `.env` comme `IGUANE_ALERT_CHANNEL={channel_id}` pour recevoir les alertes Iguane ici._"
            )
            return

        elif text.startswith("profil") or text.startswith("profile"):
            profil_text = text[6:].strip() if text.startswith("profil") else text[7:].strip()
            if profil_text:
                # Parse key=value pairs
                kwargs: dict[str, Any] = {"channel": channel_id}
                for part in profil_text.split():
                    if "=" in part:
                        k, v = part.split("=", 1)
                        k = k.strip().lower()
                        v = v.strip()
                        if k in ("activité", "activite", "activity"):
                            kwargs["activity"] = v
                        elif k in ("taille", "size", "company_size"):
                            kwargs["company_size"] = v
                        elif k in ("mots-clés", "mots_cles", "keywords"):
                            kwargs["keywords"] = [x.strip() for x in v.split(",")]
                        elif k in ("secteurs", "sectors"):
                            kwargs["sectors"] = [x.strip() for x in v.split(",")]
                        elif k in ("zones", "départements", "departements"):
                            kwargs["zones"] = [x.strip() for x in v.split(",")]
                        elif k in ("min", "min_amount"):
                            kwargs["min_amount"] = int(v.replace(" ", "").replace("€", ""))
                        elif k in ("max", "max_amount"):
                            kwargs["max_amount"] = int(v.replace(" ", "").replace("€", ""))
                upsert_profile(settings.cache_dir, user_id, **kwargs)
                await say("✅ Profil mis à jour.")
            profile = get_profile(settings.cache_dir, user_id)
            if profile:
                await say(format_profile(profile))
            else:
                await say(
                    "*Aucun profil renseigné.*\n"
                    "Exemple : `/datagouv profil activité=BTP taille=PME zones=75,93 min=50000`"
                )
            return

        elif text.startswith("stop ") or text.startswith("remove "):
            try:
                alert_id = int(text.split(" ")[1])
                if remove_alert(settings.cache_dir, alert_id, user_id):
                    await say(f"✅ Alerte #{alert_id} supprimée.")
                else:
                    await say(f"❌ Alerte #{alert_id} introuvable ou pas à toi.")
            except (ValueError, IndexError):
                await say("Usage: `/datagouv stop [numéro]`")
            return

        elif text.startswith("apikey") or text == "api":
            action = text[6:].strip() if text.startswith("apikey") else ""
            if action == "reset":
                key = generate_api_key(settings.cache_dir, user_id)
                await say(
                    f"🔑 Nouvelle clé API générée :\n```\n{key}\n```\n"
                    f"_Stocke-la bien, je ne la réafficherai pas._\n"
                    f"Utilise-la dans le header `X-API-Key` pour appeler l'API :\n"
                    f"• `GET http://localhost:8080/api/v1/stats`\n"
                    f"• `GET http://localhost:8080/api/v1/alerts`\n"
                    f"• `GET http://localhost:8080/api/v1/tracked`\n"
                    f"• `GET http://localhost:8080/api/v1/pushes`\n"
                    f"• `GET / PUT http://localhost:8080/api/v1/profile`"
                )
            else:
                existing = get_api_key(settings.cache_dir, user_id)
                if existing:
                    await say(
                        f"🔑 Ta clé API existante :\n```\n{existing}\n```\n"
                        f"_Pour régénérer :_ `/datagouv apikey reset`"
                    )
                else:
                    key = generate_api_key(settings.cache_dir, user_id)
                    await say(
                        f"🔑 Clé API générée :\n```\n{key}\n```\n"
                        f"_Stocke-la bien, je ne la réafficherai pas._\n"
                        f"Utilise-la dans le header `X-API-Key` pour appeler l'API."
                    )
            return

        elif text == "demo":
            await say("🎬 Lancement de la démo guidée…")
            await run_demo(settings, say, user_id, None)
            return

        elif not text or text in ("help", "aide"):
            await say(BOT_HELP_TEXT)
            return

        elif text in ("status", "état", "health"):
            status = await health_check(settings)
            emoji = {"healthy": "🟢", "degraded": "🟡", "critical": "🔴"}.get(
                status.get("overall", "unknown"), "⚪"
            )
            lines = [
                f"{emoji} *Status du bot* — `{status.get('overall', 'inconnu')}`",
                f"• LLM : {'✅' if status.get('llm_connected') else '❌'}",
                f"• MCP data.gouv : {'✅' if status.get('mcp_reachable') else '❌'}",
                f"• Web server : {'✅' if status.get('web_server') else '❌'}",
                "• Slack : ✅ (connecté)",
            ]
            if settings.tavily_api_key:
                lines.append("• Tavily : ✅ Clé configurée")
            else:
                lines.append("• Tavily : ⚠️ Clé manquante")
            cache_files = len(await asyncio.to_thread(lambda: list(Path(settings.cache_dir).glob("*"))))
            lines.append(f"• Cache : {cache_files} fichiers")
            lines.append(f"_Dernière vérif : {status.get('timestamp', '?')}_")
            await say("\n".join(lines))
            return

        # Recherche par défaut
        query = text
        safe_query = _escape_slack_mrkdwn(query)
        await say(f"🔍 Recherche en cours pour *{safe_query}*...")
        try:
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
                response = await client.get(
                    "https://www.data.gouv.fr/api/1/datasets/",
                    params={"q": query, "page_size": 5},
                )
                response.raise_for_status()
                data = response.json()

            datasets = data.get("data", [])
            if not datasets:
                await say(f"Aucun dataset trouvé pour *{safe_query}*.")
                return

            lines = [f"*Top résultats pour \"{safe_query}\"* :"]
            raw_results = []
            for ds in datasets[:5]:
                title = ds.get("title", "Sans titre")
                url = ds.get("page", f"https://www.data.gouv.fr/fr/datasets/{ds.get('id', '')}/")
                org = (ds.get("organization") or {}).get("name", "Organisation inconnue")
                lines.append(f"• <{url}|{title}> — _{org}_")
                raw_results.append({"nom": title, "url": url, "organisation": org})

            await say("\n".join(lines))
            # Sauvegarder pour export futur
            from app.data.exporter import save_search_results
            save_search_results(
                settings.cache_dir, query, raw_results,
                metadata={"timestamp": int(time.time())}
            )
        except Exception as exc:
            logger.exception("Erreur recherche slash command")
            await say(f"❌ Erreur pendant la recherche : {exc}")

    @app.event("member_joined_channel")
    async def on_member_joined(event, client, say):
        user_id = event.get("user", "")
        channel = event.get("channel", "")
        if user_id == state.get("bot_user_id"):
            # C'est le bot lui-même qui a rejoint
            onboarding = state.get("bot_onboarding_text", BOT_ONBOARDING_TEXT_TEMPLATE)
            await say(text=onboarding)
        else:
            # Un humain a rejoint le canal — message de bienvenue rapide
            try:
                await send_welcome(client, channel, user_id)
            except Exception:
                logger.exception("Échec envoi welcome à un nouveau membre")

    @app.event("reaction_added")
    async def on_reaction_added(event, client):
        bot_user_id = state.get("bot_user_id")
        if event.get("item_user") != bot_user_id:
            return
        reaction = event.get("reaction", "")
        if not is_accepted_reaction(reaction):
            return
        channel = event["item"]["channel"]
        message_ts = event["item"]["ts"]
        user_id = event["user"]

        ao_data = get_ao_from_message(settings.cache_dir, channel, message_ts)
        if not ao_data:
            return

        track_tender(
            settings.cache_dir,
            user_id=user_id,
            channel=channel,
            message_ts=message_ts,
            ao_id=ao_data["ao_id"],
            objet=ao_data["objet"],
            deadline=ao_data["deadline"],
        )
        try:
            await client.reactions_add(
                channel=channel, timestamp=message_ts, name="hourglass_flowing_sand"
            )
            await client.chat_postMessage(
                channel=channel,
                thread_ts=message_ts,
                text="✅ AO suivi ! Je te rappellerai à J-15, J-7 et J-2.",
            )
        except Exception:
            logger.exception("Échec confirmation suivi AO")

    async def resolve_bot_user_id() -> None:
        auth = await app.client.auth_test()
        state["bot_user_id"] = auth["user_id"]
        bot_name = auth.get("user", "DATAGOUV-AGENT")
        state["bot_onboarding_text"] = BOT_ONBOARDING_TEXT_TEMPLATE.format(bot_name=bot_name)
        logger.info("Bot connecté en tant que %s (%s)", bot_name, state["bot_user_id"])
        # Nettoyer les vieux fichiers de cache
        _cleanup_cache_files(settings.cache_dir)
        logger.info("Cache nettoyé au démarrage")
        # Annuler l'ancien scheduler s'il existe (évite tâches zombies sur reconnexion)
        old_scheduler = state.get("alert_scheduler_task")
        if old_scheduler and not old_scheduler.done():
            old_scheduler.cancel()
            logger.info("Ancien scheduler d'alertes annulé")
        # Démarrer le scheduler d'alertes en tâche de fond
        state["alert_scheduler_task"] = asyncio.create_task(
            alert_scheduler(settings.cache_dir, app.client, settings, interval_minutes=60)
        )
        logger.info("Scheduler d'alertes démarré (vérification toutes les heures)")

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    app._resolve_bot_user_id = resolve_bot_user_id  # exposé pour main.py
    return app, handler
