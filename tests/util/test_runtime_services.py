"""Phase 0 contract tests for the realtime service-name registry.

Covers the heartbeat schema, atomic publication, state classification
(disabled / unsupported-schema / stale / active / degraded), and the
route-family-to-service dependency map recorded in
plans/realtime-runner-decomposition-plan.md.
"""

from datetime import datetime, timedelta, timezone

import pytest

from util.runtime.services import (
    CANONICAL_SERVICE_NAMES,
    HEARTBEAT_SCHEMA_VERSION,
    ROUTE_SERVICE_REQUIREMENTS,
    SERVICE_STATES,
    ServiceHeartbeat,
    HeartbeatSchemaError,
    classify_heartbeat_state,
    heartbeat_path,
    read_heartbeat_file,
    required_service_for_route,
    scan_service_states,
    services_dir,
    write_heartbeat,
)


def make_beat(service="ewmrs", **overrides):
    values = dict(
        service=service,
        pid=4321,
        run_id="run-abc",
        updated_at=datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc),
        phase="idle",
        version="3.0.0",
    )
    values.update(overrides)
    return ServiceHeartbeat(**values)


class TestRegistry:
    def test_canonical_names_are_exactly_three_services(self):
        assert CANONICAL_SERVICE_NAMES == ("edgewarn", "ewmrs", "nexrad")

    def test_heartbeat_paths_live_under_state_realtime_services(self, tmp_path):
        base = tmp_path / "base"
        for name in CANONICAL_SERVICE_NAMES:
            assert heartbeat_path(base, name) == (
                base / "state" / "realtime" / "services" / f"{name}.json"
            )

    def test_noncanonical_names_are_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            heartbeat_path(tmp_path, "metar")
        with pytest.raises(ValueError):
            heartbeat_path(tmp_path, "../edgewarn")

    def test_every_mapped_service_is_canonical(self):
        assert set(ROUTE_SERVICE_REQUIREMENTS.values()) <= set(CANONICAL_SERVICE_NAMES)

    def test_required_service_for_route_matches_longest_prefix(self):
        assert required_service_for_route("/api/v3/cells") == "edgewarn"
        assert required_service_for_route("/api/v3/cells/ABC123") == "edgewarn"
        assert required_service_for_route("/api/v3/radar-sites/KOAX") == "nexrad"
        assert required_service_for_route("/renders/latest") == "ewmrs"
        # A longer unrelated prefix must not be shadowed by a shorter one.
        assert required_service_for_route("/nexrad/sites") == "nexrad"

    def test_unknown_routes_have_no_requirement(self):
        assert required_service_for_route("/health/live") is None


class TestHeartbeatSchema:
    def test_round_trip_preserves_all_fields(self):
        beat = make_beat(
            last_successful_activity=datetime(2026, 8, 23, 11, 59, tzinfo=timezone.utc),
            degraded_children=("wpc",),
        )
        restored = ServiceHeartbeat.from_dict(beat.as_dict())
        assert restored == beat

    def test_missing_required_fields_are_rejected(self):
        payload = make_beat().as_dict()
        del payload["run_id"]
        with pytest.raises(HeartbeatSchemaError):
            ServiceHeartbeat.from_dict(payload)

    def test_unsupported_schema_version_is_rejected(self):
        payload = make_beat().as_dict()
        payload["schema_version"] = HEARTBEAT_SCHEMA_VERSION + 1
        with pytest.raises(HeartbeatSchemaError):
            ServiceHeartbeat.from_dict(payload)

    def test_noncanonical_service_name_is_rejected(self):
        with pytest.raises(HeartbeatSchemaError):
            make_beat(service="metar")

    def test_bad_pid_is_rejected(self):
        with pytest.raises(HeartbeatSchemaError):
            make_beat(pid=0)
        with pytest.raises(HeartbeatSchemaError):
            make_beat(pid="4321")

    def test_malformed_timestamps_are_rejected(self):
        payload = make_beat().as_dict()
        payload["updated_at"] = "not-a-time"
        with pytest.raises(HeartbeatSchemaError):
            ServiceHeartbeat.from_dict(payload)

    def test_degraded_children_must_be_strings(self):
        with pytest.raises(HeartbeatSchemaError):
            ServiceHeartbeat.from_dict({**make_beat().as_dict(), "degraded_children": [7]})


