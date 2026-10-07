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
