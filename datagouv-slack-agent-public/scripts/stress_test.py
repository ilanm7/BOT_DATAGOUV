#!/usr/bin/env python3
"""Stress-test local des composants critiques du bot.

Lance un serveur HTTP local qui sert des fichiers avec/sans extension,
puis teste downloader → analyzer → SQL sur des cas limites.
Aucun besoin de Slack ni de data.gouv.fr en ligne.
"""

import asyncio
import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

# Ajouter le projet au path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.data.analyzer import analyze_file, query_file
from app.data.downloader import _detect_suffix, download_resource
from app.data.charts import generate_bar_chart, generate_categorical_chart, generate_pie_chart
from app.data.exporter import export_to_csv, export_to_excel, save_search_results

# ---------------------------------------------------------------------------
# Serveur HTTP local minimal
# ---------------------------------------------------------------------------
PORT = 0  # attribué dynamiquement

CSV_BODY = b"ville,population,insee\nParis,2161000,75056\nLyon,515695,69123\nMarseille,861635,13055\n"
JSON_BODY = b'[{"ville":"Paris","population":2161000},{"ville":"Lyon","population":515695}]'

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence

    def do_GET(self):
        if self.path == "/exports/csv":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.end_headers()
            self.wfile.write(CSV_BODY)
        elif self.path == "/data.csv":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.end_headers()
            self.wfile.write(CSV_BODY)
        elif self.path == "/data.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(JSON_BODY)
        elif self.path == "/data.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(CSV_BODY)
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.end_headers()
            # 12 Mo de zéros pour tester la limite
            self.wfile.write(b"0," * 1000 + b"\n")
        else:
            self.send_response(404)
            self.end_headers()


