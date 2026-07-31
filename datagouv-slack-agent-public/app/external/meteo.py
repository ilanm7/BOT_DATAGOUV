"""Client API Météo France — prévisions et historique."""

import logging
from typing import Any

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.meteofrance.com/api"
_TABLE = "meteo_cache"


class MeteoClient(ExternalAPIClient):
    """Client Météo France. Pas d'authentification pour l'API publique de prévisions."""

    def __init__(self, cache_dir: str):
        super().__init__(cache_dir, _BASE_URL, _TABLE, ttl_days=1)

    async def get_forecast(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Prévisions sur 5 jours pour un point GPS."""
        data = await self._aget("/forecast", params={"lat": lat, "lon": lon})
        return data

    async def get_rain(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Prévisions de pluie à court terme (1h)."""
        data = await self._aget("/rain", params={"lat": lat, "lon": lon})
        return data


def format_forecast(data: dict[str, Any] | None) -> str:
    if not data:
        return "Données météo indisponibles."
    city = data.get("city", {}).get("name", "")
    forecast = data.get("forecast", [])
    lines = [f"🌤️ *Prévisions Météo France — {city}*"]
    for day in forecast[:3]:
        date = day.get("datetime", "")[:10]
        temp_min = day.get("tmin", "N/A")
        temp_max = day.get("tmax", "N/A")
        probarain = day.get("probarain", 0)
        weather = day.get("weather", "")
        lines.append(f"• {date} : {temp_min}°C → {temp_max}°C, pluie {probarain}%, temps {weather}")
    return "\n".join(lines)
