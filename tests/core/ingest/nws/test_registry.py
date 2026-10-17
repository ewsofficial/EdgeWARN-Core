"""
Unit tests for the NWS AlertRegistry class.

Tests cover:
- Alert addition and deduplication
- Alert ID extraction from features
- TTL-based cleanup of expired alerts
- Registry persistence to disk
- Registry summary and retrieval methods
"""

import pytest
import json
import tempfile
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

from common.ingest.nws.registry import (
    AlertRegistry,
    get_registry,
    reset_registry,
    DecimalEncoder
)
from decimal import Decimal


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_registry_dir(tmp_path):
    """Create a temporary path for the registry directory."""
    path = tmp_path / "alerts_registry"
    path.mkdir()
    return path


@pytest.fixture
def registry(temp_registry_dir):
    """Create a fresh AlertRegistry instance for each test."""
    return AlertRegistry(temp_registry_dir, ttl_hours=2.0)


@pytest.fixture
def sample_feature():
    """Create a sample NWS alert feature."""
    return {
        "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.2406210827.1",
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-97.0, 35.0], [-97.0, 36.0], [-96.0, 36.0], [-96.0, 35.0], [-97.0, 35.0]]]
        },
        "properties": {
            "event": "Severe Thunderstorm Warning",
            "headline": "Severe Thunderstorm Warning issued February 23 at 3:00PM CST",
            "description": "A severe thunderstorm warning has been issued.",
            "effective": "2026-02-23T21:00:00Z",
            "expires": "2026-02-23T22:00:00Z",
            "severity": "Severe",
            "urgency": "Immediate",
            "certainty": "Observed",
            "areaDesc": "Oklahoma County, OK"
        },
        "Polygon": [[[-97.0, 35.0], [-97.0, 36.0], [-96.0, 36.0], [-96.0, 35.0], [-97.0, 35.0]]]
    }


@pytest.fixture
def sample_feature_2():
    """Create a second sample NWS alert feature with different ID."""
    return {
        "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.2406210828.1",
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-98.0, 35.0], [-98.0, 36.0], [-97.0, 36.0], [-97.0, 35.0], [-98.0, 35.0]]]
        },
        "properties": {
            "event": "Tornado Warning",
            "headline": "Tornado Warning issued February 23 at 3:30PM CST",
            "description": "A tornado warning has been issued.",
            "effective": "2026-02-23T21:30:00Z",
            "expires": "2026-02-23T22:30:00Z",
            "severity": "Extreme",
            "urgency": "Immediate",
            "certainty": "Observed",
            "areaDesc": "Cleveland County, OK"
        },
        "Polygon": [[[-98.0, 35.0], [-98.0, 36.0], [-97.0, 36.0], [-97.0, 35.0], [-98.0, 35.0]]]
    }


# =============================================================================
# Test Alert ID Extraction
# =============================================================================

class TestAlertIdExtraction:
    """Tests for _extract_alert_id method."""

    def test_extract_id_from_url(self, registry, sample_feature):
        """Test extracting alert ID from full URL format."""
        alert_id = registry._extract_alert_id(sample_feature)
        assert alert_id == "urn:oid:2.49.0.1.840.0.2406210827.1"

    def test_extract_id_from_urn_only(self, registry):
        """Test extracting alert ID when it's just the URN."""
        feature = {
            "id": "urn:oid:2.49.0.1.840.0.2406210827.1"
        }
        alert_id = registry._extract_alert_id(feature)
        assert alert_id == "urn:oid:2.49.0.1.840.0.2406210827.1"

    def test_extract_id_missing(self, registry):
        """Test handling of missing alert ID."""
        feature = {"type": "Feature", "properties": {}}
        alert_id = registry._extract_alert_id(feature)
        assert alert_id is None

    def test_extract_id_from_properties(self, registry):
        """Test extracting ID from properties fallback location."""
        feature = {
            "properties": {
                "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.9999"
            }
        }
        alert_id = registry._extract_alert_id(feature)
        assert alert_id == "urn:oid:2.49.0.1.840.0.9999"


# =============================================================================
# Test Alert Processing
# =============================================================================

