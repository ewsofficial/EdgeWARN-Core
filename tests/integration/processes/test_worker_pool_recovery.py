"""OS-level recovery coverage for the production NEXRAD process pool."""

import fcntl
import multiprocessing
import os
from concurrent.futures.process import BrokenProcessPool

import pytest

import common.ingest.nexrad.worker_pool as worker_pool


def _crash_with_locked_artifact(lock_path, pid_path, artifact_path):
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    with open(pid_path, "w", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
    with open(artifact_path, "w", encoding="utf-8") as stream:
        stream.write("incomplete")
        stream.flush()
        os.fsync(stream.fileno())
    os._exit(17)


@pytest.mark.process
def test_dead_nexrad_worker_generation_is_replaced_and_releases_resources(tmp_path):
    worker_pool.shutdown_nexrad_pool(wait=False)
    old_generation = worker_pool._GENERATION
    lock_path = tmp_path / "worker.lock"
    pid_path = tmp_path / "worker.pid"
    partial_path = tmp_path / ".render.part"

    pool = worker_pool.get_nexrad_pool(max_workers=1)
    assert pool._executor._mp_context.get_start_method() == multiprocessing.get_start_method()
    failed = pool._executor.submit(
        _crash_with_locked_artifact,
        str(lock_path),
        str(pid_path),
        str(partial_path),
    )
    with pytest.raises(BrokenProcessPool):
        failed.result(timeout=10)

    old_pid = int(pid_path.read_text(encoding="utf-8"))
    worker_pool.shutdown_nexrad_pool(wait=True)
    with pytest.raises(ProcessLookupError):
        os.kill(old_pid, 0)
    with lock_path.open("r+") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    # The failed generation may leave an uncommitted artifact, but it is no
    # longer held open and can be cleaned before the replacement publishes.
    partial_path.unlink()
    assert not partial_path.exists()
    replacement = worker_pool.get_nexrad_pool(max_workers=1)
    replacement_pid = replacement._executor.submit(os.getpid).result(timeout=10)
    assert replacement_pid != old_pid
    assert worker_pool._GENERATION == old_generation + 2
    worker_pool.shutdown_nexrad_pool(wait=True)
