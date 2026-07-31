"""Analyse des données de consommation électrique par région (SDES/ODRE)."""

import asyncio
import csv
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SECTOR_MAP: dict[str, str] = {
    "A": "Agriculture",
    "I": "Industrie",
    "R": "Résidentiel",
    "T": "Tertiaire",
    "X": "Non affecté",
}

# Dataset SDES : Données locales de consommation d'électricité et de gaz naturel - région
ELECTRICITY_CSV_URL = (
    "https://data.statistiques.developpement-durable.gouv.fr/"
    "dido/api/v1/datafiles/e841c15d-03e0-4289-b336-8c97b2d3a594/csv"
)


def _get_cache_path(cache_dir: str) -> Path:
    path = Path(cache_dir) / "energy"
    path.mkdir(parents=True, exist_ok=True)
    return path / "consommation_electricite_region.csv"


async def _download_csv(cache_dir: str, force: bool = False) -> Path:
    """Télécharge le CSV SDES s'il n'est pas en cache (TTL 7 jours)."""
    path = _get_cache_path(cache_dir)
    if not force and await asyncio.to_thread(path.exists):
        cache_stat = await asyncio.to_thread(Path(cache_dir).stat)
        path_stat = await asyncio.to_thread(path.stat)
        age_days = (cache_stat.st_mtime - path_stat.st_mtime) / 86400
        if age_days < 7:
            return path

    logger.info("Téléchargement du CSV consommation électrique SDES…")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(ELECTRICITY_CSV_URL)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    logger.info("CSV téléchargé (%s octets)", path.stat().st_size)
    return path


def _parse_csv(path: Path, year: str) -> list[dict[str, Any]]:
    """Lit le CSV et retourne les lignes pour l'année demandée (électricité uniquement)."""
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("ANNEE") == year and row.get("FILIERE") == "E":
                try:
                    conso = float(row.get("CONSO", "0").replace(",", ".") or 0)
                except ValueError:
                    conso = 0.0
                rows.append(
                    {
                        "region": row.get("CODE_REGION_LIBELLE", "Inconnu"),
                        "conso_mwh": conso,
                        "category": row.get("CODE_CATEGORIE_CONSOMMATION", ""),
                        "sector": row.get("CODE_GRAND_SECTEUR", ""),
                    }
                )
    return rows


def _aggregate(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, float], dict[str, float], dict[str, dict[str, float]]]:
    """Agrège la consommation par région et par secteur."""
    region_totals: dict[str, float] = {}
    sector_totals: dict[str, float] = {}
    region_sectors: dict[str, dict[str, float]] = {}

    for r in rows:
        region = r["region"]
        if not region or region.lower() in ("région fictive", "inconnu", ""):
            continue
        conso = r["conso_mwh"]
        sector_code = r.get("sector", "X") or "X"
        sector = _SECTOR_MAP.get(sector_code, "Autres")

        region_totals[region] = region_totals.get(region, 0.0) + conso
        sector_totals[sector] = sector_totals.get(sector, 0.0) + conso
        if region not in region_sectors:
            region_sectors[region] = {}
        region_sectors[region][sector] = region_sectors[region].get(sector, 0.0) + conso

    return region_totals, sector_totals, region_sectors


def _format_table(
    region_totals: dict[str, float],
    sector_totals: dict[str, float],
    region_sectors: dict[str, dict[str, float]],
    year: str,
) -> str:
    """Formate un tableau Slack avec les consommations par région + répartition sectorielle."""
    sorted_regions = sorted(region_totals.items(), key=lambda x: x[1], reverse=True)
    total = sum(region_totals.values())

    lines = [
        f"⚡ *Consommation électrique par région — {year}*",
        "Source : SDES / ODRE (Données locales de consommation d'électricité)",
        f"Total France : *{total / 1_000_000:.1f} TWh*\n",
        "| Région | Consommation (TWh) | Part | Secteur dominant |",
        "|:---|---:|---:|:---|",
    ]

    for region, mwh in sorted_regions:
        twh = mwh / 1_000_000
        pct = (mwh / total * 100) if total else 0
        sectors = region_sectors.get(region, {})
        top_sector = max(sectors, key=sectors.get) if sectors else "—"
        lines.append(f"| {region} | {twh:.1f} | {pct:.1f}% | {top_sector} |")

    # Répartition nationale par secteur
    lines.append("\n*Répartition nationale par secteur d'usage*")
    for sector, mwh in sorted(sector_totals.items(), key=lambda x: x[1], reverse=True):
        pct = (mwh / total * 100) if total else 0
        lines.append(f"• {sector} : {mwh / 1_000_000:.1f} TWh ({pct:.1f}%)")

    return "\n".join(lines)


async def get_electricity_consumption_by_region(year: str, cache_dir: str) -> str:
    """
    Récupère et formate la consommation électrique par région pour une année donnée.
    Retourne un tableau Slack markdown incluant la répartition sectorielle.
    """
    try:
        path = await _download_csv(cache_dir)
        rows = _parse_csv(path, year)
        if not rows:
            return (
                f"Aucune donnée trouvée pour l'année {year} dans le dataset SDES.\n"
                "Années disponibles : 2018 à 2024."
            )
        region_totals, sector_totals, region_sectors = _aggregate(rows)
        return _format_table(region_totals, sector_totals, region_sectors, year)
    except Exception:
        logger.exception("Erreur analyse consommation électrique")
        return "ERREUR : impossible de charger les données de consommation électrique."