class TestProcessAlert:
    """Tests for process_alert method."""

    def test_process_new_alert(self, registry, sample_feature):
        """Test adding a new alert to the registry."""
        current_time = datetime.now(timezone.utc)
        is_new, alert_id = registry.process_alert(sample_feature, current_time)
        
        assert is_new is True
        assert alert_id == "urn:oid:2.49.0.1.840.0.2406210827.1"
        assert registry.alert_count == 1

    def test_process_duplicate_alert(self, registry, sample_feature):
        """Test that duplicate alerts update existing entry."""
        current_time = datetime.now(timezone.utc)
        
        # First add
        is_new_1, alert_id_1 = registry.process_alert(sample_feature, current_time)
        
        # Second add (same alert, later time)
        later_time = current_time + timedelta(minutes=2)
        is_new_2, alert_id_2 = registry.process_alert(sample_feature, later_time)
        
        assert is_new_1 is True
        assert is_new_2 is False
        assert alert_id_1 == alert_id_2
        assert registry.alert_count == 1  # Still only 1 alert

    def test_process_multiple_alerts(self, registry, sample_feature, sample_feature_2):
        """Test processing multiple different alerts."""
        current_time = datetime.now(timezone.utc)
        
        is_new_1, _ = registry.process_alert(sample_feature, current_time)
        is_new_2, _ = registry.process_alert(sample_feature_2, current_time)
        
        assert is_new_1 is True
        assert is_new_2 is True
        assert registry.alert_count == 2

    def test_process_alert_missing_id(self, registry):
        """Test handling of alert with missing ID."""
        feature = {"type": "Feature", "properties": {"event": "Test"}}
        current_time = datetime.now(timezone.utc)
        
        is_new, alert_id = registry.process_alert(feature, current_time)
        
        assert is_new is False
        assert alert_id is None
        assert registry.alert_count == 0


class TestSnapshotAlertNaming:
    """Tests for snapshot alert display-name normalization."""

    def test_snapshot_alert_uses_tornado_emergency_label(self, registry, sample_feature_2):
        sample_feature_2["properties"]["description"] = (
            "THIS IS A TORNADO EMERGENCY FOR THE WARNED AREA."
        )

        snapshot = registry._build_snapshot_alert("urn:oid:test", {"feature": sample_feature_2})

        assert snapshot["name"] == "Tornado Emergency"

    def test_snapshot_alert_uses_pds_tornado_warning_label(self, registry, sample_feature_2):
        sample_feature_2["properties"]["description"] = (
            "THIS IS A PARTICULARLY DANGEROUS SITUATION WITH A CONFIRMED TORNADO."
        )

        snapshot = registry._build_snapshot_alert("urn:oid:test", {"feature": sample_feature_2})

        assert snapshot["name"] == "PDS Tornado Warning"


class TestProcessAlerts:
    """Tests for process_alerts method."""

    def test_process_multiple_features(self, registry, sample_feature, sample_feature_2):
        """Test processing a list of features."""
        current_time = datetime.now(timezone.utc)
        features = [sample_feature, sample_feature_2]
        
        new_count, updated_count = registry.process_alerts(features, current_time)
        
        assert new_count == 2
        assert updated_count == 0
        assert registry.alert_count == 2

    def test_process_with_duplicates(self, registry, sample_feature, sample_feature_2):
        """Test processing with some duplicates."""
        current_time = datetime.now(timezone.utc)
        
        # First batch
        registry.process_alerts([sample_feature, sample_feature_2], current_time)
        
        # Second batch (same alerts)
        later_time = current_time + timedelta(minutes=5)
        new_count, updated_count = registry.process_alerts(
            [sample_feature, sample_feature_2], 
            later_time
        )
        
        assert new_count == 0
        assert updated_count == 2


# =============================================================================
# Test Cleanup
# =============================================================================

