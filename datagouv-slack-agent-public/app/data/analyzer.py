from __future__ import annotations

import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

"""Moteur d'analyse locale des fichiers de données.

DuckDB fait le gros du travail : détection automatique des délimiteurs,
types, lecture partielle. Le module produit un résumé compact structuré,
seul élément transmis au LLM. Le fichier brut ne quitte jamais la machine.
"""

TABULAR_EXTS = {".csv", ".tsv", ".txt", ".gz"}
EXCEL_EXTS = {".xlsx", ".xls"}
JSON_EXTS = {".json", ".jsonl", ".geojson", ".ndjson"}
PARQUET_EXTS = {".parquet"}

GEO_HINTS = ("insee", "commune", "departement", "dept", "region", "latitude",
             "longitude", "lat", "lon", "lng", "geo", "adresse", "code_postal",
             "cp", "epci", "iris", "parcelle", "codgeo")
DATE_HINTS = ("date", "annee", "année", "year", "mois", "jour", "periode",
              "timestamp", "arrete")


class AnalysisError(Exception):
    """Erreur contrôlée d'analyse (format non supporté, fichier illisible...)."""


def _duckdb_escape_path(path: Path) -> str:
    """Échappe un chemin pour l'utiliser dans une requête DuckDB.
    Remplace les quotes simples par l'échappement DuckDB ('')."""
    return str(path).replace("'", "''")


def _csv_readers(path: Path, compression: str = "") -> list[str]:
    """Construit une liste de readers DuckDB à essayer pour un CSV.

    Ordre : read_csv_auto (sniffing) → read_csv avec ;/utf-8 → read_csv avec
    ;/latin-1 → read_csv avec ;/cp1252 → read_csv avec ,/utf-8.
    Gère les CSV français/administratifs (Mélodi, SIRENE, etc.).
    """
    escaped = _duckdb_escape_path(path)
    comp = f", compression='{compression}'" if compression else ""
    readers: list[str] = [
        f"read_csv_auto('{escaped}', sample_size=20000, ignore_errors=true{comp})",
    ]
    for delim in (";", ","):
        for encoding in ("utf-8", "LATIN1", "CP1252"):
            readers.append(
                f"read_csv('{escaped}', delim='{delim}', encoding='{encoding}', "
                f"header=true, ignore_errors=true, sample_size=20000{comp})"
            )
    # [SKIP METADATA LINE] Certains CSV administratifs (FINESS, etc.) commencent
    # par une ligne de métadonnées avant l'en-tête. On essaie en sautant 1 ligne.
    for delim in (";", ","):
        for encoding in ("utf-8", "LATIN1", "CP1252"):
            readers.append(
                f"read_csv('{escaped}', delim='{delim}', encoding='{encoding}', "
                f"header=true, skip=1, ignore_errors=true, sample_size=20000{comp})"
            )
    return readers


def _pandas_csv_fallback(path: Path) -> Path:
    """Lit un CSV problématique avec pandas (détection rapide encoding/delim)
    et le convertit en UTF-8 pour DuckDB."""
    tmp = Path(tempfile.mktemp(suffix=".csv"))

    # Essai rapide avec engine="c" (20× plus rapide que engine="python")
    for enc in ("utf-8", "latin-1", "cp1252", "iso-8859-1"):
        for sep in (";", ",", "\t", "|"):
            try:
                # Test sur 5 lignes d'abord
                pd.read_csv(path, sep=sep, engine="c", encoding=enc, nrows=5)
                # Si OK, lecture complète par chunks pour les gros fichiers
                chunks = pd.read_csv(
                    path, sep=sep, engine="c", encoding=enc, chunksize=100_000, low_memory=False
                )
                header_written = False
                for chunk in chunks:
                    chunk.to_csv(tmp, index=False, encoding="utf-8", mode="a", header=not header_written)
                    header_written = True
                return tmp
            except Exception:
                continue

    # Fallback engine="python" avec détection auto du séparateur
    for enc in ("utf-8", "latin-1", "cp1252", "iso-8859-1"):
        try:
            df = pd.read_csv(path, sep=None, engine="python", encoding=enc)
            df.to_csv(tmp, index=False, encoding="utf-8")
            return tmp
        except Exception:
            continue

    # [SKIP METADATA LINE] Essai en sautant la première ligne (métadonnées FINESS, etc.)
    for enc in ("utf-8", "latin-1", "cp1252", "iso-8859-1"):
        for sep in (";", ",", "\t", "|"):
            try:
                pd.read_csv(path, sep=sep, engine="c", encoding=enc, nrows=5, skiprows=1)
                chunks = pd.read_csv(
                    path, sep=sep, engine="c", encoding=enc, chunksize=100_000,
                    low_memory=False, skiprows=1
                )
                header_written = False
                for chunk in chunks:
                    chunk.to_csv(tmp, index=False, encoding="utf-8", mode="a", header=not header_written)
                    header_written = True
                return tmp
            except Exception:
                continue
        try:
            df = pd.read_csv(path, sep=None, engine="python", encoding=enc, skiprows=1)
            df.to_csv(tmp, index=False, encoding="utf-8")
            return tmp
        except Exception:
            continue

    raise AnalysisError(
        f"Impossible de lire {path.name} avec pandas (encodings testés: utf-8, latin-1, cp1252, iso-8859-1)"
    )


