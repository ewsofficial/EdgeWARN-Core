"""Primary EdgeWARN service functions (decomposition Phase 1).

Owns the truthful cycle-state store, retry policy, local readiness selection,
and the polling loop that drives ``run_primary_cycle_once``. Extracted verbatim
from the former monolithic ``run.py`` so the future ``run_edgewarn.py`` entry
point can call this module directly while the old runner remains a temporary
adapter.

Since the independent-ingest cutover this module performs **no remote
discovery**: :class:`LocalReadinessReader` selects pending scans from the
durable readiness records the ingest service commits, so a scan becomes a
candidate only when every configured check modifier is already downloaded and
validated locally. Waiting for a later prerequisite costs no analysis retry,
because no attempt is recorded until a start record actually exists.

The primary service must not import EWMRS or NEXRAD implementations at module
load: the EWMRS render jobs are independent per-input work owned by the EWMRS
service, and accessory loops live behind ``util.runtime.background``, which
this module never imports.
"""

import multiprocessing
import time
import uuid

import util.file as fs
from common.config import loader as config_loader, overlay
from util.runtime.cycle import (
    CycleRetryPolicy,
    CycleOutcome,
    CycleStageResult,
    CycleStatus,
    CycleStateStore,
    PrimaryCycleConfig,
    run_primary_cycle_once,
)
from util.runtime.config import resolve_file, section
from util.runtime.ingest_handoff import IngestRecordError, utc
from util.runtime.scheduler import load_last_processed_from_stormcells


def build_cycle_config(args):
    """Freeze one validated per-cycle configuration for the primary service."""
    from common.ingest.mrms.config import get_ingest_dependencies

    mrms_core_only = args.mrms_core_only
    handoff_settings = section("handoff")
    handoff_enabled = bool(overlay.resolve(
        None,
        env_names=["EDGEWARN_HANDOFF_ENABLED"],
        yaml_value=handoff_settings["enabled"],
        key="handoff.enabled",
    ))
    dependencies = get_ingest_dependencies(
        mrms_core_only=mrms_core_only,
        disable_goes=args.disable_goes,
    )
    consumers = section("consumers")
    return PrimaryCycleConfig(
        lat_limits=tuple(args.lat_limits),
        lon_limits=tuple(args.lon_limits),
        profile=args.profile,
        disable_ctam=args.disable_ctam,
        disable_ctam_modules=args.disable_ctam_modules,
        disable_stormprob=args.disable_stormprob,
        ctam_discovery=getattr(args, "ctam_discovery", None),
        disable_tracking=args.disable_tracking,
        disable_polygon_expansion=args.disable_polygon_expansion,
        refl_threshold=args.refl_threshold,
        min_seed_percentage=args.min_seed_percentage,
        drop_offset=args.drop_offset,
        config_dir=args.config_dir,
        goes_enabled=not args.disable_goes and not mrms_core_only,
        mrms_core_only=mrms_core_only,
        base_dir=args.base_dir,
        handoff_enabled=handoff_enabled,
        dependencies=dependencies,
        ingest_run_id=args.ingest_run_id,
        readiness_check_seconds=consumers["core_readiness_seconds"],
    )


class LocalReadinessReader:
    """Select pending scans from durable local readiness records.

    Replaces the remote S3/HTTPS timestamp intersection entirely. A scan is a
    candidate only once the ingest service has committed a complete, locally
    valid check set for it, so Core never selects, downloads, or validates a
    source itself. Candidates are returned oldest first to keep tracking and
    lineage monotonic.
    """

    def __init__(self, *, base_dir, run_id, dependencies, log=None):
        from util.runtime.ingest_handoff import IngestHandoff

        self.base_dir = base_dir
        self.dependencies = dependencies
        self._log = log if log is not None else print
        self.handoff = IngestHandoff(base_dir, fingerprint=dependencies.fingerprint,
                                     run_id=run_id)
        self._reported = set()

    def pending_scans(self):
        """Candidate scan times in timestamp order, oldest first."""
        from common.pipeline.readiness import validate_phase_dependencies

        pending = set()
        for record in self.handoff.records("core-start-ready"):
            if self.handoff.read("terminal", record.key) is not None:
                continue
            try:
                validate_phase_dependencies(record, self.dependencies)
            except IngestRecordError as exc:
                if record.key not in self._reported:
                    self._reported.add(record.key)
                    self._log(f"[Scheduler] Ignoring unusable start readiness for "
                              f"{record.key}: {exc}")
                continue
            manifest = record.to_manifest()
            if manifest is not None:
                pending.add(manifest.cycle_time)
        return tuple(sorted(pending))

    def skip(self, scan, reason):
        """Persist an explicit skip so a late old scan never rewinds tracking."""
        from util.runtime.handoff import canonical_cycle_id

        key = canonical_cycle_id(utc(scan))
        if self.handoff.read("terminal", key) is not None:
            return
        self.handoff.disposition("terminal", key, status="skipped", reason=reason)
        self._log(f"[Scheduler] Skipped {key}: {reason}")