class TestCleanupExpired:
    """Tests for cleanup_expired method."""

    def test_cleanup_expired_alerts(self, registry, sample_feature):
        """Test removal of alerts older than TTL."""
        current_time = datetime.now(timezone.utc)
        
        # Add an alert
        registry.process_alert(sample_feature, current_time)
        
        # Simulate time passing beyond TTL (2 hours + 1 minute)
        future_time = current_time + timedelta(hours=2, minutes=1)
        
        removed_count = registry.cleanup_expired(future_time)
        
        assert removed_count == 1
        assert registry.alert_count == 0

    def test_cleanup_preserves_recent_alerts(self, registry, sample_feature):
        """Test that recent alerts are not removed."""
        current_time = datetime.now(timezone.utc)
        
        # Update expires to be in the future (beyond the cleanup time)
        sample_feature["properties"]["expires"] = (
            (current_time + timedelta(hours=3)).isoformat()
        )
        
        # Add an alert
        registry.process_alert(sample_feature, current_time)
        
        # Simulate time passing but within TTL
        future_time = current_time + timedelta(hours=1, minutes=30)
        
        removed_count = registry.cleanup_expired(future_time)
        
        assert removed_count == 0
        assert registry.alert_count == 1

    def test_cleanup_by_expiration(self, registry, sample_feature):
        """Test removal of alerts that have expired (per expires field)."""
        current_time = datetime.now(timezone.utc)
        
        # Add an alert that expires in 30 minutes
        sample_feature["properties"]["expires"] = (
            (current_time + timedelta(minutes=30)).isoformat()
        )
        registry.process_alert(sample_feature, current_time)
        
        # Simulate time passing past expiration
        future_time = current_time + timedelta(minutes=31)
        
        removed_count = registry.cleanup_expired(future_time)
        
        assert removed_count == 1
        assert registry.alert_count == 0

    def test_cleanup_mixed_alerts(self, registry, sample_feature, sample_feature_2):
        """Test cleanup with mix of expired and active alerts."""
        current_time = datetime.fromisoformat("2026-02-23T21:45:00Z")
        
        # Add two alerts
        registry.process_alert(sample_feature, current_time)
        registry.process_alert(sample_feature_2, current_time)
        
        # Manually set one to be old
        alert_id_1 = registry._extract_alert_id(sample_feature)
        registry._registry["alerts"][alert_id_1]["last_seen"] = (
            (current_time - timedelta(hours=3)).isoformat()
        )
        
        # Cleanup
        removed_count = registry.cleanup_expired(current_time)
        
        assert removed_count == 1
        assert registry.alert_count == 1


class TestReconcileWithActiveIds:
    """Tests for reconcile_with_active_ids method."""

    def test_reconcile_removes_alerts_not_in_active_set(
        self,
        registry,
        sample_feature,
        sample_feature_2,
    ):
        """Only IDs present in latest upstream active set should remain."""
        current_time = datetime.now(timezone.utc)
        registry.process_alert(sample_feature, current_time)
        registry.process_alert(sample_feature_2, current_time)

        keep_id = registry._extract_alert_id(sample_feature)
        removed_count = registry.reconcile_with_active_ids([keep_id], current_time)

        assert removed_count == 1
        assert registry.alert_count == 1
        assert registry.get_active_ids() == [keep_id]

    def test_reconcile_with_empty_active_set_clears_registry(
        self,
        registry,
        sample_feature,
        sample_feature_2,
    ):
        """An empty successful upstream payload should clear saved active alerts."""
        current_time = datetime.now(timezone.utc)
        registry.process_alert(sample_feature, current_time)
        registry.process_alert(sample_feature_2, current_time)

        removed_count = registry.reconcile_with_active_ids([], current_time)

        assert removed_count == 2
        assert registry.alert_count == 0
        assert registry.get_active_ids() == []


# =============================================================================
# Test Persistence
# =============================================================================

class TestPersistence:
    """Tests for registry persistence."""

    def test_save_and_load(self, temp_registry_dir, sample_feature):
        """Test saving and loading registry from disk."""
        current_time = datetime.now(timezone.utc)
        
        # Create registry and add alert
        registry1 = AlertRegistry(temp_registry_dir)
        registry1.process_alert(sample_feature, current_time)
        registry1.save()
        
        # Create new registry instance (should load from disk)
        registry2 = AlertRegistry(temp_registry_dir)
        
        assert registry2.alert_count == 1
        assert registry2.last_updated is not None

    def test_atomic_write(self, temp_registry_dir, sample_feature):
        """Test that save uses atomic write pattern."""
        current_time = datetime.now(timezone.utc)
        
        registry = AlertRegistry(temp_registry_dir)
        registry.process_alert(sample_feature, current_time)
        registry.save()
        
        # Check files exist
        assert (temp_registry_dir / "ids").exists()
        assert (temp_registry_dir / "timestamps").exists()

    def test_save_writes_enriched_timestamp_snapshot(self, temp_registry_dir, sample_feature):
        """Test timestamp snapshots include precomputed official alert summaries."""
        current_time = datetime(2026, 2, 23, 21, 0, tzinfo=timezone.utc)

        registry = AlertRegistry(temp_registry_dir)
        registry.process_alert(sample_feature, current_time)
        registry.save()

        snapshot_path = temp_registry_dir / "timestamps" / "20260223-210000.json"
        with open(snapshot_path, 'r', encoding='utf-8') as f:
            snapshot_data = json.load(f)

        assert snapshot_data["count"] == 1
        assert snapshot_data["alerts"] == [{
            "id": "urn:oid:2.49.0.1.840.0.2406210827.1",
            "name": "Severe Thunderstorm Warning",
            "urn_oid": "urn:oid:2.49.0.1.840.0.2406210827.1",
            "effective": "2026-02-23T21:00:00Z",
            "expires": "2026-02-23T22:00:00Z",
            "severity": "Severe",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-97.0, 35.0], [-97.0, 36.0], [-96.0, 36.0], [-96.0, 35.0], [-97.0, 35.0]]]
            }
        }]

    def test_load_corrupted_file(self, temp_registry_dir):
        """Test handling of corrupted registry file."""
        # Write invalid JSON
        registry1 = AlertRegistry(temp_registry_dir)
        corrupted = registry1.ids_dir / "corrupted.json"
        with open(corrupted, 'w') as f:
            f.write("{ invalid json }")
        
        # Should create new empty registry
        registry = AlertRegistry(temp_registry_dir)
        
        assert registry.alert_count == 0
        assert registry._registry["alerts"] == {}


