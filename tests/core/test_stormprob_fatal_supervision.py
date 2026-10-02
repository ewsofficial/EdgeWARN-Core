"""A fatal dependency is recorded and propagated through the Core loop.

Core selects scans from durable local readiness records, so the loop is driven
here through a stubbed reader and a stubbed producer agreement. No source
acquisition and no remote listing is involved.
"""

from datetime import datetime, timezone

import pytest

from EdgeWARN.ctam.preflight import StormProbDependencyError
from util.runtime import primary_service
from util.runtime.cycle import PrimaryCycleConfig, CycleStateStore


def test_primary_loop_records_fatal_cycle_and_exits(tmp_path, monkeypatch):
    cycle_time = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    state_path = tmp_path / 'cycle_state.json'

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
            return (cycle_time,)

        def skip(self, scan, reason):
            self.skipped.append((scan, reason))

    monkeypatch.setattr(primary_service, 'LocalReadinessReader', Reader)

    def fatal(*_args, **_kwargs):
        raise StormProbDependencyError('WARNING: required MRMS_Reflectivity_0C_00.50 unavailable')

    monkeypatch.setattr(primary_service, 'run_primary_cycle_once', fatal)

    config = PrimaryCycleConfig(
        lat_limits=(20, 55), lon_limits=(230, 300), profile=False,
        disable_ctam=False, disable_ctam_modules=False,
        disable_tracking=False, disable_polygon_expansion=False,
        refl_threshold=35, min_seed_percentage=0.1, drop_offset=10,
        config_dir='config', goes_enabled=False, mrms_core_only=False,
        base_dir=str(tmp_path), disable_stormprob=False, handoff_enabled=True,
        dependencies=SimpleDependencies(),
    )
    with pytest.raises(StormProbDependencyError, match='MRMS_Reflectivity_0C_00.50'):
        primary_service.run_primary_cycle_loop(cycle_config=config)
    state = CycleStateStore(state_path).load()
    assert state.outcome['completed'] is False
    assert state.outcome['retryable'] is False
    assert 'MRMS_Reflectivity_0C_00.50' in state.outcome['errors'][0]
    assert manager.stopped


class SimpleDependencies:
    """Only the fingerprint is read on this path."""

    fingerprint = 'a' * 64
