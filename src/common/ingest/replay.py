"""Cycle-owned raw-input protection and immutable ingest reports.

The conservative lease protects all raw inputs while selection and processing
are active. Cleanup acquires the same OS lock, closing the select/delete race;
a crashed process releases the lease automatically.
"""
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import uuid
import threading

_deferred_cleanup = {}
_deferred_lock = threading.Lock()


def input_lock(base_dir):
    from util.runtime.handoff import _AdvisoryFileLock
    root = Path(base_dir).resolve()
    path = root / "state" / "input-pins.lock"
    if not path.resolve().is_relative_to(root):
        raise ValueError("Input lease escapes runtime root")
    return _AdvisoryFileLock(path)


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
    lock = input_lock(base_dir)
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
