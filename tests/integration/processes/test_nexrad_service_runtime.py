"""Phase 3 NEXRAD service runtime tests: lock, lease, supervision, quiescence."""

from datetime import datetime, timedelta, timezone

import pytest

import util.runtime.background as background
from util.runtime.handoff import (
    PrimaryActivityLease,
    ServiceLock,
    primary_lease_path,
    primary_activity_held,
)
from util.runtime.processes import AccessorySupervisor
from util.runtime.services import (
    ServiceHeartbeat,
    classify_heartbeat_state,
    services_dir,
    write_heartbeat,
)


class TestServiceLock:
    def test_second_instance_is_rejected_then_allowed_after_release(self, tmp_path):
        first = ServiceLock(tmp_path, "nexrad")
        second = ServiceLock(tmp_path, "nexrad")
        first.acquire()
        with pytest.raises(RuntimeError):
            second.acquire()
        first.release()
        second.acquire()  # restart-safe: released owner never blocks a new one
        second.release()

    def test_lock_rejects_non_canonical_names(self, tmp_path):
        with pytest.raises(ValueError):
            ServiceLock(tmp_path, "goes")

    def test_lock_file_lives_in_services_registry_dir(self, tmp_path):
        lock = ServiceLock(tmp_path, "nexrad")
        lock.acquire()
        try:
            assert (tmp_path / "state" / "realtime" / "services" / "nexrad.lock").exists()
        finally:
            lock.release()


class TestPrimaryActivityLease:
    def test_held_after_acquire_and_gone_after_release(self, tmp_path):
        lease = PrimaryActivityLease(tmp_path, run_id="run-1", ttl_seconds=60)
        assert primary_activity_held(tmp_path) is None

        lease.acquire("20240501T120000Z")
        held = primary_activity_held(tmp_path)
        assert held is not None
        assert held.run_id == "run-1"
        assert held.cycle_id == "20240501T120000Z"
        assert held.pid > 0
        # The commit point is the final filename; no temp siblings survive.
        assert primary_lease_path(tmp_path).exists()

        lease.release()
        assert primary_activity_held(tmp_path) is None

    def test_expired_lease_is_not_held(self, tmp_path):
        lease = PrimaryActivityLease(tmp_path, run_id="run-1", ttl_seconds=0.05)
        lease.acquire("20240501T120000Z")
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        assert primary_activity_held(tmp_path, now=later) is None

    def test_corrupt_lease_reads_as_idle(self, tmp_path):
        primary_lease_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        primary_lease_path(tmp_path).write_text("{broken")
        assert primary_activity_held(tmp_path) is None


class TestQuiescenceWait:
    def test_noop_when_policy_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            background, "section", lambda name: {
                "pause_ingest_during_primary_activity": False,
                "pause_max_wait_seconds": 30.0,
                "pause_poll_interval_seconds": 0.5,
            }
        )
        lease = PrimaryActivityLease(tmp_path, run_id="r", ttl_seconds=60)
        lease.acquire("20240501T120000Z")
        # Must return immediately despite the held lease.
        background._wait_for_primary_quiescence(tmp_path)

    def test_waits_while_leased_then_proceeds_on_expiry(self, tmp_path, monkeypatch):
        settings = {
            "pause_ingest_during_primary_activity": True,
            "pause_max_wait_seconds": 0.05,
            "pause_poll_interval_seconds": 0.01,
        }
        monkeypatch.setattr(background, "section", lambda name: settings)
        sleeps = []

        monkeypatch.setattr(background, "sleep_for", lambda seconds, interval=None: sleeps.append(seconds))

        lease = PrimaryActivityLease(tmp_path, run_id="r", ttl_seconds=60)
        lease.acquire("20240501T120000Z")
        background._wait_for_primary_quiescence(tmp_path)
        assert sleeps  # waited at least once while the lease was held

        lease.release()
        sleeps.clear()
        background._wait_for_primary_quiescence(tmp_path)
        assert sleeps == []


class TestSupervisorDegradedChildren:
    def test_disabled_names_reports_crash_looped_children_only(self):
        supervisor = AccessorySupervisor(max_restarts=1, restart_window_seconds=60)
        healthy = supervisor.add("NEXRAD Render", lambda: None)
        crashed = supervisor.add("NEXRAD Ingest", lambda: None)

        class _FakeProcess:
            pid = 1234

        healthy["process"] = _FakeProcess()
        crashed["process"] = _FakeProcess()
        crashed["enabled"] = False

        assert supervisor.disabled_names() == ["NEXRAD Ingest"]


class TestHeartbeatPublicationContract:
    def test_service_state_classifier_matches_run_nexrad_payload(self, tmp_path):
        """The heartbeat written by run_nexrad.py classifies as active/degraded."""
        beat_kwargs = dict(
            service="nexrad",
            pid=4321,
            run_id="abc",
            updated_at=datetime.now(timezone.utc),
            phase="supervising",
            version="3.0.0",
        )
        destination = services_dir(tmp_path) / "nexrad.json"

        write_heartbeat(ServiceHeartbeat(**beat_kwargs), destination)
        state, parsed = classify_heartbeat_state(destination, stale_after_seconds=60)
        assert (state, parsed.phase, parsed.degraded_children) == ("active", "supervising", ())

        degraded = ServiceHeartbeat(
            **beat_kwargs, degraded_children=("NEXRAD Ingest",)
        )
        write_heartbeat(degraded, destination)
        state, parsed = classify_heartbeat_state(destination, stale_after_seconds=60)
        assert state == "degraded"
        assert parsed.degraded_children == ("NEXRAD Ingest",)


class TestLeaseReleaseOwnership:
    def test_release_never_deletes_a_successors_lease(self, tmp_path):
        stale_owner = PrimaryActivityLease(tmp_path, run_id="run-1", ttl_seconds=60)
        successor = PrimaryActivityLease(tmp_path, run_id="run-2", ttl_seconds=60)
        successor.acquire("20240501T120000Z")
        # A zombie primary waking up after a crash must not clear the
        # successor's live lease.
        stale_owner.release()
        held = primary_activity_held(tmp_path)
        assert held is not None and held.run_id == "run-2"
        successor.release()
        assert primary_activity_held(tmp_path) is None

    def test_release_clears_corrupt_lease_it_can_claim(self, tmp_path):
        primary_lease_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        primary_lease_path(tmp_path).write_text("{broken")
        PrimaryActivityLease(tmp_path, run_id="run-1", ttl_seconds=60).release()
        assert not primary_lease_path(tmp_path).exists()
