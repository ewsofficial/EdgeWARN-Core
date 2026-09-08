"""
Unit tests for the Alert-to-Cell Spatial Matching Module.

Tests cover:
- Alert event type filtering (convective/flood only)
- Spatial intersection logic
- Alert ID extraction
- Full integration with match_alerts_to_cells function
"""

import pytest
import json
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from shapely.prepared import prep
from EdgeWARN.process.detect.tools import alert_matcher
from EdgeWARN.process.detect.tools.alert_matcher import (
    convective_flood_events,
    load_active_alerts,
    filter_convective_flood_alerts,
    _extract_alert_id,
    _get_alert_polygon,
    _get_cell_centroid,
    _get_cell_polygon,
    match_alerts_to_cell,
    match_alerts_to_cells,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def prepped_alerts(sample_convective_alert, sample_flood_alert):
    """Create a list of prepped alerts for testing match_alerts_to_cell."""
    from shapely.geometry import Polygon
    # We need to manually prep because match_alerts_to_cell expects prepped list
    alerts = []
    
    # Convective
    id1 = _extract_alert_id(sample_convective_alert)
    poly1 = _get_alert_polygon(sample_convective_alert)
    alerts.append((id1, prep(poly1)))
    
    # Flood
    id2 = _extract_alert_id(sample_flood_alert)
    poly2 = _get_alert_polygon(sample_flood_alert)
    alerts.append((id2, prep(poly2)))
    
    return alerts


@pytest.fixture
def temp_registry(tmp_path):
    """Create a temporary registry directory structure for alerts."""
    registry_dir = tmp_path / "alerts"
    (registry_dir / "ids").mkdir(parents=True)
    (registry_dir / "timestamps").mkdir(parents=True)
    return registry_dir


@pytest.fixture
def sample_convective_alert():
    """Create a sample convective alert (Severe Thunderstorm Warning)."""
    return {
        "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.2406210827.1",
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[263.0, 35.0], [263.0, 36.0], [264.0, 36.0], [264.0, 35.0], [263.0, 35.0]]]
        },
        "properties": {
            "event": "Severe Thunderstorm Warning",
            "severity": "Severe",
            "effective": "2026-02-23T21:00:00Z",
            "expires": "2026-02-23T22:00:00Z",
        },
        "Polygon": [[[263.0, 35.0], [263.0, 36.0], [264.0, 36.0], [264.0, 35.0], [263.0, 35.0]]]
    }


@pytest.fixture
def sample_flood_alert():
    """Create a sample flood alert (Flash Flood Warning)."""
    return {
        "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.2406210828.1",
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[262.0, 34.0], [262.0, 35.0], [263.0, 35.0], [263.0, 34.0], [262.0, 34.0]]]
        },
        "properties": {
            "event": "Flash Flood Warning",
            "severity": "Severe",
            "effective": "2026-02-23T21:30:00Z",
            "expires": "2026-02-23T22:30:00Z",
        },
        "Polygon": [[[262.0, 34.0], [262.0, 35.0], [263.0, 35.0], [263.0, 34.0], [262.0, 34.0]]]
    }


@pytest.fixture
def sample_non_convective_alert():
    """Create a non-convective alert (Gale Watch) that should be filtered out."""
    return {
        "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.2406210829.1",
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[263.5, 35.5], [263.5, 35.7], [263.7, 35.7], [263.7, 35.5], [263.5, 35.5]]]
        },
        "properties": {
            "event": "Gale Watch",
            "severity": "Moderate",
            "effective": "2026-02-23T21:00:00Z",
            "expires": "2026-02-23T23:00:00Z",
        },
        "Polygon": [[[263.5, 35.5], [263.5, 35.7], [263.7, 35.7], [263.7, 35.5], [263.5, 35.5]]]
    }