class TestPublicationAndClassification:
    REFERENCE = datetime(2026, 8, 23, 12, 0, 30, tzinfo=timezone.utc)

    def write(self, tmp_path, name, beat):
        path = heartbeat_path(tmp_path, name)
        write_heartbeat(beat, path)
        return path

    def test_write_is_atomic_and_leaves_no_temporary_siblings(self, tmp_path):
        path = self.write(tmp_path, "edgewarn", make_beat(service="edgewarn"))
        assert path.exists()
        siblings = list(path.parent.iterdir())
        assert siblings == [path]

    def test_fresh_heartbeat_classifies_active(self, tmp_path):
        path = self.write(tmp_path, "edgewarn", make_beat(service="edgewarn"))
        state, beat = classify_heartbeat_state(
            path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert state == "active"
        assert beat.pid == 4321

    def test_expired_heartbeat_classifies_stale(self, tmp_path):
        beat = make_beat(
            updated_at=self.REFERENCE - timedelta(seconds=91),
        )
        path = self.write(tmp_path, "ewmrs", beat)
        state, parsed = classify_heartbeat_state(
            path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert state == "stale"
        assert parsed is not None and parsed.run_id == "run-abc"

    def test_boundary_age_within_threshold_is_active(self, tmp_path):
        beat = make_beat(updated_at=self.REFERENCE - timedelta(seconds=90))
        path = self.write(tmp_path, "nexrad", beat)
        state, _ = classify_heartbeat_state(
            path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert state == "active"

    def test_missing_file_classifies_disabled(self, tmp_path):
        path = heartbeat_path(tmp_path, "nexrad")
        state, beat = classify_heartbeat_state(
            path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert state == "disabled"
        assert beat is None

    def test_malformed_json_classifies_unsupported_schema(self, tmp_path):
        path = heartbeat_path(tmp_path, "ewmrs")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        state, beat = classify_heartbeat_state(
            path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert state == "unsupported-schema"
        assert beat is None

    def test_wrong_schema_version_classifies_unsupported_schema(self, tmp_path):
        payload = make_beat().as_dict()
        payload["schema_version"] = 99
        import json

        path = heartbeat_path(tmp_path, "ewmrs")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        state, beat = classify_heartbeat_state(
            path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert state == "unsupported-schema"
        assert beat is None

    def test_degraded_children_report_degraded_while_fresh(self, tmp_path):
        beat = make_beat(degraded_children=("metar",))
        path = self.write(tmp_path, "ewmrs", beat)
        state, parsed = classify_heartbeat_state(
            path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert state == "degraded"
        assert tuple(parsed.degraded_children) == ("metar",)

    def test_scan_reports_every_canonical_name_once(self, tmp_path):
        self.write(tmp_path, "edgewarn", make_beat(service="edgewarn"))
        states = scan_service_states(
            tmp_path, stale_after_seconds=90, now=self.REFERENCE
        )
        assert set(states) == set(CANONICAL_SERVICE_NAMES)
        assert states["edgewarn"][0] == "active"
        assert states["ewmrs"][0] == "disabled"
        assert states["nexrad"][0] == "disabled"

    def test_read_returns_none_for_missing_file(self, tmp_path):
        assert read_heartbeat_file(heartbeat_path(tmp_path, "ewmrs")) is None

    def test_states_vocabulary_is_complete(self):
        assert set(SERVICE_STATES) == {
            "active",
            "stale",
            "disabled",
            "degraded",
            "unsupported-schema",
        }


def test_max_backlog_cycles_is_configured():
    """Phase 0 defines the maximum retained cycle backlog for consumers."""
    from util.runtime.config import section

    assert int(section("cycle")["max_backlog_cycles"]) >= 1
