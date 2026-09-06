"""
Tests for NWS GeoMapper module
"""

import pytest
import json
import shutil
from pathlib import Path
from unittest.mock import patch, mock_open
from EdgeWARN.ingest.nws.geomapper import (
    ZoneLookup,
    _assets_dir,
    _reset_bootstrap,
    ensure_zone_assets,
    extract_exterior_polygon,
    process_warning
)
import common.config.loader as config_loader


@pytest.fixture(autouse=True)
def _reset_zone_asset_precondition():
    """Keep each test independent of the process-local asset check."""
    _reset_bootstrap()
    yield
    _reset_bootstrap()


class TestAssetsDirOwnership:
    """The zone directory has one owner, shared by the writer and the reader."""

    def test_assets_dir_follows_the_relocated_catalog(self, tmp_path, monkeypatch):
        """A relocated config root must move lookups, not just the sync.

        This reader resolved the directory by walking up from its own __file__,
        which agreed with zone_sync.py only for an unrelocated tree. When it
        disagreed, every polygon lookup missed silently.
        """
        alt_repo = tmp_path / "alt_repo"
        alt_config = alt_repo / "config"
        shutil.copytree(Path(__file__).resolve().parents[4] / "config", alt_config)
        catalog = alt_config / "nws.yaml"
        text = catalog.read_text(encoding="utf-8")
        assert "  assets_dir: assets/nws_zones" in text
        catalog.write_text(
            text.replace("  assets_dir: assets/nws_zones", "  assets_dir: relocated_zones"),
            encoding="utf-8",
        )

        monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(alt_config))
        config_loader.reset_cache()
        try:
            assert _assets_dir() == alt_repo / "relocated_zones"
        finally:
            config_loader.reset_cache()

    def test_assets_dir_falls_back_to_bundled_container_snapshot(
        self, tmp_path, monkeypatch
    ):
        alt_repo = tmp_path / "alt_repo"
        alt_config = alt_repo / "config"
        shutil.copytree(Path(__file__).resolve().parents[4] / "config", alt_config)
        bundled = tmp_path / "bundled"
        state_dir = bundled / "TX"
        state_dir.mkdir(parents=True)
        (state_dir / "zones.json").write_text("[]", encoding="utf-8")

        monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(alt_config))
        monkeypatch.setenv("EDGEWARN_BUNDLED_NWS_ZONES_DIR", str(bundled))
        config_loader.reset_cache()
        try:
            assert _assets_dir() == bundled
        finally:
            config_loader.reset_cache()

    def test_operator_assets_override_bundled_container_snapshot(
        self, tmp_path, monkeypatch
    ):
        alt_repo = tmp_path / "alt_repo"
        alt_config = alt_repo / "config"
        shutil.copytree(Path(__file__).resolve().parents[4] / "config", alt_config)
        configured = alt_repo / "assets" / "nws_zones"
        state_dir = configured / "TX"
        state_dir.mkdir(parents=True)
        (state_dir / "zones.json").write_text("[]", encoding="utf-8")
        bundled = tmp_path / "bundled"
        bundled_state = bundled / "OK"
        bundled_state.mkdir(parents=True)
        (bundled_state / "zones.json").write_text("[]", encoding="utf-8")

        monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(alt_config))
        monkeypatch.setenv("EDGEWARN_BUNDLED_NWS_ZONES_DIR", str(bundled))
        config_loader.reset_cache()
        try:
            assert _assets_dir() == configured
        finally:
            config_loader.reset_cache()


