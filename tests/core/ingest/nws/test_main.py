"""
Tests for NWS ingest main module
"""

import pytest
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from EdgeWARN.ingest.nws.main import download_alerts, _get_registry
from EdgeWARN.ingest.nws.registry import reset_registry

class TestDownloadAlerts:
    """Tests for download_alerts function"""

    @pytest.fixture
    def mock_io(self):
        """Mock the module-level io_manager"""
        with patch('EdgeWARN.ingest.nws.main.io_manager') as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def reset_registry_fixture(self):
        """Reset the singleton registry instance before each test"""
        reset_registry()

    @pytest.fixture
    def empty_response(self):
        """Return a valid empty GeoJSON response"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "type": "FeatureCollection",
            "features": []
        }).encode('utf-8')
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    def test_download_creates_output_directory(self, mock_io, empty_response, tmp_path):
        """Test that output directory is created"""
        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', return_value=empty_response):
            download_alerts(datetime(2023, 10, 15, 14, 30))
            
            assert tmp_path.exists()

    def test_download_creates_correct_filename(self, mock_io, empty_response, tmp_path):
        """Test that correct registry file is saved"""
        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', return_value=empty_response):
            download_alerts(datetime(2023, 10, 15, 14, 30))
            
            # Should create registry file
            assert (tmp_path / "timestamps").exists()

    def test_download_filters_dropped_events(self, mock_io, tmp_path):
        """Test that events in DROPPED_EVENTS are excluded"""
        # Mock urllib response with mixed events
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "features": [
                {
                    "properties": {
                        "id": "1",
                        "event": "Severe Thunderstorm Warning",
                        "geocode": {"SAME": ["048121"]}
                    },
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0]]]}
                },
                {
                    "properties": {
                        "id": "2",
                        "event": "Administrative Message",  # Should be dropped
                        "geocode": {"SAME": ["048121"]}
                    },
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0]]]}
                }
            ]
        }).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        
        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', return_value=mock_response):
            
            download_alerts(datetime(2023, 10, 15, 14, 30, tzinfo=timezone.utc))
            
            # Read output file
            registry = _get_registry()
            alerts = registry.get_active_alerts()
            
            assert len(alerts) == 1
            events = [a["properties"]["event"] for a in alerts]
            assert "Severe Thunderstorm Warning" in events
            assert "Administrative Message" not in events

    def test_download_applies_geomapper(self, mock_io, tmp_path):
        """Test that GeoMapper is applied to alerts"""
        # Mock urllib response with alert that has geocode
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "features": [
                {
                    "properties": {
                        "id": "1",
                        "event": "Severe Thunderstorm Warning",
                        "geocode": {"SAME": ["048121"]},
                        "references": "should be removed"
                    },
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0]]]}
                }
            ]
        }).encode('utf-8')
        mock_response.__enter__.return_value = mock_response
        
        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', return_value=mock_response):
            
            download_alerts(datetime(2023, 10, 15, 14, 30, tzinfo=timezone.utc))
            
            registry = _get_registry()
            alerts = registry.get_active_alerts()
            
            assert len(alerts) == 1
            assert "references" not in alerts[0]["properties"]

    def test_download_handles_network_error(self, mock_io, tmp_path):
        """Test handling of network errors"""
        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', side_effect=Exception("Network error")):
            
            with pytest.raises(Exception):
                download_alerts(datetime(2023, 10, 15, 14, 30))
            
            # Should log error
            mock_io.write_error.assert_called_once()

    def test_download_cleans_old_files(self, mock_io, empty_response, tmp_path):
        """Test that old registry items are cleaned up instead of old files."""
        # Note: clean_files_by_age logic has been replaced by registry cleanup
        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('EdgeWARN.ingest.nws.main._get_registry') as mock_get_registry, \
             patch('urllib.request.urlopen', return_value=empty_response):
            
            mock_registry = MagicMock()
            mock_registry.cleanup_expired.return_value = 0
            mock_get_registry.return_value = mock_registry
            download_alerts(datetime(2023, 10, 15, 14, 30))
            
            # Should reconcile to latest upstream active IDs
            mock_registry.reconcile_with_active_ids.assert_called_once()
            # Should call cleanup_expired
            mock_registry.cleanup_expired.assert_called_once()

    def test_download_with_custom_base_dir(self, mock_io, empty_response, tmp_path):
        """Test download respects base registry paths"""
        custom_dir = tmp_path / "custom_nws"
        
        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', custom_dir), \
             patch('urllib.request.urlopen', return_value=empty_response):
            
            download_alerts(datetime(2023, 10, 15, 14, 30))
            
            assert custom_dir.exists()

    def test_download_writes_enriched_timestamp_snapshot(self, mock_io, tmp_path):
        """Test download persists precomputed official snapshot summaries for the API."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "features": [
                {
                    "id": "https://api.weather.gov/alerts/urn:oid:test-alert-1",
                    "type": "Feature",
                    "properties": {
                        "event": "Severe Thunderstorm Warning",
                        "effective": "2023-10-15T14:00:00Z",
                        "expires": "2023-10-15T15:00:00Z",
                        "geocode": {"SAME": ["048121"]}
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]
                    }
                }
            ]
        }).encode('utf-8')
        mock_response.__enter__.return_value = mock_response

        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', return_value=mock_response):

            download_alerts(datetime(2023, 10, 15, 14, 30, tzinfo=timezone.utc))

            snapshot_path = tmp_path / 'timestamps' / '20231015-143000.json'
            with open(snapshot_path, 'r', encoding='utf-8') as f:
                snapshot_data = json.load(f)

            assert snapshot_data['count'] == 1
            assert snapshot_data['alerts'][0]['id'] == 'urn:oid:test-alert-1'
            assert snapshot_data['alerts'][0]['name'] == 'Severe Thunderstorm Warning'
            assert snapshot_data['alerts'][0]['urn_oid'] == 'urn:oid:test-alert-1'
            assert snapshot_data['alerts'][0]['effective'] == '2023-10-15T14:00:00Z'
            assert snapshot_data['alerts'][0]['expires'] == '2023-10-15T15:00:00Z'
            assert snapshot_data['alerts'][0]['geometry']['type'] == 'Polygon'

    def test_download_reconciles_and_drops_missing_active_alerts(self, mock_io, tmp_path):
        """Second ingest cycle should drop prior IDs absent from latest active payload."""
        cycle_one = MagicMock()
        cycle_one.read.return_value = json.dumps({
            "features": [
                {
                    "id": "https://api.weather.gov/alerts/urn:oid:test-alert-a",
                    "type": "Feature",
                    "properties": {
                        "event": "Severe Thunderstorm Warning",
                        "effective": "2023-10-15T14:00:00Z",
                        "expires": "2023-10-15T15:30:00Z",
                        "geocode": {"SAME": ["048121"]}
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]
                    }
                },
                {
                    "id": "https://api.weather.gov/alerts/urn:oid:test-alert-b",
                    "type": "Feature",
                    "properties": {
                        "event": "Tornado Warning",
                        "effective": "2023-10-15T14:01:00Z",
                        "expires": "2023-10-15T15:31:00Z",
                        "geocode": {"SAME": ["048121"]}
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]]]
                    }
                }
            ]
        }).encode('utf-8')
        cycle_one.__enter__.return_value = cycle_one

        cycle_two = MagicMock()
        cycle_two.read.return_value = json.dumps({
            "features": [
                {
                    "id": "https://api.weather.gov/alerts/urn:oid:test-alert-b",
                    "type": "Feature",
                    "properties": {
                        "event": "Tornado Warning",
                        "effective": "2023-10-15T14:02:00Z",
                        "expires": "2023-10-15T15:31:00Z",
                        "geocode": {"SAME": ["048121"]}
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]]]
                    }
                }
            ]
        }).encode('utf-8')
        cycle_two.__enter__.return_value = cycle_two

        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', side_effect=[cycle_one, cycle_two]):
            download_alerts(datetime(2023, 10, 15, 14, 30, tzinfo=timezone.utc))
            download_alerts(datetime(2023, 10, 15, 14, 32, tzinfo=timezone.utc))

            registry = _get_registry()
            active_ids = set(registry.get_active_ids())
            assert active_ids == {"urn:oid:test-alert-b"}

            latest_snapshot = tmp_path / "timestamps" / "20231015-143200.json"
            with open(latest_snapshot, 'r', encoding='utf-8') as f:
                snapshot_data = json.load(f)

            snapshot_ids = {item["id"] for item in snapshot_data["alerts"]}
            assert snapshot_ids == {"urn:oid:test-alert-b"}
            assert snapshot_data["count"] == 1

    def test_empty_feed_preserves_unexpired_alerts(self, mock_io, tmp_path):
        """An empty pull preserves unexpired alerts until their normal expiration."""
        cycle_one = MagicMock()
        cycle_one.read.return_value = json.dumps({
            "features": [
                {
                    "id": "https://api.weather.gov/alerts/urn:oid:test-alert-a",
                    "type": "Feature",
                    "properties": {
                        "event": "Severe Thunderstorm Warning",
                        "effective": "2023-10-15T14:00:00Z",
                        "expires": "2023-10-15T15:30:00Z",
                        "geocode": {"SAME": ["048121"]}
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]
                    }
                }
            ]
        }).encode('utf-8')
        cycle_one.__enter__.return_value = cycle_one

        cycle_two = MagicMock()
        cycle_two.read.return_value = json.dumps({
            "type": "FeatureCollection",
            "features": []
        }).encode('utf-8')
        cycle_two.__enter__.return_value = cycle_two

        with patch('EdgeWARN.ingest.nws.main.fs.MRMS_NWS_DIR', tmp_path), \
             patch('urllib.request.urlopen', side_effect=[cycle_one, cycle_two]):
            download_alerts(datetime(2023, 10, 15, 14, 30, tzinfo=timezone.utc))
            download_alerts(datetime(2023, 10, 15, 14, 32, tzinfo=timezone.utc))

            registry = _get_registry()
            assert registry.get_active_ids() == ["urn:oid:test-alert-a"]

            latest_snapshot = tmp_path / "timestamps" / "20231015-143200.json"
            with open(latest_snapshot, 'r', encoding='utf-8') as f:
                snapshot_data = json.load(f)

            assert snapshot_data["count"] == 1
            assert snapshot_data["alerts"][0]["id"] == "urn:oid:test-alert-a"


def test_malformed_feature_does_not_block_valid_feature(tmp_path, monkeypatch):
    from common.ingest.nws import main
    from common.ingest.nws.registry import AlertRegistry
    path = tmp_path / "feed.json"
    path.write_text(json.dumps({"features": [
        {"id": "bad", "properties": None},
        {"id": "good", "properties": {"id": "good", "event": "Tornado Warning",
         "expires": "2026-10-07T19:00:00Z"}, "geometry": None}]}))
    registry = AlertRegistry(tmp_path / "registry")
    monkeypatch.setattr(main, "process_warning", lambda feature: feature)
    _, _, seen = main._process_nws_file_with_registry(path, registry,
        datetime(2026, 10, 7, 18, tzinfo=timezone.utc))
    assert "good" in seen
    assert "good" in registry.get_active_ids()
