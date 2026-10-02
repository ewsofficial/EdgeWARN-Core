from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
import uuid

from common.ingest.manifest import CycleInputManifest
from common.ingest.replay import protect_runtime_inputs  # noqa: F401  (historical pipeline)
from EdgeWARN.pipeline import edgewarn_cycle_worker
from EdgeWARN.process.detect.config import DetectionConfig
from util.runtime.handoff import canonical_cycle_id
from util.runtime.ingest_handoff import IngestRecordError

from .config import section
from .logging import drain_log_queue
from .processes import StartedProcessRegistry


class CycleStatus(str, Enum):
    """Authoritative terminal state for one required pipeline stage."""

    COMPLETED = "completed"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class CycleStageResult:
    """Terminal state, outputs, and process status for one cycle stage."""

    status: CycleStatus
    produced_artifacts: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    worker_exit_status: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "status", CycleStatus(self.status))
        object.__setattr__(
            self,
            "produced_artifacts",
            tuple(str(path) for path in self.produced_artifacts),
        )
        object.__setattr__(self, "errors", tuple(str(error) for error in self.errors))

    @property
    def successful(self) -> bool:
        return (
            self.status in {CycleStatus.COMPLETED, CycleStatus.DISABLED}
            and self.worker_exit_status in {None, 0}
            and not self.errors
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "produced_artifacts": list(self.produced_artifacts),
            "errors": list(self.errors),
            "worker_exit_status": self.worker_exit_status,
        }


@dataclass(frozen=True)
class CycleOutcome:
    """Validated terminal outcome for a full primary cycle."""

    timestamp: datetime
    stages: dict[str, CycleStageResult]
    retryable: bool
    input_manifest: CycleInputManifest | None = None

    @property
    def completed(self) -> bool:
        return bool(self.stages) and all(stage.successful for stage in self.stages.values())

    @property
    def produced_artifacts(self) -> tuple[str, ...]:
        return tuple(
            artifact
            for stage in self.stages.values()
            for artifact in stage.produced_artifacts
        )

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(error for stage in self.stages.values() for error in stage.errors)

    @property
    def worker_exit_status(self) -> dict[str, int | None]:
        return {
            stage_name: stage.worker_exit_status
            for stage_name, stage in self.stages.items()
        }

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
            "completed": self.completed,
            "retryable": self.retryable,
            "produced_artifacts": list(self.produced_artifacts),
            "errors": list(self.errors),
            "worker_exit_status": self.worker_exit_status,
            "input_manifest": (
                self.input_manifest.as_dict()
                if self.input_manifest is not None
                else None
            ),
            "stages": {
                stage_name: stage.as_dict()
                for stage_name, stage in self.stages.items()
            },
        }


@dataclass(frozen=True)
class CycleRetryPolicy:
    """Bounded exponential retry policy for a single scan."""

    max_attempts: int = field(
        default_factory=lambda: section("cycle")["retry"]["max_attempts"]
    )
    initial_backoff_seconds: float = field(
        default_factory=lambda: section("cycle")["retry"]["initial_backoff_seconds"]
    )
    max_backoff_seconds: float = field(
        default_factory=lambda: section("cycle")["retry"]["max_backoff_seconds"]
    )

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("retry backoff values must be non-negative")

    def delay_after(self, attempt: int) -> float:
        exponent = max(0, int(attempt) - 1)
        return min(
            self.max_backoff_seconds,
            self.initial_backoff_seconds * (2 ** exponent),
        )


@dataclass(frozen=True)
class PersistedCycleState:
    """Restart-visible distinction between attempted, successful, and abandoned scans."""

    last_attempted: datetime | None = None
    last_successful: datetime | None = None
    last_abandoned: datetime | None = None
    attempt_count: int = 0
    outcome: dict = field(default_factory=dict)

    @property
    def selection_cursor(self) -> datetime | None:
        values = [
            value
            for value in (self.last_successful, self.last_abandoned)
            if value is not None
        ]
        return max(values) if values else None

    @property
    def retry_timestamp(self) -> datetime | None:
        if self.last_attempted is None:
            return None
        if self.last_attempted == self.last_successful:
            return None
        if self.last_attempted == self.last_abandoned:
            return None
        if not bool(self.outcome.get("retryable")):
            return None
        return self.last_attempted