def _pick_excel_sheet(path: Path) -> tuple[str, pd.DataFrame]:
    """Choisit la feuille Excel avec le plus de lignes de données."""
    xls = pd.ExcelFile(path)
    sheet_names = xls.sheet_names
    best_sheet = sheet_names[0]
    best_rows = -1
    for sn in sheet_names:
        try:
            df_sample = pd.read_excel(path, sheet_name=sn, nrows=1000)
            n_rows = len(df_sample)
            if n_rows > best_rows:
                best_rows = n_rows
                best_sheet = sn
        except Exception:
            continue
    df = pd.read_excel(path, sheet_name=best_sheet)
    return best_sheet, df


def analyze_file(
    path: Path,
    max_scan_rows: int = 100_000,
    max_sample_rows: int = 30,
    source_url: str = "",
    max_file_size_mb: int = 1_500,
) -> dict[str, Any]:
    """Analyse un fichier local et retourne un résumé compact structuré."""
    file_size_mb = path.stat().st_size / (1024 * 1024)
    if file_size_mb > max_file_size_mb:
        raise AnalysisError(
            f"Fichier trop volumineux pour l'analyse locale: {file_size_mb:.0f} Mo "
            f"(limite {max_file_size_mb} Mo)."
        )
    ext = path.suffix.lower()

    if ext == ".zip":
        return _analyze_zip(path, max_scan_rows, max_sample_rows, source_url)
    if ext == ".gz":
        # DuckDB peut lire directement les .csv.gz — on essaie plusieurs dialectes
        readers = _csv_readers(path, compression="gzip")
        return _analyze_with_duckdb(path, readers, "csv.gz", max_scan_rows, max_sample_rows, source_url)
    if ext == ".bin":
        # Fallback : traiter comme CSV (cas des URLs sans extension qui renvoient du CSV)
        reader = _csv_readers(path)
        ext = ".csv"
    elif ext in EXCEL_EXTS:
        best_sheet, df = _pick_excel_sheet(path)
        sheet_names = pd.ExcelFile(path).sheet_names
        tmp_csv = Path(tempfile.mktemp(suffix=".csv"))
        try:
            df.to_csv(tmp_csv, index=False)
            reader = f"read_csv_auto('{_duckdb_escape_path(tmp_csv)}', sample_size=20000, ignore_errors=true)"
            ext = ".csv"
            result = _analyze_with_duckdb(tmp_csv, reader, ext.lstrip("."), max_scan_rows,
                                          max_sample_rows, source_url)
            if len(sheet_names) > 1:
                result["excel_sheets"] = sheet_names
                result["excel_warning"] = (
                    f"Ce fichier Excel contient {len(sheet_names)} feuilles. "
                    f"La feuille analysée est '{best_sheet}' ({len(df)} lignes). "
                    f"Autres feuilles: {', '.join(s for s in sheet_names if s != best_sheet)}."
                )
            return result
        finally:
            tmp_csv.unlink(missing_ok=True)
    elif ext in TABULAR_EXTS:
        if path.suffix.lower() == ".gz" or str(path).lower().endswith(".csv.gz"):
            reader = _csv_readers(path, compression="gzip")
        else:
            reader = _csv_readers(path)
    elif ext in JSON_EXTS:
        reader = _json_reader(path)
    elif ext in PARQUET_EXTS:
        reader = f"read_parquet('{_duckdb_escape_path(path)}')"
    else:
        raise AnalysisError(
            f"Format non supporté pour l'analyse locale: '{ext}'. "
            "Formats gérés: CSV, TSV, TXT tabulaire, JSON, JSONL, GeoJSON, Parquet, ZIP."
        )

    try:
        return _analyze_with_duckdb(path, reader, ext.lstrip("."), max_scan_rows,
                                    max_sample_rows, source_url)
    except AnalysisError:
        # Fallback pandas pour les CSV mal encodés (windows-1252, etc.)
        if ext in {".csv", ".tsv", ".txt", ".bin", ".gz"}:
            tmp = _pandas_csv_fallback(path)
            try:
                return _analyze_with_duckdb(
                    tmp,
                    f"read_csv_auto('{tmp}', sample_size=20000, ignore_errors=true)",
                    "csv",
                    max_scan_rows,
                    max_sample_rows,
                    source_url,
                )
            finally:
                tmp.unlink(missing_ok=True)
        raise


