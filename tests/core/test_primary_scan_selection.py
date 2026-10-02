"""Realtime Core takes the newest ready scan and supersedes older ones.

test-run-1001 backfilled two hours oldest-first, so Core worked forward through
stale scans while the freshest one waited. The loop is driven here through a
stubbed reader and producer agreement, as in the fatal-supervision tests.
"""

from datetime import datetime, timedelta, timezone

from util.runtime import primary_service
from util.runtime.cycle import CycleOutcome, CycleStageResult, CycleStatus, PrimaryCycleConfig

T = datetime(2026, 10, 1, 23, 12, tzinfo=timezone.utc)


class _Dependencies:
    fingerprint = "f" * 64


def _drive(monkeypatch, tmp_path, pending, *, cursor_state=None):
    """Run the real loop until one cycle runs or one idle wait passes."""
    import threading

    stop = threading.Event()
    ran = []

    class Manager:
        def shutdown(self):
            pass

    class Handoff:
        @staticmethod
        def release_stale_pins(**_kwargs):
            return ()

    class Reader:
        instance = None

        def __init__(self, **_kwargs):
            self.skipped = []
            self.handoff = Handoff()
            Reader.instance = self

        def pending_scans(self):
            done = {scan for scan, _ in self.skipped} | set(ran)
            return tuple(scan for scan in pending if scan not in done)

        def skip(self, scan, reason):
            self.skipped.append((scan, reason))

    def cycle(dt, _manager, **_kwargs):
        ran.append(dt)
        stop.set()
        return CycleOutcome(dt, {"ingest": CycleStageResult(CycleStatus.COMPLETED)},
                            retryable=False)

    state_path = tmp_path / "cycle_state.json"
    monkeypatch.setattr(primary_service.multiprocessing, "Manager", Manager)
    monkeypatch.setattr(primary_service, "load_last_processed_from_stormcells",
                        lambda _path: (cursor_state, "seeded"))
    monkeypatch.setattr(primary_service, "resolve_file", lambda *_args: state_path)
    monkeypatch.setattr(primary_service, "report_effective_config", lambda *_args: None)
    monkeypatch.setattr(primary_service, "require_ingest_producer", lambda *_a, **_k: True)
    monkeypatch.setattr(primary_service, "LocalReadinessReader", Reader)
    monkeypatch.setattr(primary_service, "run_primary_cycle_once", cycle)
    monkeypatch.setattr(primary_service, "_wait_ticks", lambda *_args: stop.set())

    config = PrimaryCycleConfig(
        lat_limits=(20, 55), lon_limits=(230, 300), profile=False,
        disable_ctam=True, disable_ctam_modules=True,
        disable_tracking=False, disable_polygon_expansion=False,
        refl_threshold=35, min_seed_percentage=0.1, drop_offset=10,
        config_dir="config", goes_enabled=False, mrms_core_only=False,
        base_dir=str(tmp_path), disable_stormprob=True, handoff_enabled=True,
        dependencies=_Dependencies(),
    )
    primary_service.run_primary_cycle_loop(cycle_config=config, stop_event=stop)
    return ran, Reader.instance.skipped


def test_cold_start_backlog_processes_the_newest_scan_first(monkeypatch, tmp_path):
    backlog = tuple(T - timedelta(minutes=2 * step) for step in range(5, -1, -1))
    ran, skipped = _drive(monkeypatch, tmp_path, backlog)
    assert ran == [T]
    assert [scan for scan, _ in skipped] == list(backlog[:-1])
    assert all("superseded" in reason for _, reason in skipped)


def test_a_newest_scan_behind_the_cursor_is_skipped_not_run(monkeypatch, tmp_path):
    old = T - timedelta(minutes=10)
    ran, skipped = _drive(monkeypatch, tmp_path, (old,), cursor_state=T)
    assert ran == []
    assert [scan for scan, _ in skipped] == [old]
    assert "processing cursor" in skipped[0][1]
