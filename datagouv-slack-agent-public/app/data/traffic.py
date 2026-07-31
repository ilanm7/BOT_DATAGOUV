"""Analyse des données TMJA (Trafic Moyen Journalier Annuel) du réseau routier national."""

import asyncio
import csv
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Mapping départements -> région (simplifié pour les zones les plus demandées)
_DEPT_TO_REGION: dict[str, str] = {
    **dict.fromkeys(["75", "77", "78", "91", "92", "93", "94", "95"], "Île-de-France"),
    **dict.fromkeys(["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"], "Auvergne-Rhône-Alpes"),
    **dict.fromkeys(["02", "59", "60", "62", "80"], "Hauts-de-France"),
    **dict.fromkeys(["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"], "Grand Est"),
    **dict.fromkeys(["14", "27", "50", "61", "76"], "Normandie"),
    **dict.fromkeys(["22", "29", "35", "56"], "Bretagne"),
    **dict.fromkeys(["44", "49", "53", "72", "85"], "Pays de la Loire"),
    **dict.fromkeys(["18", "28", "36", "37", "41", "45"], "Centre-Val de Loire"),
    **dict.fromkeys(["21", "25", "39", "58", "70", "71", "89", "90"], "Bourgogne-Franche-Comté"),
    **dict.fromkeys(["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"], "Nouvelle-Aquitaine"),
    **dict.fromkeys(["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"], "Occitanie"),
    **dict.fromkeys(["04", "05", "06", "13", "83", "84"], "Provence-Alpes-Côte d'Azur"),
    **dict.fromkeys(["2A", "2B"], "Corse"),
    "97": "Outre-mer",  # simplifié
}

# URLs des CSV par année (id data.gouv constant, mais on garde l'URL latest si possible)
_TMJA_URLS: dict[str, str] = {
    "2024": (
        "https://static.data.gouv.fr/resources/trafic-moyen-journalier-annuel-sur-le-reseau-routier-national/"
        "20250818-100154/tmja-rrnc-2024.csv"
    ),
    "2023": (
        "https://static.data.gouv.fr/resources/trafic-moyen-journalier-annuel-sur-le-reseau-routier-national/"
        "20250818-095712/tmja-rrnc-2023.csv"
    ),
    "2022": (
        "https://static.data.gouv.fr/resources/trafic-moyen-journalier-annuel-sur-le-reseau-routier-national/"
        "20250818-095350/tmja-rrnc-2022.csv"
    ),
}


def _get_cache_path(cache_dir: str, year: str) -> Path:
    path = Path(cache_dir) / "traffic"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"tmja_{year}.csv"


async def _download_csv(cache_dir: str, year: str, force: bool = False) -> Path:
    """Télécharge le CSV TMJA s'il n'est pas en cache (TTL 7 jours)."""
    path = _get_cache_path(cache_dir, year)
    if not force and await asyncio.to_thread(path.exists):
        cache_stat = await asyncio.to_thread(Path(cache_dir).stat)
        path_stat = await asyncio.to_thread(path.stat)
        age_days = (cache_stat.st_mtime - path_stat.st_mtime) / 86400
        if age_days < 7:
            return path

    url = _TMJA_URLS.get(year)
    if not url:
        raise ValueError(f"Année TMJA non supportée: {year}")

    logger.info("Téléchargement du CSV TMJA %s…", year)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    logger.info("CSV TMJA %s téléchargé (%s octets)", year, path.stat().st_size)
    return path


def _parse_csv(path: Path) -> list[dict[str, Any]]:
    """Lit le CSV TMJA et retourne une liste de sections."""
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            try:
                tmja = int(row.get("TMJA", "0") or 0)
            except ValueError:
                tmja = 0
            try:
                ratio_pl = float(row.get("ratio_PL", "0").replace(",", ".") or 0)
            except ValueError:
                ratio_pl = 0.0
            try:
                longueur = float(row.get("longueur", "0").replace(",", ".") or 0)
            except ValueError:
                longueur = 0.0
            rows.append(
                {
                    "route": row.get("route", "").strip(),
                    "dept": row.get("depPrD", "").strip(),
                    "concession": row.get("concessionPrD", "").strip(),
                    "tmja": tmja,
                    "ratio_pl": ratio_pl,
                    "longueur": longueur,
                    "annee": row.get("anneeMesureTrafic", "").strip(),
                }
            )
    return rows


def _region_from_dept(dept: str) -> str:
    return _DEPT_TO_REGION.get(dept, "Autre")


