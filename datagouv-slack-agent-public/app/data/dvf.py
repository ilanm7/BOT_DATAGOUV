"""Outil d'analyse des Demandes de Valeurs Foncières (DVF)."""

import asyncio
import zipfile
from pathlib import Path

import httpx

logger = __import__("logging").getLogger(__name__)

DVF_DATASET_ID = "5c4ae55a634f4117716d5656"
DATAGOUV_API_BASE = "https://www.data.gouv.fr/api/1"

REGIONS = {
    "bretagne": ["22", "29", "35", "56"],
    "ile de france": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "ile-de-france": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "provence alpes cote d azur": ["04", "05", "06", "13", "83", "84"],
    "provence-alpes-cote-d-azur": ["04", "05", "06", "13", "83", "84"],
    "auvergne rhone alpes": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "auvergne-rhone-alpes": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "nouvelle aquitaine": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "nouvelle-aquitaine": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "occitanie": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
    "hauts de france": ["02", "59", "60", "62", "80"],
    "hauts-de-france": ["02", "59", "60", "62", "80"],
    "grand est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "grand-est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "pays de la loire": ["44", "49", "53", "72", "85"],
    "pays-de-la-loire": ["44", "49", "53", "72", "85"],
    "normandie": ["14", "27", "50", "61", "76"],
    "bourgogne franche comte": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "bourgogne-franche-comte": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "centre val de loire": ["18", "28", "36", "37", "41", "45"],
    "centre-val-de-loire": ["18", "28", "36", "37", "41", "45"],
    "corse": ["2A", "2B"],
}

DVF_COLUMNS = {
    "code_postal": "Code postal",
    "date_mutation": "Date mutation",
    "nature_mutation": "Nature mutation",
    "valeur_fonciere": "Valeur fonciere",
    "type_local": "Type local",
    "surface_reelle_bati": "Surface reelle bati",
    "code_departement": "Code departement",
    "code_commune": "Code commune",
    "commune": "Commune",
}

_VOIE_TYPES = (
    "boulevard", "boul", "bd", "bld",
    "rue", "r",
    "avenue", "av", "ave",
    "allée", "allee",
    "place", "pl",
    "impasse", "imp",
    "chemin", "chem",
    "passage", "pass",
    "quai", "q",
    "faubourg", "fg",
    "cours", "crs",
    "square", "sq",
    "tertre", "ter",
    "montée", "montee",
    "descente",
    "promenade", "prom",
    "voie", "v",
    "route", "rte",
    "sentier", "sent",
    "chaussée", "chassee", "chaussee", "chaus",
    "pont",
    "porte",
    "zone",
    "lotissement",
    "residence", "résidence", "res",
    "mail",
    "passerelle",
    "parvis",
    "rocade",
    "tunnel",
    "traverse",
    "villa",
    "village",
)


def _clean_voie(voie: str) -> str:
    """Retire le type de voie du début pour ne garder que le nom propre.

    Exemples :
        'boulevard des batignolles' → 'des batignolles'
        'rue de rivoli'             → 'de rivoli'
        'AVENUE DES CHAMPS ELYSEES' → 'des champs elysees'
    """
    v = voie.lower().strip()
    for vt in _VOIE_TYPES:
        if v.startswith(vt + " "):
            v = v[len(vt):].strip()
            break
        elif v.startswith(vt + "."):
            v = v[len(vt) + 1:].strip()
            break
    return v.strip(" -.,;")


def _resolve_departements(location: str) -> list[str] | None:
    """Résout un nom de région ou département en liste de codes département."""
    loc = location.lower().strip()
    # Région connue ?
    if loc in REGIONS:
        return REGIONS[loc]
    # Département numérique (2-3 chiffres)
    if loc.isdigit() and 1 <= len(loc) <= 3:
        return [loc.zfill(2) if len(loc) == 1 else loc]
    # Corse
    if loc in ("2a", "2b"):
        return [loc.upper()]
    return None


