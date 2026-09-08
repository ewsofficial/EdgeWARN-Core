from datetime import datetime, timezone

from common.ingest.manifest import CycleInputManifest
from util.runtime import (
    CycleOutcome,
    CycleRetryPolicy,
    CycleStageResult,
    CycleStateStore,
    CycleStatus,
    StartedProcessRegistry,
)
from util.runtime.cycle import _stage_result_from_shared


class FakeProcess:
    def __init__(self, *, alive=True):
        self.alive = alive
        self.start_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_calls = []

    def start(self):
        self.start_calls += 1

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminate_calls += 1
        self.alive = False

    def kill(self):
        self.kill_calls += 1
        self.alive = False

    def join(self, timeout=None):
        self.join_calls.append(timeout)


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class FakeManager:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


def test_started_process_registry_starts_and_shuts_down_processes_in_reverse_order():
    registry = StartedProcessRegistry()
    first = FakeProcess()
    second = FakeProcess()
    queue_obj = FakeQueue()
    manager = FakeManager()

    registry.start(first, "first")
    registry.start(second, "second")
    registry.shutdown(queue_sentinels=[(queue_obj, None)], manager=manager)

    assert first.start_calls == 1
    assert second.start_calls == 1
    assert second.terminate_calls == 1
    assert first.terminate_calls == 1
    assert queue_obj.items == [None]
    assert manager.shutdown_calls == 1
    assert registry.processes == []


def test_started_process_registry_ignores_none_processes():
    registry = StartedProcessRegistry()

    result = registry.start(None, "missing")

    assert result is None
    assert registry.processes == []


def test_cycle_state_store_keeps_attempt_separate_from_success(tmp_path):
    timestamp = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    store = CycleStateStore(tmp_path / "runtime" / "cycle_state.json")
    failed = CycleOutcome(
        timestamp=timestamp,
        stages={
            "edgewarn": CycleStageResult(
                CycleStatus.UNAVAILABLE,
                errors=("inputs unavailable",),
                worker_exit_status=0,
            )
        },
        retryable=True,
    )

    store.record_attempt(timestamp, 1)
    state = store.record_outcome(failed, 1)

    assert state.last_attempted == timestamp
    assert state.last_successful is None
    assert state.last_abandoned is None
    assert state.retry_timestamp == timestamp
    assert state.selection_cursor is None


def test_cycle_state_store_records_success_and_explicit_abandonment(tmp_path):
    first = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    second = datetime(2026, 7, 26, 18, 2, tzinfo=timezone.utc)
    store = CycleStateStore(tmp_path / "cycle_state.json")
    completed = CycleOutcome(
        timestamp=first,
        stages={
            "edgewarn": CycleStageResult(
                CycleStatus.COMPLETED,
                produced_artifacts=("stormcells.json",),
                worker_exit_status=0,
            )
        },
        retryable=False,
    )
    failed = CycleOutcome(
        timestamp=second,
        stages={
            "edgewarn": CycleStageResult(
                CycleStatus.FAILED,
                errors=("worker crashed",),
                worker_exit_status=9,
            )
        },
        retryable=True,
    )

    store.record_attempt(first, 1)
    store.record_outcome(completed, 1)
    store.record_attempt(second, 3)
    state = store.record_outcome(failed, 3, abandoned=True)

    assert state.last_successful == first
    assert state.last_abandoned == second
    assert state.selection_cursor == second
    assert state.retry_timestamp is None


def test_cycle_retry_policy_is_bounded():
    policy = CycleRetryPolicy(
        max_attempts=4,
        initial_backoff_seconds=2,
        max_backoff_seconds=5,
    )

    assert [policy.delay_after(attempt) for attempt in range(1, 5)] == [2, 4, 5, 5]


def test_cycle_outcome_includes_input_manifest_metadata():
    timestamp = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    outcome = CycleOutcome(
        timestamp=timestamp,
        stages={
            "ingest": CycleStageResult(CycleStatus.COMPLETED),
        },
        retryable=False,
        input_manifest=CycleInputManifest(cycle_time=timestamp),
    )

    assert outcome.as_dict()["input_manifest"]["cycle_time"] == timestamp.isoformat()


