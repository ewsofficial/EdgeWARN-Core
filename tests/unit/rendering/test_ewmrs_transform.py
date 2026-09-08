"""Tests for EWMRS transform utilities (reprojection, timestamp extraction)."""
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

import EWMRS.render.tools as tools


class TestFindTimestamp:
    """Test timestamp extraction from various filename formats."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("MRMS_MergedReflectivityQC_20260317-200000", "2026-03-17T20:00:00"),
            ("MRMS_CompRefQC_20260101-000000", "2026-01-01T00:00:00"),
            ("20260317-200000_renamed", "2026-03-17T20:00:00"),
            ("file_20260317-200000.nc", "2026-03-17T20:00:00"),
            ("data_MRMS_EchoTop18_20260317-150500_grib", "2026-03-17T15:05:00"),
            ("/path/to/file_MRMS_20260317-123000_grib2", "2026-03-17T12:30:00"),
        ],
    )
    def test_patterns_match(self, filename, expected):
        result = tools.TransformUtils.find_timestamp(filename)
        assert result == expected

    def test_no_match_returns_utcnow_fallback(self):
        result = tools.TransformUtils.find_timestamp("no_timestamp_here_file.txt")
        dt = datetime.fromisoformat(result)
        assert isinstance(dt, datetime)

    def test_invalid_date_string_falls_through(self):
        result = tools.TransformUtils.find_timestamp("s00000000000")
        dt = datetime.fromisoformat(result)
        assert isinstance(dt, datetime)

    def test_pathlib_path_input(self):
        result = tools.TransformUtils.find_timestamp(Path("/data/MRMS_20260317-180000.grib2"))
        assert result == "2026-03-17T18:00:00"

    def test_partial_iso_format_grabbed(self):
        result = tools.TransformUtils.find_timestamp("MRMS_20260317-200000")
        assert result == "2026-03-17T20:00:00"


class TestLoadDs:
    """Test dataset loading for grib and netcdf formats."""

    def test_missing_file_returns_none(self):
        result = tools.TransformUtils.load_ds(Path("/nonexistent/file.nc"))
        assert result is None

    def test_gzipped_grib_finds_uncompressed_sibling(self, tmp_path, monkeypatch):
        grib_path = tmp_path / "data.grib2.gz"
        actual_path = tmp_path / "data.grib2"
        grib_path.write_bytes(b"dummy")
        actual_path.write_bytes(b"not really grib")
        monkeypatch.setattr(tools.TransformUtils, "load_ds", lambda *args, **kwargs: "loaded")
        result = tools.TransformUtils.load_ds(grib_path)
        assert result == "loaded"

    def test_netcdf_lat_lon_slice(self, tmp_path):
        result = tools.TransformUtils.load_ds(tmp_path / "nonexistent.nc", lat_limits=(30, 40), lon_limits=(260, 270))
        assert result is None

    def test_grib_with_lat_lon_limits_warns_and_skips(self, tmp_path, capsys):
        grib_file = tmp_path / "data.grib2"
        grib_file.write_bytes(b"fake")
        result = tools.TransformUtils.load_ds(grib_file, lat_limits=(30, 40), lon_limits=(260, 270))
        assert result is None
        captured = capsys.readouterr()
        assert "lat/lon limits not supported" in captured.out


class TestConfigureProjRuntime:
    """Test PROJ runtime configuration."""

    def test_configure_proj_runtime_returns_a_path_or_none(self):
        result = tools.configure_proj_runtime()
        assert result is None or isinstance(result, str)

    def test_proj_data_dir_cached(self):
        result1 = tools.PROJ_DATA_DIR
        result2 = tools.PROJ_DATA_DIR
        assert result1 == result2


class TestOverlayManifestUtils:
    """Test overlay manifest utilities."""

    def test_validate_bounds_accepts_valid_dict(self):
        utils = tools.OverlayManifestUtils()
        bounds = {"north": 7361866.1, "south": 2273030.9, "west": -14471533.8, "east": -6679169.5}
        utils.validate_bounds(bounds)

    def test_validate_bounds_rejects_non_dict(self):
        utils = tools.OverlayManifestUtils()
        with pytest.raises(ValueError, match="Bounds must be a dictionary"):
            utils.validate_bounds("not a dict")

    def test_validate_bounds_rejects_missing_keys(self):
        utils = tools.OverlayManifestUtils()
        with pytest.raises(ValueError, match="Bounds must have keys"):
            utils.validate_bounds({"north": 1, "south": 2})

    def test_validate_bounds_rejects_extra_keys(self):
        utils = tools.OverlayManifestUtils()
        with pytest.raises(ValueError, match="Bounds must have keys"):
            utils.validate_bounds(
                {"north": 1, "south": 2, "west": 3, "east": 4, "extra": 5}
            )

    def test_validate_bounds_rejects_non_numeric(self):
        utils = tools.OverlayManifestUtils()
        with pytest.raises(ValueError, match="must be numeric"):
            utils.validate_bounds({"north": "string", "south": 2, "west": 3, "east": 4})

    def test_add_layer_uses_default_bounds_when_none_provided(self):
        utils = tools.OverlayManifestUtils()
        utils.add_layer("TestLayer", "TestCmap", "/path/to/latest.png", "20260317-200000")
        layers = utils.get_layers()
        assert layers[0]["bounds"] == utils.bounds

    def test_add_layer_accepts_custom_bounds(self):
        utils = tools.OverlayManifestUtils()
        custom = {"north": 100, "south": 0, "west": -200, "east": 100}
        utils.add_layer("TestLayer", "TestCmap", "/path/to/latest.png", "20260317-200000", bounds=custom)
        assert utils.get_layers()[0]["bounds"] == custom

    def test_add_layer_invalid_bounds_falls_back_to_default(self, capsys):
        utils = tools.OverlayManifestUtils()
        utils.add_layer("TestLayer", "TestCmap", "/path/to/latest.png", "20260317-200000", bounds="bad")
        assert utils.get_layers()[0]["bounds"] == utils.bounds
        captured = capsys.readouterr()
        assert "defaulting to default bounds" in captured.out

    def test_save_to_json_round_trip(self, tmp_path):
        utils = tools.OverlayManifestUtils()
        utils.add_layer("L1", "C1", "/p1.png", "20260317-200000")
        utils.add_layer("L2", "C2", "/p2.png", "20260317-200000")
        json_path = tmp_path / "manifest.json"
        utils.save_to_json(str(json_path))
        loaded = json.loads(json_path.read_text())
        assert len(loaded) == 2
        assert loaded[0]["name"] == "L1"

    def test_clear_layers(self):
        utils = tools.OverlayManifestUtils()
        utils.add_layer("L1", "C1", "/p1.png", "20260317-200000")
        utils.clear_layers()
        assert utils.get_layers() == []
