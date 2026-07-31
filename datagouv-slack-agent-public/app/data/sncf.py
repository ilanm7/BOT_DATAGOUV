"""Analyse de la ponctualité TGV SNCF (régularité mensuelle par liaison)."""

import asyncio
import csv
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SNCF_CSV_URL = (
    "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/"
    "regularite-mensuelle-tgv-aqst/exports/csv?use_labels="
)


def _get_cache_path(cache_dir: str) -> Path:
    path = Path(cache_dir) / "sncf"
    path.mkdir(parents=True, exist_ok=True)
    return path / "regularite_tgv.csv"


async def _download_csv(cache_dir: str, force: bool = False) -> Path:
    """Télécharge le CSV SNCF s'il n'est pas en cache (TTL 1 jour)."""
    path = _get_cache_path(cache_dir)
    if not force and await asyncio.to_thread(path.exists):
        cache_stat = await asyncio.to_thread(Path(cache_dir).stat)
        path_stat = await asyncio.to_thread(path.stat)
        age_days = (cache_stat.st_mtime - path_stat.st_mtime) / 86400
        if age_days < 1:
            return path

    logger.info("Téléchargement du CSV ponctualité SNCF…")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(_SNCF_CSV_URL)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    logger.info("CSV SNCF téléchargé (%s octets)", path.stat().st_size)
    return path


def _safe_float(val: str | None) -> float:
    try:
        return float((val or "").replace(",", ".").strip() or 0)
    except ValueError:
        return 0.0


def _safe_int(val: str | None) -> int:
    try:
        return int((val or "").strip() or 0)
    except ValueError:
        return 0


def _parse_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            rows.append(
                {
                    "date": row.get("Date", "").strip(),
                    "service": row.get("Service", "").strip(),
                    "depart": row.get("Gare de départ", "").strip(),
                    "arrivee": row.get("Gare d'arrivée", "").strip(),
                    "duree": _safe_int(row.get("Durée moyenne du trajet")),
                    "prevus": _safe_int(row.get("Nombre de circulations prévues")),
                    "annules": _safe_int(row.get("Nombre de trains annulés")),
                    "retard_depart": _safe_float(row.get("Retard moyen de tous les trains au départ")),
                    "retard_arrivee": _safe_float(row.get("Retard moyen de tous les trains à l'arrivée")),
                    "retard15": _safe_int(row.get("Nombre trains en retard > 15min")),
                    "retard30": _safe_int(row.get("Nombre trains en retard > 30min")),
                    "retard60": _safe_int(row.get("Nombre trains en retard > 60min")),
                    "pct_externes": _safe_float(row.get("Prct retard pour causes externes")),
                    "pct_infra": _safe_float(row.get("Prct retard pour cause infrastructure")),
                    "pct_trafic": _safe_float(row.get("Prct retard pour cause gestion trafic")),
                    "pct_materiel": _safe_float(row.get("Prct retard pour cause matériel roulant")),
                    "pct_gare": _safe_float(row.get("Prct retard pour cause gestion en gare et réutilisation de matériel")),
                    "pct_voyageurs": _safe_float(row.get("Prct retard pour cause prise en compte voyageurs (affluence, gestions PSH, correspondances)")),
                }
            )
    return rows


