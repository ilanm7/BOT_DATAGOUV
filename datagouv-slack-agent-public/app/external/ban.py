"""Client Base Adresse Nationale (BAN) — géocodage et recherche d'adresses."""

import logging
from typing import Any

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api-adresse.data.gouv.fr"
_TABLE = "ban_cache"


class BANClient(ExternalAPIClient):
    def __init__(self, cache_dir: str):
        super().__init__(cache_dir, _BASE_URL, _TABLE, ttl_days=30)

    async def geocode(self, address: str, limit: int = 1) -> list[dict]:
        """Géocode une adresse (retourne lat/lon + score)."""
        if not address:
            return []
        data = await self._aget("/search", params={"q": address, "limit": limit})
        if not data:
            return []
        return data.get("features", [])

    async def reverse_geocode(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Adresse depuis des coordonnées GPS."""
        data = await self._aget("/reverse", params={"lat": lat, "lon": lon})
        if not data:
            return None
        features = data.get("features", [])
        return features[0] if features else None


def format_ban_feature(feature: dict[str, Any]) -> str:
    """Formate un résultat BAN en texte lisible."""
    if not feature:
        return ""
    props = feature.get("properties", {})
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [])
    lines = [
        f"📍 {props.get('label', '')}",
        f"• Commune : {props.get('city', '')} ({props.get('postcode', '')})",
    ]
    if len(coords) >= 2:
        lines.append(f"• Coordonnées : {coords[1]}, {coords[0]}")
    lines.append(f"• Score BAN : {props.get('score', 'N/A')}")
    return "\n".join(lines)
