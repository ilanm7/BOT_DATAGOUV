"""Analyse de la Surface Agricole Utile (SAU) par région (Ecolab / Agreste)."""

import asyncio
import csv
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SAU_REGION_URL = (
    "https://static.data.gouv.fr/resources/surface-agricole-utile-totale/"
    "20260128-162206/surface-agricole-utile-totale-region.csv"
)


def _get_cache_path(cache_dir: str) -> Path:
    path = Path(cache_dir) / "sau"
    path.mkdir(parents=True, exist_ok=True)
    return path / "sau_region.csv"


async def _download_csv(cache_dir: str, force: bool = False) -> Path:
    path = _get_cache_path(cache_dir)
    if not force and await asyncio.to_thread(path.exists):
        cache_stat = await asyncio.to_thread(Path(cache_dir).stat)
        path_stat = await asyncio.to_thread(path.stat)
        age_days = (cache_stat.st_mtime - path_stat.st_mtime) / 86400
        if age_days < 7:
            return path

    logger.info("Téléchargement du CSV SAU régional…")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(_SAU_REGION_URL)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    logger.info("CSV SAU téléchargé (%s octets)", path.stat().st_size)
    return path


def _parse_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=",")
        for row in reader:
            try:
                valeur = float(row.get("valeur", "0") or 0)
            except ValueError:
                valeur = 0.0
            date_str = row.get("date_mesure", "").strip()
            # Extraire l'année
            annee = date_str[:4] if len(date_str) >= 4 else "Inconnu"
            rows.append(
                {
                    "annee": annee,
                    "region_code": row.get("geocode_region", "").strip(),
                    "region": row.get("libelle_region", "").strip(),
                    "valeur_ha": valeur,
                }
            )
    return rows


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrège par région en prenant la valeur de la dernière année disponible."""
    # Dictionnaire région -> {dernière_année, valeur}
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        reg = r["region"]
        annee = r["annee"]
        if not reg or not annee.isdigit():
            continue
        if reg not in latest or int(annee) > int(latest[reg]["annee"]):
            latest[reg] = {"annee": annee, "valeur": r["valeur_ha"], "code": r["region_code"]}

    if not latest:
        return {"count": 0}

    total = sum(v["valeur"] for v in latest.values())
    sorted_regions = sorted(latest.items(), key=lambda x: x[1]["valeur"], reverse=True)

    return {
        "count": len(latest),
        "total_ha": round(total),
        "total_kha": round(total / 1000),
        "regions": sorted_regions,
    }


def _format_summary(data: dict[str, Any]) -> str:
    if data.get("count", 0) == 0:
        return "Aucune donnée de surface agricole trouvée."

    lines = [
        "🌾 *Surface Agricole Utile (SAU) par région*",
        "Source : Ecolab / Agreste (données régionales)",
        "",
        f"• Nombre de régions : *{data['count']}*",
        f"• SAU totale France : *{data['total_kha']:,} kha* ({data['total_ha']:,.0f} ha)",
        "",
        "| Rang | Région | SAU (kha) | Part | Année |",
        "|:---|:---|---:|---:|:---|",
    ]

    for i, (region, info) in enumerate(data["regions"], start=1):
        kha = info["valeur"] / 1000
        pct = (info["valeur"] / data["total_ha"] * 100) if data["total_ha"] else 0
        lines.append(f"| {i} | {region} | {kha:,.1f} | {pct:.1f} % | {info['annee']} |")

    return "\n".join(lines)


async def get_sau_by_region(cache_dir: str) -> str:
    """Récupère et formate la SAU par région."""
    try:
        path = await _download_csv(cache_dir)
        rows = _parse_csv(path)
        data = _summarize(rows)
        return _format_summary(data)
    except Exception:
        logger.exception("Erreur analyse SAU")
        return "ERREUR : impossible de charger les données de surface agricole."
