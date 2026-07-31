"""Enrichissement historique d'un acheteur public via DECP + SIRENE."""

import asyncio
import json
import logging
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.data.analyzer import query_file
from app.data.downloader import download_resource

logger = logging.getLogger(__name__)

DB_NAME = "alerts.db"
_DATASET_ID = "5cd57bf68b4c4179299eb0e9"

_TRANCHE_EFFECTIF: dict[str, str] = {
    "00": "0 salarié",
    "01": "1-2 salariés",
    "02": "3-5 salariés",
    "03": "6-9 salariés",
    "11": "10-19 salariés",
    "12": "20-49 salariés",
    "21": "50-99 salariés",
    "22": "100-199 salariés",
    "31": "200-249 salariés",
    "32": "250-499 salariés",
    "41": "500-999 salariés",
    "42": "1000-1999 salariés",
    "51": "2000-4999 salariés",
    "52": "5000-9999 salariés",
    "53": "10000+ salariés",
}


def _get_db_path(cache_dir: str) -> Path:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / DB_NAME


def _init_cache(cache_dir: str) -> None:
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buyer_history_cache (
                cache_key TEXT PRIMARY KEY,
                year TEXT,
                results TEXT,
                cached_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sirene_cache (
                siren TEXT PRIMARY KEY,
                data TEXT,
                cached_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buyer_siret_cache (
                buyer_name TEXT PRIMARY KEY,
                siret TEXT,
                cached_at INTEGER
            )
            """
        )
        conn.commit()


def _get_cached_siret(cache_dir: str, buyer_name: str, ttl_days: int = 30) -> str | None:
    _init_cache(cache_dir)
    db_path = _get_db_path(cache_dir)
    cutoff = int(time.time()) - (ttl_days * 86400)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT siret FROM buyer_siret_cache WHERE buyer_name = ? AND cached_at > ?",
            (buyer_name.lower().strip(), cutoff),
        ).fetchone()
    return row[0] if row else None


def _save_cached_siret(cache_dir: str, buyer_name: str, siret: str) -> None:
    _init_cache(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO buyer_siret_cache (buyer_name, siret, cached_at) VALUES (?, ?, ?)",
            (buyer_name.lower().strip(), siret, int(time.time())),
        )
        conn.commit()


async def _resolve_buyer_siret(buyer_name: str, cache_dir: str) -> str | None:
    """Résout un nom d'acheteur en SIRET via l'API recherche-entreprises (gratuit)."""
    cached = _get_cached_siret(cache_dir, buyer_name)
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}) as client:
            resp = await client.get(
                "https://recherche-entreprises.api.gouv.fr/search",
                params={"q": buyer_name, "page": 1, "per_page": 5},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return None
            # Prendre le premier résultat (meilleur match)
            siret = results[0].get("siege", {}).get("siret")
            if siret:
                _save_cached_siret(cache_dir, buyer_name, siret)
                return siret
    except Exception:
        logger.exception("Échec résolution SIRET pour %s", buyer_name)
    return None


def _get_cached(cache_dir: str, cache_key: str, ttl_days: int = 7) -> list[dict] | None:
    _init_cache(cache_dir)
    db_path = _get_db_path(cache_dir)
    if not db_path.exists():
        return None
    cutoff = int(time.time()) - (ttl_days * 86400)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT results FROM buyer_history_cache WHERE cache_key = ? AND cached_at > ?",
            (cache_key, cutoff),
        ).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None
    return None


def _save_cache(cache_dir: str, cache_key: str, year: str, results: list[dict]) -> None:
    _init_cache(cache_dir)
    db_path = _get_db_path(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO buyer_history_cache (cache_key, year, results, cached_at) VALUES (?, ?, ?, ?)",
            (cache_key, year, json.dumps(results, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


async def _get_insee_token(settings: Settings) -> str | None:
    """Obtient un token OAuth2 ou la clé d'API directe pour Sirene INSEE."""
    if not settings.insee_consumer_key:
        return None
    if not settings.insee_consumer_secret:
        return settings.insee_consumer_key
    import base64
    credentials = f"{settings.insee_consumer_key}:{settings.insee_consumer_secret}"
    auth = base64.b64encode(credentials.encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.insee.fr/token",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials",
        )
        resp.raise_for_status()
        return resp.json().get("access_token")


def _sirene_auth_headers(token_or_key: str) -> dict[str, str]:
    """Retourne les headers adaptés selon ancien ou nouveau portail INSEE."""
    if len(token_or_key) > 40:
        # Nouveau portail : clé d'API longue
        return {"X-INSEE-Api-Key-Integration": token_or_key}
    else:
        # Ancien portail : token OAuth2 court
        return {"Authorization": f"Bearer {token_or_key}"}


async def fetch_sirene_by_siren(siren: str, settings: Settings) -> dict[str, Any] | None:
    """Interroge l'API Sirene 3.11 pour un SIREN donné. Retourne un dict compact."""
    if not siren or len(siren) != 9:
        return None

    cache_dir = settings.cache_dir
    db_path = _get_db_path(cache_dir)
    if db_path.exists():
        cutoff = int(time.time()) - (30 * 86400)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT data FROM sirene_cache WHERE siren = ? AND cached_at > ?",
                (siren, cutoff),
            ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass

    token = await _get_insee_token(settings)
    if not token:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.insee.fr/api-sirene/3.11/siren/{siren}",
                headers=_sirene_auth_headers(token),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("Échec API Sirene pour SIREN %s", siren)
        return None

    ul = data.get("uniteLegale", {})
    periodes = ul.get("periodesUniteLegale", [])
    periode = periodes[0] if periodes else {}
    siege = ul.get("etablissementSiege", {})
    adresse = siege.get("adresseEtablissement", {})

    result = {
        "siren": siren,
        "denomination": periode.get("denominationUniteLegale", ""),
        "date_creation": ul.get("dateCreationUniteLegale", ""),
        "forme_juridique": periode.get("categorieJuridiqueUniteLegale", ""),
        "naf": periode.get("activitePrincipaleUniteLegale", ""),
        "effectif_code": periode.get("trancheEffectifsUniteLegale", ""),
        "effectif_label": _TRANCHE_EFFECTIF.get(
            periode.get("trancheEffectifsUniteLegale", ""), "Inconnu"
        ),
        "code_postal": adresse.get("codePostalEtablissement", ""),
        "commune": adresse.get("libelleCommuneEtablissement", ""),
    }

    _init_cache(cache_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sirene_cache (siren, data, cached_at) VALUES (?, ?, ?)",
            (siren, json.dumps(result, ensure_ascii=False), int(time.time())),
        )
        conn.commit()

    return result


def _extract_titulaire_siren(tit: Any) -> str:
    """Extrait le SIREN (9 chiffres) d'un titulaire DECP."""
    if not isinstance(tit, dict):
        return ""
    siret = ""
    if "titulaire" in tit and isinstance(tit["titulaire"], dict):
        siret = tit["titulaire"].get("id", "")
    else:
        siret = tit.get("id", "")
    # SIRET = 14 chiffres, SIREN = 9 premiers
    if isinstance(siret, str) and len(siret) >= 9:
        return siret[:9]
    return ""


def _extract_titulaire_name(tit: Any) -> str:
    if isinstance(tit, dict):
        if "titulaire" in tit and isinstance(tit["titulaire"], dict):
            return tit["titulaire"].get("denominationSociale") or tit["titulaire"].get("id", "Inconnu")
        return tit.get("denominationSociale") or tit.get("id", "Inconnu")
    return str(tit)


async def enrich_titulaires_with_sirene(
    rows: list[dict[str, Any]],
    settings: Settings,
) -> list[dict[str, Any]]:
    """Enrichit les rows DECP avec les données Sirene des titulaires (cache 30j)."""
    # Extraire tous les SIREN uniques
    sirens: set[str] = set()
    for r in rows:
        tits = r.get("titulaires", [])
        if isinstance(tits, list):
            for t in tits:
                s = _extract_titulaire_siren(t)
                if s:
                    sirens.add(s)
        elif isinstance(tits, dict):
            s = _extract_titulaire_siren(tits)
            if s:
                sirens.add(s)

    if not sirens:
        return rows

    # Récupérer en parallèle (max 5 simultanés pour ne pas saturer l'API)
    semaphore = asyncio.Semaphore(5)
    sirene_data: dict[str, dict | None] = {}

    async def _fetch_one(s: str) -> None:
        async with semaphore:
            sirene_data[s] = await fetch_sirene_by_siren(s, settings)

    await asyncio.gather(*[_fetch_one(s) for s in sirens])

    # Injecter les données Sirene dans chaque row
    for r in rows:
        r["_titulaires_sirene"] = []
        tits = r.get("titulaires", [])
        tit_list = tits if isinstance(tits, list) else [tits] if isinstance(tits, dict) else []
        for t in tit_list:
            siren = _extract_titulaire_siren(t)
            info = sirene_data.get(siren) if siren else None
            if info:
                r["_titulaires_sirene"].append({
                    "nom": _extract_titulaire_name(t),
                    "siren": siren,
                    "effectif": info.get("effectif_label", "Inconnu"),
                    "date_creation": info.get("date_creation", ""),
                    "localisation": f"{info.get('code_postal', '')} {info.get('commune', '')}".strip(),
                })

    return rows


def format_buyer_history(rows: list[dict[str, Any]]) -> str:
    """Formate l'historique DECP d'un acheteur en texte Slack compact."""
    if not rows:
        return ""

    total = len(rows)
    montants = [
        r.get("montant", 0)
        for r in rows
        if isinstance(r.get("montant"), (int, float)) and r["montant"] > 0
    ]
    moyenne = sum(montants) / len(montants) if montants else 0
    max_montant = max(montants) if montants else 0

    # Titulaires fréquents
    titulaires = Counter()
    for r in rows:
        tits = r.get("titulaires", [])
        if isinstance(tits, list):
            for ti in tits:
                nom = _extract_titulaire_name(ti)
                if nom and nom != "Inconnu":
                    titulaires[nom] += 1
        elif isinstance(tits, dict):
            nom = _extract_titulaire_name(tits)
            if nom and nom != "Inconnu":
                titulaires[nom] += 1

    lines = [
        f"📜 *Historique de l'acheteur* ({total} marché(s) notifié(s) {rows[0].get('dateNotification', '')[:4]})",
        f"• Montant moyen : {moyenne:,.0f} € | Max : {max_montant:,.0f} €".replace(",", " "),
    ]

    if titulaires:
        lines.append("• Titulaires fréquents :")
        for nom, cnt in titulaires.most_common(3):
            lines.append(f"  ◦ {nom} — {cnt} marché(s)")

    # --- Analyse SIRENE des concurrents ---
    effectifs: Counter[str] = Counter()
    localisations: Counter[str] = Counter()
    ages: list[int] = []
    for r in rows:
        for ts in r.get("_titulaires_sirene", []):
            eff = ts.get("effectif", "")
            if eff and eff != "Inconnu":
                effectifs[eff] += 1
            loc = ts.get("localisation", "")
            if loc:
                localisations[loc] += 1
            dc = ts.get("date_creation", "")
            if dc:
                try:
                    created = datetime.strptime(str(dc)[:10], "%Y-%m-%d")
                    ages.append((datetime.now() - created).days // 365)
                except Exception:
                    pass

    if effectifs:
        top_eff = effectifs.most_common(1)[0]
        lines.append(f"• Profil type du gagnant : {top_eff[0]} ({top_eff[1]} fois)")
    if ages:
        avg_age = sum(ages) // len(ages)
        lines.append(f"• Ancienneté moyenne des titulaires : {avg_age} ans")
    if localisations:
        top_loc = localisations.most_common(1)[0]
        lines.append(f"• Localisation dominante : {top_loc[0]} ({top_loc[1]} fois)")

    dernier = rows[0]
    lines.append(
        f"• Dernier marché : {dernier.get('objet', '')[:80]} "
        f"({dernier.get('dateNotification', '')})"
    )

    return "\n".join(lines)


def _escape_sql_like(val: str) -> str:
    return val.lower().replace("'", "''")


async def fetch_buyer_history(
    buyer_name: str,
    year: str,
    settings: Settings,
    max_results: int = 15,
) -> list[dict[str, Any]]:
    """
    Interroge le dataset DECP pour récupérer l'historique des attributions
    d'un acheteur public donné, enrichi avec SIRENE. Utilise le cache SQLite.
    """
    if not buyer_name or len(buyer_name) < 3:
        return []

    cache_dir = settings.cache_dir
    _init_cache(cache_dir)
    # v2 = nouvelle logique de filtre par SIRET (acheteur.id) au lieu de acheteur.nom
    cache_key = f"{buyer_name.lower().strip()}|{year}|v2"
    cached = _get_cached(cache_dir, cache_key)
    if cached is not None:
        return cached

    # 1. Récupérer les ressources DECP (mensuelles en priorité, plus petites)
    try:
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "datagouv-slack-agent/0.1.0"}
        ) as client:
            resp = await client.get(
                f"https://www.data.gouv.fr/api/1/datasets/{_DATASET_ID}/"
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("Échec récupération metadata DECP")
        return []

    monthly_targets: list[dict] = []
    consolidated_target: dict | None = None
    for r in data.get("resources", []):
        title = r.get("title", "")
        fmt = r.get("format", "").lower()
        if fmt == "json":
            if title.startswith(f"decp-{year}-") and title.endswith(".json"):
                monthly_targets.append(r)
            elif title == f"decp-{year}.json":
                consolidated_target = r

    monthly_targets.sort(key=lambda r: r.get("title", ""), reverse=True)
    targets = monthly_targets if monthly_targets else ([consolidated_target] if consolidated_target else [])

    if not targets:
        logger.warning("Aucun fichier DECP trouvé pour %s", year)
        return []

    # 2. Résoudre le nom d'acheteur en SIRET pour filtrer DECP (2024+ n'a plus acheteur.nom)
    siret = await _resolve_buyer_siret(buyer_name, cache_dir)
    if siret and len(siret) >= 9:
        siren = siret[:9]
        buyer_filter = f"unnest.acheteur.id LIKE '{siren}%'"
    else:
        # Les DECP récents n'ont plus acheteur.nom → impossible de filtrer sans SIREN
        logger.warning("SIREN acheteur non résolu pour '%s' — historique DECP indisponible", buyer_name)
        return []

    sql = (
        f"SELECT unnest.objet as objet, unnest.montant as montant, "
        f"unnest.dateNotification as dateNotification, "
        f"unnest.titulaires as titulaires "
        f"FROM scan, unnest(marches.marche) "
        f"WHERE {buyer_filter} "
        f"ORDER BY dateNotification DESC "
        f"LIMIT {max_results}"
    )

    # 3. Télécharger + requêter cible par cible (mensuelles d'abord)
    all_rows: list[dict[str, Any]] = []
    for target in targets:
        try:
            path = await download_resource(
                target["url"],
                cache_dir,
                settings.max_download_mb,
                max_mb_override=300 if monthly_targets else 800,
            )
        except Exception:
            logger.warning("Échec téléchargement %s", target.get("title"))
            continue

        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(query_file, Path(path), sql, settings.max_scan_rows),
                timeout=90,
            )
        except Exception:
            logger.exception("Échec requête DECP sur %s", target.get("title"))
            continue

        if rows:
            all_rows.extend(rows)
            if len(all_rows) >= max_results:
                break

    rows = all_rows[:max_results]

    # 4. Enrichissement SIRENE des titulaires
    try:
        rows = await asyncio.wait_for(
            enrich_titulaires_with_sirene(rows, settings),
            timeout=60,
        )
    except Exception:
        logger.exception("Enrichissement SIRENE échoué")

    _save_cache(cache_dir, cache_key, year, rows)
    return rows