def _json_reader(path: Path) -> str:
    """Choisit le bon lecteur JSON. Un GeoJSON FeatureCollection est aplati
    sur ses features pour exposer les properties comme colonnes."""
    with path.open("rb") as f:
        head = f.read(2048).lstrip()
    escaped = _duckdb_escape_path(path)
    if path.suffix.lower() == ".geojson" or b'"FeatureCollection"' in head:
        return (
            f"(SELECT unnest(features, recursive := true) "
            f"FROM read_json('{escaped}', maximum_object_size=536870912))"
        )
    fmt = "newline_delimited" if path.suffix.lower() in {".jsonl", ".ndjson"} else "auto"
    return f"read_json_auto('{escaped}', format='{fmt}', maximum_object_size=536870912)"


def _has_symlink_in_dir(directory: Path) -> bool:
    """Vérifie si un répertoire contient des symlinks (protection Zip Slip)."""
    return any(entry.is_symlink() for entry in directory.iterdir())


def _analyze_zip(path: Path, max_scan_rows: int, max_sample_rows: int,
                 source_url: str) -> dict[str, Any]:
    """Extrait le contenu tabulaire d'un ZIP et analyse le plus gros fichier."""
    extract_dir = path.parent / f"{path.stem}_extracted"
    inner_names: list[str] = []

    with zipfile.ZipFile(path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        inner_names = [m.filename for m in members]
        supported = [
            m for m in members
            if Path(m.filename).suffix.lower() in TABULAR_EXTS | JSON_EXTS | PARQUET_EXTS | EXCEL_EXTS
        ]
        if not supported:
            raise AnalysisError(
                f"Le ZIP ne contient aucun fichier analysable. Contenu: {inner_names[:20]}"
            )
        # Choisir le plus gros sans tout extraire (évite fuite disque sur ZIPs massifs)
        main_member = max(supported, key=lambda m: m.file_size)
        extract_dir.mkdir(exist_ok=True)
        # Protection Zip Slip via symlink : si extract_dir contient des symlinks,
        # zipfile.extract pourrait écrire hors du répertoire
        if _has_symlink_in_dir(extract_dir):
            raise AnalysisError("Répertoire d'extraction compromis (symlink détecté).")
        zf.extract(main_member, extract_dir)
        main_path = extract_dir / main_member.filename
        # Sécurité : s'assurer que le fichier extrait reste dans extract_dir
        main_path = main_path.resolve()
        extract_dir_resolved = extract_dir.resolve()
        if not str(main_path).startswith(str(extract_dir_resolved)):
            raise AnalysisError("Chemin malveillant détecté dans l'archive ZIP.")

    try:
        logger.info("ZIP %s: analyse de %s (parmi %d fichiers)", path.name, main_member.filename, len(supported))
        result = analyze_file(main_path, max_scan_rows, max_sample_rows, source_url)
        result["zip_contents"] = inner_names[:20]
        result["analyzed_member"] = main_member.filename
        return result
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def _analyze_with_duckdb(path: Path, reader: str | list[str], fmt: str, max_scan_rows: int,
                         max_sample_rows: int, source_url: str) -> dict[str, Any]:
    readers = [reader] if isinstance(reader, str) else reader
    con = duckdb.connect()
    last_err: Exception | None = None
    try:
        for r in readers:
            try:
                con.execute(f"CREATE VIEW src AS SELECT * FROM {r}")
                con.execute(f"CREATE TABLE scan AS SELECT * FROM src LIMIT {max_scan_rows}")
                break
            except duckdb.Error as exc:
                last_err = exc
                con.execute("DROP VIEW IF EXISTS src")
                continue
        else:
            raise AnalysisError(
                f"DuckDB n'a pas pu lire {path.name} avec aucun des {len(readers)} dialectes testés. "
                f"Dernière erreur: {last_err}"
            ) from last_err

        schema = con.execute("DESCRIBE scan").fetchall()
        columns = [row[0] for row in schema]
        col_types = {row[0]: row[1] for row in schema}

        row_count_result = con.execute("SELECT count(*) FROM scan").fetchone()
        rows_scanned = row_count_result[0] if row_count_result else 0
        scan_limit_reached = rows_scanned >= max_scan_rows

        sample = con.execute(f"SELECT * FROM scan LIMIT {max_sample_rows}").fetchall()
        sample_rows = [
            {col: _jsonable(val) for col, val in zip(columns, row, strict=False)} for row in sample
        ]

        numeric_cols = [
            c for c, t in col_types.items()
            if any(k in t.upper() for k in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "HUGEINT"))
            and "[]" not in t and "STRUCT" not in t.upper()
        ]
        numeric_stats: dict[str, dict[str, Any]] = {}
        for col in numeric_cols[:25]:
            safe_col = col.replace('"', '""')
            stats_row = con.execute(
                f'SELECT min("{safe_col}"), max("{safe_col}"), avg("{safe_col}") FROM scan'
            ).fetchone()
            if stats_row:
                mn, mx, avg = stats_row
                numeric_stats[col] = {
                    "min": _jsonable(mn), "max": _jsonable(mx),
                    "avg": round(avg, 2) if avg is not None else None,
                }

        missing: dict[str, int] = {}
        for col in columns[:60]:
            safe_col = col.replace('"', '""')
            null_result = con.execute(
                f'SELECT count(*) FROM scan WHERE "{safe_col}" IS NULL'
            ).fetchone()
            if null_result:
                n = null_result[0]
                if n:
                    missing[col] = n

        lower_cols = {c: c.lower() for c in columns}
        detected_geo = [c for c, lc in lower_cols.items() if any(h in lc for h in GEO_HINTS)]
        detected_date = [
            c for c, lc in lower_cols.items()
            if any(h in lc for h in DATE_HINTS) or "DATE" in col_types[c].upper()
            or "TIMESTAMP" in col_types[c].upper()
        ]

        return {
            "resource_url": source_url,
            "local_path": str(path),
            "format": fmt,
            "file_size_mb": round(path.stat().st_size / 1e6, 2),
            "columns": columns,
            "column_types": col_types,
            "rows_scanned": rows_scanned,
            "scan_limit_reached": scan_limit_reached,
            "sample_rows": sample_rows[:max_sample_rows],
            "numeric_stats": numeric_stats,
            "detected_geo_columns": detected_geo,
            "detected_date_columns": detected_date,
            "missing_values_summary": missing,
        }
    except duckdb.Error as exc:
        raise AnalysisError(f"DuckDB n'a pas pu lire {path.name}: {exc}") from exc
    finally:
        con.close()


