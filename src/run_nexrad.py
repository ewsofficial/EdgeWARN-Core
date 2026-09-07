"""Standalone NEXRAD service (decomposition Phase 3).

Owns the complete NEXRAD lifecycle: Level-II ingest (with its non-daemonic
parser pool), GUI rendering, retention, and cleanup. Run directly:

    python src/run_nexrad.py [--base_dir DIR] [--config-dir DIR]

Behavior:

- Takes the single-instance ``nexrad`` lock beneath
  ``<BASE_DIR>/state/realtime/services/``; a second instance fails clearly.
- Supervises ingest and render children independently with bounded restart
  backoff; a crash-looped child is surfaced as a degraded child in the
  service heartbeat instead of being hidden.
- Publishes an atomic heartbeat at ``state/realtime/services/nexrad.json``
  under the canonical registry name for API visibility.
- Handles SIGINT/SIGTERM: stops admission, joins its own children, and leaves
  no orphaned worker or stale pause. It never touches another service's lock,
  checkpoint, lease, or output.

No import side effects: parsing, filesystem initialization, and process
startup all happen inside ``main()``.
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


SERVICE_NAME = "nexrad"


def _parse_args(argv=None):
    parser = build_service_parser("nexrad")
    args = parser.parse_args(argv)

    filesystem = config_loader.load_config("filesystem", config_dir=args.config_dir)
    args.base_dir = str(
        overlay.resolve_base_dir(args.base_dir, filesystem).expanduser().resolve()
    )

    run_cfg = config_loader.load_config("runtime", config_dir=args.config_dir)["run"]
    # No env layer, matching IOManager's resolution of run.* keys elsewhere.
    overlay.resolve(getattr(args, "profile"), yaml_value=run_cfg["profile"], key="run.profile")
    args.mrms_core_only = bool(overlay.resolve(
        args.mrms_core_only,
        yaml_value=run_cfg["mrms_core_only"],
        key="run.mrms_core_only",
    ))

    # Publish the resolved config root so spawned children inherit it,
    # matching the monolithic runner's behavior.
    root = config_loader.export_config_root(args.config_dir)
    config_loader.validate_all_configs(config_dir=root)
    return args


def _legacy_ingest_activity(heartbeat_path):
    """Latest NEXRAD ingest activity from the legacy heartbeat, if any."""
    try:
        with open(heartbeat_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return datetime.fromisoformat(str(payload["updated_at"])).replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return None


def main():
    from util.runtime.nexrad_service import register_nexrad_supervision

    sys.stdout = TimestampedOutput(sys.stdout)
    sys.stderr = TimestampedOutput(sys.stderr)

    io_manager = IOManager("[NEXRAD]")
    args = _parse_args()
    if args.mrms_core_only:
        print("[NEXRAD] mrms-core-only is enabled; NEXRAD service will not start.")
        return
    fs.initialize_filesystem(args.base_dir)

    print(f"NEXRAD service started (v{get_release_version()}). Press CTRL+C to exit.")

    run_id = uuid.uuid4().hex
    lock = ServiceLock(args.base_dir, SERVICE_NAME)
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"[NEXRAD] {exc}")
        sys.exit(1)

    supervisor_settings = section("supervisor")
    nexrad_heartbeat_path = str(resolve_file(
        supervisor_settings["nexrad_heartbeat_file"],
        "supervisor.nexrad_heartbeat_file",
    ))
    log_queue = multiprocessing.Queue()

    supervisor = AccessorySupervisor(
        max_restarts=supervisor_settings["max_restarts"],
        restart_window_seconds=supervisor_settings["restart_window_seconds"],
        base_backoff_seconds=supervisor_settings["base_backoff_seconds"],
        max_backoff_seconds=supervisor_settings["max_backoff_seconds"],
        health_path=str(resolve_file(
            supervisor_settings["health_file"], "supervisor.health_file"
        )),
    )
    register_nexrad_supervision(
        supervisor,
        base_dir=args.base_dir,
        nexrad_log_queue=log_queue,
        nexrad_heartbeat_path=nexrad_heartbeat_path,
        enabled=True,
    )

    stop_event = threading.Event()

    def _request_stop(signum, _frame):
        print("[NEXRAD] Shutdown signal received; stopping children...")
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

    heartbeat_destination = str(
        services_dir(args.base_dir) / f"{SERVICE_NAME}.json"
    )
    check_ticks = supervisor_settings["check_ticks"]
    tick_seconds = supervisor_settings["tick_seconds"]

    try:
        while not stop_event.is_set():
            drain_log_queue(log_queue)
            activity = _legacy_ingest_activity(nexrad_heartbeat_path)
            beat = ServiceHeartbeat(
                service=SERVICE_NAME,
                pid=os.getpid(),
                run_id=run_id,
                updated_at=datetime.now(timezone.utc),
                phase="supervising",
                version=get_release_version(),
                last_successful_activity=activity,
                degraded_children=tuple(supervisor.disabled_names()),
            )
            write_heartbeat(beat, heartbeat_destination)
            for _ in range(check_ticks):
                # Bounded-backoff restarts, crash-loop disabling, and
                # heartbeat-staleness checks all happen here; without this
                # call the supervision registration would be inert.
                supervisor.check()
                drain_log_queue(log_queue)
                if stop_event.wait(tick_seconds):
                    break
    finally:
        stop_event.set()
        supervisor.request_stop()
        started_processes.shutdown(queue_sentinels=[(log_queue, None)])
        supervisor.shutdown()
        drain_log_queue(log_queue)
        try:
            os.unlink(heartbeat_destination)
        except OSError:
            pass
        lock.release()
        print("[NEXRAD] Service stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("CTRL+C detected, exiting ...")
        sys.exit(0)
    except config_loader.ConfigError:
        raise
