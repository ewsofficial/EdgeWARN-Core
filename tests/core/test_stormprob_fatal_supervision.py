"""Core loop handling of StormProb dependency failures.

A process-wide dependency error is recorded and propagated, while a per-scan
StormProb gap is recorded as an abandoned scan and the loop continues.

Core selects scans from durable local readiness records, so the loop is driven
here through a stubbed reader and a stubbed producer agreement. No source
acquisition and no remote listing is involved.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from EdgeWARN.ctam.preflight import StormProbDependencyError
from util.runtime import primary_service
from util.runtime.cycle import (
    CycleOutcome, CycleStageResult, CycleStatus, CycleStateStore, PrimaryCycleConfig,
)


def _install_loop_stubs(monkeypatch, state_path, pending_scans):
    """Stub the loop's collaborators; *pending_scans* returns the ready scans."""
    class Manager:
        stopped = False

        def shutdown(self):
            self.stopped = True

    manager = Manager()
    monkeypatch.setattr(primary_service.multiprocessing, 'Manager', lambda: manager)
    monkeypatch.setattr(primary_service, 'load_last_processed_from_stormcells',
                        lambda _path: (None, 'no previous cycle'))
    monkeypatch.setattr(primary_service, 'resolve_file', lambda *_args: state_path)
    monkeypatch.setattr(primary_service, 'report_effective_config', lambda *_args: None)
    monkeypatch.setattr(primary_service, 'require_ingest_producer',
                        lambda *_args, **_kwargs: True)

    class Handoff:
        @staticmethod
        def release_stale_pins(**_kwargs):
            return ()

    class Reader:
        def __init__(self, **_kwargs):
            self.skipped = []
            self.handoff = Handoff()

        def pending_scans(self):
            return pending_scans()

        def skip(self, scan, reason):
            self.skipped.append((scan, reason))

    monkeypatch.setattr(primary_service, 'LocalReadinessReader', Reader)

    return manager


def test_primary_loop_records_fatal_cycle_and_exits(tmp_path, monkeypatch):
    cycle_time = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    state_path = tmp_path / 'cycle_state.json'
    manager = _install_loop_stubs(monkeypatch, state_path, lambda: (cycle_time,))

    def fatal(*_args, **_kwargs):
        raise StormProbDependencyError('WARNING: required MRMS_Reflectivity_0C_00.50 unavailable')

    monkeypatch.setattr(primary_service, 'run_primary_cycle_once', fatal)

    config = _config(tmp_path)
    with pytest.raises(StormProbDependencyError, match='MRMS_Reflectivity_0C_00.50'):
        primary_service.run_primary_cycle_loop(cycle_config=config)
    state = CycleStateStore(state_path).load()
    assert state.outcome['completed'] is False
    assert state.outcome['retryable'] is False
    assert 'MRMS_Reflectivity_0C_00.50' in state.outcome['errors'][0]
    assert manager.stopped


def test_primary_loop_skips_a_stormprob_unavailable_scan_and_continues(tmp_path, monkeypatch):
    skipped_scan = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    next_scan = skipped_scan + timedelta(minutes=2)
    state_path = tmp_path / 'cycle_state.json'
    ready = [(skipped_scan,), (skipped_scan,), (next_scan,)]
    _install_loop_stubs(monkeypatch, state_path,
                        lambda: ready.pop(0) if ready else ())

    attempts = []

    def cycle(dt, *_args, **_kwargs):
        attempts.append(dt)
        if dt == skipped_scan:
            return CycleOutcome(
                dt,
                {'stormprob': CycleStageResult(
                    CycleStatus.UNAVAILABLE,
                    errors=('WARNING: StormProb dependency failure: MRMS_VII',))},
                retryable=False,
            )
        return CycleOutcome(
            dt, {'edgewarn': CycleStageResult(CycleStatus.COMPLETED)}, retryable=False)

    monkeypatch.setattr(primary_service, 'run_primary_cycle_once', cycle)

    stop = threading.Event()

    ticks = []

    def wait_ticks(*_args, **_kwargs):
        # Bounded so a regression to retrying fails instead of spinning.
        ticks.append(None)
        if next_scan in attempts or len(attempts) > 2 or len(ticks) > 20:
            stop.set()

    monkeypatch.setattr(primary_service, '_wait_ticks', wait_ticks)

    primary_service.run_primary_cycle_loop(cycle_config=_config(tmp_path), stop_event=stop)

    # The skipped scan was attempted once, never retried, and Core moved on.
    assert attempts == [skipped_scan, next_scan]
    state = CycleStateStore(state_path).load()
    assert state.last_abandoned == skipped_scan
    assert state.last_successful == next_scan


def _config(tmp_path):
    return PrimaryCycleConfig(
        lat_limits=(20, 55), lon_limits=(230, 300), profile=False,
        disable_ctam=False, disable_ctam_modules=False,
        disable_tracking=False, disable_polygon_expansion=False,
        refl_threshold=35, min_seed_percentage=0.1, drop_offset=10,
        config_dir='config', goes_enabled=False, mrms_core_only=False,
        base_dir=str(tmp_path), disable_stormprob=False, handoff_enabled=True,
        dependencies=SimpleDependencies(),
    )


class SimpleDependencies:
    """Only the fingerprint is read on this path."""

    fingerprint = 'a' * 64