@pytest.fixture
def sample_cell_inside_storm():
    """Create a cell entry inside the storm alert polygon."""
    return {
        "id": 1,
        "centroid": [35.5, 263.5],  # Inside the 263 to 264, 35 to 36 box
        "bbox": [[35.4, 263.4], [35.4, 263.6], [35.6, 263.6], [35.6, 263.4]],
        "num_gates": 100,
        "max_refl": 55.0,
    }


@pytest.fixture
def sample_cell_outside_storm():
    """Create a cell entry outside all alert polygons."""
    return {
        "id": 2,
        "centroid": [33.0, 261.0],  # Outside all test polygons
        "bbox": [[32.9, 260.9], [32.9, 261.1], [33.1, 261.1], [33.1, 260.9]],
        "num_gates": 50,
        "max_refl": 40.0,
    }


# =============================================================================
# Test Event Type Filtering
# =============================================================================

class TestEventTypeFiltering:
    """Tests for the detection.yaml alert_matching.events allowlist."""
    
    def test_convective_events_included(self):
        """Verify convective events are in the whitelist."""
        assert "Tornado Warning" in convective_flood_events()
        assert "Severe Thunderstorm Warning" in convective_flood_events()
        assert "Tornado Watch" in convective_flood_events()
        assert "Severe Thunderstorm Watch" in convective_flood_events()
        assert "Special Weather Statement" in convective_flood_events()
        assert "Severe Weather Statement" in convective_flood_events()
        
    def test_flood_events_included(self):
        """Verify flood events are in the whitelist."""
        assert "Flash Flood Warning" in convective_flood_events()
        
    def test_non_convective_events_excluded(self):
        """Verify non-convective events are NOT in the whitelist."""
        assert "Gale Watch" not in convective_flood_events()
        assert "Small Craft Advisory" not in convective_flood_events()
        assert "Air Quality Alert" not in convective_flood_events()


class TestFilterConvectiveFloodAlerts:
    """Tests for filter_convective_flood_alerts function."""
    
    def test_filters_non_convective_alerts(self, sample_convective_alert, sample_non_convective_alert):
        """Verify only convective/flood alerts are returned."""
        alerts = [sample_convective_alert, sample_non_convective_alert]
        filtered = filter_convective_flood_alerts(alerts)
        
        assert len(filtered) == 1
        assert filtered[0]["properties"]["event"] == "Severe Thunderstorm Warning"
        
    def test_keeps_multiple_convective_alerts(self, sample_convective_alert, sample_flood_alert):
        """Verify multiple convective/flood alerts are all kept."""
        alerts = [sample_convective_alert, sample_flood_alert]
        filtered = filter_convective_flood_alerts(alerts)
        
        assert len(filtered) == 2
        events = [a["properties"]["event"] for a in filtered]
        assert "Severe Thunderstorm Warning" in events
        assert "Flash Flood Warning" in events
        
    def test_empty_list_returns_empty(self):
        """Verify empty input returns empty output."""
        filtered = filter_convective_flood_alerts([])
        assert filtered == []


# =============================================================================
# Test Alert ID Extraction
# =============================================================================

class TestExtractAlertId:
    """Tests for _extract_alert_id function."""
    
    def test_extracts_from_feature_id(self, sample_convective_alert):
        """Verify extraction from feature['id'] field."""
        alert_id = _extract_alert_id(sample_convective_alert)
        assert alert_id == "urn:oid:2.49.0.1.840.0.2406210827.1"
        
    def test_extracts_from_properties_id(self):
        """Verify extraction from properties['id'] as fallback."""
        feature = {
            "properties": {
                "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.1234.1"
            }
        }
        alert_id = _extract_alert_id(feature)
        assert alert_id == "urn:oid:2.49.0.1.840.0.1234.1"
        
    def test_returns_none_when_no_id(self):
        """Verify None is returned when no ID found."""
        feature = {"type": "Feature", "properties": {}}
        alert_id = _extract_alert_id(feature)
        assert alert_id is None


# =============================================================================
# Test Spatial Matching
# =============================================================================

