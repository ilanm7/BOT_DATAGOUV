"""Tests du module analyzer."""

import tempfile
from pathlib import Path

import pytest

from app.data.analyzer import analyze_file, query_file, AnalysisError


class TestAnalyzeFile:
    def test_csv_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("nom,age\nAlice,30\nBob,25\n")
            path = Path(f.name)
        try:
            result = analyze_file(path)
            assert result["format"] == "csv"
            assert "nom" in result["columns"]
            assert result["rows_scanned"] == 2
        finally:
            path.unlink()

    def test_bin_fallback_to_csv(self):
        """Un fichier .bin contenant du CSV doit être analysé comme CSV."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bin", delete=False) as f:
            f.write("ville,population\nParis,2161000\nLyon,515695\n")
            path = Path(f.name)
        try:
            result = analyze_file(path)
            assert result["format"] == "csv"
            assert "ville" in result["columns"]
            assert result["rows_scanned"] == 2
        finally:
            path.unlink()

    def test_json_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('[{"nom": "Alice", "age": 30}, {"nom": "Bob", "age": 25}]')
            path = Path(f.name)
        try:
            result = analyze_file(path)
            assert result["format"] == "json"
            assert "nom" in result["columns"]
        finally:
            path.unlink()

    def test_unsupported_format_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".foo", delete=False) as f:
            f.write("hello")
            path = Path(f.name)
        try:
            with pytest.raises(AnalysisError):
                analyze_file(path)
        finally:
            path.unlink()


class TestQueryFile:
    def test_query_csv(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("a,b\n1,2\n3,4\n5,6\n")
            path = Path(f.name)
        try:
            rows = query_file(path, "SELECT * FROM scan WHERE a > 2")
            assert len(rows) == 2
            assert rows[0]["a"] == 3
        finally:
            path.unlink()

    def test_query_bin_fallback(self):
        """Un fichier .bin doit accepter une requête SQL traité comme CSV."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bin", delete=False) as f:
            f.write("x,y\n10,20\n30,40\n50,60\n")
            path = Path(f.name)
        try:
            rows = query_file(path, "SELECT SUM(x) as total FROM scan")
            assert len(rows) == 1
            assert rows[0]["total"] == 90
        finally:
            path.unlink()

    def test_non_select_query_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("a\n1\n")
            path = Path(f.name)
        try:
            with pytest.raises(AnalysisError):
                query_file(path, "DROP TABLE scan")
        finally:
            path.unlink()

    @pytest.mark.parametrize("sql", [
        "SELECT content FROM read_text('{secret}')",
        "SELECT size FROM read_blob('{secret}')",
        "SELECT * FROM glob('{secret}')",
        "SELECT * FROM '{secret}'",
    ])
    def test_query_cannot_read_other_files(self, sql, tmp_path):
        data = tmp_path / "data.csv"
        data.write_text("a\n1\n")
        secret = tmp_path / "secret.csv"
        secret.write_text("TOKEN\nxoxb\n")
        with pytest.raises(AnalysisError, match="DuckDB erreur SQL"):  # refusé par DuckDB, pas par le filtre
            query_file(data, sql.format(secret=secret))
