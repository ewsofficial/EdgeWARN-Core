from datetime import datetime, timezone
from types import SimpleNamespace

from util.runtime import background


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def test_nexrad_ingest_loop_restarts_after_exception(monkeypatch):
    log_queue = FakeQueue()
    calls = []

    def _fake_run_realtime_ingestion_pipeline(base_dir=None, **_kwargs):
        calls.append(base_dir)
        if len(calls) == 1:
            raise RuntimeError("boom")
        raise KeyboardInterrupt()

    monkeypatch.setattr(background, "run_realtime_ingestion_pipeline", _fake_run_realtime_ingestion_pipeline)
    monkeypatch.setattr(background, "sleep_for", lambda *_args, **_kwargs: None)

    background.nexrad_ingest_loop(log_queue, "/tmp/nexrad-base")

    assert calls == ["/tmp/nexrad-base", "/tmp/nexrad-base"]
    assert any("Starting NEXRAD ingest pipeline" in item for item in log_queue.items)
    assert any("NEXRAD ingest pipeline crashed: boom" in item for item in log_queue.items)


def test_nexrad_ingest_loop_skips_restart_when_shutdown_requested(monkeypatch):
    log_queue = FakeQueue()
    calls = []

    def _fake_run_realtime_ingestion_pipeline(base_dir=None, **_kwargs):
        calls.append(base_dir)
        background._SHUTDOWN_REQUESTED = True
        return None

    monkeypatch.setattr(background, "run_realtime_ingestion_pipeline", _fake_run_realtime_ingestion_pipeline)
    monkeypatch.setattr(background, "sleep_for", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(background, "_configure_process_runtime", lambda _name: None)
    background._SHUTDOWN_REQUESTED = False

    background.nexrad_ingest_loop(log_queue, "/tmp/nexrad-base")

    assert calls == ["/tmp/nexrad-base"]
    assert not any("restarting in 5s" in item for item in log_queue.items)
