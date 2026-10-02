"""Durable cross-service handoff records (decomposition Phase 2).

Implements the phase-record and consumer-checkpoint contracts from
plans/realtime-runner-decomposition-plan.md:

- Immutable, timestamp-pinned phase records published atomically beneath
  ``<BASE_DIR>/state/realtime/cycles/<cycle-id>/<phase>.json``. The final
  filename is the only commit point: a successful record is never overwritten,
  not even by a later attempt of the same cycle.
- Consumer checkpoints beneath ``<BASE_DIR>/state/realtime/consumers/`` that
  advance only to explicitly recorded cycles and never move backward.

Phase 2 runs this publication in shadow mode alongside the existing in-memory
release callbacks; nothing consumes these records for correctness yet. The
shadow validation helpers at the bottom let tests and operators confirm that a
committed record still pins exact, existing source paths without publishing any
GUI output.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common.ingest.manifest import CycleInputManifest, StagedInput
from util.atomic import atomic_write_json

from .services import CANONICAL_SERVICE_NAMES, services_dir

PHASE_RECORD_SCHEMA_VERSION = 1
CONSUMER_CHECKPOINT_SCHEMA_VERSION = 1

#: Phases published during the shadow window. Each maps to one immutable
#: record filename inside a cycle directory.
PHASE_NAMES: tuple[str, ...] = ("mrms-ready", "rap-ready")

#: Canonical producer of phase records during Phase 2 (the primary service).
PRIMARY_SERVICE_NAME = "edgewarn"


class PhaseRecordError(ValueError):
    """A phase record does not match the supported schema."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_cycle_id(analysis_time: datetime) -> str:
    """Filesystem-safe, sortable UTC identity for one analysis timestamp."""
    return _coerce_utc(analysis_time).strftime("%Y%m%dT%H%M%SZ")


def parse_cycle_id(cycle_id: str) -> datetime:
    """Inverse of :func:`canonical_cycle_id`; raises ValueError when malformed."""
    parsed = datetime.strptime(str(cycle_id), "%Y%m%dT%H%M%SZ")
    return parsed.replace(tzinfo=timezone.utc)


def state_realtime_dir(base_dir: str | os.PathLike) -> Path:
    return Path(base_dir) / "state" / "realtime"


def cycles_dir(base_dir: str | os.PathLike) -> Path:
    return state_realtime_dir(base_dir) / "cycles"


def cycle_dir(base_dir: str | os.PathLike, cycle_id: str) -> Path:
    return cycles_dir(base_dir) / str(cycle_id)


def phase_record_path(base_dir: str | os.PathLike, cycle_id: str, phase: str) -> Path:
    if phase not in PHASE_NAMES:
        raise ValueError(
            f"{phase!r} is not a published phase name (expected one of "
            f"{', '.join(PHASE_NAMES)})"
        )
    return cycle_dir(base_dir, cycle_id) / f"{phase}.json"


def consumers_dir(base_dir: str | os.PathLike) -> Path:
    return state_realtime_dir(base_dir) / "consumers"


def consumer_checkpoint_path(base_dir: str | os.PathLike, consumer: str) -> Path:
    if not consumer or "/" in consumer or consumer.startswith("."):
        raise ValueError(f"invalid consumer name {consumer!r}")
    return consumers_dir(base_dir) / f"{consumer}.json"


def leases_dir(base_dir: str | os.PathLike) -> Path:
    return state_realtime_dir(base_dir) / "leases"


def primary_lease_path(base_dir: str | os.PathLike) -> Path:
    return leases_dir(base_dir) / "primary-active.json"


class InputLockTimeout(TimeoutError):
    """A bounded advisory-lock wait reached its deadline without the lock."""


def _lock_contention(exc: OSError) -> bool:
    """Whether a failed non-blocking lock attempt means "held elsewhere"."""
    import errno

    return isinstance(exc, BlockingIOError) or exc.errno in {
        errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK}