class TestZoneLookup:
    """Tests for ZoneLookup class"""

    def test_get_polygon_valid_zone(self, tmp_path):
        """Test getting polygon for a valid zone code"""
        # Setup mock zone data
        zone_data = [
            {"code": "TXC121", "Polygon": [[-97.0, 30.0], [-97.0, 31.0], [-96.0, 31.0], [-96.0, 30.0]]},
            {"code": "TXC122", "Polygon": [[-96.0, 30.0], [-96.0, 31.0], [-95.0, 31.0], [-95.0, 30.0]]}
        ]
        
        # Mock the assets directory
        assets_dir = tmp_path / "assets" / "nws_zones"
        tx_dir = assets_dir / "TX"
        tx_dir.mkdir(parents=True)
        
        zone_file = tx_dir / "zones.json"
        zone_file.write_text(json.dumps(zone_data))
        
        with patch.object(ZoneLookup, '_cache', {}):
            with patch('EdgeWARN.ingest.nws.geomapper._assets_dir', return_value=assets_dir):
                polygon = ZoneLookup.get_polygon("TXC121")
                
                assert polygon is not None
                assert len(polygon) == 4
                assert polygon[0] == [-97.0, 30.0]

    def test_get_polygon_invalid_zone(self, tmp_path):
        """Test getting polygon for an invalid zone code"""
        assets_dir = tmp_path / "assets" / "nws_zones"
        tx_dir = assets_dir / "TX"
        tx_dir.mkdir(parents=True)
        
        zone_file = tx_dir / "zones.json"
        zone_file.write_text(json.dumps([]))
        
        with patch.object(ZoneLookup, '_cache', {}):
            with patch('EdgeWARN.ingest.nws.geomapper._assets_dir', return_value=assets_dir):
                polygon = ZoneLookup.get_polygon("INVALID")
                assert polygon is None

    def test_get_polygon_short_code(self):
        """Test getting polygon for a code that's too short"""
        with patch.object(ZoneLookup, '_cache', {}):
            polygon = ZoneLookup.get_polygon("X")
            assert polygon is None

    def test_caching(self, tmp_path):
        """Test that zone data is cached after first load"""
        zone_data = [{"code": "TXC121", "Polygon": [[-97.0, 30.0]]}]
        
        assets_dir = tmp_path / "assets" / "nws_zones"
        tx_dir = assets_dir / "TX"
        tx_dir.mkdir(parents=True)
        
        zone_file = tx_dir / "zones.json"
        zone_file.write_text(json.dumps(zone_data))
        
        with patch('EdgeWARN.ingest.nws.geomapper._assets_dir', return_value=assets_dir):
            # Clear cache
            ZoneLookup._cache.clear()
            
            # First call should load from file
            polygon1 = ZoneLookup.get_polygon("TXC121")
            assert "TX" in ZoneLookup._cache
            
            # Second call should use cache
            polygon2 = ZoneLookup.get_polygon("TXC121")
            assert polygon1 == polygon2

    def test_missing_assets_explain_how_to_sync(self, tmp_path):
        """An empty asset tree must fail before alert processing begins."""
        assets_dir = tmp_path / "assets" / "nws_zones"
        
        with patch.object(ZoneLookup, '_cache', {}):
            with patch('EdgeWARN.ingest.nws.geomapper._assets_dir', return_value=assets_dir):
                with pytest.raises(
                    RuntimeError, match=r"edgewarn sync-nws-zones --apply"
                ):
                    ZoneLookup.get_polygon("XXC001")

    def test_public_preflight_accepts_existing_zone_assets(self, tmp_path):
        assets_dir = tmp_path / "assets" / "nws_zones"
        state_dir = assets_dir / "TX"
        state_dir.mkdir(parents=True)
        (state_dir / "zones.json").write_text("[]", encoding="utf-8")

        with patch('EdgeWARN.ingest.nws.geomapper._assets_dir', return_value=assets_dir):
            ensure_zone_assets()


class TestExtractExteriorPolygon:
    """Tests for extract_exterior_polygon function"""

    def test_single_polygon(self):
        """Test extracting exterior from single polygon"""
        polygons = [
            [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]
        ]
        
        result = extract_exterior_polygon(polygons)
        
        assert len(result) == 1
        assert len(result[0]) == 5  # Including closing point

    def test_multiple_polygons_union(self):
        """Test union of multiple polygons"""
        # Two overlapping squares
        polygons = [
            [[0, 0], [0, 2], [2, 2], [2, 0], [0, 0]],
            [[1, 1], [1, 3], [3, 3], [3, 1], [1, 1]]
        ]
        
        result = extract_exterior_polygon(polygons)
        
        assert len(result) >= 1

    def test_coordinate_rounding(self):
        """Test that coordinates are rounded to 4 decimal places"""
        polygons = [
            [[0.123456, 0.654321], [0.123456, 1.654321], [1.123456, 1.654321], [1.123456, 0.654321], [0.123456, 0.654321]]
        ]
        
        # Call with tolerance=0 to focus on rounding
        result = extract_exterior_polygon(polygons, tolerance=0)
        
        assert len(result) == 1
        for coord in result[0]:
            assert len(str(coord[0]).split('.')[-1]) <= 4
            assert len(str(coord[1]).split('.')[-1]) <= 4
            assert coord[0] == 0.1235 or coord[0] == 1.1235
            assert coord[1] == 0.6543 or coord[1] == 1.6543

    def test_polygon_simplification(self):
        """Test that polygon simplification reduces point count for clustered points"""
        # A polygon with many redundant points along a line (simulating a detailed coastline/river)
        # We'll add many points between (0,0) and (1,0) that are very close to each other.
        detailed_boundary = [[0, 0]]
        for i in range(1, 100):
            # Points slightly off the line but very close
            x = i / 100.0
            y = 0.00001 if i % 2 == 0 else 0
            detailed_boundary.append([x, y])
        detailed_boundary.extend([[1, 0], [1, 1], [0, 1], [0, 0]])
        
        polygons = [detailed_boundary]
        
        # Without simplification (tolerance=0)
        result_no_sim = extract_exterior_polygon(polygons, tolerance=0)
        # With new default simplification (tolerance=0.03)
        result_sim = extract_exterior_polygon(polygons)
        
        assert len(result_no_sim[0]) > len(result_sim[0])
        # The simplified one should have significantly fewer points
        assert len(result_sim[0]) < 10 # Should be around 5 (square-ish)

    def test_empty_input(self):
        """Test with empty input"""
        result = extract_exterior_polygon([])
        assert result == []

    def test_invalid_polygons(self):
        """Test with invalid polygon data"""
        polygons = [
            [],  # Empty polygon
            [[0, 0]],  # Too few points
            "invalid",  # Wrong type
            [[0, 0], [1, 1], [2, 2]]  # Valid but not closed
        ]
        
        result = extract_exterior_polygon(polygons)
        # Should handle gracefully
        assert isinstance(result, list)


