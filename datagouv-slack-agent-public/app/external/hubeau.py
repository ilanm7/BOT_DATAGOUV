"""Client API Hub'Eau — eaux souterraines, qualité, piézométrie."""

import logging
from datetime import datetime, timedelta
from typing import Any

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://hubeau.eaufrance.fr/api/v1"
_TABLE = "hubeau_cache"


class HubEauClient(ExternalAPIClient):
    def __init__(self, cache_dir: str):
        super().__init__(cache_dir, _BASE_URL, _TABLE, ttl_days=1)

    async def get_nappes_latest(
        self,
        bbox: str | None = None,
        code_bss: str | None = None,
        days: int = 30,
    ) -> list[dict]:
        """Dernières mesures de piézométrie (nappes phréatiques).
        bbox format : 'min_lon,min_lat,max_lon,max_lat'
        """
        date_fin = datetime.now().strftime("%Y-%m-%d")
        date_debut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        params: dict[str, Any] = {
            "date_debut_mesure": date_debut,
            "date_fin_mesure": date_fin,
            "size": 20,
        }
        if bbox:
            params["bbox"] = bbox
        if code_bss:
            params["code_bss"] = code_bss

        data = await self._aget("/niveaux_nappes/chroniques", params=params)
        if not data:
            return []
        return data.get("data", [])

    async def get_qualite_eau(
        self,
        commune: str = "",
        bbox: str | None = None,
    ) -> list[dict]:
        """Dernières analyses de qualité des eaux superficielles."""
        params: dict[str, Any] = {"size": 20}
        if commune:
            params["nom_commune"] = commune
        if bbox:
            params["bbox"] = bbox
        data = await self._aget("/qualite_eau_surface/analyses", params=params)
        if not data:
            return []
        return data.get("data", [])


def format_piezo(mesure: dict[str, Any]) -> str:
    if not mesure:
        return ""
    return (
        f"• {mesure.get('nom_commune', 'N/A')} — "
        f"Nappe {mesure.get('code_bss', '')}: "
        f"{mesure.get('niveau_nappe_eau', 'N/A')} m "
        f"({mesure.get('date_mesure', '')})"
    )
