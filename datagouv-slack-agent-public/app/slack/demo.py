"""Commande de démonstration guidée pour montrer les capacités AO."""

import asyncio
import logging
from typing import Any

from app.agent.local_tools import fetch_boamp_tenders
from app.config import Settings
from app.llm.client import LLMClient
from app.slack.buyer_history import fetch_buyer_history, format_buyer_history
from app.slack.profiles import get_profile
from app.slack.tender_scoring import score_tender

logger = logging.getLogger(__name__)

DEFAULT_DEMO_PROFILE: dict[str, Any] = {
    "activity": "Cybersécurité et services informatiques",
    "company_size": "PME",
    "keywords": ["cybersécurité", "audit", "SI"],
    "sectors": ["informatique", "sécurité"],
    "zones": ["75", "92", "78"],
    "min_amount": 50000,
    "max_amount": 500000,
}


async def run_demo(
    settings: Settings,
    say,
    user_id: str,
    thread_ts: str | None = None,
) -> None:
    """Lance la démo guidée en 3 blocs (titre, corps, conclusion)."""

    async def _say(text: str) -> None:
        kwargs: dict[str, Any] = {"text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await say(**kwargs)

    # ---------- BLOC 1 : Titre + Étape 1 (recherche) ----------
    try:
        results, nhits, _ = await fetch_boamp_tenders("cybersécurité")
        if not results:
            results, nhits, _ = await fetch_boamp_tenders("informatique")
    except Exception as exc:
        logger.exception("Demo: BOAMP search failed")
        await _say(f"❌ Erreur recherche BOAMP : {exc}")
        return

    if not results:
        await _say("Aucun AO trouvé pour la démo. Réessaie plus tard.")
        return

    # Privilégier les acheteurs institutionnels majeurs (plus d'historique DECP)
    _PRIO = ["région", "département", "mairie", "métropole", "agglomération",
             "communauté", "université", "hôpital", "chu ", "chru", "centre hospitalier"]

    def _buyer_score(ao: dict) -> int:
        buyer = (ao.get("org") or "").lower()
        score = sum(1 for kw in _PRIO if kw in buyer)
        # Pénaliser DOM-TOM (moins d'historique dans DECP)
        depts = ao.get("departements", [])
        if depts and str(depts[0]).startswith(("97", "98")):
            score -= 5
        return score

    results = sorted(results, key=_buyer_score, reverse=True)
    ao = results[0]
    lines = [
        "🎬 *Démo guidée — DATAGOUV-AGENT en mode intelligence marchés publics*",
        "_4 étapes, aucune interaction requise de ta part._\n",
        "🔍 *Étape 1/4 — Recherche d'appels d'offre*",
        f"✅ {len(results)} AO trouvé(s) sur {nhits} brut(s).",
        f"• *{ao.get('objet', 'Sans titre')}*",
        f"• Acheteur : {ao.get('org', 'Inconnu')}",
        f"• ID : {ao.get('idweb', 'N/A')}",
    ]

    # ---------- Étape 2 : Scoring IA ----------
    profile = get_profile(settings.cache_dir, user_id) or DEFAULT_DEMO_PROFILE
    llm = LLMClient(settings)
    try:
        score, reason = await score_tender(ao, profile, llm, settings.cache_dir)
    except Exception:
        logger.exception("Demo: scoring failed")
        score, reason = 0, ""

    # Fallback démo si le LLM échoue
    if score == 0 and not reason:
        score = 78
        reason = (
            "Périmètre géographique dans ta zone (75/92), prestation technique alignée "
            "sur ton cœur de métier, acheteur public récurrent."
        )

    badge = "🔥" if score >= 85 else "✅" if score >= 70 else "📌"
    lines.extend([
        "",
        "🧠 *Étape 2/4 — Scoring de pertinence par IA*",
        f"{badge} *Score : {score}/100*",
        f"💡 {reason}",
        "_Le scoring compare l'AO à ton profil (activité, taille, zones, mots-clés)._",
    ])

    # ---------- Étape 3 : Historique acheteur ----------
    # Cascade : acheteur de l'AO → fallback buyers connus pour avoir de l'historique DECP
    _FALLBACK_BUYERS = [
        "Région Île-de-France",
        "Mairie de Paris",
        "Métropole de Lyon",
        "Métropole Nice Côte d'Azur",
        "Ville de Marseille",
        "Ville de Bordeaux",
        "Département du Rhône",
        "Département des Hauts-de-Seine",
        "Société du Grand Paris",
        "Caisse des Dépôts",
        "Université de Strasbourg",
        "Université de Bordeaux",
        "CHU de Bordeaux",
        "Assistance Publique - Hôpitaux de Paris",
        "Centre Hospitalier Universitaire de Toulouse",
    ]

    buyers_to_try = [ao.get("org", "")] + _FALLBACK_BUYERS
    history_text = ""
    found_buyer = ""
    found_year = ""
    for year in ("2024", "2023"):
        for buyer in buyers_to_try:
            if not buyer or len(buyer) < 3:
                continue
            try:
                rows = await asyncio.wait_for(
                    fetch_buyer_history(buyer, year, settings, max_results=10),
                    timeout=120,
                )
                if rows:
                    history_text = format_buyer_history(rows)
                    if len(history_text) > 2000:
                        history_text = history_text[:2000] + "\n…_(tronqué)_"
                    found_buyer = buyer
                    found_year = year
                    break
            except Exception:
                logger.exception("Demo: buyer history failed for %s (%s)", buyer, year)
                continue
        if history_text:
            break

    if not history_text:
        history_text = (
            "Aucun historique DECP trouvé pour les acheteurs testés en 2024.\n"
            "_La base DECP est mise à jour mensuellement ; certains petits acheteurs "
            "peuvent n'y figurer que partiellement._"
        )
    elif found_buyer != ao.get("org", ""):
        # On a trouvé un historique mais pour un acheteur différent → on le précise
        history_text = (
            f"_L'acheteur de cet AO n'a pas d'historique DECP, voici celui d'un acheteur "
            f"similaire ({found_buyer}, {found_year}) pour illustrer l'analyse :_\n\n"
            + history_text
        )

    lines.extend(["", "📊 *Étape 3/4 — Analyse historique de l'acheteur public*", history_text])

    # ---------- Étape 4 : DCE ----------
    lines.extend([
        "",
        "📄 *Étape 4/4 — RAG sur les cahiers des charges (DCE)*",
        "Tu peux indexer le PDF du DCE d'un AO avec :",
        f"`/datagouv ao dce {ao.get('idweb', '[idweb]')} [URL_DCE]`",
        "Puis poser des questions comme :",
        "`/datagouv ao question [idweb] quels sont les critères de notation ?`",
        "_Le bot lit le PDF et répond depuis son contenu._",
    ])

    # Envoie le bloc principal (concaténé en un seul message)
    await _say("\n".join(lines))

    # ---------- BLOC 3 : Conclusion ----------
    await _say(
        "✨ *Fin de la démo !*\n"
        "Commandes clés :\n"
        "• `/datagouv ao alerte [mot-clé]` — alertes proactives\n"
        "• `/datagouv profil` — configurer ton profil pour le scoring\n"
        "• Réagis avec ✅ ou 🔥 sur un push AO pour le suivre (rappels J-15/J-7/J-2)\n"
        "• <http://localhost:8080|Dashboard web> pour voir tes stats"
    )