class TestProcessWarning:
    """Tests for process_warning function"""

    def test_process_warning_with_geocodes(self, tmp_path):
        """Test processing warning with geocodes"""
        # Setup zone data
        zone_data = [
            {"code": "TXC121", "Polygon": [[-97.0, 30.0], [-97.0, 31.0], [-96.0, 31.0], [-96.0, 30.0]]}
        ]
        
        assets_dir = tmp_path / "assets" / "nws_zones"
        tx_dir = assets_dir / "TX"
        tx_dir.mkdir(parents=True)
        
        zone_file = tx_dir / "zones.json"
        zone_file.write_text(json.dumps(zone_data))
        
        feature = {
            "properties": {
                "geocode": {
                    "SAME": ["048121"]
                },
                "event": "Severe Thunderstorm Warning",
                "references": "some-ref",
                "sender": "NWS"
            },
            "geometry": None
        }
        
        with patch.object(ZoneLookup, '_cache', {}):
            with patch('EdgeWARN.ingest.nws.geomapper._assets_dir', return_value=assets_dir):
                result = process_warning(feature)
                
                assert "properties" in result
                assert "geometry" in result
                # Junk keys should be removed
                assert "references" not in result["properties"]
                assert "sender" not in result["properties"]

    def test_process_warning_without_geocodes(self):
        """Test processing warning without geocodes"""
        feature = {
            "properties": {
                "event": "Severe Thunderstorm Warning"
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]
            }
        }
        
        result = process_warning(feature)
        
        assert result["properties"]["event"] == "Severe Thunderstorm Warning"
        # Original geometry should be preserved (only rounded)
        assert result["geometry"]["type"] == "Polygon"

    def test_process_warning_cleans_properties(self):
        """Test that junk keys are removed from properties"""
        feature = {
            "properties": {
                "event": "Tornado Warning",
                "references": "ref123",
                "sender": "NWS",
                "parameters": {},
                "instruction": "Take shelter",
                "response": "Shelter",
                "scope": "Public",
                "code": "TOR",
                "language": "en",
                "web": "https://weather.gov",
                "eventCode": "TOR",
                "severity": "Extreme"  # This should remain
            },
            "geometry": None
        }
        
        result = process_warning(feature)
        
        # Junk keys should be removed
        for key in ["references", "sender", "parameters", "instruction", 
                    "response", "scope", "code", "language", "web", "eventCode"]:
            assert key not in result["properties"]
        
        # Valid keys should remain
        assert result["properties"]["event"] == "Tornado Warning"
        assert result["properties"]["severity"] == "Extreme"

    def test_process_warning_skips_mapping_if_geometry_exists(self, tmp_path):
        """Test that zone mapping is skipped if geometry already exists (prevents simplification)"""
        # Detailed geometry that shouldn't be simplified
        detailed_coords = [[[0.123456, 0.654321], [0.123456, 1.654321], [1.123456, 1.654321], [1.123456, 0.654321], [0.123456, 0.654321]]]
        
        feature = {
            "properties": {
                "event": "Severe Thunderstorm Warning",
                "geocode": {
                    "UGC": ["TXC121"]
                }
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": detailed_coords
            }
        }
        
        # Mock ZoneLookup to ensure it's NOT called
        with patch.object(ZoneLookup, 'get_polygon') as mock_get_poly:
            result = process_warning(feature)
            
            # 1. Zone mapping should be skipped
            mock_get_poly.assert_not_called()
            assert "Polygon" not in result
            
            # 2. Original geometry should be rounded but NOT simplified (point count remains same)
            assert result["geometry"]["type"] == "Polygon"
            assert len(result["geometry"]["coordinates"][0]) == 5
            
            # 3. Check rounding
            rounded_point = result["geometry"]["coordinates"][0][0]
            assert rounded_point == [0.1235, 0.6543]
            
            # 4. geocode should still be popped for cleanliness
            assert "geocode" not in result["properties"]

    def test_process_warning_skips_mapping_if_polygon_key_exists(self, tmp_path):
        """Test that zone mapping is skipped if Polygon key already exists"""
        # Feature with existing Polygon key (from a previous mapping run)
        existing_polygon = [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]
        
        feature = {
            "properties": {
                "event": "Tornado Warning",
                "geocode": {
                    "UGC": ["TXC121"]
                }
            },
            "Polygon": existing_polygon
        }
        
        # Mock ZoneLookup to ensure it's NOT called
        with patch.object(ZoneLookup, 'get_polygon') as mock_get_poly:
            result = process_warning(feature)
            
            # 1. Zone mapping should be skipped
            mock_get_poly.assert_not_called()
            
            # 2. Existing Polygon should still exist (and be rounded)
            assert "Polygon" in result
            assert result["Polygon"] == existing_polygon
            
            # 3. geocode should be popped
            assert "geocode" not in result["properties"]
