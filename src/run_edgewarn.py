"""Supported primary EdgeWARN command (decomposition Phase 5).

The latency-sensitive primary service. Owns MRMS timestamp selection and the
S3/HTTPS selection policy, detection/integration MRMS downloads exactly once
per cycle, raw RAP download, scan-time GLM when GOES is enabled, detection,
tracking/lineage, integration, CTAM, alert generation, and API index updates,
plus the truthful primary cycle outcome/retry state and publication of the
immutable ``mrms-ready``/``rap-ready`` records ``run_ewmrs.py`` consumes.

Run directly:

    python src/run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300

It does not import or start EWMRS, NEXRAD, METAR, NWS, WPC, or GOES ABI loops.
Behavior mirrors the other direct services: a single-instance lock beneath
``state/realtime/services/``, an atomic canonical heartbeat refreshed from the
selection loop's ticks, and clean SIGINT/SIGTERM shutdown of its own worker.
No import side effects: parsing and runtime initialization happen in ``main()``.
"""

import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from common.config import loader as config_loader
from EdgeWARN import initialize_runtime
from EdgeWARN.stormprob.assets import validate_assets
from EdgeWARN.schedule.scheduler import MRMSUpdateChecker
from util.io import TimestampedOutput, IOManager
from util.release import get_release_version
from util.runtime.handoff import ServiceLock
from util.runtime.primary_service import (
    build_cycle_config,
    log_effective_flags,
    report_effective_config,
    run_primary_cycle_loop,
)
from util.runtime.services import (
    ServiceHeartbeat,
    services_dir,
    write_heartbeat,
)


SERVICE_NAME = "edgewarn"
HEARTBEAT_MIN_INTERVAL_SECONDS = 2.0


def main():
    sys.stdout = TimestampedOutput(sys.stdout)
    sys.stderr = TimestampedOutput(sys.stderr)

    io_manager = IOManager("[EdgeWARN]")
    args = io_manager.get_args()

    try:
        validate_assets()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[EdgeWARN] StormProb startup validation failed: {exc}")
        sys.exit(1)

    initialize_runtime(base_dir=args.base_dir, io_manager=io_manager)

    print(f"Primary EdgeWARN service started (v{get_release_version()}). Press CTRL+C to exit.")
    print("[EdgeWARN] EWMRS and its accessories are owned by run_ewmrs.py; NEXRAD by run_nexrad.py.")
    log_effective_flags(args)

    run_id = uuid.uuid4().hex
    lock = ServiceLock(args.base_dir, SERVICE_NAME)
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"[EdgeWARN] {exc}")
        sys.exit(1)

    stop_event = threading.Event()

    def _request_stop(signum, _frame):
        print("[EdgeWARN] Shutdown signal received; stopping after the current atomic unit...")
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _request_stop)

    heartbeat_destination = str(services_dir(args.base_dir) / f"{SERVICE_NAME}.json")
    last_beat = {"monotonic": 0.0}

    def refresh_heartbeat():
        now_monotonic = time.monotonic()
        if stop_event.is_set() or (
            now_monotonic - last_beat["monotonic"] < HEARTBEAT_MIN_INTERVAL_SECONDS
        ):
            return
        last_beat["monotonic"] = now_monotonic
        beat = ServiceHeartbeat(
            service=SERVICE_NAME,
            pid=os.getpid(),
            run_id=run_id,
            updated_at=datetime.now(timezone.utc),
            phase="cycling",
            version=get_release_version(),
            degraded_children=(),
        )
        write_heartbeat(beat, heartbeat_destination)

    try:
        run_primary_cycle_loop(
            checker=MRMSUpdateChecker(verbose=True),
            cycle_config=build_cycle_config(args),
            supervisor=None,
            on_tick=refresh_heartbeat,
            stop_event=stop_event,
        )
    finally:
        stop_event.set()
        try:
            os.unlink(heartbeat_destination)
        except OSError:
            pass
        lock.release()
        print("[EdgeWARN] Primary service stopped.")


if __name__ == "__main__":
    try:
        print(f"Running EdgeWARN v{get_release_version()}")
        main()
    except KeyboardInterrupt:
        print("CTRL+C detected, exiting ...")
        sys.exit(0)
    except config_loader.ConfigError:
        # This catches configuration failures reached after argument parsing.
        # Import-time ConfigError instances cannot be caught here; CI's
        # validate-config gate is responsible for rejecting those before startup.
        report_effective_config()
        raise
