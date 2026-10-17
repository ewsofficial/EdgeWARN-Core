import asyncio
from datetime import datetime, timezone
import json
import os
import signal
import sys
import time
import traceback

from common.ingest.nexrad.pipeline import run_realtime_ingestion_pipeline
from util.io import QueueWriter

from .config import section
from .logging import queue_log
from .process_identity import set_parent_death_signal as _set_parent_death_signal
from .process_identity import set_process_name as _set_process_name
from .timing import sleep_for, sleep_until_boundary


_SHUTDOWN_REQUESTED = False


def _install_exit_signal_handlers() -> None:
    def _raise_system_exit(signum, _frame):
        global _SHUTDOWN_REQUESTED
        _SHUTDOWN_REQUESTED = True
        raise SystemExit(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _raise_system_exit)
        except Exception:
            pass


def _configure_process_runtime(name: str) -> None:
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = False
    _set_process_name(name)
    _set_parent_death_signal()
    _install_exit_signal_handlers()






def _write_nexrad_heartbeat(heartbeat_path, payload, *, latest_output):
    """Atomically publish completed NEXRAD-cycle progress for the supervisor."""
    if heartbeat_path is None:
        return latest_output
    records = payload.get("completed_records", [])
    if records:
        record = records[-1]
        latest_output = {
            "site": record.site,
            "volume_id": record.volume_id,
            "scan_timestamp": record.scan_timestamp,
        }
    heartbeat = {
        "pid": os.getpid(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "cycle": payload["cycle"],
        "output_count": payload["output_count"],
        "timed_out": payload["timed_out"],
        "latest_output": latest_output,
    }
    os.makedirs(os.path.dirname(heartbeat_path), exist_ok=True)
    temporary = f"{heartbeat_path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(heartbeat, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, heartbeat_path)
    return latest_output


def _wait_for_primary_quiescence(base_dir, log_func=None):
    """Cooperative cross-service throttle (Phase 3, default off).

    While the primary holds an unexpired activity lease, NEXRAD waits before
    admitting a new ingest scan or render batch. An atomic unit already in
    progress is never interrupted, and the wait is bounded by
    ``pause_max_wait_seconds`` so the weather cycle can never block NEXRAD
    indefinitely -- nor NEXRAD block itself on a crashed primary.
    """
    coordination = section("nexrad_coordination")
    if not coordination["pause_ingest_during_primary_activity"]:
        return

    from util.runtime.handoff import primary_activity_held

    deadline = time.monotonic() + coordination["pause_max_wait_seconds"]
    logged_wait = False
    while time.monotonic() < deadline:
        held = primary_activity_held(base_dir)
        if held is None:
            return
        if not logged_wait:
            message = (
                f"Primary cycle {held.cycle_id} holds the activity lease; "
                f"waiting before admitting new NEXRAD work"
            )
            if log_func is not None:
                log_func(f"INFO: {message}")
            else:
                print(f"[NEXRAD] {message}")
            logged_wait = True
        sleep_for(
            max(
                0.1,
                min(deadline - time.monotonic(), float(
                    coordination["pause_poll_interval_seconds"]
                )),
            ),
            interval=coordination["pause_poll_interval_seconds"],
        )


def nexrad_ingest_loop(log_queue, base_dir, heartbeat_path=None):
    from common.ingest.nexrad.worker_pool import shutdown_nexrad_pool

    _configure_process_runtime("NEXRAD-Ingest")
    sys.stdout = QueueWriter(log_queue)
    sys.stderr = QueueWriter(log_queue)
    latest_output = None
    intervals = section("background_intervals")
    restart_seconds = intervals["nexrad_seconds"]

    def _heartbeat(payload):
        nonlocal latest_output
        latest_output = _write_nexrad_heartbeat(
            heartbeat_path,
            payload,
            latest_output=latest_output,
        )

    try:
        while not _SHUTDOWN_REQUESTED:
            try:
                if _SHUTDOWN_REQUESTED:
                    return
                _wait_for_primary_quiescence(base_dir, log_func=lambda msg: queue_log(log_queue, msg))
                if _SHUTDOWN_REQUESTED:
                    return
                queue_log(log_queue, "INFO: Starting NEXRAD ingest pipeline")
                run_realtime_ingestion_pipeline(base_dir=base_dir, heartbeat_callback=_heartbeat)
                if _SHUTDOWN_REQUESTED:
                    return
                queue_log(
                    log_queue,
                    f"WARNING: NEXRAD ingest pipeline exited; restarting in {restart_seconds}s",
                )
            except (KeyboardInterrupt, SystemExit):
                return
            except Exception as exc:
                queue_log(log_queue, f"ERROR: NEXRAD ingest pipeline crashed: {exc}")
                for line in traceback.format_exc().splitlines():
                    queue_log(log_queue, f"ERROR: {line}")

            if _SHUTDOWN_REQUESTED:
                return
            sleep_for(
                restart_seconds,
                interval=intervals["nexrad_interval_seconds"],
            )
    finally:
        shutdown_nexrad_pool(wait=False)


def nexrad_render_loop(base_dir):
    from NEXRAD.gui_pipeline import run_nexrad_render_loop

    _configure_process_runtime("NEXRAD-Render")
    try:
        # Quiescence is checked per poll cycle inside the loop so an atomic
        # render already in progress is never interrupted.
        run_nexrad_render_loop(
            base_dir=base_dir,
            wait_for_quiescence=lambda: _wait_for_primary_quiescence(base_dir),
        )
    except (KeyboardInterrupt, SystemExit):
        return