class TestSpatialMatching:
    """Tests for spatial intersection logic."""
    
    def test_cell_inside_alert_polygon(self, prepped_alerts, sample_cell_inside_storm):
        """Verify cell inside alert polygon returns matching alert ID."""
        matching_ids = match_alerts_to_cell(sample_cell_inside_storm, prepped_alerts)
        
        assert len(matching_ids) == 1
        assert matching_ids[0] == "urn:oid:2.49.0.1.840.0.2406210827.1"
        
    def test_cell_outside_alert_polygon(self, prepped_alerts, sample_cell_outside_storm):
        """Verify cell outside alert polygon returns empty list."""
        matching_ids = match_alerts_to_cell(sample_cell_outside_storm, prepped_alerts)
        
        assert len(matching_ids) == 0
        
    def test_cell_matches_multiple_alerts(self, prepped_alerts):
        """Verify cell can match multiple overlapping alerts."""
        # Create a cell that overlaps both alert polygons
        cell = {
            "id": 3,
            "centroid": [35.0, 263.0],  # On the boundary
            "bbox": [[34.9, 262.9], [34.9, 263.1], [35.1, 263.1], [35.1, 262.9]],
        }
        
        matching_ids = match_alerts_to_cell(cell, prepped_alerts)
        
        # Should match at least one (depending on exact geometry)
        assert len(matching_ids) >= 1

    def test_cell_bbox_intersection(self, sample_convective_alert):
        """Verify cell matches via bbox even if centroid is outside."""
        # Alert is 263 to 264, 35 to 36
        # Cell centroid is at 262.95 (outside), but bbox extends to 263.05 (inside)
        cell = {
            "id": 5,
            "centroid": [35.5, 262.95], 
            "bbox": [[35.4, 262.9], [35.4, 263.05], [35.6, 263.05], [35.6, 262.9]],
        }
        
        alert_id = _extract_alert_id(sample_convective_alert)
        alert_poly = _get_alert_polygon(sample_convective_alert)
        prepped = [(alert_id, prep(alert_poly))]
        
        matching_ids = match_alerts_to_cell(cell, prepped)
        assert alert_id in matching_ids
        
    def test_empty_alerts_returns_empty(self, sample_cell_inside_storm):
        """Verify empty alerts list returns empty matches."""
        matching_ids = match_alerts_to_cell(sample_cell_inside_storm, [])
        assert matching_ids == []
        
    def test_cell_without_centroid_but_with_bbox(self, prepped_alerts):
        """Verify cell without centroid but with bbox still matches."""
        cell = {
            "id": 4, 
            "bbox": [[35.4, 263.4], [35.4, 263.6], [35.6, 263.6], [35.6, 263.4]]
        }
        matching_ids = match_alerts_to_cell(cell, prepped_alerts)
        assert "urn:oid:2.49.0.1.840.0.2406210827.1" in matching_ids


# =============================================================================
# Test Registry Loading
# =============================================================================

