"""Client API France Travail (Offres d'emploi + BMO)."""

import logging
from typing import Any

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2"
_BMO_URL = "https://api.francetravail.io/partenaire/bmo/v1"
_TABLE = "france_travail_cache"


class FranceTravailClient(ExternalAPIClient):
    """Client pour l'API France Travail (offres d'emploi).
    Nécessite un token OAuth2 si on veut les données détaillées ;
    en mode public on utilise le moteur de recherche simplifié.
    """

    def __init__(self, cache_dir: str):
        super().__init__(cache_dir, _BASE_URL, _TABLE, ttl_days=1)

    async def search_jobs(self, query: str, code_commune: str = "", limit: int = 5) -> list[dict]:
        """Recherche d'offres d'emploi."""
        params: dict[str, Any] = {"motsCles": query, "range": f"0-{limit - 1}"}
        if code_commune:
            params["commune"] = code_commune
        data = await self._aget("/offres/search", params=params)
        if not data:
            return []
        return data.get("resultats", [])


def format_job(job: dict[str, Any]) -> str:
    """Formate une offre d'emploi en texte Slack."""
    if not job:
        return ""
    lines = [
        f"💼 *{job.get('intitule', 'Offre')}*",
        f"• Entreprise : {job.get('entreprise', {}).get('nom', 'N/A')}",
        f"• Lieu : {job.get('lieuTravail', {}).get('libelle', 'N/A')}",
        f"• Contrat : {job.get('typeContrat', '')} — {job.get('natureContrat', '')}",
        f"• Salaire : {job.get('salaire', {}).get('libelle', 'N/A')}",
    ]
    return "\n".join(lines)
