"""Client API Legifrance (Piste) — recherche dans les codes, lois, décrets."""

import logging
from typing import Any

import httpx

from app.external.base import ExternalAPIClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
_TABLE = "legifrance_cache"

_TOKEN_URL = "https://oauth.piste.gouv.fr/api/oauth/token"


class LegifranceClient(ExternalAPIClient):
    """Client Legifrance via l'API Piste.
    Nécessite un compte gratuit sur https://www.piste.gouv.fr
    """

    def __init__(self, client_id: str, client_secret: str, cache_dir: str):
        super().__init__(cache_dir, _BASE_URL, _TABLE, ttl_days=30)
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None

    async def _get_token(self) -> str | None:
        if self._token:
            return self._token
        if not self.client_id or not self.client_secret:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "openid",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._token = data.get("access_token")
        except Exception:
            logger.exception("Échec auth Legifrance")
            return None
        return self._token

    async def search_code(self, query: str, code_name: str = "Code des marchés publics", limit: int = 5) -> list[dict]:
        """Recherche un article dans un code (ex: 'Code des marchés publics')."""
        token = await self._get_token()
        if not token:
            return []
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {
            "recherche": {
                "champ": query,
                "pageSize": limit,
                "pageNumber": 1,
                "typePagination": "DEFAUT",
            },
            "fond": "CODE",
            "filtres": [{"name": "nomCode", "value": code_name}],
        }
        cache_key = self._cache_key("/search", {"q": query, "code": code_name})
        cached = _get_cached(self.cache_dir, self.table_name, cache_key, self.ttl_days)
        if cached is not None:
            return cached.get("results", [])

        try:
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                resp = await client.post(f"{self.base_url}/search", json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Erreur recherche Legifrance")
            return []

        results = data.get("results", []) if isinstance(data, dict) else []
        _save_cache(self.cache_dir, self.table_name, cache_key, {"results": results})
        return results


def format_legifrance_result(r: dict[str, Any]) -> str:
    if not r:
        return ""
    titre = r.get("titre", "Article")
    num = r.get("num", "")
    texte = r.get("texte", "")[:300]
    code = r.get("code", "")
    url = f"https://www.legifrance.gouv.fr/codes/article_lc/{r.get('id', '')}"
    lines = [f"📜 *{titre}* {num}", f"• Code : {code}", f"• Extrait : {texte}..."]
    if r.get("id"):
        lines.append(f"• <{url}|Voir sur Légifrance>")
    return "\n".join(lines)


from app.external.base import _get_cached, _save_cache  # noqa: E402