def test_nonzero_worker_exit_overrides_published_completion():
    stage = _stage_result_from_shared(
        {
            "status": "completed",
            "produced_artifacts": ["stormcells.json"],
            "errors": [],
        },
        worker_exit_status=17,
        fallback_error="missing terminal state",
    )

    assert stage.status is CycleStatus.FAILED
    assert stage.successful is False
    assert "17" in stage.errors[-1]


def test_seed_last_successful_does_not_promote_detection_only_watermark(tmp_path):
    timestamp = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    store = CycleStateStore(tmp_path / "cycle_state.json")
    failed = CycleOutcome(
        timestamp=timestamp,
        stages={
            "edgewarn": CycleStageResult(
                CycleStatus.UNAVAILABLE,
                errors=("inputs unavailable",),
                worker_exit_status=0,
            )
        },
        retryable=True,
    )

    store.record_attempt(timestamp, 1)
    store.record_outcome(failed, 1)

    state = store.seed_last_successful(timestamp)

    assert state.last_successful is None
    assert state.last_attempted == timestamp
    assert state.retry_timestamp == timestamp, "pending retry must survive the stormcell watermark seed"


def test_seed_last_successful_migrates_when_no_cycle_state_exists(tmp_path):
    timestamp = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    store = CycleStateStore(tmp_path / "cycle_state.json")

    state = store.seed_last_successful(timestamp)

    assert state.last_successful == timestamp
    assert state.last_attempted == timestamp
    assert state.retry_timestamp is None


def test_supervisor_clears_cleanup_event_on_process_death(tmp_path):
    import multiprocessing
    import time

    from util.runtime.processes import AccessorySupervisor

    calls = []

    def worker():
        calls.append("run")

    event = multiprocessing.Event()
    supervisor = AccessorySupervisor(
        health_path=str(tmp_path / "health.json"),
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.1,
        restart_window_seconds=60,
    )
    supervisor.add("test", worker, enabled=True, daemon=True, cleanup_event=event)
    supervisor.start_all()
    proc = supervisor._process_info[0]["process"]
    event.set()

    proc.kill()
    proc.join(timeout=2)
    assert not proc.is_alive()

    supervisor.check()
    time.sleep(0.2)

    assert not event.is_set(), "cleanup event must be cleared when a registered process dies"
    event.clear()
    supervisor.shutdown()


def test_supervisor_clears_cleanup_event_when_restarts_disabled(tmp_path):
    import multiprocessing
    import time

    from util.runtime.processes import AccessorySupervisor

    def worker():
        pass  # exits immediately

    event = multiprocessing.Event()
    supervisor = AccessorySupervisor(
        health_path=str(tmp_path / "health.json"),
        max_restarts=2,
        restart_window_seconds=60,
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    supervisor.add("crashy", worker, enabled=True, daemon=True, cleanup_event=event)

    crashy_info = supervisor._process_info[0]
    for _ in range(10):
        event.set()
        supervisor.check()
        if not crashy_info["enabled"]:
            break
        time.sleep(0.05)

    assert not crashy_info["enabled"], "crash-loop should disable restarts"
    assert not event.is_set(), "cleanup event must be cleared even when restarts are disabled"


def test_supervisor_restarts_alive_process_with_stale_heartbeat(tmp_path):
    import json
    import multiprocessing
    import time
    from datetime import datetime, timedelta, timezone

    from util.runtime.processes import AccessorySupervisor

    def worker():
        time.sleep(5)

    heartbeat_path = tmp_path / "nexrad_heartbeat.json"
    supervisor = AccessorySupervisor(
        health_path=str(tmp_path / "health.json"),
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    supervisor.add(
        "NEXRAD Ingest",
        worker,
        daemon=True,
        heartbeat_path=str(heartbeat_path),
        heartbeat_stale_seconds=0.01,
        heartbeat_startup_grace_seconds=60,
    )
    supervisor.start_all()
    old_proc = supervisor._process_info[0]["process"]
    heartbeat_path.write_text(json.dumps({
        "pid": old_proc.pid,
        "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
    }), encoding="utf-8")

    supervisor.check()
    # Restart backoff must not block `check()` (and therefore heartbeat
    # publication); a later supervision tick performs the restart.
    time.sleep(0.02)
    supervisor.check()

    new_proc = supervisor._process_info[0]["process"]
    assert new_proc is not None and new_proc.is_alive()
    assert new_proc.pid != old_proc.pid
    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert "stale heartbeat" in health["NEXRAD Ingest"]["last_error"]
    supervisor.shutdown()
