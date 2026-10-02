"""Supported independent realtime ingest command (plan phase 4).

The ingest service owns every realtime MRMS, raw RAP, and scan-time GLM
acquisition. It polls its sources on a fixed cadence, publishes each validated
input to the durable inventory and render outbox, and evaluates per-scan Core
readiness. It performs no scientific analysis and no rendering, and it never
imports EWMRS or the Core analysis modules: its outbox describes source
products, and EWMRS owns the product-to-layer mapping.

Because nothing here waits for Core or EWMRS, a blocked Core worker, a failed
detection, or a completely stopped consumer never pauses acquisition.

Run directly:

    python src/run_ingest.py

Behavior mirrors the other direct services: a single-instance lock beneath
``state/realtime/services/``, an atomic canonical heartbeat refreshed by an
independent liveness thread, and clean SIGINT/SIGTERM shutdown that joins every
owned download/decode worker within the configured termination bound. No import
side effects: parsing and runtime initialization happen in ``main()``.
"""

import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone

from common.config import loader as config_loader, overlay
from util.cli import build_service_parser
from util.io import IOManager, TimestampedOutput
from util.release import get_release_version
from util.runtime.handoff import ServiceLock
from util.runtime.ingest_service import IngestResources, IngestService
from util.runtime.services import (
    ServiceHeartbeat,
    run_heartbeat_loop,
    services_dir,
    write_heartbeat,
)

SERVICE_NAME = "ingest"
HEARTBEAT_MIN_INTERVAL_SECONDS = 2.0


def _parse_args(argv=None):
    """Resolve only the flags the ingest service honors.

    Resolution order matches the other direct services: an explicit flag wins,
    then the runtime catalog. Acquisition-only flags belong to this service; the
    dependency-shared flags (``--mrms-core-only``, ``--disable-goes``,
    ``--disable-ctam``, ``--disable-stormprob``) are
    accepted here so the producer's effective dependency agreement is explicit,
    and the launcher propagates the same value to every consumer.
    """
    args = build_service_parser(SERVICE_NAME).parse_args(argv)
    filesystem = config_loader.load_config("filesystem", config_dir=args.config_dir)
    args.base_dir = str(
        overlay.resolve_base_dir(args.base_dir, filesystem).expanduser().resolve()
    )
    run_cfg = config_loader.load_config("runtime", config_dir=args.config_dir)["run"]
    for flag, key in (("disable_goes", "run.disable_goes"),
                      ("disable_ctam", "run.disable_ctam"),
                      ("disable_stormprob", "run.disable_stormprob"),
                      ("mrms_core_only", "run.mrms_core_only"),
                      ("profile", "run.profile")):
        setattr(args, flag, overlay.resolve(
            getattr(args, flag), yaml_value=run_cfg[flag], key=key))
    root = config_loader.export_config_root(args.config_dir)
    config_loader.validate_all_configs(config_dir=root)
    return args


def main():
    sys.stdout = TimestampedOutput(sys.stdout)
    sys.stderr = TimestampedOutput(sys.stderr)

    io_manager = IOManager("[Ingest]")
    args = _parse_args()

    from util.runtime.mrms_migration import require_completed_migration

    try:
        require_completed_migration(args.config_dir)
    except RuntimeError as exc:
        print(f"[Ingest] {exc}")
        sys.exit(1)

    import util.file as fs

    fs.initialize_filesystem(args.base_dir)
    from common.ingest.mrms.config import get_ingest_dependencies, get_registry

    registry = get_registry()
    if registry is None:
        print("[Ingest] Independent ingest requires the version 2 MRMS registry.")
        sys.exit(1)
    try:
        dependencies = get_ingest_dependencies(
            mrms_core_only=args.mrms_core_only, disable_goes=args.disable_goes,
            disable_ctam=args.disable_ctam, disable_stormprob=args.disable_stormprob)
    except ValueError as exc:
        print(f"[Ingest] Dependency preflight failed: {exc}")
        sys.exit(1)

    # Topology preflight precedes any runtime-tree mutation: an unsupported
    # configuration must fail before directories or records exist.
    handoff_enabled = overlay.resolve(
        None, env_names=["EDGEWARN_HANDOFF_ENABLED"],
        yaml_value=config_loader.load_config(
            "runtime", config_dir=args.config_dir)["handoff"]["enabled"],
        key="handoff.enabled")
    if not handoff_enabled:
        print("[Ingest] handoff.enabled=false is not supported in the independent ingest "
              "topology: Core and EWMRS both require durable readiness records. Enable it, "
              "or run the retired batch pipeline instead.")
        sys.exit(1)

    fs.ensure_mrms_directories(registry)

    print(f"Independent realtime ingest service started (v{get_release_version()}). "
          "Press CTRL+C to exit.")
    print("[Ingest] Acquisition owner for realtime MRMS, raw RAP, and scan-time GLM.")
    print("[Ingest] Core consumes its readiness records and EWMRS its render outbox; "
          "neither is waited on here.")
    if args.mrms_core_only:
        print("[Ingest] MRMS-core-only: RAP and scan-time GLM acquisition disabled.")
    if args.disable_goes:
        print("[Ingest] GOES/GLM acquisition disabled via --disable-goes")

    run_id = uuid.uuid4().hex
    lock = ServiceLock(args.base_dir, SERVICE_NAME)
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"[Ingest] {exc}")
        sys.exit(1)

    from util.runtime.mrms_registry import publish_ingest_registry

    publish_ingest_registry(registry, run_id,
                            dependency_fingerprint=dependencies.fingerprint)

    stop_event = threading.Event()
    service = IngestService(
        base_dir=fs.BASE_DIR, run_id=run_id, registry=registry,
        dependencies=dependencies,
        resources=IngestResources.resolve(config_dir=args.config_dir),
        io=io_manager, stop_event=stop_event)

    def _request_stop(_signum, _frame):
        print("[Ingest] Shutdown signal received; stopping after the current atomic unit...")
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _request_stop)

    heartbeat_destination = str(services_dir(args.base_dir) / f"{SERVICE_NAME}.json")

    def refresh_heartbeat():
        # A heartbeat is poll liveness, never proof that an input is ready: a
        # quiet upstream leaves this service active with no new inputs.
        write_heartbeat(ServiceHeartbeat(
            service=SERVICE_NAME, pid=os.getpid(), run_id=run_id,
            updated_at=datetime.now(timezone.utc), phase=service.status()["phase"],
            version=get_release_version(), degraded_children=(),
        ), heartbeat_destination)

    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=run_heartbeat_loop, args=(heartbeat_stop, refresh_heartbeat),
        kwargs={"interval_seconds": HEARTBEAT_MIN_INTERVAL_SECONDS},
        name="ingest-heartbeat", daemon=False,
    )
    heartbeat_thread.start()

    try:
        service.run()
    finally:
        stop_event.set()
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=HEARTBEAT_MIN_INTERVAL_SECONDS + 1.0)
        try:
            os.unlink(heartbeat_destination)
        except OSError:
            pass
        lock.release()
        print("[Ingest] Ingest service stopped.")


if __name__ == "__main__":
    try:
        print(f"Running EdgeWARN ingest v{get_release_version()}")
        main()
    except KeyboardInterrupt:
        print("CTRL+C detected, exiting ...")
        sys.exit(0)
    except config_loader.ConfigError:
        # This catches configuration failures reached after argument parsing.
        # Import-time ConfigError instances cannot be caught here; CI's
        # validate-config gate is responsible for rejecting those before startup.
        from util.runtime.primary_service import report_effective_config

        report_effective_config()
        raise
