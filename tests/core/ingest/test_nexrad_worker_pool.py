import common.ingest.nexrad.worker as worker_module
import common.ingest.nexrad.worker_pool as worker_pool


def test_get_child_rss_kb_prefers_current_vmrss(monkeypatch):
    status_payload = "Name:\tpython\nVmRSS:\t  4321 kB\n"

    def _fake_open(*_args, **_kwargs):
        class _Reader:
            def __enter__(self):
                from io import StringIO
                return StringIO(status_payload)

            def __exit__(self, *_exc):
                return False

        return _Reader()

    monkeypatch.setattr(worker_module, "open", _fake_open, raising=False)

    assert worker_module._get_child_rss_kb() == 4321.0


def test_record_volume_and_maybe_recycle_restarts_pool(monkeypatch):
    class _FakePool:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, wait=True):
            self.shutdown_calls.append(wait)

    pool = _FakePool()
    monkeypatch.setenv("NEXRAD_WORKER_RECYCLE_INTERVAL", "2")
    monkeypatch.setattr(worker_pool, "_POOL", pool)
    monkeypatch.setattr(worker_pool, "_POOL_SIZE", 4)
    monkeypatch.setattr(worker_pool, "_VOLUME_COUNT", 0)

    worker_pool.record_volume_and_maybe_recycle()
    assert worker_pool._POOL is pool
    assert worker_pool._VOLUME_COUNT == 1
    assert pool.shutdown_calls == []

    worker_pool.record_volume_and_maybe_recycle()
    assert pool.shutdown_calls == [True]
    assert worker_pool._POOL is None
    assert worker_pool._POOL_SIZE == 0
    assert worker_pool._VOLUME_COUNT == 0


def test_record_volume_and_maybe_recycle_can_be_disabled(monkeypatch):
    class _FakePool:
        def shutdown(self, wait=True):
            raise AssertionError("shutdown should not be called")

    pool = _FakePool()
    monkeypatch.setenv("NEXRAD_WORKER_RECYCLE_INTERVAL", "0")
    monkeypatch.setattr(worker_pool, "_POOL", pool)
    monkeypatch.setattr(worker_pool, "_POOL_SIZE", 4)
    monkeypatch.setattr(worker_pool, "_VOLUME_COUNT", 0)

    worker_pool.record_volume_and_maybe_recycle()

    assert worker_pool._POOL is pool
    assert worker_pool._VOLUME_COUNT == 0


def test_recycle_releases_global_lock_before_waiting(monkeypatch):
    import threading
    entered, finish = threading.Event(), threading.Event()
    class Pool:
        def shutdown(self, wait=True):
            entered.set()
            finish.wait(3)
    monkeypatch.setenv("NEXRAD_WORKER_RECYCLE_INTERVAL", "1")
    monkeypatch.setattr(worker_pool, "_POOL", Pool())
    monkeypatch.setattr(worker_pool, "_VOLUME_COUNT", 0)
    thread = threading.Thread(target=worker_pool.record_volume_and_maybe_recycle)
    thread.start()
    try:
        assert entered.wait(1)
        assert worker_pool._LOCK.acquire(blocking=False)
        worker_pool._LOCK.release()
    finally:
        finish.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_submit_on_pool_retired_by_recycle_is_retried_on_fresh_pool(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import util.file as fs
    from common.ingest.nexrad.service import NexradIngestService
    import common.ingest.nexrad.service as service_module

    class _Retired:
        def submit(self, **_kwargs):
            raise RuntimeError("cannot schedule new futures after shutdown")

    class _Fresh:
        submits = 0

        def submit(self, **_kwargs):
            _Fresh.submits += 1
            payload = SimpleNamespace(
                visible_sweeps=1, saved_sweep_count=0, saved_elevations=[],
                parse_error=None, child_rss_kb=1.0, buffer_trimmed=False, runtime_size=None,
            )
            return SimpleNamespace(result=lambda timeout=None: payload)

    fresh = _Fresh()
    fs.initialize_filesystem(tmp_path)
    runtime_path = tmp_path / "volume.ar2v"
    runtime_path.write_bytes(b"partial")
    state = SimpleNamespace(file_path=str(runtime_path), parse_errors=[], bytes_written=7)
    # Simulate the snapshot-copy window: the first get_nexrad_pool() result is already retired.
    pools = iter([_Retired()])
    monkeypatch.setattr(service_module, "get_nexrad_pool", lambda: next(pools, fresh))

    NexradIngestService()._run_worker_parse(
        state, "KTLH", "999", "20260507-150000", {}, None,
        elevation_timestamps_by_id={}, base_dir=tmp_path,
    )

    assert _Fresh.submits == 1
    assert state.parse_errors == []


def test_submit_retry_is_bounded_and_unrelated_runtime_error_propagates(monkeypatch):
    import common.ingest.nexrad.service as service_module

    class _Retired:
        calls = 0

        def submit(self, **_kwargs):
            _Retired.calls += 1
            raise RuntimeError("cannot schedule new futures after shutdown")

    class _Other:
        calls = 0

        def submit(self, **_kwargs):
            _Other.calls += 1
            raise RuntimeError("something else")

    monkeypatch.setattr(service_module, "get_nexrad_pool", lambda: _Retired())
    try:
        service_module._submit_to_live_pool(_Retired())
    except RuntimeError as exc:
        assert "after shutdown" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert _Retired.calls == service_module._POOL_SHUTDOWN_SUBMIT_ATTEMPTS

    try:
        service_module._submit_to_live_pool(_Other())
    except RuntimeError as exc:
        assert str(exc) == "something else"
    else:
        raise AssertionError("expected RuntimeError")
    assert _Other.calls == 1