async def _get_dvf_file_path(annee: str, cache_path: Path) -> Path | str:
    """Télécharge et extrait le fichier DVF pour une année donnée.

    Retourne le Path du fichier .txt extrait, ou un message d'erreur string.
    """
    zip_path = cache_path / f"dvf_{annee}.zip"
    txt_path = cache_path / f"dvf_{annee}.txt"

    if not txt_path.exists():
        # Trouver l'URL
        resource_url = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{DATAGOUV_API_BASE}/datasets/{DVF_DATASET_ID}/")
                resp.raise_for_status()
                data = resp.json()
            for r in data.get("resources", []):
                if r.get("title") == f"Valeurs foncières {annee}":
                    resource_url = r.get("url")
                    break
        except Exception as exc:
            logger.exception("Erreur récupération dataset DVF")
            return f"ERREUR: Impossible de récupérer les métadonnées DVF : {exc}"

        if not resource_url:
            return f"ERREUR: Données DVF pour l'année {annee} non trouvées."

        # Télécharger
        if not zip_path.exists():
            try:
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    resp = await client.get(resource_url)
                    resp.raise_for_status()
                    zip_path.write_bytes(resp.content)
                    logger.info("DVF %s téléchargé (%s octets)", annee, len(resp.content))
            except Exception as exc:
                logger.exception("Erreur téléchargement DVF %s", annee)
                return f"ERREUR: Impossible de télécharger le fichier DVF {annee} : {exc}"

        # Extraire
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                txt_files = [n for n in z.namelist() if n.endswith(".txt")]
                if not txt_files:
                    return "ERREUR: Fichier texte non trouvé dans l'archive DVF."
                z.extract(txt_files[0], cache_path)
                extracted = cache_path / txt_files[0]
                extracted.rename(txt_path)
        except Exception as exc:
            logger.exception("Erreur extraction DVF %s", annee)
            return f"ERREUR: Impossible d'extraire le fichier DVF : {exc}"

    return txt_path