def require_ingest_producer(registry, dependencies, *, log):
    """Fail visibly on a producer disagreement instead of weakening readiness."""
    from util.runtime.mrms_registry import MrmsProducerUnavailable, require_ingest_agreement

    if registry is None:
        log("[Scheduler] Independent ingest requires the version 2 MRMS registry; "
            "Core cannot read durable readiness records.")
        return False
    try:
        require_ingest_agreement(registry, dependencies.fingerprint)
    except MrmsProducerUnavailable as exc:
        log(f"[Scheduler] {exc}")
        return False
    return True



def report_effective_config(config_dir=None):
    """Where this process's configuration actually came from.

    Names only the catalogs already read, not all of ``CONFIG_NAMES``:
    ``get_provenance`` loads on a miss, so naming every catalog would parse and
    schema-validate 19 files purely to describe them.

    Reports the winning layer per key rather than the value, so a key holding a
    credential cannot be disclosed by a diagnostic.
    """
    root = config_loader.config_root(config_dir)
    catalogs = ", ".join(
        f"{name}@{config_loader.get_provenance(name, config_dir=config_dir)['schema_version']}"
        for name in config_loader.loaded_config_names(config_dir=config_dir)
    )
    print(f"[Scheduler] Config root: {root}")
    print(f"[Scheduler] Catalogs loaded: {catalogs or 'none'}")
    active = overlay.overrides()
    if active:
        summary = ", ".join(f"{key} <- {layer}" for key, layer in sorted(active.items()))
    else:
        summary = "none; every resolved value came from YAML"
    print(f"[Scheduler] Active overrides: {summary}")
    all_origins = overlay.origins()
    provenance = ", ".join(f"{key} <- {layer}" for key, layer in sorted(all_origins.items())) or "none"
    print(f"[Scheduler] Resolved-key provenance: {provenance}")
    print("[Scheduler] Provenance limit: only values resolved with overlay.resolve(key=...) are key-level; direct catalog reads are covered by Catalogs loaded above.")

    # Lazy imports keep diagnostics from making optional render dependencies eager
    # at module import time.
    from common.ingest.mrms.config import get_mrms_modifiers
    from EWMRS.render.config import get_mrms_file_list, get_goes_file_list
    from EdgeWARN.process.integrate.config import get_datasets_config
    from EWMRS.rap.config import get_rap_uint16_layers
    print(
        "[Scheduler] Enabled products: "
        f"MRMS ingest={len(get_mrms_modifiers())}, "
        f"MRMS readiness={len(get_check_modifiers())}, "
        f"EWMRS MRMS={len(get_mrms_file_list())}, "
        f"GOES={len(get_goes_file_list())}, "
        f"integration datasets={len(get_datasets_config())}, "
        f"RAP layers={len(get_rap_uint16_layers())}"
    )
    print("[Scheduler] Configuration changes require a process restart to take effect.")


def log_effective_flags(args):
    mrms_core_only = args.mrms_core_only
    goes_enabled = not args.disable_goes and not mrms_core_only
    print(
        "[Scheduler] Configuration: "
        f"lat={tuple(args.lat_limits)}, lon={tuple(args.lon_limits)}, "
        f"refl_threshold={args.refl_threshold}, "
        f"min_seed_percentage={args.min_seed_percentage}, "
        f"drop_offset={args.drop_offset}, "
        f"goes_decoupled={'yes' if goes_enabled else 'no'}"
    )
    if args.disable_ctam:
        print("[Scheduler] CTAM execution disabled via --disable-ctam")
    elif args.disable_ctam_modules:
        print("[Scheduler] External CTAM modules disabled; built-in StormProb remains enabled")
    if args.disable_tracking:
        print("[Scheduler] Tracking disabled via --disable-tracking")
    if args.disable_polygon_expansion:
        print("[Scheduler] Polygon expansion disabled via --disable-polygon-expansion; using original ProbSevere polygons")
    if args.disable_ewmrs:
        print("[Scheduler] EWMRS pipeline disabled via --disable-ewmrs")
    if args.disable_nws:
        print("[Scheduler] NWS background ingest disabled via --disable-nws")
    if args.disable_metar:
        print("[Scheduler] METAR background ingest disabled via --disable-metar")
    if args.disable_goes:
        print("[Scheduler] GOES/GLM ingest and GOES rendering disabled via --disable-goes")
    if args.disable_nexrad:
        print("[Scheduler] NEXRAD runs in its own service (run_nexrad.py); this runner never starts it")
    if mrms_core_only:
        print("[Scheduler] MRMS-core-only mode: running MRMS detection, MRMS integration, and CTAM only")


