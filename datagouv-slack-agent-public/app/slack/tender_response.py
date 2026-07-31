"""Génération de trame de réponse à l'AO (mémoire technique squelette)."""

import logging
from typing import Any

from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

_RESPONSE_SKELETON_PROMPT = """Tu es un consultant senior en réponse aux marchés publics français.
Génère un squelette de mémoire technique pour répondre à l'appel d'offre ci-dessous.

CONTEXTE DE L'APPEL D'OFFRE :
- Objet : {objet}
- Acheteur public : {org}
- Descripteurs : {descripteurs}
- Départements : {depts}

PROFIL DU CANDIDAT :
- Activité principale : {activity}
- Taille de l'entreprise : {company_size}
- Secteurs d'expertise : {sectors}
- Zones géographiques : {zones}
- Mots-clés métier : {keywords}

INSTRUCTIONS STRICTES :
1. Rédige un plan structuré de 8 à 12 sections type mémoire technique.
2. Pour chaque section, donne 2 à 3 bullets points expliquant ce qu'il faut y développer.
3. Insère des placeholders du type [INSÉRER RÉFÉRENCE CLIENT] là où le candidat doit personnaliser avec ses propres références, certifs ou CV.
4. Adapte le ton au profil : PME = agilité, proximité, réactivité ; ETI/GE = structuration, méthodologie, gouvernance.
5. Ne dépasse pas 4000 caractères. Sois concis et actionnable.
6. Termine OBLIGATOIREMENT par ce disclaimer exact :

⚠️ *Cette trame est indicative et générée automatiquement. Elle ne remplace pas l'analyse complète du DCE ni l'expertise métier d'un consultant en réponse aux marchés publics.*

Réponds en français, format Slack mrkdwn (*gras*, • bullets)."""


async def generate_response_skeleton(
    ao: dict[str, Any],
    profile: dict[str, Any],
    llm: LLMClient,
) -> str:
    """Génère un squelette de mémoire technique à partir d'un AO et d'un profil."""
    def _fmt(val: Any) -> str:
        if isinstance(val, list):
            return ", ".join(str(v) for v in val if v) or "Non précisé"
        return str(val) if val else "Non précisé"

    prompt = _RESPONSE_SKELETON_PROMPT.format(
        objet=_fmt(ao.get("objet")),
        org=_fmt(ao.get("org")),
        descripteurs=_fmt(ao.get("descripteurs")),
        depts=_fmt(ao.get("departements")),
        activity=_fmt(profile.get("activity")),
        company_size=_fmt(profile.get("company_size")),
        sectors=_fmt(profile.get("sectors")),
        zones=_fmt(profile.get("zones")),
        keywords=_fmt(profile.get("keywords")),
    )

    try:
        msg = await llm.chat(
            messages=[
                {"role": "system", "content": "Tu rédiges des mémoires techniques pour les marchés publics français."},
                {"role": "user", "content": prompt},
            ]
        )
        text = (msg.content or "").strip()
        if not text:
            return "❌ Je n'ai pas pu générer de trame. Peux-tu reformuler ou préciser ton profil avec `/datagouv profil` ?"
        return text
    except Exception:
        logger.exception("Erreur génération trame mémoire technique")
        return "❌ Désolé, la génération de la trame a échoué. Réessaie dans un instant."