async def query_dvf_stats(
    code_postal: str = "",
    departement: str = "",
    annee: str = "",
    voie: str = "",
    cache_dir: str = "",
) -> str:
    """Interroge les données DVF pour un code postal, département, année et voie optionnelle."""
    if not annee:
        return "ERREUR: année obligatoire."
    if not code_postal and not departement:
        return "ERREUR: code_postal ou departement obligatoire."

    cache_path = Path(cache_dir)
    await asyncio.to_thread(cache_path.mkdir, parents=True, exist_ok=True)

    txt_path = await _get_dvf_file_path(annee, cache_path)
    if isinstance(txt_path, str):
        return txt_path  # erreur

    # [SÉCURITÉ] Valider que le fichier est bien dans le répertoire cache
    txt_resolved = await asyncio.to_thread(txt_path.resolve)
    cache_resolved = await asyncio.to_thread(cache_path.resolve)
    if not str(txt_resolved).startswith(str(cache_resolved)):
        return "ERREUR: Chemin de fichier DVF invalide."

    try:
        import duckdb

        con = duckdb.connect()

        # [SÉCURITÉ] Requête paramétrée — plus de f-string SQL avec entrées utilisateur
        params: list = []
        conditions = ['"Nature mutation" = \'Vente\'']
        conditions = ['"Nature mutation" = \'Vente\'', '"Date mutation" LIKE ?']
        params.append(f"%/{annee}")

        if code_postal:
            conditions.append('"Code postal" = ?')
            params.append(code_postal)

        if departement:
            d = departement.zfill(2) if departement.isdigit() else departement
            conditions.append('TRIM(LEADING \'0\' FROM "Code departement") = TRIM(LEADING \'0\' FROM ?)')
            params.append(d)

        if voie:
            voie_clean = _clean_voie(voie)
            conditions.append('lower("Voie") LIKE ?')
            params.append(f"%{voie_clean}%")

        conditions.append('CAST(REPLACE("Valeur fonciere", \',\', \'.\') AS DOUBLE) > 0')

        where_clause = " AND ".join(conditions)
        txt_path_escaped = str(txt_path).replace("'", "''")

        query = f"""
            SELECT
                COUNT(*) as ventes,
                COUNT(CASE WHEN \"Type local\" = 'Appartement' THEN 1 END) as appartements,
                COUNT(CASE WHEN \"Type local\" = 'Maison' THEN 1 END) as maisons,
                COUNT(CASE WHEN \"Type local\" NOT IN ('Appartement', 'Maison', '') THEN 1 END) as locaux,
                COUNT(CASE WHEN \"Type local\" LIKE '%Dépendance%' THEN 1 END) as dependances,
                AVG(CAST(REPLACE(\"Valeur fonciere\", ',', '.') AS DOUBLE) /
                    NULLIF(CAST(REPLACE(\"Surface reelle bati\", ',', '.') AS DOUBLE), 0)
                ) as prix_m2_moyen,
                MEDIAN(CAST(REPLACE(\"Valeur fonciere\", ',', '.') AS DOUBLE) /
                    NULLIF(CAST(REPLACE(\"Surface reelle bati\", ',', '.') AS DOUBLE), 0)
                ) as prix_m2_median,
                COUNT(CASE WHEN CAST(REPLACE(\"Surface reelle bati\", ',', '.') AS DOUBLE) > 0 THEN 1 END) as avec_surface
            FROM read_csv_auto('{txt_path_escaped}', delim='|', header=true, all_varchar=true)
            WHERE {where_clause}
        """

        row = con.execute(query, params).fetchone()
        con.close()

        if not row or row[0] == 0:
            return (
                f"Aucune transaction trouvée pour **{code_postal or 'dépt ' + departement}"
                f"{' — ' + voie if voie else ''}** en **{annee}**._"
            )

        ventes, appartements, maisons, locaux, dependances, prix_m2_moyen, prix_m2_median, avec_surface = row

        lines = [
            f"📊 *Transactions à {code_postal or 'dépt ' + departement}{' — ' + voie if voie else ''} en {annee}*",
            "",
            f"• Ventes : *{ventes:,}*",
        ]
        if appartements > 0:
            lines.append(f"• Appartements : {appartements:,}")
        if maisons > 0:
            lines.append(f"• Maisons : {maisons:,}")
        if locaux > 0:
            lines.append(f"• Locaux commerciaux : {locaux:,}")
        if dependances > 0:
            lines.append(f"• Dépendances : {dependances:,}")
        if avec_surface and avec_surface > 0:
            lines.append("")
            lines.append(f"• Prix moyen au m² : *{prix_m2_moyen:,.0f} €*")
            lines.append(f"• Prix médian au m² : *{prix_m2_median:,.0f} €*")
            lines.append(f"  (sur {avec_surface:,} biens avec surface)")
        lines.append("")
        lines.append("_Source : DGFiP, DVF — data.gouv.fr_")
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("Erreur DuckDB DVF")
        return f"ERREUR: Impossible d'analyser le fichier DVF : {exc}"