def _resolve_retry_policy(cycle_settings):
    retry_settings = cycle_settings["retry"]
    return CycleRetryPolicy(
        max_attempts=max(1, int(overlay.resolve(
            None,
            env_names=["EDGEWARN_CYCLE_MAX_ATTEMPTS"],
            yaml_value=retry_settings["max_attempts"],
            key="cycle.retry.max_attempts",
        ))),
        initial_backoff_seconds=max(0.0, float(overlay.resolve(
            None,
            env_names=["EDGEWARN_CYCLE_RETRY_BACKOFF_SECONDS"],
            yaml_value=retry_settings["initial_backoff_seconds"],
            key="cycle.retry.initial_backoff_seconds",
        ))),
        max_backoff_seconds=max(0.0, float(overlay.resolve(
            None,
            env_names=["EDGEWARN_CYCLE_MAX_BACKOFF_SECONDS"],
            yaml_value=retry_settings["max_backoff_seconds"],
            key="cycle.retry.max_backoff_seconds",
        ))),
    )


def run_primary_cycle_loop(
    *,
    cycle_config,
    supervisor=None,
    on_tick=None,
    stop_event=None,
):
    """Drive primary cycles from local durable readiness until interrupted.

    A scan is selected only when the ingest service has committed a complete,
    locally valid check set for it, and the oldest pending scan is processed
    first so tracking and lineage stay monotonic. Waiting for a scan to become
    ready costs no analysis retry: an attempt is recorded only once a start
    record actually exists.

    ``last_successful`` and the selection cursor advance only after a validated
    ``CycleOutcome``. A failed scan retries with bounded exponential backoff and
    is abandoned explicitly after ``max_attempts``; a start record that arrives
    behind the processing cursor is recorded as an explicit skip and never
    rewinds tracking. The supervisor is optional: the primary service owns no
    accessory children, so it is None for the standalone primary.
    """
    stormcell_last_successful, init_message = load_last_processed_from_stormcells(fs.STORMCELL_DIR)
    print(init_message)
    cycle_settings = section("cycle")
    cycle_state_store = CycleStateStore(
        resolve_file(cycle_settings["state_file"], "cycle.state_file")
    )
    persisted_cycle_state = cycle_state_store.load()
    if stormcell_last_successful is not None:
        persisted_cycle_state = cycle_state_store.seed_last_successful(
            stormcell_last_successful
        )

    last_successful = persisted_cycle_state.last_successful
    last_abandoned = persisted_cycle_state.last_abandoned
    selection_cursor = persisted_cycle_state.selection_cursor
    pending_timestamp = persisted_cycle_state.retry_timestamp
    pending_attempt_count = (
        persisted_cycle_state.attempt_count if pending_timestamp is not None else 0
    )
    retry_not_before = 0.0
    retry_policy = _resolve_retry_policy(cycle_settings)
    report_effective_config(cycle_config.config_dir)
    print(
        "[Scheduler] Cycle progress: "
        f"last_successful={last_successful}, "
        f"last_attempted={persisted_cycle_state.last_attempted}, "
        f"last_abandoned={last_abandoned}, "
        f"pending_retry={pending_timestamp}"
    )

    from common.ingest.mrms.config import get_registry

    registry = get_registry()
    reader = LocalReadinessReader(
        base_dir=cycle_config.base_dir,
        run_id=cycle_config.ingest_run_id or uuid.uuid4().hex,
        dependencies=cycle_config.dependencies,
    )
    max_backlog = int(cycle_settings["max_backlog_cycles"])

    supervisor_settings = section("supervisor")

    # Optional cross-service NEXRAD throttle (decomposition Phase 3, default
    # off): while a latency-sensitive cycle runs, the primary holds an
    # expiring lease so NEXRAD can cooperatively defer new work.
    nexrad_coordination = section("nexrad_coordination")
    primary_lease = None
    if nexrad_coordination["pause_ingest_during_primary_activity"]:
        from util.runtime.handoff import PrimaryActivityLease

        primary_lease = PrimaryActivityLease(
            fs.BASE_DIR,
            run_id=uuid.uuid4().hex,
            ttl_seconds=nexrad_coordination["primary_lease_ttl_seconds"],
        )

    # Hoisted out of the per-cycle path: a Manager spawns a child server
    # process and IPC machinery on construction; one instance serves every
    # cycle in this process.
    manager = multiprocessing.Manager()

    try:
        while stop_event is None or not stop_event.is_set():
            if not require_ingest_producer(registry, cycle_config.dependencies, log=print):
                _wait_ticks(supervisor, stop_event, on_tick, supervisor_settings)
                continue

            pending = reader.pending_scans()
            if pending_timestamp is None and pending:
                # Oldest first, and a newer scan never overtakes an older
                # candidate except through the explicit backlog cap.
                selection = pending[0]
                if selection_cursor is not None and selection <= selection_cursor:
                    reader.skip(
                        selection,
                        "start readiness arrived at or before the processing cursor; "
                        "processing continues forward only",
                    )
                    pending = reader.pending_scans()
                    selection = pending[0] if pending else None
                if selection is not None:
                    if len(pending) > max_backlog:
                        for expired in pending[:len(pending) - max_backlog]:
                            reader.skip(
                                expired,
                                f"backlog exceeded cycle.max_backlog_cycles={max_backlog}",
                            )
                    pending_timestamp = selection
                    pending_attempt_count = 0
                    print(
                        f"[Scheduler] Local check set complete for {pending_timestamp}; "
                        "scheduling detection"
                    )

            should_run_pipeline = (
                pending_timestamp is not None
                and time.monotonic() >= retry_not_before
            )

            if should_run_pipeline:
                dt = pending_timestamp
                pending_attempt_count += 1
                cycle_state_store.record_attempt(dt, pending_attempt_count)
                print(
                    f"[Scheduler] Starting primary cycle for {dt} "
                    f"(attempt {pending_attempt_count}/{retry_policy.max_attempts})"
                )

                outcome = None
                try:
                    if primary_lease is not None:
                        primary_lease.acquire(dt)
                    outcome = run_primary_cycle_once(
                        dt,
                        manager,
                        config=cycle_config,
                        stop_event=stop_event,
                    )
                except Exception as exc:
                    from EdgeWARN.ctam.preflight import StormProbDependencyError
                    if isinstance(exc, StormProbDependencyError):
                        cycle_state_store.record_outcome(
                            CycleOutcome(
                                dt,
                                {"stormprob": CycleStageResult(
                                    CycleStatus.FAILED, errors=(str(exc),))},
                                retryable=False,
                            ),
                            pending_attempt_count,
                        )
                    raise
                finally:
                    if primary_lease is not None:
                        primary_lease.release()
                if outcome.completed:
                    cycle_state_store.record_outcome(outcome, pending_attempt_count)
                    last_successful = dt
                    selection_cursor = max(
                        value
                        for value in (last_successful, last_abandoned)
                        if value is not None
                    )
                    pending_timestamp = None
                    pending_attempt_count = 0
                    retry_not_before = 0.0
                    print(
                        f"Primary cycle for {dt} finished with "
                        f"{len(outcome.produced_artifacts)} validated artifact(s)"
                    )
                else:
                    abandon = pending_attempt_count >= retry_policy.max_attempts
                    cycle_state_store.record_outcome(
                        outcome,
                        pending_attempt_count,
                        abandoned=abandon,
                    )
                    if abandon:
                        last_abandoned = dt
                        selection_cursor = max(
                            value
                            for value in (last_successful, last_abandoned)
                            if value is not None
                        )
                        pending_timestamp = None
                        pending_attempt_count = 0
                        retry_not_before = 0.0
                        print(
                            f"[Scheduler] Primary cycle for {dt} was explicitly "
                            f"abandoned after {retry_policy.max_attempts} attempts; "
                            f"errors={list(outcome.errors)}"
                        )
                    else:
                        delay = retry_policy.delay_after(pending_attempt_count)
                        retry_not_before = time.monotonic() + delay
                        print(
                            f"[Scheduler] Primary cycle for {dt} failed and remains "
                            f"pending; retrying in {delay:.1f}s; "
                            f"errors={list(outcome.errors)}"
                        )

            else:
                if pending_timestamp is not None:
                    remaining = max(0.0, retry_not_before - time.monotonic())
                    print(
                        f"[Scheduler] Retry for {pending_timestamp} is pending "
                        f"for another {remaining:.1f}s"
                    )
                else:
                    print(
                        "[Scheduler] No locally complete check set yet; waiting for the "
                        "ingest service to publish scan readiness."
                    )

            # Wait/Check loop — also monitor any supervised children
            _wait_ticks(supervisor, stop_event, on_tick, supervisor_settings)

    except KeyboardInterrupt:
        print("CTRL+C detected, exiting ...")
    finally:
        manager.shutdown()


def _wait_ticks(supervisor, stop_event, on_tick, supervisor_settings):
    for _ in range(supervisor_settings["check_ticks"]):
        if stop_event is not None and stop_event.is_set():
            break
        time.sleep(supervisor_settings["tick_seconds"])
        if on_tick is not None:
            on_tick()
        if supervisor is not None:
            supervisor.check()
