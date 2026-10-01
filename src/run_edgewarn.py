"""Supported primary EdgeWARN command (decomposition Phase 5).

The latency-sensitive primary service. Owns detection, tracking/lineage,
integration, CTAM, alert generation, and API index updates, plus the truthful
primary cycle outcome/retry state.

Since the independent-ingest cutover this command performs **no source
acquisition at all**: realtime MRMS, raw RAP, and scan-time GLM downloads are
owned by ``run_ingest.py``. Core reads the immutable readiness records the
ingest service commits beneath ``state/realtime/ingest/v1/``, starts detection
only once every configured check modifier is locally valid, and waits locally
for the later integration prerequisites. Launching Core without a separately
running ingest service leaves it waiting, with an explicit diagnostic, until
scan readiness appears.

It does not import or start EWMRS, NEXRAD, METAR, NWS, WPC, or GOES ABI loops.
Behavior mirrors the other direct services: a single-instance lock beneath
``state/realtime/services/``, an atomic canonical heartbeat refreshed by an
independent liveness thread, and clean SIGINT/SIGTERM shutdown of its own worker.
No import side effects: parsing and runtime initialization happen in ``main()``.
"""

import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone

from common.config import loader as config_loader
from EdgeWARN import initialize_runtime
from EdgeWARN.ctam.preflight import PreflightError, StormProbDependencyError, check_core_startup
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
    run_heartbeat_loop,
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
    args.ingest_run_id = uuid.uuid4().hex

    try:
        from util.runtime.mrms_migration import require_completed_migration
        require_completed_migration(args.config_dir)
        args.ctam_discovery = check_core_startup(
            config_dir=args.config_dir, base_dir=args.base_dir,
            module_root=args.ctam_module_dir, disable_ctam=args.disable_ctam,
            disable_ctam_modules=args.disable_ctam_modules,
            disable_stormprob=args.disable_stormprob,
            mrms_core_only=args.mrms_core_only,
        )
    except (PreflightError, StormProbDependencyError, RuntimeError) as exc:
        print(f"[EdgeWARN] {exc}")
        sys.exit(1)

    initialize_runtime(base_dir=args.base_dir, io_manager=io_manager)
    import util.file as fs
    from common.ingest.mrms.config import get_registry

    registry = get_registry()
    if registry is not None:
        fs.ensure_mrms_directories(registry)

    print(f"Primary EdgeWARN service started (v{get_release_version()}). Press CTRL+C to exit.")
    print("[EdgeWARN] EWMRS and its accessories are owned by run_ewmrs.py; NEXRAD by run_nexrad.py.")
    print("[EdgeWARN] Realtime MRMS/RAP/GLM acquisition is owned by run_ingest.py; this "
          "process consumes its durable readiness records and waits when they are absent.")
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
    def refresh_heartbeat():
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

    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=run_heartbeat_loop,
        args=(heartbeat_stop, refresh_heartbeat),
        kwargs={"interval_seconds": HEARTBEAT_MIN_INTERVAL_SECONDS},
        name="edgewarn-heartbeat",
        daemon=False,
    )
    heartbeat_thread.start()

    try:
        run_primary_cycle_loop(
            cycle_config=build_cycle_config(args),
            supervisor=None,
            on_tick=None,
            stop_event=stop_event,
        )
    finally:
        stop_event.set()
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=HEARTBEAT_MIN_INTERVAL_SECONDS + 1.0)
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