async def query_dvf_details(
    code_postal: str = "",
    departement: str = "",
    annee: str = "",
    voie: str = "",
    type_local: str = "Appartement",
    limit: int = 15,
    cache_dir: str = "",
) -> str:
    """Interroge les données DVF et retourne les N dernières transactions individuelles."""
    if not annee:
        return "ERREUR: année obligatoire."
    if not code_postal and not departement:
        return "ERREUR: code_postal ou departement obligatoire."

    cache_path = Path(cache_dir)
    await asyncio.to_thread(cache_path.mkdir, parents=True, exist_ok=True)

    txt_path = await _get_dvf_file_path(annee, cache_path)
    if isinstance(txt_path, str):
        return txt_path  # erreur

    # [SÉCURITÉ] Valider que le fichier est bien dans le répertoire cache
    txt_resolved = await asyncio.to_thread(txt_path.resolve)
    cache_resolved = await asyncio.to_thread(cache_path.resolve)
    if not str(txt_resolved).startswith(str(cache_resolved)):
        return "ERREUR: Chemin de fichier DVF invalide."

    try:
        import duckdb

        con = duckdb.connect()

        # [SÉCURITÉ] Requête paramétrée — plus de f-string SQL avec entrées utilisateur
        params: list = []
        conditions = ['"Nature mutation" = \'Vente\'', '"Date mutation" LIKE ?']
        params.append(f"%/{annee}")

        if code_postal:
            conditions.append('"Code postal" = ?')
            params.append(code_postal)

        if departement:
            d = departement.zfill(2) if departement.isdigit() else departement
            conditions.append('TRIM(LEADING \'0\' FROM "Code departement") = TRIM(LEADING \'0\' FROM ?)')
            params.append(d)

        if voie:
            voie_clean = _clean_voie(voie)
            conditions.append('lower("Voie") LIKE ?')
            params.append(f"%{voie_clean}%")

        if type_local:
            conditions.append('"Type local" = ?')
            params.append(type_local)

        conditions.append('CAST(REPLACE("Valeur fonciere", \',\', \'.\') AS DOUBLE) > 0')

        where_clause = " AND ".join(conditions)
        txt_path_escaped = str(txt_path).replace("'", "''")

        query = f"""
            SELECT
                "Date mutation",
                "No voie",
                "B/T/Q",
                "Type de voie",
                "Voie",
                "Code postal",
                CAST(REPLACE("Valeur fonciere", ',', '.') AS DOUBLE) as prix,
                CAST(REPLACE("Surface reelle bati", ',', '.') AS DOUBLE) as surface,
                CAST("Nombre pieces principales" AS INTEGER) as pieces
            FROM read_csv_auto('{txt_path_escaped}', delim='|', header=true, all_varchar=true)
            WHERE {where_clause}
            QUALIFY row_number() OVER (
                PARTITION BY "Date mutation", "No voie", "B/T/Q", "Type de voie", "Voie", "Code postal", "Valeur fonciere"
                ORDER BY "Date mutation"
            ) = 1
            ORDER BY strptime("Date mutation", '%d/%m/%Y') DESC
            LIMIT ?
        """
        params.append(limit)

        rows = con.execute(query, params).fetchall()
        con.close()

        if not rows:
            return (
                f"Aucune transaction trouvée pour **{code_postal or 'dépt ' + departement}"
                f"{' — ' + voie if voie else ''}** en **{annee}**._"
            )

        lines = [
            f"📋 *Dernières ventes — {code_postal or 'dépt ' + departement}{' — ' + voie if voie else ''} ({annee})*",
            "",
            "| Date | Adresse | Prix | Surface (m²) | Pièces |",
            "|------|---------|------|--------------|--------|",
        ]
        for row in rows:
            date_mut, no_voie, btq, type_voie, nom_voie, cp, prix, surface, pieces = row
            adresse = f"{no_voie or ''}{' ' + btq if btq else ''} {type_voie or ''} {nom_voie or ''} ({cp})".strip()
            prix_str = f"{prix:,.0f} €".replace(",", " ")
            surface_str = f"{surface:,.0f}".replace(",", " ") if surface else "—"
            pieces_str = str(pieces) if pieces else "—"
            lines.append(f"| {date_mut} | {adresse} | {prix_str} | {surface_str} | {pieces_str} |")

        lines.append("")
        lines.append("_Source : DGFiP, DVF — data.gouv.fr_")
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("Erreur DuckDB DVF détails")
        return f"ERREUR: Impossible d'analyser le fichier DVF : {exc}"


