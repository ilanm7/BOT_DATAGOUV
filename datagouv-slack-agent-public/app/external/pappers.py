"""Client API Pappers — recherche entreprise, dirigeants, bilans, liens."""

import logging
from typing import Any

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.pappers.fr/v2"
_TABLE = "pappers_cache"


class PappersClient(ExternalAPIClient):
    def __init__(self, api_key: str, cache_dir: str):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        super().__init__(cache_dir, _BASE_URL, _TABLE, headers=headers, ttl_days=30)
        self.api_key = api_key

    async def search_company(self, query: str, limit: int = 5) -> list[dict]:
        """Recherche une entreprise par nom ou SIREN."""
        if not self.api_key:
            return []
        data = await self._aget(
            "/recherche",
            params={"q": query, "api_token": self.api_key, "par_page": limit},
        )
        if not data:
            return []
        return data.get("resultats", [])

    async def get_company(self, siren: str) -> dict[str, Any] | None:
        """Récupère le détail d'une entreprise par SIREN."""
        if not self.api_key or len(siren) != 9:
            return None
        data = await self._aget(
            "/entreprise",
            params={"siren": siren, "api_token": self.api_key},
        )
        return data

    async def get_beneficiaires(self, siren: str) -> list[dict]:
        """Bénéficiaires effectifs (dirigeants)."""
        if not self.api_key or len(siren) != 9:
            return []
        data = await self._aget(
            "/beneficiaires_effectifs",
            params={"siren": siren, "api_token": self.api_key},
        )
        if not data:
            return []
        return data.get("resultats", [])

    async def get_bilan(self, siren: str) -> dict[str, Any] | None:
        """Dernier bilan comptable disponible."""
        if not self.api_key or len(siren) != 9:
            return None
        data = await self._aget(
            "/entreprise",
            params={"siren": siren, "api_token": self.api_key, "bilan": "true"},
        )
        if not data:
            return None
        bilans = data.get("bilans", [])
        return bilans[0] if bilans else None


def format_pappers_result(raw: dict[str, Any]) -> str:
    """Formate un résultat Pappers en texte Slack compact."""
    if not raw:
        return ""
    nom = raw.get("nom_entreprise") or raw.get("denomination") or "Inconnu"
    siren = raw.get("siren", "")
    siege = raw.get("siege", {}) or {}
    adresse = f"{siege.get('adresse_ligne_1', '')} {siege.get('code_postal', '')} {siege.get('ville', '')}".strip()
    effectif = raw.get("effectif", "Inconnu")
    ca = raw.get("chiffre_affaires", "")
    ca_str = f"{ca:,.0f} €".replace(",", " ") if isinstance(ca, (int, float)) and ca else "N/A"
    date_creation = raw.get("date_immatriculation", "")

    lines = [
        f"🏢 *{nom}* (SIREN {siren})",
        f"• Adresse : {adresse or 'N/A'}",
        f"• Effectif : {effectif}",
        f"• Chiffre d'affaires : {ca_str}",
        f"• Immatriculation : {date_creation}",
    ]
    return "\n".join(lines)
