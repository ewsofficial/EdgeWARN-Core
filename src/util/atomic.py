"""Small filesystem commit primitives for runtime artifacts.

Final artifact names are observable by independent API processes.  Writers
must therefore create and validate a sibling temporary file before atomically
replacing the visible name.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _fsync_directory(directory: Path) -> None:
    """Persist a rename in *directory* when the platform supports it."""
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(directory, flags)
    except (AttributeError, OSError):
        # Windows cannot open directories this way.  os.replace still gives
        # atomic visibility there; FlushFileBuffers support is not exposed for
        # a directory handle by Python.
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def atomic_output_path(destination: str | Path, *, suffix: str = ".part") -> Iterator[Path]:
    """Yield a sibling temporary path and replace *destination* on success.

    The existing destination is deliberately left untouched if writing or
    validation fails.  The temporary path is removed on every failure path.
    Files use normal creation permissions (0666 filtered by the process umask)
    so an independent API user can read published artifacts.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}{suffix}"
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    os.close(fd)
    try:
        yield temporary
        # "r+b", not "rb": os.fsync on Windows issues FlushFileBuffers, which
        # requires a handle opened for writing and returns EBADF on a read-only
        # descriptor.  Read-only is the intuitive mode for a file you only want
        # to flush, and choosing it made every atomic_write_json call fail on
        # Windows -- see plans/modular-ctam-phase0-findings.md finding 1.  This
        # re-open is redundant for atomic_write_bytes, which fsyncs its own
        # writable handle below, but it is the only fsync a caller that drives
        # the context manager directly gets, so it stays.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        # File fsync makes the payload durable; fsyncing the parent makes the
        # name replacement durable across power loss on POSIX filesystems.
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_bytes(destination: str | Path, payload: bytes) -> Path:
    destination = Path(destination)
    with atomic_output_path(destination) as temporary:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return destination


def atomic_write_text(destination: str | Path, payload: str, *, encoding="utf-8") -> Path:
    return atomic_write_bytes(Path(destination), payload.encode(encoding))


def atomic_write_json(destination: str | Path, value, *, indent=None, default=None) -> Path:
    payload = json.dumps(value, indent=indent, default=default)
    return atomic_write_text(destination, payload)