async def query_dvf_trend(
    location: str,
    annees: list[str],
    type_local: str = "",
    cache_dir: str = "",
) -> str:
    """Analyse la tendance des prix DVF sur plusieurs années pour une région/département.

    Utilise DuckDB pour agréger rapidement les fichiers DVF.
    """
    if not location or not annees:
        return "ERREUR: location et annees sont obligatoires."

    cache_path = Path(cache_dir)
    await asyncio.to_thread(cache_path.mkdir, parents=True, exist_ok=True)

    # Résoudre la location en départements
    departements = _resolve_departements(location)
    if departements is None:
        # Essayer de traiter comme un code postal ou département direct
        if location.isdigit():
            if len(location) == 5:
                # C'est un code postal : on va utiliser query_dvf_stats pour chaque année
                pass
            else:
                departements = [location.zfill(2) if len(location) == 1 else location]
        else:
            return (
                f"ERREUR: Localisation '{location}' non reconnue. "
                f"Essayez un nom de région (Bretagne, Île-de-France…) ou un numéro de département."
            )

    # Préparer les fichiers
    files_to_process = []
    for annee in annees:
        txt_path = await _get_dvf_file_path(annee, cache_path)
        if isinstance(txt_path, str):
            return txt_path  # erreur
        files_to_process.append((annee, txt_path))

    # Utiliser DuckDB pour agréger
    try:
        import duckdb

        con = duckdb.connect()
        results = []

        for annee, txt_path in files_to_process:
            # [SÉCURITÉ] Valider que le fichier est bien dans le répertoire cache
            txt_resolved = await asyncio.to_thread(txt_path.resolve)
            cache_resolved = await asyncio.to_thread(cache_path.resolve)
            if not str(txt_resolved).startswith(str(cache_resolved)):
                logger.warning("Chemin de fichier DVF invalide ignoré : %s", txt_path)
                continue

            # Construire la requête SQL — clauses paramétrables
            dept_list = ", ".join(f"'{d}'" for d in departements) if departements else ""
            dept_filter = f"AND \"Code departement\" IN ({dept_list})" if departements else ""

            params: list = []
            conditions = [
                '"Nature mutation" = \'Vente\'',
                'CAST(REPLACE("Surface reelle bati", \',\', \'.\') AS DOUBLE) > 0',
                'CAST(REPLACE("Valeur fonciere", \',\', \'.\') AS DOUBLE) > 0',
            ]

            if not departements and len(location) == 5:
                conditions.append('"Code postal" = ?')
                params.append(location)

            if type_local:
                conditions.append('"Type local" = ?')
                params.append(type_local)

            where_clause = " AND ".join(conditions)
            txt_path_escaped = str(txt_path).replace("'", "''")

            query = f"""
                SELECT
                    '{annee}' as annee,
                    COUNT(*) as ventes,
                    AVG(CAST(REPLACE("Valeur fonciere", ',', '.') AS DOUBLE) /
                        NULLIF(CAST(REPLACE("Surface reelle bati", ',', '.') AS DOUBLE), 0)
                    ) as prix_m2_moyen,
                    MEDIAN(CAST(REPLACE("Valeur fonciere", ',', '.') AS DOUBLE) /
                        NULLIF(CAST(REPLACE("Surface reelle bati", ',', '.') AS DOUBLE), 0)
                    ) as prix_m2_median
                FROM read_csv_auto('{txt_path_escaped}', delim='|', header=true, all_varchar=true)
                WHERE {where_clause}
                  {dept_filter}
            """

            try:
                row = con.execute(query, params).fetchone()
                if row and row[1] and row[1] > 0:  # ventes > 0
                    results.append({
                        "annee": annee,
                        "ventes": int(row[1]),
                        "prix_m2_moyen": float(row[2]) if row[2] else 0,
                        "prix_m2_median": float(row[3]) if row[3] else 0,
                    })
            except Exception as e:
                logger.warning("DuckDB query échouée pour %s: %s", annee, e)
                continue

        con.close()

        if not results:
            return (
                f"Aucune donnée trouvée pour **{location}** sur la période demandée.\n"
                f"Vérifiez le nom de la région ou le département."
            )

        # Formater le résultat
        lines = [
            f"📈 *Évolution des prix à {location.title()}*",
            "",
            "| Année | Ventes | Prix moyen m² | Prix médian m² |",
            "|-------|--------|---------------|----------------|",
        ]
        for r in results:
            lines.append(
                f"| {r['annee']} | {r['ventes']:,} | {r['prix_m2_moyen']:,.0f} € | {r['prix_m2_median']:,.0f} € |"
            )

        # Calculer l'évolution globale
        if len(results) >= 2:
            first = results[0]["prix_m2_median"]
            last = results[-1]["prix_m2_median"]
            if first > 0:
                evol = ((last - first) / first) * 100
                lines.append("")
                lines.append(f"📊 *Évolution sur la période* : {evol:+.1f}%")

        lines.append("")
        lines.append("_Source : DGFiP, Demandes de valeurs foncières (DVF) — data.gouv.fr_")
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("Erreur DuckDB DVF trend")
        return f"ERREUR: Impossible d'analyser la tendance DVF : {exc}"
