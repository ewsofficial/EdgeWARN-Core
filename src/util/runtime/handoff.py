"""Shared advisory locks, cycle identities, and Core activity leases."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from util.atomic import atomic_write_json

from .services import CANONICAL_SERVICE_NAMES, services_dir


class PhaseRecordError(ValueError):
    """A durable lease record does not match the supported schema."""


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