class CycleStateStore:
    """Persist cycle progress without conflating attempts with success."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @staticmethod
    def _parse_timestamp(value) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def load(self) -> PersistedCycleState:
        if not self.path.is_file():
            return PersistedCycleState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return PersistedCycleState(
                last_attempted=self._parse_timestamp(payload.get("last_attempted")),
                last_successful=self._parse_timestamp(payload.get("last_successful")),
                last_abandoned=self._parse_timestamp(payload.get("last_abandoned")),
                attempt_count=max(0, int(payload.get("attempt_count", 0))),
                outcome=dict(payload.get("outcome") or {}),
            )
        except Exception:
            return PersistedCycleState()

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def record_attempt(self, timestamp: datetime, attempt_count: int) -> PersistedCycleState:
        current = self.load()
        payload = {
            "last_attempted": timestamp.astimezone(timezone.utc).isoformat(),
            "last_successful": (
                current.last_successful.isoformat()
                if current.last_successful is not None
                else None
            ),
            "last_abandoned": (
                current.last_abandoned.isoformat()
                if current.last_abandoned is not None
                else None
            ),
            "attempt_count": int(attempt_count),
            "outcome": current.outcome,
        }
        self._write(payload)
        return self.load()

    def seed_last_successful(self, timestamp: datetime) -> PersistedCycleState:
        """Record an existing validated stormcell watermark during migration.

        The stormcell watermark reflects at most detection-stage progress and is
        only authoritative when no cycle state has been recorded yet.  When
        authoritative state exists (an attempted/successful/abandoned scan or a
        persisted outcome), it must never be overwritten by the watermark:
        doing so would collapse a pending retry into ``last_successful`` and
        skip the incomplete scan after a restart.
        """
        current = self.load()
        has_authoritative_state = any(
            value is not None
            for value in (
                current.last_attempted,
                current.last_successful,
                current.last_abandoned,
            )
        ) or bool(current.outcome)
        if has_authoritative_state:
            return current
        successful = timestamp
        payload = {
            "last_attempted": (
                current.last_attempted.isoformat()
                if current.last_attempted is not None
                else successful.astimezone(timezone.utc).isoformat()
            ),
            "last_successful": successful.astimezone(timezone.utc).isoformat(),
            "last_abandoned": (
                current.last_abandoned.isoformat()
                if current.last_abandoned is not None
                else None
            ),
            "attempt_count": current.attempt_count,
            "outcome": current.outcome,
        }
        self._write(payload)
        return self.load()

    def record_outcome(
        self,
        outcome: CycleOutcome,
        attempt_count: int,
        *,
        abandoned: bool = False,
    ) -> PersistedCycleState:
        current = self.load()
        payload = {
            "last_attempted": outcome.timestamp.astimezone(timezone.utc).isoformat(),
            "last_successful": (
                outcome.timestamp.astimezone(timezone.utc).isoformat()
                if outcome.completed
                else (
                    current.last_successful.isoformat()
                    if current.last_successful is not None
                    else None
                )
            ),
            "last_abandoned": (
                outcome.timestamp.astimezone(timezone.utc).isoformat()
                if abandoned
                else (
                    current.last_abandoned.isoformat()
                    if current.last_abandoned is not None
                    else None
                )
            ),
            "attempt_count": int(attempt_count),
            "outcome": outcome.as_dict(),
        }
        self._write(payload)
        return self.load()


@dataclass(frozen=True)
class PrimaryCycleConfig:
    lat_limits: tuple[float, float]
    lon_limits: tuple[float, float]
    profile: bool
    disable_ctam: bool
    disable_ctam_modules: bool
    disable_tracking: bool
    disable_polygon_expansion: bool
    refl_threshold: float
    min_seed_percentage: float
    drop_offset: float
    config_dir: str | None
    goes_enabled: bool
    mrms_core_only: bool
    # Durable ingest v1: Core is a consumer of the immutable readiness records
    # the ingest service commits beneath <base_dir>/state/realtime/ingest/v1/.
    base_dir: str | None = None
    handoff_enabled: bool = False
    disable_stormprob: bool = False
    ctam_discovery: object = None
    # The frozen producer/consumer agreement. ``dependencies.fingerprint`` is
    # what every record is validated against, so a configuration change on
    # either side fails visibly instead of weakening a gate.
    dependencies: object | None = None
    ingest_run_id: str | None = None
    # ``None`` resolves runtime.consumers.core_readiness_seconds, the one-second
    # local check interval that replaced the old 15-second supervisor wait.
    readiness_check_seconds: float | None = None


def readiness_handoff(config: PrimaryCycleConfig):
    """Bind this Core process to the durable ingest v1 namespace."""
    from util.runtime.ingest_handoff import IngestHandoff

    if config.base_dir is None or config.dependencies is None:
        raise ValueError(
            "Realtime Core requires the independent ingest agreement: no base directory "
            "or dependency fingerprint was configured"
        )
    return IngestHandoff(
        config.base_dir,
        fingerprint=config.dependencies.fingerprint,
        run_id=config.ingest_run_id or uuid.uuid4().hex,
    )


def release_pin_safely(handoff, owner, *, log=print):
    """Release a Core pin without letting a lock failure change the outcome.

    A failure here must never mask the cycle's own exception or turn a clean
    cycle into a crash. A pin left behind only delays retention of its inputs;
    :func:`sweep_core_pins` reclaims it on the next cycle or restart.
    """
    try:
        handoff.release_pin(owner)
        return True
    except Exception as exc:
        log(f"[Readiness] WARNING: could not release pin {owner}; it is left for the "
            f"next stale-pin sweep: {type(exc).__name__}: {exc}")
        return False


def sweep_core_pins(handoff, *, keep=None, log=print):
    """Release every Core scan pin except ``keep``'s, tolerating lock failure.

    Core runs one cycle at a time under its single-instance service lock, so a
    ``core:<scan>`` pin other than the active cycle's belongs to a finished or
    dead run. Pins are keyed by owner digest, so candidates are derived from
    the scans the ingest namespace knows about.
    """
    try:
        return handoff.release_stale_pins(
            prefix="core:", keep=None if keep is None else f"core:{keep}")
    except Exception as exc:
        log(f"[Readiness] WARNING: stale Core pin sweep deferred: "
            f"{type(exc).__name__}: {exc}")
        return ()


def read_ready_phase(handoff, kind, key, dependencies, base_dir, owner):
    """Return a committed phase, verifying its exact pinned bytes, or ``None``.

    Dependency preflight, exact-selection matching, and byte verification all
    run before a consumer can act on a record, and the reference is taken under
    the same lease that retention deletion needs.
    """
    from common.pipeline.readiness import validate_phase_dependencies

    record = handoff.read(kind, key)
    if record is None:
        return None
    validate_phase_dependencies(record, dependencies)
    handoff.pin_phase(owner, record)
    return record


class ReadinessWatcher(threading.Thread):
    """Install each validated immutable snapshot before releasing its barrier.

    Core never waits on a network producer. This watcher polls the local
    durable namespace, so a long integration wait costs no analysis retry and a
    stopped ingest service produces an explicit wait/degraded diagnostic rather
    than a hung cycle. A terminal scan disposition or an operator shutdown wakes
    every outstanding barrier with a truthful unavailable state.
    """

    def __init__(self, *, handoff, key, dependencies, base_dir, shared_state, owner,
                 interval, stop_event, release, emit_phase, is_worker_alive, io=None):
        super().__init__(name="core-readiness-watcher", daemon=True)
        self.handoff = handoff
        self.key = key
        self.dependencies = dependencies
        self.base_dir = base_dir
        self.shared_state = shared_state
        self.owner = owner
        self.interval = max(0.05, float(interval))
        self._stop = stop_event
        self._release = release
        self._emit_phase = emit_phase
        self._is_worker_alive = is_worker_alive
        self._io = io
        self.installed = {"core-integration-ready": False, "core-final-ready": False}
        self.reason = ""

    def run(self):
        while not self._stop.is_set():
            if all(self.installed.values()):
                return
            if not self._is_worker_alive():
                return
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            try:
                self._poll()
            except IngestRecordError as exc:
                self._warn(f"readiness snapshot for {self.key} is unusable: {exc}")
            except Exception as exc:
                self._warn(f"readiness watcher failed: {type(exc).__name__}: {exc}")
        self._finish_unavailable()

    def _poll(self):
        if self.handoff.read("terminal", self.key) is not None:
            self.reason = "scan has a terminal ingest disposition"
            self._finish_unavailable()
            return
        if not self.installed["core-integration-ready"]:
            record = read_ready_phase(self.handoff, "core-integration-ready", self.key,
                                      self.dependencies, self.base_dir, self.owner)
            if record is not None:
                self._install(record, "integration_manifest",
                             "edgewarn_integration_inputs_ready", "integration")
                self.installed["core-integration-ready"] = True
        if not self.installed["core-final-ready"]:
            record = read_ready_phase(self.handoff, "core-final-ready", self.key,
                                      self.dependencies, self.base_dir, self.owner)
            if record is not None:
                self._install(record, "ctam_manifest", "optional_inputs_complete", "optional")
                self.installed["core-final-ready"] = True

    def _install(self, record, manifest_key, ready_key, barrier):
        # The complete immutable snapshot is written before its barrier moves,
        # so a woken worker can never observe a released event without inputs.
        self.shared_state[manifest_key] = record.to_manifest().as_dict()
        self.shared_state[ready_key] = True
        self._emit_phase(f"{manifest_key}_installed", "ready")
        self._release(barrier, "ready")

    def _finish_unavailable(self):
        if not self.installed["core-integration-ready"]:
            self.shared_state["edgewarn_integration_inputs_ready"] = False
            self._release("integration", "unavailable")
        if not self.installed["core-final-ready"]:
            self.shared_state["optional_inputs_complete"] = False
            self._release("optional", "failed")

    def _warn(self, message):
        if self._io is not None:
            self._io.write_warning(f"[Readiness] {message}")
        else:
            print(f"[Readiness] WARN: {message}", flush=True)


def run_primary_cycle_once(
    dt,
    manager,
    *,
    config: PrimaryCycleConfig,
    stop_event=None,
):
    """Run one primary cycle from durable local readiness records.

    Core performs no source acquisition: the ingest service owns every realtime
    MRMS, raw RAP, and scan-time GLM download. This function validates the
    committed start record, spawns exactly one worker, and then waits locally
    for the integration and final snapshots the ingest service publishes.

    Unlike the retired acquisition path, this cycle does **not** hold the
    coarse input lease for its whole duration. Every selection and pin takes
    the same lease briefly and retention honours those pins, so a long
    integration wait blocks neither maintenance nor another producer while the
    exact bytes this cycle needs stay protected.

    Historical processing keeps its own explicit staged ingest entry point in
    ``EdgeWARN.pipeline.historical_pipeline``; only the realtime path here
    became a pure consumer.
    """
    from common.pipeline.readiness import validate_phase_dependencies

    cycle_settings = section("cycle")
    log_queue = multiprocessing.Queue()
    shared_state = manager.dict()

    detection_ready_event = multiprocessing.Event()
    integration_ready_event = multiprocessing.Event()
    optional_complete_event = multiprocessing.Event()
    shared_state.update({
        "detection_inputs_ready": False,
        "edgewarn_integration_inputs_ready": False,
        "edgewarn_generated_file": "",
        "input_manifest": {},
        "edgewarn_stage": {
            "status": "pending",
            "produced_artifacts": [],
            "errors": [],
        },
        "errors": {},
    })
    released_phases: set[str] = set()

    def emit_phase(phase: str, status: str):
        print(
            "[PhaseTelemetry] "
            f"utc={datetime.now(timezone.utc).isoformat()} "
            f"monotonic={time.perf_counter():.6f} "
            f"cycle={dt.isoformat()} phase={phase} status={status}",
            flush=True,
        )

    def release(event, phase: str, status: str):
        if phase not in released_phases:
            emit_phase(phase, status)
            released_phases.add(phase)
        event.set()

    if not config.handoff_enabled:
        return _unavailable_outcome(
            dt,
            "Core requires the durable realtime handoff; runtime.handoff.enabled is false",
        )

    handoff = readiness_handoff(config)
    key = canonical_cycle_id(dt)
    owner = f"core:{key}"
    sweep_core_pins(handoff, keep=key)

    # Freeze the producer generation for the spawned worker, exactly as before.
    from common.ingest.mrms.config import get_registry

    registry = get_registry()
    if registry is not None:
        shared_state["mrms_registry"] = {
            "base_dir": str(registry.base_dir),
            "config_dir": str(config.config_dir) if config.config_dir else None,
            "fingerprint": registry.fingerprint,
        }

    start = read_ready_phase(handoff, "core-start-ready", key, config.dependencies,
                             config.base_dir, owner)
    if start is None:
        release_pin_safely(handoff, owner)
        return _unavailable_outcome(
            dt,
            "No committed start readiness record for this scan; the local check set is "
            "not complete",
        )
    validate_phase_dependencies(start, config.dependencies)
    detection_manifest = start.to_manifest()
    if detection_manifest is None or detection_manifest.validate_alignment():
        release_pin_safely(handoff, owner)
        return _unavailable_outcome(
            dt, "The committed start readiness record failed alignment validation")

    # Resolved in the parent so the spawned worker inherits one frozen, already
    # validated object instead of re-reading and re-validating the YAML.
    detection_config = DetectionConfig.from_yaml(
        config_dir=config.config_dir,
        refl_threshold=config.refl_threshold,
        min_seed_percentage=config.min_seed_percentage,
        drop_offset=config.drop_offset,
    )

    edgewarn_proc = multiprocessing.Process(
        target=edgewarn_cycle_worker,
        args=(
            log_queue, shared_state, detection_ready_event, integration_ready_event,
            dt, config.lat_limits, config.lon_limits, detection_config,
            config.profile, config.disable_ctam, config.disable_ctam_modules, config.disable_tracking,
            config.disable_polygon_expansion, config.mrms_core_only, optional_complete_event,
            config.disable_stormprob, config.ctam_discovery,
        ),
    )
    started_processes = StartedProcessRegistry()
    watcher_stop = threading.Event()
    watcher: ReadinessWatcher | None = None

    try:
        started_processes.start(edgewarn_proc, "EdgeWARN")
        emit_phase("edgewarn_worker_started", "started")
        # Detection begins from the pinned manifest, never a directory's newest
        # file, and the barrier is released only after the snapshot is installed.
        shared_state["detection_manifest"] = detection_manifest.as_dict()
        shared_state["input_manifest"] = detection_manifest.as_dict()
        shared_state["detection_inputs_ready"] = True
        emit_phase("detection_mrms_validated", "validated")
        release(detection_ready_event, "detection_released", "ready")

        interval = (config.readiness_check_seconds
                    if config.readiness_check_seconds is not None
                    else section("consumers")["core_readiness_seconds"])
        watcher = ReadinessWatcher(
            handoff=handoff, key=key, dependencies=config.dependencies,
            base_dir=config.base_dir, shared_state=shared_state, owner=owner,
            interval=interval, stop_event=watcher_stop,
            release=lambda name, status: release(
                integration_ready_event if name == "integration" else optional_complete_event,
                "integration_released" if name == "integration" else "optional_complete",
                status),
            emit_phase=emit_phase, is_worker_alive=edgewarn_proc.is_alive)
        watcher.start()

        try:
            while edgewarn_proc.is_alive() or not log_queue.empty():
                drain_log_queue(log_queue)
                time.sleep(cycle_settings["log_drain_poll_seconds"])
        except KeyboardInterrupt:
            print("CTRL+C detected, stopping primary cycle workers...")
            watcher_stop.set()
            raise
    except (KeyboardInterrupt, SystemExit):
        watcher_stop.set()
        if watcher is not None:
            watcher.join(timeout=5)
        release_pin_safely(handoff, owner)
        started_processes.shutdown()
        raise
    finally:
        watcher_stop.set()
        started_processes.shutdown()
        drain_log_queue(log_queue)
    if watcher is not None:
        watcher.join(timeout=5)
    # Any barrier the watcher never installed is released with its truthful
    # terminal state so no worker can outlive this call.
    release(integration_ready_event, "integration_released",
            "ready" if bool(shared_state.get("edgewarn_integration_inputs_ready")) else "unavailable")
    release(optional_complete_event, "optional_complete",
            "complete" if bool(shared_state.get("optional_inputs_complete")) else "failed")
    release_pin_safely(handoff, owner)
    if watcher is not None and watcher.reason:
        shared_state["errors"] = dict(shared_state.get("errors", {})) | {
            "ingest_readiness": watcher.reason}

    edgewarn_stage = _stage_result_from_shared(
        shared_state.get("edgewarn_stage"),
        worker_exit_status=edgewarn_proc.exitcode,
        fallback_error="EdgeWARN worker exited without publishing a terminal stage result",
    )
    if shared_state.get("fatal_dependency"):
        from EdgeWARN.ctam.preflight import StormProbDependencyError

        raise StormProbDependencyError(shared_state["fatal_dependency"])
    if not config.disable_ctam and not config.disable_stormprob and not (
            shared_state.get("edgewarn_integration_inputs_ready")):
        from EdgeWARN.ctam.preflight import StormProbDependencyError

        raise StormProbDependencyError(
            "WARNING: Cannot continue Core: StormProb required inputs are unavailable: "
            "the committed integration readiness snapshot never arrived. Core is exiting "
            "nonzero."
        )

    errors = tuple(dict(shared_state.get("errors", {})).items())
    integration_ready = bool(shared_state.get("edgewarn_integration_inputs_ready"))
    ingest_stage = CycleStageResult(
        status=CycleStatus.COMPLETED if integration_ready else CycleStatus.UNAVAILABLE,
        errors=() if integration_ready else (
            tuple(f"{name}: {message}" for name, message in errors)
            or ("Core integration readiness snapshot unavailable",)),
    )
    stages = {"ingest": ingest_stage, "edgewarn": edgewarn_stage}
    retryable = any(
        stage.status in {CycleStatus.UNAVAILABLE, CycleStatus.FAILED}
        for stage in stages.values()
    )
    final_manifest = (CycleInputManifest.from_dict(shared_state.get("ctam_manifest"))
                      or CycleInputManifest.from_dict(shared_state.get("integration_manifest"))
                      or CycleInputManifest.from_dict(shared_state.get("detection_manifest")))
    return CycleOutcome(
        timestamp=dt,
        stages=stages,
        retryable=retryable,
        input_manifest=final_manifest,
    )


def _unavailable_outcome(dt, reason):
    return CycleOutcome(
        timestamp=dt,
        stages={"ingest": CycleStageResult(CycleStatus.UNAVAILABLE, errors=(reason,))},
        retryable=True,
    )


def _stage_result_from_shared(
    payload,
    *,
    worker_exit_status: int | None,
    fallback_error: str,
) -> CycleStageResult:
    """Convert a worker-published mapping into an authoritative stage result."""
    stage_payload = dict(payload or {})
    status_value = stage_payload.get("status")
    try:
        status = CycleStatus(status_value)
    except (TypeError, ValueError):
        status = CycleStatus.FAILED

    errors = tuple(stage_payload.get("errors") or ())
    if worker_exit_status not in {None, 0}:
        status = CycleStatus.FAILED
        errors = (*errors, f"Worker exited with status {worker_exit_status}")
    elif status not in {
        CycleStatus.COMPLETED,
        CycleStatus.DISABLED,
        CycleStatus.UNAVAILABLE,
        CycleStatus.FAILED,
    }:
        status = CycleStatus.FAILED

    if status is CycleStatus.FAILED and not errors:
        errors = (fallback_error,)

    return CycleStageResult(
        status=status,
        produced_artifacts=tuple(stage_payload.get("produced_artifacts") or ()),
        errors=errors,
        worker_exit_status=worker_exit_status,
    )