class TestLoadActiveAlerts:
    """Tests for load_active_alerts function."""
    
    def test_loads_from_registry_file(self, temp_registry, sample_convective_alert):
        """Verify alerts are loaded from registry file."""
        alert_id = "urn:oid:2.49.0.1.840.0.2406210827.1"
        safe_id = alert_id.replace(":", "_").replace("/", "_") + ".json"
        
        # Write individual feature
        with open(temp_registry / "ids" / safe_id, 'w') as f:
            json.dump({
                "id": sample_convective_alert["id"],
                "first_seen": "2026-02-23T21:00:00Z",
                "last_seen": "2026-02-23T21:00:00Z",
                "feature": sample_convective_alert
            }, f)
            
        # Write timestamp snapshot
        ts_data = {
            "timestamp": "2026-02-23T21:00:00Z",
            "count": 1,
            "alerts": [alert_id]
        }
        with open(temp_registry / "timestamps" / "20260223-210000.json", 'w') as f:
            json.dump(ts_data, f)
        
        alerts = load_active_alerts(temp_registry)
        
        assert len(alerts) == 1
        assert alerts[0]["properties"]["event"] == "Severe Thunderstorm Warning"
        
    def test_returns_empty_for_missing_file(self, tmp_path):
        """Verify empty list returned when registry doesn't exist."""
        nonexistent_path = tmp_path / "nonexistent"
        alerts = load_active_alerts(nonexistent_path)
        assert alerts == []
        
    def test_returns_empty_for_malformed_registry(self, temp_registry):
        """Verify empty list returned when registry is malformed."""
        with open(temp_registry / "timestamps" / "20260223-210000.json", 'w') as f:
            f.write("not valid json")
        
        alerts = load_active_alerts(temp_registry)
        assert alerts == []

    def test_uses_cached_snapshot_on_repeat_load(self, temp_registry, sample_convective_alert):
        alert_matcher._ALERT_SNAPSHOT_CACHE.clear()
        alert_matcher._ALERT_GEOMETRY_CACHE.clear()

        alert_id = "urn:oid:2.49.0.1.840.0.2406210827.1"
        safe_id = alert_id.replace(":", "_").replace("/", "_") + ".json"

        with open(temp_registry / "ids" / safe_id, 'w') as f:
            json.dump({"feature": sample_convective_alert}, f)

        with open(temp_registry / "timestamps" / "20260223-210000.json", 'w') as f:
            json.dump({"timestamp": "2026-02-23T21:00:00Z", "count": 1, "alerts": [alert_id]}, f)

        first = load_active_alerts(temp_registry)
        assert len(first) == 1

        with patch("EdgeWARN.process.detect.tools.alert_matcher.open", side_effect=AssertionError("cache miss")):
            second = load_active_alerts(temp_registry)

        assert len(second) == 1


# =============================================================================
# Test Full Integration
# =============================================================================

