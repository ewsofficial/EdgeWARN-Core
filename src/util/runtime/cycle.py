from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import time

from common.ingest.manifest import CycleInputManifest
from common.ingest.replay import protect_runtime_inputs, commit_ingest_report, commit_input_snapshot
from common.pipeline.coordinator import run_staged_ingest_cycle
from EdgeWARN.pipeline import edgewarn_cycle_worker
from EdgeWARN.process.detect.config import DetectionConfig

from .config import section
from .goes import download_glm_for_scan
from .logging import drain_log_queue, queue_log
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
    # Phase 2 durable handoff: publish immutable mrms-ready/rap-ready records
    # beneath <base_dir>/state/realtime/cycles/ for the EWMRS service to
    # consume. Publication failures never fail a cycle.
    base_dir: str | None = None
    handoff_enabled: bool = False
    disable_stormprob: bool = False
    ctam_discovery: object = None


@protect_runtime_inputs
def run_primary_cycle_once(
    dt,
    manager,
    *,
    config: PrimaryCycleConfig,
):
    """Run one primary cycle: staged ingest, scan-time GLM, and the EdgeWARN worker.

    Decomposition Phase 4: this is now a primary-only cycle. The EWMRS worker,
    its readiness events, and the GOES render task queue are gone — EWMRS runs
    as its own service (``run_ewmrs.py``) and consumes the committed
    ``mrms-ready``/``rap-ready`` phase records published here.
    """
    cycle_settings = section("cycle")
    log_queue = multiprocessing.Queue()
    shared_state = manager.dict()

    detection_ready_event = multiprocessing.Event()
    integration_ready_event = multiprocessing.Event()
    optional_complete_event = multiprocessing.Event()
    # No worker waits on render-input readiness anymore (the EWMRS service
    # consumes durable records), but the transition keeps its own event so the
    # release/telemetry path stays uniform.
    render_inputs_ready_event = multiprocessing.Event()
    shared_state.update({
        "detection_inputs_ready": False,
        "render_mrms_inputs_ready": False,
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

    # Freeze the producer generation for a spawned Core worker. Phase 6 adds
    # complete dependency preflight before this process orchestration begins.
    from common.ingest.mrms.config import get_registry
    registry = get_registry()
    if registry is not None:
        shared_state["mrms_registry"] = {
            "base_dir": str(registry.base_dir),
            "config_dir": str(config.config_dir) if config.config_dir else None,
            "fingerprint": registry.fingerprint,
        }

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
    released_phases: set[str] = set()

    def emit_phase(phase: str, status: str):
        """Temporary direct phase telemetry; bypasses delayed queue draining."""
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

    # Durable handoff: publish immutable phase records alongside the in-memory
    # callbacks below. A failed publication or validation is logged and never
    # blocks or fails the cycle. Validation deliberately stays primary-owned
    # (alignment plus exact input existence) so no EWMRS import is needed here;
    # the consumer re-validates against the configured render layers.
    publisher = None
    if config.handoff_enabled and config.base_dir:
        from util.runtime.handoff import PhaseRecordPublisher

        publisher = PhaseRecordPublisher(config.base_dir)

    def durable_handoff(phase: str, state, ready: bool):
        if publisher is None or not ready or state.input_manifest is None:
            return
        try:
            committed = publisher.publish(phase, state.input_manifest)
            if committed is None:
                return
            from util.runtime.handoff import read_phase_record, shadow_validate_phase_record

            record = read_phase_record(committed)
            if record is None:
                print(
                    f"[Handoff] {phase} record for {committed.parent.name} "
                    "could not be re-read after commit"
                )
                return
            problems = shadow_validate_phase_record(record)
            if problems:
                print(
                    f"[Handoff] Validation problems for {phase} "
                    f"{record.cycle_id}: {list(problems)}"
                )
            else:
                print(f"[Handoff] Published validated {phase} record for {record.cycle_id}")
        except Exception as exc:
            print(f"[Handoff] Durable handoff publication failed for {phase}: {exc}")

    def commit_snapshot(manifest, phase):
        if config.base_dir:
            path = commit_input_snapshot(config.base_dir, manifest,
                                         registry.fingerprint if registry else None, phase)
            return CycleInputManifest.from_dict(json.loads(path.read_text())["snapshots"][phase])
        return manifest

    def publish(state, event, phase: str):
        """Write the complete snapshot before waking a worker."""
        if phase == "detection_released" and state.detection_inputs_ready and state.input_manifest is not None:
            state.input_manifest = commit_snapshot(state.input_manifest, "detection")
        shared_state["detection_inputs_ready"] = state.detection_inputs_ready
        shared_state["render_mrms_inputs_ready"] = state.ewmrs_mrms_inputs_ready
        if state.input_manifest is not None:
            shared_state["input_manifest"] = state.input_manifest.as_dict()
            if phase == "detection_released":
                shared_state["detection_manifest"] = state.input_manifest.as_dict()
        shared_state["errors"] = dict(state.errors)
        ready_key = {
            "detection_released": "detection_inputs_ready",
            "render_mrms_released": "render_mrms_inputs_ready",
        }[phase]
        if phase == "detection_released":
            emit_phase(
                "detection_mrms_validated",
                "validated" if shared_state[ready_key] else "unavailable",
            )
        release(event, phase, "ready" if shared_state[ready_key] else "unavailable")

    try:
        started_processes.start(edgewarn_proc, "EdgeWARN")
        emit_phase("edgewarn_worker_started", "started")

        async def ingest_and_glm():
            glm_task = None
            if config.goes_enabled:
                glm_task = asyncio.create_task(asyncio.to_thread(download_glm_for_scan, dt))
            base_ready = False
            base_terminal = False
            glm_ready = not config.goes_enabled
            glm_records = ()
            glm_terminal = not config.goes_enabled
            optional_state = None

            def publish_integration_if_ready():
                finalize_optional()
                if base_terminal and (not base_ready or glm_terminal and not glm_ready):
                    shared_state["edgewarn_integration_inputs_ready"] = False
                    release(integration_ready_event, "integration_released", "unavailable")
                    return
                if base_ready and glm_ready and not integration_ready_event.is_set():
                    if glm_records:
                        base_manifest = CycleInputManifest.from_dict(
                            shared_state.get("input_manifest")
                        ) or CycleInputManifest(cycle_time=dt)
                        shared_state["input_manifest"] = base_manifest.with_inputs(
                            glm_records
                        ).as_dict()
                    frozen = commit_snapshot(CycleInputManifest.from_dict(shared_state["input_manifest"]), "integration")
                    shared_state["integration_manifest"] = frozen.as_dict()
                    shared_state["edgewarn_integration_inputs_ready"] = True
                    release(integration_ready_event, "integration_released", "ready")

            def base_integration_ready(state):
                nonlocal base_ready, base_terminal
                base_terminal = True
                base_ready = state.edgewarn_integration_inputs_ready
                if state.input_manifest is not None:
                    shared_state["input_manifest"] = state.input_manifest.as_dict()
                shared_state["errors"] = dict(state.errors)
                # rap_inputs_ready is settled before the base-integration
                # release, so the raw-RAP record can be published here. In
                # mrms-core-only mode no RAP is staged, and publishing a
                # "successful" record without its input would poison the
                # consumer's rap phase forever.
                durable_handoff(
                    "rap-ready",
                    state,
                    state.rap_inputs_ready and not config.mrms_core_only,
                )
                publish_integration_if_ready()

            def finalize_optional():
                if optional_state is None or not glm_terminal or optional_complete_event.is_set():
                    return
                state = optional_state
                final = state.ctam_manifest.with_inputs(glm_records)
                report = dict(state.ingest_report)
                if report:
                    report["snapshots"] = dict(report["snapshots"])
                    report["snapshots"]["integration"] = state.integration_manifest.with_inputs(glm_records).as_dict()
                    report["snapshots"]["ctam"] = final.as_dict()
                    if config.base_dir and state.edgewarn_integration_inputs_ready and glm_ready:
                        path = commit_ingest_report(config.base_dir, report)
                        final = CycleInputManifest.from_dict(json.loads(path.read_text())["snapshots"]["ctam"])
                shared_state["ctam_manifest"] = final.as_dict()
                shared_state["optional_inputs_complete"] = True
                release(optional_complete_event, "optional_complete", "complete")

            def optional_complete(state):
                nonlocal optional_state
                optional_state = state
                finalize_optional()

            cycle_task = asyncio.create_task(run_staged_ingest_cycle(
                dt, lambda msg: queue_log(log_queue, msg),
                include_goes=False,
                include_rap=not config.mrms_core_only,
                on_detection_ready=lambda state: publish(state, detection_ready_event, "detection_released"),
                on_ewmrs_mrms_ready=lambda state: (
                    durable_handoff("mrms-ready", state, state.ewmrs_mrms_inputs_ready),
                    publish(state, render_inputs_ready_event, "render_mrms_released"),
                ),
                on_base_integration_ready=base_integration_ready,
                on_optional_complete=optional_complete,
            ))
            if glm_task is not None:
                try:
                    glm_results = tuple(await glm_task)
                    glm_records = glm_results
                    glm_manifest = CycleInputManifest(
                        cycle_time=dt,
                        inputs=glm_results,
                    )
                    glm_errors = glm_manifest.validate_alignment()
                    glm_ready = bool(glm_results) and not glm_errors
                    glm_path = glm_results[-1].path if glm_ready else None
                    queue_log(log_queue, (
                        f"INFO: Scan-time GLM ingest satisfied by {len(glm_results)} file(s)"
                        if glm_results else f"INFO: Scan-time GLM ingest found no files for {dt.isoformat()}"
                    ))
                    if glm_ready:
                        queue_log(log_queue, f"INFO: Local GLM readiness satisfied by {glm_path}")
                    else:
                        detail = "; ".join(glm_errors) if glm_errors else "no staged file"
                        queue_log(log_queue, f"INFO: No valid pinned GLM input for {dt.isoformat()}: {detail}")
                except Exception as exc:
                    queue_log(log_queue, f"WARN: Scan-time GLM ingest failed for {dt.isoformat()}: {exc}")
                    glm_ready = False
                glm_terminal = True
                publish_integration_if_ready()
            else:
                queue_log(log_queue, "INFO: GOES/GLM components disabled; EdgeWARN integration will not wait for GLM inputs")
            result = await cycle_task
            finalize_optional()
            return result, glm_ready

        cycle_state, glm_ready = asyncio.run(ingest_and_glm())
    except (KeyboardInterrupt, SystemExit):
        started_processes.shutdown()
        raise
    except Exception as exc:
        print(f"[Scheduler] Primary ingest cycle failed for {dt}: {exc}")
        cycle_state = None
        glm_ready = False

    edgewarn_integration_ready = bool(
        cycle_state
        and cycle_state.detection_inputs_ready
        and cycle_state.mrms_integration_inputs_ready
        and (cycle_state.rap_inputs_ready or config.mrms_core_only)
        and (glm_ready or not config.goes_enabled)
    )
    shared_state["edgewarn_integration_inputs_ready"] = edgewarn_integration_ready
    errors = dict(shared_state.get("errors", {}))
    if not edgewarn_integration_ready:
        errors.setdefault("edgewarn_integration_ingest", "EdgeWARN integration inputs unavailable")
    shared_state["errors"] = errors
    # Failure paths may have occurred before a coordinator callback.  Release
    # every waiter after the terminal false state has been written.
    release(detection_ready_event, "detection_released", "ready" if shared_state["detection_inputs_ready"] else "unavailable")
    release(render_inputs_ready_event, "render_mrms_released", "ready" if shared_state["render_mrms_inputs_ready"] else "unavailable")
    release(integration_ready_event, "integration_released", "ready" if edgewarn_integration_ready else "unavailable")
    release(optional_complete_event, "optional_complete", "complete" if shared_state.get("optional_inputs_complete") else "failed")

    try:
        while edgewarn_proc.is_alive() or not log_queue.empty():
            drain_log_queue(log_queue)
            time.sleep(cycle_settings["log_drain_poll_seconds"])
    except KeyboardInterrupt:
        print("CTRL+C detected, stopping primary cycle workers...")
        raise
    finally:
        started_processes.shutdown()
        drain_log_queue(log_queue)

    edgewarn_stage = _stage_result_from_shared(
        shared_state.get("edgewarn_stage"),
        worker_exit_status=edgewarn_proc.exitcode,
        fallback_error="EdgeWARN worker exited without publishing a terminal stage result",
    )
    if shared_state.get("fatal_dependency"):
        from EdgeWARN.ctam.preflight import StormProbDependencyError
        raise StormProbDependencyError(shared_state["fatal_dependency"])
    if not config.disable_ctam and not config.disable_stormprob and (
        cycle_state is None or not cycle_state.detection_inputs_ready
        or not cycle_state.rap_inputs_ready
    ):
        from EdgeWARN.ctam.preflight import StormProbDependencyError
        missing = []
        if cycle_state is None or not cycle_state.detection_inputs_ready:
            missing.append("protected MRMS detection inputs")
        if cycle_state is None or not cycle_state.rap_inputs_ready:
            missing.append("RAP environment and wind fields")
        raise StormProbDependencyError(
            "WARNING: Cannot continue Core: StormProb required inputs are unavailable: "
            + ", ".join(missing) + ". Core is exiting nonzero."
        )

    ingest_errors = tuple(
        f"{name}: {message}"
        for name, message in dict(shared_state.get("errors", {})).items()
        if name != "ewmrs_goes_ingest"
    )
    ingest_ready = bool(
        cycle_state
        and cycle_state.detection_inputs_ready
        and cycle_state.mrms_integration_inputs_ready
        and (cycle_state.rap_inputs_ready or config.mrms_core_only)
    )
    ingest_stage = CycleStageResult(
        status=CycleStatus.COMPLETED if ingest_ready else CycleStatus.UNAVAILABLE,
        errors=() if ingest_ready else (ingest_errors or ("Required ingest inputs unavailable",)),
    )

    stages = {
        "ingest": ingest_stage,
        "edgewarn": edgewarn_stage,
    }
    retryable = any(
        stage.status in {CycleStatus.UNAVAILABLE, CycleStatus.FAILED}
        for stage in stages.values()
    )
    return CycleOutcome(
        timestamp=dt,
        stages=stages,
        retryable=retryable,
        input_manifest=CycleInputManifest.from_dict(
            shared_state.get("ctam_manifest", shared_state.get("input_manifest"))
        ),
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