def _summarize(rows: list[dict[str, Any]], region_filter: str | None = None, top_n: int = 10) -> dict[str, Any]:
    """Agrège les données TMJA par région/département et calcule les stats clés."""
    filtered = rows
    if region_filter:
        target = region_filter.lower()
        filtered = [r for r in rows if target in _region_from_dept(r["dept"]).lower() or target in r["dept"].lower()]

    if not filtered:
        return {"count": 0, "region": region_filter or "France entière"}

    total_sections = len(filtered)
    total_tmja = sum(r["tmja"] for r in filtered)
    avg_tmja = total_tmja / total_sections if total_sections else 0
    avg_pl = sum(r["ratio_pl"] for r in filtered) / total_sections if total_sections else 0

    # Sections les plus chargées
    top_sections = sorted(filtered, key=lambda x: x["tmja"], reverse=True)[:top_n]

    # Répartition concédé / non concédé
    conc = [r for r in filtered if r["concession"] == "C"]
    non_conc = [r for r in filtered if r["concession"] == "N"]
    conc_tmja = sum(r["tmja"] for r in conc)
    non_conc_tmja = sum(r["tmja"] for r in non_conc)

    # Par département
    dept_stats: dict[str, dict[str, Any]] = {}
    for r in filtered:
        d = r["dept"]
        if d not in dept_stats:
            dept_stats[d] = {"sections": 0, "tmja": 0, "longueur": 0.0}
        dept_stats[d]["sections"] += 1
        dept_stats[d]["tmja"] += r["tmja"]
        dept_stats[d]["longueur"] += r["longueur"]

    return {
        "count": total_sections,
        "region": region_filter or "France entière",
        "avg_tmja": round(avg_tmja),
        "avg_pl": round(avg_pl, 1),
        "total_tmja": total_tmja,
        "concession": {"sections": len(conc), "tmja": conc_tmja},
        "non_concession": {"sections": len(non_conc), "tmja": non_conc_tmja},
        "top_sections": top_sections,
        "dept_stats": dept_stats,
    }


def _format_summary(data: dict[str, Any], year: str) -> str:
    """Formate le résumé TMJA en markdown Slack."""
    region = data.get("region", "")
    if data.get("count", 0) == 0:
        return f"Aucune donnée TMJA trouvée pour *{region}* en {year}."

    lines = [
        f"🚗 *Trafic moyen journalier annuel (TMJA) — {region} — {year}*",
        "Source : Ministère de la Transition écologique (réseau routier national non concédé + concédé)",
        "",
        f"• Sections analysées : *{data['count']:,}*",
        f"• TMJA moyen : *{data['avg_tmja']:,}* véhicules/jour",
        f"• Taux moyen de poids lourds : *{data['avg_pl']} %*",
        f"• Concédé : {data['concession']['sections']} sections ({data['concession']['tmja']:,} véhicules/jour)",
        f"• Non concédé : {data['non_concession']['sections']} sections ({data['non_concession']['tmja']:,} véhicules/jour)",
        "",
        "*Sections les plus chargées (top 10)* :",
        "| Route | TMJA | PL % | Concédé |",
        "|:---|---:|---:|:---|",
    ]
    for sec in data["top_sections"]:
        conc = "Oui" if sec["concession"] == "C" else "Non"
        lines.append(f"| {sec['route']} | {sec['tmja']:,} | {sec['ratio_pl']} % | {conc} |")

    lines.append("")
    lines.append("*Détail par département* :")
    lines.append("| Département | Sections | TMJA cumulé | Longueur (m) |")
    lines.append("|:---|---:|---:|---:|")
    for dept, stats in sorted(data["dept_stats"].items(), key=lambda x: x[1]["tmja"], reverse=True):
        lines.append(f"| {dept} | {stats['sections']:,} | {stats['tmja']:,} | {stats['longueur']:,.0f} |")

    return "\n".join(lines)


async def get_traffic_summary(region: str | None, year: str, cache_dir: str) -> str:
    """
    Récupère et formate le résumé TMJA pour une région/département donnée.
    Si region est None, renvoie les stats France entière.
    """
    try:
        path = await _download_csv(cache_dir, year)
        rows = _parse_csv(path)
        data = _summarize(rows, region_filter=region)
        return _format_summary(data, year)
    except Exception:
        logger.exception("Erreur analyse TMJA")
        return "ERREUR : impossible de charger les données de trafic routier."
