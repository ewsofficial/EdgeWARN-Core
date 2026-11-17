"""Background NWS active-alert fetch loop owned by the ingest process.

The loop runs on its own daemon thread, so a slow or failing weather.gov
request never delays MRMS/RAP/GLM acquisition or Core readiness publication.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import threading
import time

from util.io import IOManager

from .config import section


_MAX_BACKOFF_FACTOR = 8
_STALE_INTERVALS = 5


class NwsAlertLoop:
    """Fetch active NWS alerts on a fixed cadence until the stop event is set.

    Failures back off exponentially (capped) so a persistent 429/5xx is not
    retried at the base cadence. ``healthy`` also requires a recent success: a
    thread that is alive but failing every cycle is degraded, not healthy.
    """

    def __init__(self, *, stop_event, log=None, clock=time.monotonic):
        self._stop = stop_event
        self._log = log or IOManager("[NWS Loop]").write_error
        self._clock = clock
        self._thread = None
        self._started_at = None
        self._last_success = None

    @staticmethod
    def preflight():
        """Fail startup early when NWS zone geometry assets are unavailable."""
        from common.ingest.nws.geomapper import ensure_zone_assets

        ensure_zone_assets()

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="nws-alerts", daemon=True)
        self._started_at = self._clock()
        self._thread.start()

    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def healthy(self):
        """Alive and succeeding within the last few cadence intervals."""
        if not self.alive():
            return False
        interval = section("background_intervals")["nws_seconds"]
        reference = self._last_success if self._last_success is not None else self._started_at
        return self._clock() - reference <= interval * _STALE_INTERVALS

    def join(self, timeout):
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self):
        import common.ingest.nws.main as nws_ingest

        interval = section("background_intervals")["nws_seconds"]
        failures = 0
        while not self._stop.is_set():
            try:
                asyncio.run(nws_ingest.download_alerts_async(datetime.now(timezone.utc)))
                self._last_success = self._clock()
                failures = 0
            except Exception as exc:
                failures += 1
                self._log(f"Error: {exc}")
            self._stop.wait(interval * min(2 ** failures if failures else 1, _MAX_BACKOFF_FACTOR))
