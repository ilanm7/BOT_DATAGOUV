"""Tests du module downloader."""

from app.data.downloader import _detect_suffix, _cache_path
from pathlib import Path


class TestDetectSuffix:
    def test_classic_csv_url(self):
        assert _detect_suffix("https://example.com/data.csv") == ".csv"

    def test_classic_json_url(self):
        assert _detect_suffix("https://example.com/data.json") == ".json"

    def test_opendatasoft_csv_path(self):
        """URL sans point mais avec 'csv' dans le dernier segment."""
        url = (
            "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
            "registre-national-installation-production-stockage-electricite-agrege/"
            "exports/csv?use_labels=true"
        )
        assert _detect_suffix(url) == ".csv"

    def test_opendatasoft_json_path(self):
        url = (
            "https://example.com/api/explore/v2.1/catalog/datasets/foo/"
            "exports/json?use_labels=true"
        )
        assert _detect_suffix(url) == ".json"

    def test_content_type_csv(self):
        assert _detect_suffix("https://example.com/data", "text/csv") == ".csv"

    def test_content_type_excel(self):
        assert (
            _detect_suffix("https://example.com/data", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            == ".xlsx"
        )

    def test_unknown_returns_bin(self):
        assert _detect_suffix("https://example.com/data") == ".bin"

    def test_unknown_with_content_type_parquet(self):
        assert _detect_suffix("https://example.com/data", "application/octet-stream; type=parquet") == ".parquet"


class TestCachePath:
    def test_cache_path_uses_suffix(self):
        cache = Path("/tmp/cache")
        p = _cache_path(cache, "https://example.com/data.csv")
        assert p.suffix == ".csv"

    def test_cache_path_bin_fallback(self):
        cache = Path("/tmp/cache")
        p = _cache_path(cache, "https://example.com/data")
        assert p.suffix == ".bin"
