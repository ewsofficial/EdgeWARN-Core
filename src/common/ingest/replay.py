"""Cycle-owned raw-input protection and immutable ingest reports.

The conservative lease protects all raw inputs while selection and processing
are active. Cleanup acquires the same OS lock, closing the select/delete race;
a crashed process releases the lease automatically.

The lease is a short cross-process mutex, not an ownership lock: ingest, Core
and EWMRS all take it briefly. :func:`input_lock` therefore waits a bounded
time (``runtime.handoff.input_lock_timeout_seconds``) instead of failing on the
first overlap, and is re-entrant per thread so nested critical sections cannot
deadlock against themselves. Cleanup stays a single non-blocking attempt so it
keeps deferring whenever any holder, including the calling thread, is active.
"""
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import time
import uuid
import threading

_deferred_cleanup = {}
_deferred_lock = threading.Lock()
_held = threading.local()


def input_lock_path(base_dir):
    root = Path(base_dir).resolve()
    path = root / "state" / "input-pins.lock"
    if not path.resolve().is_relative_to(root):
        raise ValueError("Input lease escapes runtime root")
    return path


def _lock_timeouts():
    """(acquire timeout, hold-warning threshold) in seconds, from runtime.yaml."""
    from util.runtime.config import section

    handoff = section("handoff")
    return (float(handoff["input_lock_timeout_seconds"]),
            float(handoff["input_lock_hold_warning_seconds"]))


class InputLease:
    """Thread re-entrant, bounded-wait hold on the shared input lock.

    Only the outermost acquisition in a thread touches the OS lock; nested
    acquisitions increase a depth counter. Separate threads and processes
    contend normally, because each outermost hold opens its own descriptor.
    """

    def __init__(self, path):
        self._path = str(path)

    @staticmethod
    def _table():
        table = getattr(_held, "leases", None)
        if table is None:
            table = _held.leases = {}
        return table

    def acquire(self):
        from util.runtime.handoff import _AdvisoryFileLock

        table = self._table()
        entry = table.get(self._path)
        if entry is not None:
            entry[0] += 1
            return
        timeout, _ = _lock_timeouts()
        lock = _AdvisoryFileLock(Path(self._path))
        lock.acquire(timeout=timeout)
        table[self._path] = [1, lock, time.monotonic()]

    def release(self):
        table = self._table()
        entry = table.get(self._path)
        if entry is None:
            return
        entry[0] -= 1
        if entry[0] > 0:
            return
        del table[self._path]
        held_for = time.monotonic() - entry[2]
        entry[1].release()
        _, warn_after = _lock_timeouts()
        if held_for > warn_after:
            print(f"[InputLock] WARNING: input lock held for {held_for:.2f}s "
                  f"(threshold {warn_after:g}s) by thread {threading.current_thread().name}",
                  flush=True)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


def input_lock(base_dir):
    return InputLease(input_lock_path(base_dir))


def protect_runtime_inputs(function):
    @wraps(function)
    def protected(*args, **kwargs):
        import util.file as fs
        try:
            with input_lock(fs.BASE_DIR):
                return function(*args, **kwargs)
        finally:
            with _deferred_lock:
                pending = tuple(_deferred_cleanup.values())
                _deferred_cleanup.clear()
            for cleanup, cleanup_args, cleanup_kwargs in pending:
                cleanup(*cleanup_args, **cleanup_kwargs)
    return protected


@contextmanager
def cleanup_permission(base_dir):
    # Deliberately one non-blocking attempt on a fresh descriptor, outside the
    # re-entrant table: a cleanup inside a held lease must defer, not run.
    from util.runtime.handoff import _AdvisoryFileLock
    lock = _AdvisoryFileLock(input_lock_path(base_dir))
    try:
        lock.acquire()
    except OSError:
        yield False
    else:
        try:
            yield True
        finally:
            lock.release()


def guard_cleanup(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        import util.file as fs
        with cleanup_permission(fs.BASE_DIR) as allowed:
            if allowed:
                return function(*args, **kwargs)
            with _deferred_lock:
                _deferred_cleanup[(function.__name__, str(args[0] if args else kwargs))] = (guarded, args, kwargs)
    return guarded


def commit_ingest_report(base_dir, report, *, historical=False, phase=None):
    """Commit once; retry may reuse identical input selections, never replace them."""
    from datetime import datetime
    from datetime import timezone
    if phase not in {None, "detection", "integration"}:
        raise ValueError("Unknown report phase")
    cycle = datetime.fromisoformat(report["cycle_time"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(base_dir).resolve()
    directory = root / "state" / ("historical" if historical else "realtime") / "ingest-reports"
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Ingest report directory escapes runtime root")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (cycle + ("-" + phase if phase else "") + ".json")
    temporary = directory / ("." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            existing = json.loads(destination.read_text(encoding="utf-8"))
            def selection(document):
                # Local reuse changes transport provenance, not observation
                # identity. Preserve the original committed provenance.
                return {name: {**manifest, "inputs": [
                    {key: value for key, value in record.items() if key != "source"}
                    for record in manifest["inputs"]]}
                    for name, manifest in document["snapshots"].items()}
            for key in ("schema_version", "registry_fingerprint"):
                if existing[key] != report[key]:
                    raise ValueError(f"Incompatible retry of committed ingest report: {destination}")
            if selection(existing) != selection(report):
                raise ValueError(f"Incompatible retry of committed ingest report: {destination}")
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def commit_input_snapshot(base_dir, manifest, fingerprint, phase, *, historical=False):
    if phase not in {"detection", "integration"}:
        raise ValueError("Unknown snapshot phase")
    return commit_ingest_report(base_dir, {
        "schema_version": 1,
        "cycle_time": manifest.cycle_time.isoformat(),
        "registry_fingerprint": fingerprint,
        "snapshots": {phase: manifest.as_dict()},
    }, historical=historical, phase=phase)
