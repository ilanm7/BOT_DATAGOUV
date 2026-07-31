"""Client OpenRouteService — distances, temps de trajet, isochrones."""

import logging
from typing import Any

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openrouteservice.org"
_TABLE = "openroute_cache"


class OpenRouteClient(ExternalAPIClient):
    def __init__(self, api_key: str, cache_dir: str):
        headers = {"Authorization": api_key} if api_key else {}
        super().__init__(cache_dir, _BASE_URL, _TABLE, headers=headers, ttl_days=30)
        self.api_key = api_key

    async def driving_distance(
        self, start_lon: float, start_lat: float, end_lon: float, end_lat: float
    ) -> dict[str, Any] | None:
        """Distance et temps de trajet en voiture entre deux points."""
        if not self.api_key:
            return None
        body = {
            "coordinates": [[start_lon, start_lat], [end_lon, end_lat]],
            "profile": "driving-car",
            "units": "km",
        }
        cache_key = self._cache_key("/v2/directions/driving-car", body)
        cached = _get_cached(self.cache_dir, self.table_name, cache_key, self.ttl_days)
        if cached is not None:
            return cached

        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                resp = await client.post(
                    f"{self.base_url}/v2/directions/driving-car",
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Échec OpenRouteService")
            return None

        _save_cache(self.cache_dir, self.table_name, cache_key, data)
        return data

    def extract_summary(self, data: dict[str, Any] | None) -> dict[str, Any]:
        """Extrait distance et durée d'une réponse directions."""
        if not data:
            return {"distance_km": None, "duration_min": None}
        routes = data.get("routes", [])
        if not routes:
            return {"distance_km": None, "duration_min": None}
        summary = routes[0].get("summary", {})
        dist = summary.get("distance", 0)
        dur = summary.get("duration", 0)
        return {
            "distance_km": round(dist / 1000, 1),
            "duration_min": round(dur / 60, 0),
        }


# Re-import pour _get_cached / _save_cache (évite circular ref à l'exécution)
from app.external.base import _get_cached, _save_cache  # noqa: E402
