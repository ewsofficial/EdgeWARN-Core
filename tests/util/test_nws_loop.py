"""The NWS alert loop runs off the ingest path and honors its stop event."""
import threading
import time

import common.ingest.nws.main as nws_main
from util.runtime import nws_loop
from util.runtime.nws_loop import NwsAlertLoop


def _patch(monkeypatch, download, seconds=0.01):
    monkeypatch.setattr(nws_main, "download_alerts_async", download)
    monkeypatch.setattr(nws_loop, "section", lambda name: {"nws_seconds": seconds})


def test_loop_fetches_repeatedly_without_blocking_caller(monkeypatch):
    calls, release = [], threading.Event()

    async def download(_dt):
        calls.append(1)
        if len(calls) == 1:
            release.wait(2)

    _patch(monkeypatch, download)
    stop = threading.Event()
    loop = NwsAlertLoop(stop_event=stop)
    started = time.monotonic()
    loop.start()
    assert time.monotonic() - started < 0.5  # a hung first fetch does not block start()
    assert loop.alive()
    release.set()
    deadline = time.monotonic() + 2
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    loop.join(2)
    assert len(calls) >= 3 and not loop.alive()


def test_loop_survives_fetch_errors(monkeypatch):
    attempts, logged = [], []

    async def download(_dt):
        attempts.append(1)
        raise RuntimeError("weather.gov unavailable")

    _patch(monkeypatch, download)
    stop = threading.Event()
    loop = NwsAlertLoop(stop_event=stop, log=logged.append)
    loop.start()
    deadline = time.monotonic() + 2
    while len(attempts) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert loop.alive()
    stop.set()
    loop.join(2)
    assert len(attempts) >= 2
    assert any("weather.gov unavailable" in line for line in logged)


def test_failures_back_off_and_mark_unhealthy(monkeypatch):
    waits, now = [], [0.0]

    async def download(_dt):
        raise RuntimeError("429")

    class Stop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) >= 4:
                self.set()
            return super().is_set()

    _patch(monkeypatch, download, seconds=10)
    stop = Stop()
    loop = NwsAlertLoop(stop_event=stop, log=lambda _m: None, clock=lambda: now[0])
    loop.start()
    loop.join(2)
    assert waits == [20, 40, 80, 80]  # capped at 8x the base cadence
    assert not loop.healthy()  # thread exited; also never succeeded


def test_healthy_requires_recent_success(monkeypatch):
    now = [0.0]
    release = threading.Event()

    async def download(_dt):
        release.wait(2)

    _patch(monkeypatch, download, seconds=10)
    stop = threading.Event()
    loop = NwsAlertLoop(stop_event=stop, clock=lambda: now[0])
    loop.start()
    assert loop.healthy()
    now[0] = 1000.0  # no success for far longer than 5 intervals
    assert not loop.healthy()
    release.set()
    deadline = time.monotonic() + 2
    while loop._last_success is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert loop.healthy()
    stop.set()
    loop.join(2)