def _start_server() -> int:
    global PORT
    sock = socketserver.TCPServer(("", 0), _Handler)
    PORT = sock.server_address[1]
    threading.Thread(target=sock.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return PORT


# ---------------------------------------------------------------------------
# Helpers de test
# ---------------------------------------------------------------------------
_passed = 0
_failed = 0


def _ok(msg: str):
    global _passed
    _passed += 1
    print(f"  ✅ {msg}")


def _ko(msg: str, exc: Exception | None = None):
    global _failed
    _failed += 1
    print(f"  ❌ {msg}")
    if exc:
        print(f"     → {exc}")


# ---------------------------------------------------------------------------
# Scénarios
# ---------------------------------------------------------------------------
async def test_detect_suffix():
    print("\n🧪 _detect_suffix")
    try:
        assert _detect_suffix("http://x.com/a.csv") == ".csv"
        _ok("extension classique .csv")
    except Exception as e:
        _ko("extension classique .csv", e)

    try:
        assert _detect_suffix("http://x.com/api/exports/csv") == ".csv"
        _ok("segment path 'csv' sans point")
    except Exception as e:
        _ko("segment path 'csv' sans point", e)

    try:
        assert _detect_suffix("http://x.com/api/exports/json") == ".json"
        _ok("segment path 'json' sans point")
    except Exception as e:
        _ko("segment path 'json' sans point", e)

    try:
        assert _detect_suffix("http://x.com/data", "text/csv") == ".csv"
        _ok("Content-Type text/csv")
    except Exception as e:
        _ko("Content-Type text/csv", e)

    try:
        assert _detect_suffix("http://x.com/data") == ".bin"
        _ok("inconnu → .bin")
    except Exception as e:
        _ko("inconnu → .bin", e)


async def test_download_and_analyze(base: str, tmpdir: str):
    print("\n🧪 download_resource + analyze_file")

    # 1. URL sans extension mais Content-Type CSV
    try:
        path = await download_resource(f"{base}/exports/csv", tmpdir, max_mb=50)
        assert path.suffix == ".csv", f"attendu .csv, reçu {path.suffix}"
        result = analyze_file(path)
        assert result["rows_scanned"] == 3
        _ok("URL sans extension → .csv détecté + analyse OK")
    except Exception as e:
        _ko("URL sans extension → .csv", e)

    # 2. URL classique .csv
    try:
        path = await download_resource(f"{base}/data.csv", tmpdir, max_mb=50)
        assert path.suffix == ".csv"
        result = analyze_file(path)
        assert "ville" in result["columns"]
        _ok("URL .csv classique")
    except Exception as e:
        _ko("URL .csv classique", e)

    # 3. URL .json
    try:
        path = await download_resource(f"{base}/data.json", tmpdir, max_mb=50)
        assert path.suffix == ".json"
        result = analyze_file(path)
        assert result["format"] == "json"
        _ok("URL .json classique")
    except Exception as e:
        _ko("URL .json classique", e)

    # 4. URL .bin contenant du CSV (fallback)
    try:
        path = await download_resource(f"{base}/data.bin", tmpdir, max_mb=50)
        # Le suffixe reste .bin car Content-Type = octet-stream
        assert path.suffix == ".bin"
        result = analyze_file(path)
        assert result["format"] == "csv"
        _ok(".bin fallback → CSV analysé")
    except Exception as e:
        _ko(".bin fallback → CSV analysé", e)

    # 5. Limite de taille (max_mb=0 → rejet immédiat dès le 1er chunk)
    # URL unique pour éviter le cache hit du test précédent
    try:
        await download_resource(f"{base}/data.csv?limit=test", tmpdir, max_mb=0)
        _ko("limite taille : aurait dû lever une erreur")
    except Exception:
        _ok("limite taille respectée (0 Mo)")


async def test_sql_on_bin(base: str, tmpdir: str):
    print("\n🧪 SQL query sur fichier .bin")
    try:
        path = await download_resource(f"{base}/data.bin", tmpdir, max_mb=50)
        rows = query_file(path, "SELECT * FROM scan WHERE population > 1000000")
        assert len(rows) == 1
        assert rows[0]["ville"] == "Paris"
        _ok("SELECT sur .bin fallback")
    except Exception as e:
        _ko("SELECT sur .bin fallback", e)

    try:
        path = await download_resource(f"{base}/exports/csv", tmpdir, max_mb=50)
        rows = query_file(path, "SELECT SUM(population) as total FROM scan")
        assert rows[0]["total"] == 3538330
        _ok("SUM agrégation sur CSV")
    except Exception as e:
        _ko("SUM agrégation sur CSV", e)


async def test_export_and_charts(tmpdir: str):
    print("\n🧪 Export CSV/Excel + Graphiques")
    fake_results = [
        {"nom": "Alpha", "effectif": 150, "activite_naf": "62.01Z"},
        {"nom": "Beta", "effectif": 80, "activite_naf": "63.11Z"},
        {"nom": "Gamma", "effectif": 220, "activite_naf": "62.01Z"},
    ]
    save_search_results(tmpdir, "test", fake_results)

    try:
        p = export_to_csv(tmpdir)
        assert Path(p).exists()
        _ok("export CSV")
    except Exception as e:
        _ko("export CSV", e)

    try:
        p = export_to_excel(tmpdir)
        assert Path(p).exists()
        _ok("export Excel")
    except Exception as e:
        _ko("export Excel", e)

    try:
        p = generate_bar_chart(fake_results, "nom", "effectif", cache_dir=tmpdir)
        assert Path(p).exists()
        _ok("graphique barres")
    except Exception as e:
        _ko("graphique barres", e)

    try:
        p = generate_pie_chart(fake_results, "nom", "effectif", cache_dir=tmpdir)
        assert Path(p).exists()
        _ok("graphique camembert")
    except Exception as e:
        _ko("graphique camembert", e)

    try:
        p = generate_categorical_chart(fake_results, "activite_naf", cache_dir=tmpdir)
        assert Path(p).exists()
        _ok("graphique catégoriel")
    except Exception as e:
        _ko("graphique catégoriel", e)


async def test_enterprise_search():
    print("\n🧪 API Recherche d'Entreprises (appel réel)")
    settings = get_settings()
    from app.agent.local_tools import execute_local_tool
    try:
        result = await execute_local_tool(
            "search_enterprises",
            {"query": "Mairie de Brest", "max_results": 3},
            settings,
        )
        assert "entreprise" in result.lower() or "brest" in result.lower() or "trouvée" in result.lower()
        _ok("recherche entreprise Brest")
    except Exception as e:
        _ko("recherche entreprise Brest", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    print("=" * 60)
    print("  STRESS-TEST datagouv-slack-agent")
    print("=" * 60)

    port = _start_server()
    base = f"http://127.0.0.1:{port}"
    tmpdir = tempfile.mkdtemp(prefix="stress_")

    await test_detect_suffix()
    await test_download_and_analyze(base, tmpdir)
    await test_sql_on_bin(base, tmpdir)
    await test_export_and_charts(tmpdir)
    await test_enterprise_search()

    print("\n" + "=" * 60)
    print(f"  Résultat : {_passed} passés, {_failed} échoués")
    print("=" * 60)

    # Nettoyage
    for f in Path(tmpdir).glob("*"):
        f.unlink(missing_ok=True)
    Path(tmpdir).rmdir()

    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
