"""Standalone EWMRS/accessory service (decomposition Phase 4).

Owns EWMRS and non-NEXRAD accessory work:

- Consumption of committed ``mrms-ready``/``rap-ready`` records, rendering
  MRMS layers from the exact pinned paths in each record.
- RAP Uint16 conversion (moved out of the shared primary coordinator).
- GOES ABI ingest and poll-based GOES rendering from locally staged files.
- METAR, NWS, and WPC continuous ingest loops.
- EWMRS GUI retention for the layers it owns.

Run directly:

    python src/run_ewmrs.py [--base_dir DIR] [--config-dir DIR]

It does not perform MRMS timestamp discovery or MRMS/RAP downloads (the
primary service owns those), does not do scan-time GLM integration work, and
does not import, launch, render, or clean NEXRAD.

Behavior mirrors ``run_nexrad.py``: a single-instance lock beneath
``state/realtime/services/``, an atomic canonical heartbeat whose degraded
children list surfaces crash-looped accessory loops, and clean SIGINT/SIGTERM
shutdown of its own children only. No import side effects.
"""

import argparse
import json
import multiprocessing
import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone

import util.file as fs
from common.config import loader as config_loader, overlay
from util.cli import build_service_parser
from util.io import IOManager, TimestampedOutput
from util.release import get_release_version
from util.runtime import AccessorySupervisor, StartedProcessRegistry, drain_log_queue
from util.runtime.config import resolve_file, section
from util.runtime.handoff import ServiceLock
from util.runtime.services import (
    ServiceHeartbeat,
    services_dir,
    write_heartbeat,
)


SERVICE_NAME = "ewmrs"


def _require_nws_zone_assets(nws_enabled: bool) -> None:
    """Fail EWMRS startup early when NWS geometry assets are unavailable."""
    if not nws_enabled:
        return

    # Zone lookup is a required part of NWS alert processing. Validate the
    # operator-maintained assets before starting any accessory child so a
    # missing initial sync is an actionable startup failure, not a later
    # background-loop error after alerts have been downloaded.
    from common.ingest.nws.geomapper import ensure_zone_assets

    ensure_zone_assets()


def _parse_args(argv=None):
    parser = build_service_parser("ewmrs")
    args = parser.parse_args(argv)

    filesystem = config_loader.load_config("filesystem", config_dir=args.config_dir)
    args.base_dir = str(
        overlay.resolve_base_dir(args.base_dir, filesystem).expanduser().resolve()
    )

    def _resolve(flag, yaml_value, key):
        # No env layer here, matching IOManager's resolution of these same
        # run.* keys in the primary parser: CLI > YAML only.
        return bool(overlay.resolve(getattr(args, flag), yaml_value=yaml_value, key=key))

    run_cfg = config_loader.load_config("runtime", config_dir=args.config_dir)["run"]
    args.disable_metar = _resolve("disable_metar", run_cfg["disable_metar"], "run.disable_metar")
    args.disable_nws = _resolve("disable_nws", run_cfg["disable_nws"], "run.disable_nws")
    args.disable_wpc = _resolve("disable_wpc", run_cfg["disable_wpc"], "run.disable_wpc")
    args.disable_goes = _resolve("disable_goes", run_cfg["disable_goes"], "run.disable_goes")
    args.mrms_core_only = _resolve("mrms_core_only", run_cfg["mrms_core_only"], "run.mrms_core_only")

    # Publish the resolved config root so spawned children inherit it,
    # matching the monolithic runner's behavior.
    root = config_loader.export_config_root(args.config_dir)
    config_loader.validate_all_configs(config_dir=root)
    return args