# =============================================================================
# Test Retrieval Methods
# =============================================================================

class TestRetrievalMethods:
    """Tests for alert retrieval methods."""

    def test_get_active_alerts(self, registry, sample_feature, sample_feature_2):
        """Test retrieving all active alerts."""
        current_time = datetime.now(timezone.utc)
        registry.process_alert(sample_feature, current_time)
        registry.process_alert(sample_feature_2, current_time)
        
        alerts = registry.get_active_alerts()
        
        assert len(alerts) == 2
        assert any(a["properties"]["event"] == "Severe Thunderstorm Warning" for a in alerts)
        assert any(a["properties"]["event"] == "Tornado Warning" for a in alerts)

    def test_get_active_ids(self, registry, sample_feature, sample_feature_2):
        """Test retrieving all active alert IDs."""
        current_time = datetime.now(timezone.utc)
        registry.process_alert(sample_feature, current_time)
        registry.process_alert(sample_feature_2, current_time)
        
        ids = registry.get_active_ids()
        
        assert len(ids) == 2
        assert "urn:oid:2.49.0.1.840.0.2406210827.1" in ids
        assert "urn:oid:2.49.0.1.840.0.2406210828.1" in ids

    def test_get_alert_by_id(self, registry, sample_feature):
        """Test retrieving a specific alert by ID."""
        current_time = datetime.now(timezone.utc)
        registry.process_alert(sample_feature, current_time)
        
        alert = registry.get_alert_by_id("urn:oid:2.49.0.1.840.0.2406210827.1")
        
        assert alert is not None
        assert alert["properties"]["event"] == "Severe Thunderstorm Warning"

    def test_get_alert_by_id_not_found(self, registry):
        """Test retrieving non-existent alert."""
        alert = registry.get_alert_by_id("non-existent-id")
        assert alert is None

    def test_get_registry_summary(self, registry, sample_feature):
        """Test getting registry summary."""
        current_time = datetime.now(timezone.utc)
        registry.process_alert(sample_feature, current_time)
        
        summary = registry.get_registry_summary()
        
        assert summary["count"] == 1
        assert summary["last_updated"] is not None
        assert len(summary["alert_ids"]) == 1


# =============================================================================
# Test Singleton
# =============================================================================

class TestSingleton:
    """Tests for singleton get_registry function."""

    def test_singleton_instance(self, temp_registry_dir):
        """Test that get_registry returns singleton instance."""
        reset_registry()
        
        registry1 = get_registry(temp_registry_dir)
        registry2 = get_registry()
        
        assert registry1 is registry2

    def test_reset_registry(self, temp_registry_dir):
        """Test that reset_registry clears the singleton."""
        reset_registry()
        
        registry1 = get_registry(temp_registry_dir)
        reset_registry()
        registry2 = get_registry(temp_registry_dir)
        
        assert registry1 is not registry2


# =============================================================================
# Test DecimalEncoder
# =============================================================================

class TestDecimalEncoder:
    """Tests for DecimalEncoder JSON encoder."""

    def test_encode_decimal(self):
        """Test encoding Decimal to float."""
        data = {"value": Decimal("123.456")}
        result = json.dumps(data, cls=DecimalEncoder)
        
        assert '"value": 123.456' in result

    def test_encode_regular_types(self):
        """Test encoding regular types still works."""
        data = {
            "string": "test",
            "int": 42,
            "float": 3.14,
            "list": [1, 2, 3]
        }
        result = json.dumps(data, cls=DecimalEncoder)
        
        parsed = json.loads(result)
        assert parsed["string"] == "test"
        assert parsed["int"] == 42