class _AdvisoryFileLock:
    """Cross-platform advisory lock used by runtime ownership.

    ``acquire()`` with no timeout makes exactly one non-blocking attempt, which
    is the fail-fast ownership semantics :class:`ServiceLock` and the primary
    lease rely on. A timeout turns it into a bounded mutex wait that polls with
    short backoff and raises :class:`InputLockTimeout` at the deadline.
    """

    _POLL_INITIAL_SECONDS = 0.005
    _POLL_MAX_SECONDS = 0.1

    def __init__(self, path: Path):
        self._path = path
        self._handle = None
        try:
            import fcntl
        except ImportError:  # pragma: no cover - exercised on Windows
            fcntl = None
        self._fcntl = fcntl

    def acquire(self, timeout: float | None = None) -> None:
        if timeout is None:
            self._try_acquire()
            return
        import time

        deadline = time.monotonic() + max(0.0, float(timeout))
        delay = self._POLL_INITIAL_SECONDS
        while True:
            try:
                self._try_acquire()
                return
            except OSError as exc:
                if not _lock_contention(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InputLockTimeout(
                        f"advisory lock {self._path} not acquired within {timeout:g}s"
                    ) from None
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, self._POLL_MAX_SECONDS)

    def _try_acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "a+")
        try:
            if self._fcntl is not None:
                self._fcntl.flock(self._handle.fileno(), self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
            else:  # pragma: no cover - Windows
                import msvcrt
                self._handle.seek(0)
                if self._handle.tell() == 0:
                    self._handle.write("0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self._handle.close()
            self._handle = None
            raise

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if self._fcntl is not None:
                self._fcntl.flock(self._handle.fileno(), self._fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


class ServiceLock:
    """Single-instance advisory lock beneath the service-name registry.

    Uses an OS file lock (``flock``), so a crashed owner releases it
    automatically and no stale-lock stealing logic is needed. The lock file
    lives beside the heartbeat files but is never read by the API; discovery
    uses heartbeats only.
    """

    def __init__(self, base_dir: str | os.PathLike, name: str):
        self._path = services_dir(base_dir) / f"{name}.lock"
        if name not in CANONICAL_SERVICE_NAMES:
            raise ValueError(
                f"{name!r} is not a canonical service name "
                f"(expected one of {', '.join(CANONICAL_SERVICE_NAMES)})"
            )
        self._lock = _AdvisoryFileLock(self._path)

    def acquire(self) -> None:
        try:
            self._lock.acquire()
        except OSError:
            raise RuntimeError(
                f"another instance of the '{self._path.stem}' service already "
                "holds its single-instance lock"
            ) from None

    def release(self) -> None:
        self._lock.release()


@dataclass(frozen=True)
class PrimaryLeaseState:
    """Parsed view of a primary-activity lease record."""

    run_id: str
    cycle_id: str
    pid: int
    updated_at: datetime
    expires_at: datetime


def _parse_lease_payload(payload, *, path_for_errors: str = "lease") -> PrimaryLeaseState:
    if not isinstance(payload, dict):
        raise PhaseRecordError(f"{path_for_errors} must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != 1:
        raise PhaseRecordError(
            f"{path_for_errors}: unsupported schema_version {schema_version!r}"
        )
    for required in ("run_id", "cycle_id", "pid", "updated_at", "expires_at"):
        if required not in payload:
            raise PhaseRecordError(f"{path_for_errors}: {required} is required")
    return PrimaryLeaseState(
        run_id=str(payload["run_id"]),
        cycle_id=str(payload["cycle_id"]),
        pid=int(payload["pid"]),
        updated_at=_parse_utc(payload["updated_at"], f"{path_for_errors}.updated_at"),
        expires_at=_parse_utc(payload["expires_at"], f"{path_for_errors}.expires_at"),
    )


class PrimaryActivityLease:
    """Cross-process lease held while a latency-sensitive primary cycle runs.

    NEXRAD checks :func:`primary_activity_held` before admitting new work so
    it can cooperatively throttle during an active cycle; the lease carries an
    expiry so a crashed primary can never pause NEXRAD forever. Feature-gated
    by ``nexrad_coordination.pause_ingest_during_primary_activity``
    (default off).
    """

    def __init__(self, base_dir: str | os.PathLike, *, run_id: str, ttl_seconds: float):
        self._base_dir = base_dir
        self._run_id = run_id
        self._ttl_seconds = float(ttl_seconds)

    def _owner_lock(self):
        """A small companion lock serializing lease replacement and release."""
        return _AdvisoryFileLock(primary_lease_path(self._base_dir).with_suffix(".lock"))

    def acquire(self, cycle_id: str) -> Path | None:
        """Create or refresh the lease for *cycle_id*; returns the commit path."""
        now = _utc_now()
        payload = {
            "schema_version": 1,
            "run_id": self._run_id,
            "cycle_id": str(cycle_id),
            "pid": os.getpid(),
            "heartbeat_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(
                now.timestamp() + self._ttl_seconds, tz=timezone.utc
            ).isoformat(),
        }
        destination = primary_lease_path(self._base_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._owner_lock():
            atomic_write_json(destination, payload)
        return destination

    def release(self) -> None:
        """Clear the lease, but never a successor's lease from a stale owner.

        A zombie primary waking up after a crash must not delete the live
        lease of a restarted service, so the file is only removed when it is
        absent, corrupt (unreadable state is ours to clear), or carries this
        lease's own run ID.
        """
        destination = primary_lease_path(self._base_dir)
        with self._owner_lock():
            try:
                raw = destination.read_text(encoding="utf-8")
                payload = json.loads(raw)
                state = _parse_lease_payload(payload)
            except (OSError, json.JSONDecodeError, PhaseRecordError, ValueError, TypeError):
                state = None
            if state is not None and state.run_id != self._run_id:
                print(
                    "[Handoff] Skipping primary-activity lease removal: "
                    f"it is held by run {state.run_id}, not {self._run_id}"
                )
                return
            try:
                destination.unlink(missing_ok=True)
            except OSError as exc:
                print(f"[Handoff] Failed to clear primary-activity lease: {exc}")


def primary_activity_held(
    base_dir: str | os.PathLike,
    *,
    now: datetime | None = None,
) -> PrimaryLeaseState | None:
    """The active unexpired lease, or ``None`` when the primary is idle."""
    reference = now or _utc_now()
    try:
        raw = primary_lease_path(base_dir).read_text(encoding="utf-8")
        payload = json.loads(raw)
        state = _parse_lease_payload(payload)
    except (OSError, json.JSONDecodeError, PhaseRecordError, ValueError, TypeError):
        return None
    if state.expires_at <= reference:
        return None
    return state


@dataclass(frozen=True)
class PhaseRecord:
    """One immutable committed-phase record with exact input paths."""

    cycle_id: str
    phase: str
    analysis_time: datetime
    published_at: datetime
    success: bool = True
    producer_service: str = PRIMARY_SERVICE_NAME
    run_id: str | None = None
    inputs: tuple[StagedInput, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    tolerances: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if self.phase not in PHASE_NAMES:
            raise PhaseRecordError(
                f"phase {self.phase!r} is not one of {', '.join(PHASE_NAMES)}"
            )
        try:
            if parse_cycle_id(self.cycle_id) != _coerce_utc(self.analysis_time):
                raise PhaseRecordError("cycle_id must equal the UTC analysis time")
        except PhaseRecordError:
            raise
        except ValueError:
            raise PhaseRecordError(
                f"cycle_id {self.cycle_id!r} is not a canonical cycle id"
            ) from None

    @classmethod
    def from_manifest(
        cls,
        manifest: CycleInputManifest,
        *,
        phase: str,
        run_id: str | None = None,
        published_at: datetime | None = None,
        warnings: tuple[str, ...] = (),
    ) -> "PhaseRecord":
        manifest_dict = manifest.as_dict()
        return cls(
            cycle_id=canonical_cycle_id(manifest.cycle_time),
            phase=phase,
            analysis_time=manifest.cycle_time,
            published_at=published_at or _utc_now(),
            run_id=run_id,
            inputs=tuple(manifest.inputs),
            warnings=tuple(warnings),
            tolerances=dict(manifest_dict.get("tolerances", {})),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PHASE_RECORD_SCHEMA_VERSION,
            "cycle_id": self.cycle_id,
            "phase": self.phase,
            "analysis_time": _coerce_utc(self.analysis_time).isoformat(),
            "published_at": _coerce_utc(self.published_at).isoformat(),
            "success": bool(self.success),
            "producer_service": self.producer_service,
            "run_id": self.run_id,
            "inputs": [record.as_dict() for record in self.inputs],
            "warnings": list(self.warnings),
            "tolerances": dict(self.tolerances),
        }

    @classmethod
    def from_dict(cls, payload) -> "PhaseRecord":
        if not isinstance(payload, dict):
            raise PhaseRecordError("phase record must be a JSON object")
        schema_version = payload.get("schema_version")
        if schema_version != PHASE_RECORD_SCHEMA_VERSION:
            raise PhaseRecordError(
                f"unsupported schema_version {schema_version!r}; "
                f"this build supports {PHASE_RECORD_SCHEMA_VERSION}"
            )
        for required in ("cycle_id", "phase", "analysis_time", "published_at"):
            if required not in payload:
                raise PhaseRecordError(f"{required} is required")
        raw_inputs = payload.get("inputs") or []
        if not isinstance(raw_inputs, list):
            raise PhaseRecordError("inputs must be a list")
        try:
            inputs = tuple(StagedInput.from_dict(item) for item in raw_inputs)
        except (KeyError, TypeError, ValueError) as exc:
            raise PhaseRecordError(f"malformed input record: {exc}") from None
        warnings = payload.get("warnings") or []
        if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
            raise PhaseRecordError("warnings must be a list of strings")
        tolerances = payload.get("tolerances") or {}
        if not isinstance(tolerances, dict):
            raise PhaseRecordError("tolerances must be an object")
        try:
            converted_tolerances = {str(k): float(v) for k, v in tolerances.items()}
        except (TypeError, ValueError):
            raise PhaseRecordError("tolerances must map names to numbers") from None
        return cls(
            cycle_id=str(payload["cycle_id"]),
            phase=str(payload["phase"]),
            analysis_time=_parse_utc(payload["analysis_time"], "analysis_time"),
            published_at=_parse_utc(payload["published_at"], "published_at"),
            success=bool(payload.get("success", True)),
            producer_service=str(payload.get("producer_service") or PRIMARY_SERVICE_NAME),
            run_id=payload.get("run_id"),
            inputs=inputs,
            warnings=tuple(warnings),
            tolerances=converted_tolerances,
        )

    def validate_exact_inputs(self, *, base_dir: str | os.PathLike | None = None) -> tuple[str, ...]:
        """Errors for any committed path that no longer exists on disk."""
        errors = []
        root = Path(base_dir).resolve() if base_dir is not None else None
        for staged in self.inputs:
            path = staged.local_path.resolve()
            if root is not None:
                try:
                    path.relative_to(root)
                except ValueError:
                    errors.append(f"{staged.product}: input path escapes runtime base directory: {staged.path}")
                    continue
            if not path.is_file():
                errors.append(f"{staged.product}: missing exact input {staged.path}")
        return tuple(errors)

    def to_manifest(self) -> CycleInputManifest:
        return CycleInputManifest(cycle_time=self.analysis_time, inputs=self.inputs)


def _parse_utc(value, dotted: str) -> datetime:
    if not isinstance(value, str):
        raise PhaseRecordError(f"{dotted} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PhaseRecordError(f"{dotted}: {value!r} is not ISO-8601") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PhaseRecordPublisher:
    """Publishes immutable phase records; duplicate-proof and failure-proof."""

    def __init__(self, base_dir: str | os.PathLike, *, run_id: str | None = None,
                 log: Callable[[str], None] | None = None):
        self._base_dir = base_dir
        self._run_id = run_id
        self._log = log or (lambda message: print(message))

    def publish(
        self,
        phase: str,
        manifest: CycleInputManifest,
        *,
        warnings: tuple[str, ...] = (),
    ) -> Path | None:
        """Atomically commit *phase* for the manifest's cycle.

        Returns the committed path, the already-committed path on an
        idempotent duplicate, or ``None`` when an incompatible record already
        occupies the commit point (which is never overwritten).
        """
        record = PhaseRecord.from_manifest(
            manifest, phase=phase, run_id=self._run_id, warnings=warnings,
        )
        destination = phase_record_path(self._base_dir, record.cycle_id, phase)
        payload = record.as_dict()
        existing = read_phase_record(destination)
        if existing is not None:
            # published_at is per-attempt metadata; content identity decides
            # whether a republication is an idempotent retry.
            def _semantic(payload_dict):
                return {k: v for k, v in payload_dict.items() if k != "published_at"}

            if _semantic(existing.as_dict()) == _semantic(payload):
                return destination
            self._log(
                f"[Handoff] Refusing to overwrite committed {phase} record for "
                f"{record.cycle_id}; keeping the earlier publication"
            )
            return None

        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(destination, payload)
        return destination


def read_phase_record_strict(path: str | os.PathLike) -> PhaseRecord | None:
    """Parse a phase record, distinguishing absence from I/O and corruption."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PhaseRecordError(f"cannot read phase record {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PhaseRecordError(f"malformed phase record {path}: {exc}") from exc
    try:
        return PhaseRecord.from_dict(payload)
    except PhaseRecordError as exc:
        raise PhaseRecordError(f"malformed phase record {path}: {exc}") from exc


def read_phase_record(path: str | os.PathLike) -> PhaseRecord | None:
    """Best-effort compatibility reader; use the strict reader for drains."""
    try:
        return read_phase_record_strict(path)
    except PhaseRecordError:
        return None


def iter_committed_records(
    base_dir: str | os.PathLike,
    phase: str,
) -> list[tuple[str, PhaseRecord | None]]:
    """All cycle records for *phase*, oldest first.

    Malformed records are yielded as ``(cycle_id, None)`` so consumers can
    surface them instead of silently skipping evidence.
    """
    root = cycles_dir(base_dir)
    results: list[tuple[str, PhaseRecord | None]] = []
    if not root.is_dir():
        return results
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            parse_cycle_id(entry.name)
        except ValueError:
            continue
        results.append((entry.name, read_phase_record(entry / f"{phase}.json")))
    results.sort(key=lambda item: item[0])
    return results


@dataclass(frozen=True)
class ConsumerCheckpoint:
    """One consumer's durable progress marker."""

    consumer: str
    last_processed_cycle_id: str
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONSUMER_CHECKPOINT_SCHEMA_VERSION,
            "consumer": self.consumer,
            "last_processed_cycle_id": self.last_processed_cycle_id,
            "updated_at": _coerce_utc(self.updated_at).isoformat(),
        }

    @classmethod
    def from_dict(cls, payload) -> "ConsumerCheckpoint":
        if not isinstance(payload, dict):
            raise PhaseRecordError("consumer checkpoint must be a JSON object")
        schema_version = payload.get("schema_version")
        if schema_version != CONSUMER_CHECKPOINT_SCHEMA_VERSION:
            raise PhaseRecordError(
                f"unsupported schema_version {schema_version!r}"
            )
        for required in ("consumer", "last_processed_cycle_id", "updated_at"):
            if required not in payload:
                raise PhaseRecordError(f"{required} is required")
        return cls(
            consumer=str(payload["consumer"]),
            last_processed_cycle_id=str(payload["last_processed_cycle_id"]),
            updated_at=_parse_utc(payload["updated_at"], "updated_at"),
        )


class ConsumerCheckpointStore:
    """Atomic checkpoint store; the cursor never moves backward."""

    def __init__(self, base_dir: str | os.PathLike, consumer: str):
        self._path = consumer_checkpoint_path(base_dir, consumer)

    def load(self) -> ConsumerCheckpoint | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PhaseRecordError(f"cannot read consumer checkpoint {self._path}: {exc}") from exc
        try:
            payload = json.loads(raw)
            return ConsumerCheckpoint.from_dict(payload)
        except (json.JSONDecodeError, PhaseRecordError) as exc:
            raise PhaseRecordError(f"corrupt consumer checkpoint {self._path}: {exc}") from exc

    def record(self, cycle_id: str) -> ConsumerCheckpoint:
        current = self.load()
        if current is not None and current.last_processed_cycle_id > str(cycle_id):
            raise PhaseRecordError(
                f"refusing to move checkpoint backward from "
                f"{current.last_processed_cycle_id} to {cycle_id}"
            )
        checkpoint = ConsumerCheckpoint(
            consumer=self._path.stem,
            last_processed_cycle_id=str(cycle_id),
            updated_at=_utc_now(),
        )
        payload = checkpoint.as_dict()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, payload)
        return checkpoint


def select_pending_records(
    base_dir: str | os.PathLike,
    phase: str,
    *,
    checkpoint: ConsumerCheckpoint | None = None,
    max_backlog: int = 10,
) -> list[tuple[str, PhaseRecord | None, str]]:
    """Records a consumer should process next, honoring the backlog cap.

    Returns ``(cycle_id, record, status)`` triples oldest first where status is
    ``"pending"``, ``"abandoned-backlog"``, or ``"already-processed"``.
    Cycles at or before the checkpoint are retained as an observable
    ``"already-processed"`` status. When more than *max_backlog* unprocessed records
    remain, the excess oldest records are marked abandoned so processing
    resumes at the oldest still-valid record instead of silently rendering
    newer files under older cycle timestamps.
    """
    committed = iter_committed_records(base_dir, phase)
    pending: list[tuple[str, PhaseRecord | None]] = []
    for cycle_id, record in committed:
        if checkpoint is not None and cycle_id <= checkpoint.last_processed_cycle_id:
            continue
        else:
            pending.append((cycle_id, record))

    abandoned_count = max(0, len(pending) - max(0, int(max_backlog)))
    selected: list[tuple[str, PhaseRecord | None, str]] = [
        (cycle_id, record, "already-processed")
        for cycle_id, record in committed
        if checkpoint is not None and cycle_id <= checkpoint.last_processed_cycle_id
    ]
    for index, (cycle_id, record) in enumerate(pending):
        if index < abandoned_count:
            selected.append((cycle_id, record, "abandoned-backlog"))
        else:
            selected.append((cycle_id, record, "pending"))
    return selected


def expected_layer_bindings(
    manifest: CycleInputManifest,
    layers,
) -> dict[str, str | None]:
    """Exact source path each render layer would pin under *manifest*.

    Mirrors the manifest-bound selection performed by the EWMRS render
    pipeline (``latest_for_directory`` pinning), so the shadow consumer can
    compare its selections against the files the live worker renders without
    producing any GUI output.
    """
    bindings: dict[str, str | None] = {}
    for layer in layers:
        name = layer.get("name")
        source_path = layer.get("filepath")
        if name is None or source_path is None:
            continue
        pinned = manifest.latest_for_directory(Path(source_path))
        bindings[name] = str(pinned.local_path) if pinned is not None else None
    return bindings


def shadow_validate_phase_record(
    record: PhaseRecord,
    *,
    layers=None,
    base_dir: str | os.PathLike | None = None,
) -> tuple[str, ...]:
    """Full shadow validation of one committed record; returns problems.

    Validates the record against the same rules the real consumer will apply:
    successful status, manifest alignment over the exact committed inputs, and
    — when *layers* is provided — a resolvable pinned path per layer. Pass
    ``EWMRS.render.config.get_mrms_file_list()`` lazily from the caller so
    importing runtime code does not load the render stack.
    """
    problems: list[str] = []
    if not record.success:
        problems.append(f"{record.phase}: record is marked unsuccessful")
    manifest = record.to_manifest()
    problems.extend(sorted(manifest.validate_alignment()))
    problems.extend(record.validate_exact_inputs(base_dir=base_dir))
    if layers is not None:
        for name, bound in expected_layer_bindings(manifest, layers).items():
            if bound is None:
                problems.append(f"{name}: no manifest record pins a source file")
    return tuple(problems)