def _jsonable(val: Any) -> Any:
    if val is None or isinstance(val, (int, float, str, bool, list, dict)):
        return val
    return str(val)


def _lock_connection(con: duckdb.DuckDBPyConnection, *paths: Path) -> None:
    """Limite la connexion à ces fichiers. Irréversible : le SQL du LLM ne peut plus rien ouvrir d'autre."""
    allowed = ", ".join(f"'{_duckdb_escape_path(p)}'" for p in paths)
    con.execute(f"SET allowed_paths = [{allowed}]")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")


def query_file(path: Path, sql_query: str, max_scan_rows: int = 100_000, max_file_size_mb: int = 1_500) -> list[dict[str, Any]]:
    """Exécute une requête SQL SELECT sur un fichier local via DuckDB."""
    path = path.resolve()
    file_size_mb = path.stat().st_size / (1024 * 1024)
    if file_size_mb > max_file_size_mb:
        raise AnalysisError(
            f"Fichier trop volumineux pour la requête SQL: {file_size_mb:.0f} Mo "
            f"(limite {max_file_size_mb} Mo)."
        )
    sql_upper = sql_query.strip().upper()
    if not any(sql_upper.startswith(k) for k in ("SELECT", "WITH")):
        raise AnalysisError("Seules les requêtes SELECT ou WITH sont autorisées.")
    # Premier filtre seulement : le vrai verrou d'accès aux fichiers est _lock_connection
    forbidden = (
        "READ_CSV", "READ_PARQUET", "READ_JSON", "READ_JSON_AUTO", "READ_CSV_AUTO",
        "COPY", "EXPORT", "ATTACH", "CREATE ", "INSERT ", "DELETE ", "DROP ",
        "UPDATE ", "PRAGMA", "ALTER ", "EXECUTE", "CALL ", "LOAD ", "INSTALL ",
    )
    for token in forbidden:
        if token in sql_upper:
            raise AnalysisError(f"Instruction SQL interdite détectée: {token.strip()}")

    ext = path.suffix.lower()
    if ext == ".zip":
        # Extraire le fichier principal du ZIP puis exécuter la requête SQL
        extract_dir = path.parent / f"{path.stem}_extracted_sql"
        with zipfile.ZipFile(path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            supported = [
                m for m in members
                if Path(m.filename).suffix.lower() in TABULAR_EXTS | JSON_EXTS | PARQUET_EXTS | EXCEL_EXTS
            ]
            if not supported:
                raise AnalysisError(f"ZIP sans fichier analysable pour SQL: {[m.filename for m in members][:20]}")
            main_member = max(supported, key=lambda m: m.file_size)
            extract_dir.mkdir(exist_ok=True)
            if _has_symlink_in_dir(extract_dir):
                raise AnalysisError("Répertoire d'extraction compromis (symlink détecté).")
            zf.extract(main_member, extract_dir)
            main_path = (extract_dir / main_member.filename).resolve()
            extract_dir_resolved = extract_dir.resolve()
            if not str(main_path).startswith(str(extract_dir_resolved)):
                raise AnalysisError("Chemin malveillant dans l'archive ZIP.")
        try:
            tmp_csv = None
            inner_ext = main_path.suffix.lower()
            if inner_ext in EXCEL_EXTS:
                tmp_csv = Path(tempfile.mktemp(suffix=".csv"))
                _best_sheet, df = _pick_excel_sheet(main_path)
                df.to_csv(tmp_csv, index=False)
                reader = f"read_csv_auto('{_duckdb_escape_path(tmp_csv)}', sample_size=20000, ignore_errors=true)"
            elif inner_ext in JSON_EXTS:
                reader = _json_reader(main_path)
            elif inner_ext in PARQUET_EXTS:
                reader = f"read_parquet('{_duckdb_escape_path(main_path)}')"
            else:
                if inner_ext == ".gz" or str(main_path).lower().endswith(".csv.gz"):
                    reader = _csv_readers(main_path, compression="gzip")
                else:
                    reader = _csv_readers(main_path)
            # Exécution SQL directe sur src (streaming, pas de chargement en mémoire)
            con = duckdb.connect()
            try:
                con.execute("SET memory_limit = '3GB'")
                con.execute("SET threads = 2")
                con.execute("SET preserve_insertion_order = false")
                readers_to_try = reader if isinstance(reader, list) else [reader]
                last_err = None
                for r in readers_to_try:
                    try:
                        con.execute(f"CREATE VIEW src AS SELECT * FROM {r}")
                        con.execute("CREATE VIEW scan AS SELECT * FROM src")
                        break
                    except duckdb.Error as exc:
                        last_err = exc
                        con.execute("DROP VIEW IF EXISTS src")
                        continue
                else:
                    raise AnalysisError(
                        f"DuckDB n'a pas pu lire {main_path.name} avec aucun des {len(readers_to_try)} dialectes testés. "
                        f"Dernière erreur: {last_err}"
                    ) from last_err
                _lock_connection(con, *filter(None, (main_path, tmp_csv)))
                sql_trimmed = sql_query.strip()
                if "LIMIT" not in sql_trimmed.upper():
                    sql_trimmed = f"{sql_trimmed} LIMIT {max_scan_rows}"
                result = con.execute(sql_trimmed).fetchall()
                columns = [desc[0] for desc in con.execute(sql_trimmed).description]
                return [{col: _jsonable(val) for col, val in zip(columns, row, strict=False)} for row in result]
            except duckdb.Error as exc:
                raise AnalysisError(f"DuckDB erreur SQL: {exc}") from exc
            finally:
                con.close()
                if tmp_csv is not None:
                    tmp_csv.unlink(missing_ok=True)
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
        # Sécurité : le code après ce bloc n'est jamais atteint car on return dans le try
        pass  # noqa: PIE790

    tmp_csv: Path | None = None
    if ext in EXCEL_EXTS:
        import pandas as pd
        tmp_csv = Path(tempfile.mktemp(suffix=".csv"))
        df = pd.read_excel(path)
        df.to_csv(tmp_csv, index=False)
        reader = f"read_csv_auto('{tmp_csv}', sample_size=20000, ignore_errors=true)"
    elif ext in TABULAR_EXTS:
        reader = _csv_readers(path)
    elif ext == ".bin":
        # Fallback : traiter comme CSV
        reader = _csv_readers(path)
    elif ext in JSON_EXTS:
        reader = _json_reader(path)
    elif ext in PARQUET_EXTS:
        reader = f"read_parquet('{path}')"
    else:
        raise AnalysisError(f"Format non supporté pour SQL: '{ext}'.")

    con = duckdb.connect()
    try:
        con.execute("SET memory_limit = '3GB'")
        con.execute("SET threads = 2")
        con.execute("SET preserve_insertion_order = false")
        readers_to_try = reader if isinstance(reader, list) else [reader]
        last_err = None
        for r in readers_to_try:
            try:
                con.execute(f"CREATE VIEW src AS SELECT * FROM {r}")
                con.execute("CREATE VIEW scan AS SELECT * FROM src")
                break
            except duckdb.Error as exc:
                last_err = exc
                con.execute("DROP VIEW IF EXISTS src")
                continue
        else:
            # Fallback pandas pour les CSV mal encodés
            if ext in {".csv", ".tsv", ".txt", ".bin", ".gz"} or str(path).lower().endswith(".csv.gz"):
                # La vue relit le fichier à chaque requête : tmp_csv est supprimé dans le finally
                tmp_csv = _pandas_csv_fallback(path)
                con.execute(f"CREATE VIEW src AS SELECT * FROM read_csv_auto('{_duckdb_escape_path(tmp_csv)}', sample_size=20000, ignore_errors=true)")
                con.execute("CREATE VIEW scan AS SELECT * FROM src")
            else:
                raise AnalysisError(
                    f"DuckDB n'a pas pu lire {path.name} avec aucun des {len(readers_to_try)} dialectes testés. "
                    f"Dernière erreur: {last_err}"
                ) from last_err
        _lock_connection(con, *filter(None, (path, tmp_csv)))
        sql_trimmed = sql_query.strip()
        if "LIMIT" not in sql_trimmed.upper():
            sql_trimmed = f"{sql_trimmed} LIMIT {max_scan_rows}"
        result = con.execute(sql_trimmed).fetchall()
        columns = [desc[0] for desc in con.execute(sql_trimmed).description]
        return [{col: _jsonable(val) for col, val in zip(columns, row, strict=False)} for row in result]
    except duckdb.Error as exc:
        raise AnalysisError(f"DuckDB erreur SQL: {exc}") from exc
    finally:
        con.close()
        if tmp_csv is not None:
            tmp_csv.unlink(missing_ok=True)


def summary_for_llm(result: dict[str, Any], max_chars: int = 8000) -> str:
    """Sérialise le résumé pour injection dans le contexte LLM, taille bornée."""
    compact = dict(result)
    text = json.dumps(compact, ensure_ascii=False, default=str)
    while len(text) > max_chars and compact.get("sample_rows"):
        n = len(compact["sample_rows"])
        if n <= 1:
            break
        compact["sample_rows"] = compact["sample_rows"][: max(1, n // 2)]
        text = json.dumps(compact, ensure_ascii=False, default=str)
    return text[:max_chars]