def main():
    from util.runtime.ewmrs_service import register_ewmrs_accessories

    sys.stdout = TimestampedOutput(sys.stdout)
    sys.stderr = TimestampedOutput(sys.stderr)

    io_manager = IOManager("[EWMRS]")
    args = _parse_args()
    if args.mrms_core_only:
        print("[EWMRS] mrms-core-only is enabled; EWMRS/accessory service will not start.")
        return
    fs.initialize_filesystem(args.base_dir)

    _require_nws_zone_assets(nws_enabled=not args.disable_nws)

    print(f"EWMRS service started (v{get_release_version()}). Press CTRL+C to exit.")
    print("[EWMRS] MRMS/RAP downloads are owned by the primary service; this service consumes its committed records.")

    run_id = uuid.uuid4().hex
    lock = ServiceLock(args.base_dir, SERVICE_NAME)
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"[EWMRS] {exc}")
        sys.exit(1)

    supervisor_settings = section("supervisor")
    goes_coordination = section("goes_coordination")

    goes_enabled = not args.disable_goes
    goes_pause_ingest_during_render = overlay.resolve(
        None,
        env_names=["EDGEWARN_PAUSE_GOES_INGEST_DURING_RENDER"],
        yaml_value=goes_coordination["pause_ingest_during_render"],
        key="goes_coordination.pause_ingest_during_render",
    )

    child_log_queue = multiprocessing.Queue()
    goes_cycle_active = multiprocessing.Event()
    goes_render_active = multiprocessing.Event()

    supervisor = AccessorySupervisor(
        max_restarts=supervisor_settings["max_restarts"],
        restart_window_seconds=supervisor_settings["restart_window_seconds"],
        base_backoff_seconds=supervisor_settings["base_backoff_seconds"],
        max_backoff_seconds=supervisor_settings["max_backoff_seconds"],
        health_path=str(resolve_file(
            supervisor_settings["health_file"], "supervisor.health_file"
        )),
    )
    register_ewmrs_accessories(
        supervisor,
        base_dir=args.base_dir,
        metar_enabled=not args.disable_metar,
        nws_enabled=not args.disable_nws,
        wpc_enabled=not args.disable_wpc,
        goes_ingest_enabled=goes_enabled,
        goes_render_enabled=goes_enabled,
        consumer_enabled=True,
        goes_cycle_active=goes_cycle_active,
        goes_render_active=goes_render_active,
        goes_pause_ingest_during_render=goes_pause_ingest_during_render,
        goes_poll_seconds=goes_coordination["poll_seconds"],
        child_log_queue=child_log_queue,
    )

    stop_event = threading.Event()

    def _request_stop(signum, _frame):
        print("[EWMRS] Shutdown signal received; stopping children...")
        stop_event.set()
        supervisor.request_stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _request_stop)

    supervisor.start_all()
    started_processes = StartedProcessRegistry()
    started_processes.processes = [
        (info["process"], info["name"])
        for info in supervisor._process_info
        if info["process"] is not None
    ]

    heartbeat_destination = str(services_dir(args.base_dir) / f"{SERVICE_NAME}.json")
    check_ticks = supervisor_settings["check_ticks"]
    tick_seconds = supervisor_settings["tick_seconds"]

    try:
        while not stop_event.is_set():
            drain_log_queue(child_log_queue)
            beat = ServiceHeartbeat(
                service=SERVICE_NAME,
                pid=os.getpid(),
                run_id=run_id,
                updated_at=datetime.now(timezone.utc),
                phase="supervising",
                version=get_release_version(),
                degraded_children=tuple(supervisor.disabled_names()),
            )
            write_heartbeat(beat, heartbeat_destination)
            for _ in range(check_ticks):
                # Bounded-backoff restarts and crash-loop disabling happen here.
                supervisor.check()
                drain_log_queue(child_log_queue)
                if stop_event.wait(tick_seconds):
                    break
    finally:
        stop_event.set()
        supervisor.request_stop()
        started_processes.shutdown(queue_sentinels=[(child_log_queue, None)])
        supervisor.shutdown()
        drain_log_queue(child_log_queue)
        try:
            os.unlink(heartbeat_destination)
        except OSError:
            pass
        lock.release()
        print("[EWMRS] Service stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("CTRL+C detected, exiting ...")
        sys.exit(0)