def _summarize(
    rows: list[dict[str, Any]],
    depart_filter: str | None = None,
    arrivee_filter: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Agrège les données ponctualité."""
    filtered = rows
    if depart_filter:
        dlow = depart_filter.lower()
        filtered = [r for r in filtered if dlow in r["depart"].lower()]
    if arrivee_filter:
        alow = arrivee_filter.lower()
        filtered = [r for r in filtered if alow in r["arrivee"].lower()]

    if not filtered:
        return {"count": 0}

    total_mois = len(filtered)
    total_prevus = sum(r["prevus"] for r in filtered)
    total_annules = sum(r["annules"] for r in filtered)
    avg_retard_dep = sum(r["retard_depart"] for r in filtered) / total_mois if total_mois else 0
    avg_retard_arr = sum(r["retard_arrivee"] for r in filtered) / total_mois if total_mois else 0
    total_r15 = sum(r["retard15"] for r in filtered)
    total_r30 = sum(r["retard30"] for r in filtered)
    total_r60 = sum(r["retard60"] for r in filtered)

    # Causes moyennes (pondérées par nombre de retards >15min si possible, sinon moyenne simple)
    def _avg_cause(key: str) -> float:
        vals = [r[key] for r in filtered if r[key] > 0]
        return sum(vals) / len(vals) if vals else 0.0

    causes = {
        "Externes": _avg_cause("pct_externes"),
        "Infrastructure": _avg_cause("pct_infra"),
        "Gestion trafic": _avg_cause("pct_trafic"),
        "Matériel roulant": _avg_cause("pct_materiel"),
        "Gestion en gare": _avg_cause("pct_gare"),
        "Prise en compte voyageurs": _avg_cause("pct_voyageurs"),
    }

    # Agrégation par liaison (dernier mois disponible ou moyenne globale)
    liaison_stats: dict[tuple[str, str], dict[str, Any]] = {}
    for r in filtered:
        key = (r["depart"], r["arrivee"])
        if key not in liaison_stats:
            liaison_stats[key] = {
                "mois": 0,
                "prevus": 0,
                "annules": 0,
                "retard_arr_sum": 0.0,
                "retard15": 0,
                "retard30": 0,
                "retard60": 0,
            }
        liaison_stats[key]["mois"] += 1
        liaison_stats[key]["prevus"] += r["prevus"]
        liaison_stats[key]["annules"] += r["annules"]
        liaison_stats[key]["retard_arr_sum"] += r["retard_arrivee"]
        liaison_stats[key]["retard15"] += r["retard15"]
        liaison_stats[key]["retard30"] += r["retard30"]
        liaison_stats[key]["retard60"] += r["retard60"]

    liaison_list = []
    for (dep, arr), s in liaison_stats.items():
        liaison_list.append(
            {
                "depart": dep,
                "arrivee": arr,
                "mois": s["mois"],
                "prevus": s["prevus"],
                "annules": s["annules"],
                "retard_arr": round(s["retard_arr_sum"] / s["mois"], 1) if s["mois"] else 0,
                "retard15": s["retard15"],
                "retard30": s["retard30"],
                "retard60": s["retard60"],
            }
        )

    # Top retards
    top_retards = sorted(liaison_list, key=lambda x: x["retard_arr"], reverse=True)[:top_n]
    # Top annulations
    top_annulations = sorted(liaison_list, key=lambda x: x["annules"] / (x["prevus"] or 1), reverse=True)[:top_n]

    return {
        "count": total_mois,
        "total_prevus": total_prevus,
        "total_annules": total_annules,
        "avg_retard_dep": round(avg_retard_dep, 1),
        "avg_retard_arr": round(avg_retard_arr, 1),
        "total_r15": total_r15,
        "total_r30": total_r30,
        "total_r60": total_r60,
        "causes": causes,
        "liaisons": liaison_list,
        "top_retards": top_retards,
        "top_annulations": top_annulations,
        "depart_filter": depart_filter,
        "arrivee_filter": arrivee_filter,
    }


def _format_summary(data: dict[str, Any]) -> str:
    if data.get("count", 0) == 0:
        return "Aucune donnée de ponctualité trouvée pour ces critères."

    dep = data.get("depart_filter") or "Toutes gares"
    arr = data.get("arrivee_filter") or "Toutes gares"
    lines = [
        f"🚄 *Ponctualité TGV SNCF — {dep} → {arr}*",
        "Source : SNCF Open Data (régularité mensuelle par liaison)",
        "",
        f"• Enregistrements mensuels analysés : *{data['count']:,}*",
        f"• Circulations prévues (cumul) : *{data['total_prevus']:,}*",
        f"• Trains annulés : *{data['total_annules']:,}* ({data['total_annules'] / data['total_prevus'] * 100:.1f} %)",
        f"• Retard moyen au départ : *{data['avg_retard_dep']} min*",
        f"• Retard moyen à l'arrivée : *{data['avg_retard_arr']} min*",
        f"• Trains > 15 min : *{data['total_r15']:,}* | > 30 min : *{data['total_r30']:,}* | > 60 min : *{data['total_r60']:,}*",
        "",
        "*Principales causes de retard (moyenne)* :",
    ]
    for cause, pct in sorted(data["causes"].items(), key=lambda x: x[1], reverse=True):
        if pct > 0:
            lines.append(f"• {cause} : {pct:.1f} %")

    lines.append("")
    lines.append("*Liaisons les plus en retard (top 10)* :")
    lines.append("| Liaison | Mois | Prévus | Retard moyen arr. | >15 min | >30 min | >60 min |")
    lines.append("|:---|---:|---:|---:|---:|---:|---:|")
    for lr in data["top_retards"]:
        lines.append(
            f"| {lr['depart']} → {lr['arrivee']} | {lr['mois']} | {lr['prevus']:,} | {lr['retard_arr']} min | "
            f"{lr['retard15']:,} | {lr['retard30']:,} | {lr['retard60']:,} |"
        )

    lines.append("")
    lines.append("*Liaisons avec le plus d'annulations (top 10)* :")
    lines.append("| Liaison | Mois | Prévus | Annulés | Taux |")
    lines.append("|:---|---:|---:|---:|---:|")
    for la in data["top_annulations"]:
        taux = la["annules"] / (la["prevus"] or 1) * 100
        lines.append(f"| {la['depart']} → {la['arrivee']} | {la['mois']} | {la['prevus']:,} | {la['annules']:,} | {taux:.1f} % |")

    return "\n".join(lines)


async def get_sncf_punctuality(depart: str | None, arrivee: str | None, cache_dir: str) -> str:
    """Récupère et formate la ponctualité TGV SNCF."""
    try:
        path = await _download_csv(cache_dir)
        rows = _parse_csv(path)
        data = _summarize(rows, depart_filter=depart, arrivee_filter=arrivee)
        return _format_summary(data)
    except Exception:
        logger.exception("Erreur analyse ponctualité SNCF")
        return "ERREUR : impossible de charger les données de ponctualité SNCF."
