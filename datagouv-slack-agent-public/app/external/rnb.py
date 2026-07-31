"""Client API RNB — Référentiel National des Bâtiments."""

import logging
from typing import Any

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://rnb-api.beta.gouv.fr/api/alpha"
_TABLE = "rnb_cache"


class RNBClient(ExternalAPIClient):
    """Client RNB. Pas d'authentification requise."""

    def __init__(self, cache_dir: str):
        super().__init__(cache_dir, _BASE_URL, _TABLE, ttl_days=30)

    async def search_building(self, address: str = "", lat: float | None = None, lon: float | None = None) -> list[dict]:
        """Recherche un bâtiment par adresse ou coordonnées GPS."""
        params: dict[str, Any] = {}
        if address:
            params["q"] = address
        if lat is not None and lon is not None:
            params["lat"] = lat
            params["lng"] = lon
        data = await self._aget("/buildings", params=params)
        if not data:
            return []
        return data.get("results", [])

    async def get_building(self, rnb_id: str) -> dict[str, Any] | None:
        """Détail d'un bâtiment par son identifiant RNB."""
        return await self._aget(f"/buildings/{rnb_id}")


def format_building(b: dict[str, Any]) -> str:
    if not b:
        return ""
    rnb_id = b.get("rnb_id", "")
    status = b.get("status", "")
    addresses = b.get("addresses", [])
    addr_str = addresses[0].get("label", "") if addresses else ""
    plots = b.get("plots", [])
    plot_str = plots[0].get("id", "") if plots else ""
    lines = [
        f"🏗️ *Bâtiment RNB {rnb_id}*",
        f"• Statut : {status}",
        f"• Adresse : {addr_str}",
    ]
    if plot_str:
        lines.append(f"• Parcelle cadastrale : {plot_str}")
    return "\n".join(lines)