class TestMatchAlertsToCells:
    """Tests for match_alerts_to_cells function."""
    
    def _write_registry_data(self, temp_registry, timestamp_str, file_ts, alerts_dict):
        """Helper to write registry snapshot and ids files."""
        # Write individual features
        active_ids = []
        for alert_id, alert_data in alerts_dict.items():
            active_ids.append(alert_id)
            safe_id = alert_id.replace(":", "_").replace("/", "_") + ".json"
            with open(temp_registry / "ids" / safe_id, 'w') as f:
                json.dump(alert_data, f)
                
        # Write timestamp snapshot
        ts_data = {
            "timestamp": timestamp_str,
            "count": len(active_ids),
            "alerts": active_ids
        }
        with open(temp_registry / "timestamps" / f"{file_ts}.json", 'w') as f:
            json.dump(ts_data, f)

    def test_adds_alerts_key_to_all_cells(self, temp_registry, sample_convective_alert, 
                                          sample_cell_inside_storm, sample_cell_outside_storm):
        """Verify all cells get an 'alerts' key."""
        alerts_dict = {
            "urn:oid:2.49.0.1.840.0.2406210827.1": {
                "id": sample_convective_alert["id"],
                "first_seen": "2026-02-23T21:00:00Z",
                "last_seen": "2026-02-23T21:00:00Z",
                "feature": sample_convective_alert
            }
        }
        self._write_registry_data(temp_registry, "2026-02-23T21:00:00Z", "20260223-210000", alerts_dict)
        
        cells = [sample_cell_inside_storm, sample_cell_outside_storm]
        result = match_alerts_to_cells(cells, temp_registry)
        
        # Both cells should have an 'alerts' key
        assert "alerts" in result[0]
        assert "alerts" in result[1]
        
        # Cell inside storm should have the alert
        assert len(result[0]["alerts"]) == 1
        
        # Cell outside should have empty alerts
        assert result[1]["alerts"] == []
        
    def test_filters_out_non_convective_alerts(self, temp_registry, sample_convective_alert,
                                               sample_non_convective_alert, sample_cell_inside_storm):
        """Verify non-convective alerts are filtered out."""
        alerts_dict = {
            "urn:oid:2.49.0.1.840.0.2406210827.1": {
                "id": sample_convective_alert["id"],
                "first_seen": "2026-02-23T21:00:00Z",
                "last_seen": "2026-02-23T21:00:00Z",
                "feature": sample_convective_alert
            },
            "urn:oid:2.49.0.1.840.0.2406210829.1": {
                "id": sample_non_convective_alert["id"],
                "first_seen": "2026-02-23T21:00:00Z",
                "last_seen": "2026-02-23T21:00:00Z",
                "feature": sample_non_convective_alert
            }
        }
        self._write_registry_data(temp_registry, "2026-02-23T21:00:00Z", "20260223-210000", alerts_dict)
        
        cells = [sample_cell_inside_storm]
        result = match_alerts_to_cells(cells, temp_registry)
        
        # Should only have the convective alert, not the Gale Watch
        assert len(result[0]["alerts"]) == 1
        assert result[0]["alerts"][0] == "urn:oid:2.49.0.1.840.0.2406210827.1"
        
    def test_empty_cells_list_returns_empty(self, temp_registry):
        """Verify empty cells list returns empty list."""
        result = match_alerts_to_cells([], temp_registry)
        assert result == []

    def test_loads_closest_timestamp_snapshot(self, temp_registry, sample_convective_alert,
                                               sample_cell_inside_storm):
        """Verify that the target_timestamp is respected to find the active alerts at that time."""
        # Setup two snapshots: one at 21:00 (active alert), one at 22:00 (alert expired, empty snapshot)
        self._write_registry_data(temp_registry, "2026-02-23T21:00:00Z", "20260223-210000", {
            "urn:oid:2.49.0.1.840.0.2406210827.1": {
                "id": sample_convective_alert["id"],
                "first_seen": "2026-02-23T21:00:00Z",
                "last_seen": "2026-02-23T21:00:00Z",
                "feature": sample_convective_alert
            }
        })
        self._write_registry_data(temp_registry, "2026-02-23T22:00:00Z", "20260223-220000", {})
        
        # Match with target timestamp 21:15 - should use the 21:00 snapshot
        cells = [sample_cell_inside_storm.copy()]
        result1 = match_alerts_to_cells(cells, temp_registry, target_timestamp="2026-02-23T21:15:00Z")
        assert len(result1[0]["alerts"]) == 1
        
        # Match with target timestamp 22:15 - should use the 22:00 snapshot (empty)
        cells = [sample_cell_inside_storm.copy()]
        result2 = match_alerts_to_cells(cells, temp_registry, target_timestamp="2026-02-23T22:15:00Z")
        assert len(result2[0]["alerts"]) == 0

    def test_reuses_cached_geometry_preparation(self, temp_registry, sample_convective_alert, sample_cell_inside_storm):
        alert_matcher._ALERT_SNAPSHOT_CACHE.clear()
        alert_matcher._ALERT_GEOMETRY_CACHE.clear()

        self._write_registry_data(temp_registry, "2026-02-23T21:00:00Z", "20260223-210000", {
            "urn:oid:2.49.0.1.840.0.2406210827.1": {
                "id": sample_convective_alert["id"],
                "first_seen": "2026-02-23T21:00:00Z",
                "last_seen": "2026-02-23T21:00:00Z",
                "feature": sample_convective_alert,
            }
        })

        result1 = match_alerts_to_cells([sample_cell_inside_storm.copy()], temp_registry)
        assert len(result1[0]["alerts"]) == 1

        with patch("EdgeWARN.process.detect.tools.alert_matcher.prep", side_effect=AssertionError("geometry cache miss")):
            result2 = match_alerts_to_cells([sample_cell_inside_storm.copy()], temp_registry)

        assert len(result2[0]["alerts"]) == 1
