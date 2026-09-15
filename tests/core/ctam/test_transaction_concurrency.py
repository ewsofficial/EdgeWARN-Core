"""Concurrent-writer safety for the CTAM transaction working set.

The loopback server is a ThreadingHTTPServer, so two modules writing at the
exact same moment execute their requests on separate threads against one
shared CTAMTransactionService. These tests pin the guarantee that both
writers' committed namespaces survive, which requires every mutation to be
serialized by the service lock.
"""
from __future__ import annotations

import sys
import threading
from contextlib import contextmanager

from EdgeWARN.ctam.manifest import ModuleManifest, ModuleWrite
from EdgeWARN.ctam.transaction import CTAMTransactionService


def _manifest(module_id: str) -> ModuleManifest:
    return ModuleManifest(
        module_id=module_id, name=module_id.title(), version="1.0.0", api_version="1",
        enabled=True, required=False, scope="stormcells", entrypoint=("{python}", "main.py"),
        timeout_seconds=10, after=(), requires=(),
        writes=(ModuleWrite("stormcells.current", f"/features/*/modules/{module_id.title()}"),),
        directory=None, manifest_path=None,
    )


@contextmanager
def _aggressive_preemption():
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def _service() -> CTAMTransactionService:
    manifests = {mid: _manifest(mid) for mid in ("moda", "modb")}
    return CTAMTransactionService(cells=[{"id": "7", "modules": {}}], manifests=manifests)


def test_simultaneous_commits_keep_both_module_namespaces():
    """Two commits racing on one cell must both land; neither may be dropped."""
    with _aggressive_preemption():
        for _ in range(25):
            service = _service()
            for mid in ("moda", "modb"):
                service.stage_cell(
                    mid, "7", revision=0,
                    operations=[{"op": "add", "path": f"/modules/{mid.title()}",
                                 "value": {"writer": mid, "payload": ["x" * 1000] * 200}}],
                )
            barrier = threading.Barrier(2)

            def commit(name: str) -> None:
                barrier.wait()
                service.commit(name)

            threads = [threading.Thread(target=commit, args=(name,)) for name in ("moda", "modb")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert set(service.cells["7"]["modules"]) == {"Moda", "Modb"}
            assert service.cell_revisions["7"] == 2


def test_mixed_concurrent_traffic_is_individually_atomic():
    """Concurrent staging, reads, alerts, and commits must not corrupt state."""
    with _aggressive_preemption():
        service = _service()
        failures: list[Exception] = []
        staged_all = threading.Barrier(2)
        ready_to_commit = threading.Barrier(3)

        def writer(name: str) -> None:
            try:
                # Concurrent staging while nothing has committed yet.
                for index in range(5):
                    service.stage_cell(
                        name, "7", revision=0,
                        operations=[{"op": "add", "path": f"/modules/{name.title()}",
                                     "value": {"attempt": index}}],
                    )
                staged_all.wait()
                service.stage_alert(name, {"id": f"{name}-1", "source": name.title(),
                                           "cell_id": "7", "geometry": {"type": "Point", "coordinates": [0, 0]}})
                service.transaction(name)
                ready_to_commit.wait()
                service.commit(name)
                # A repeated commit is idempotent even under contention.
                service.commit(name)
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                failures.append(exc)

        def reader() -> None:
            try:
                ready_to_commit.wait()
                service.committed_alerts()
                service.transaction("moda")
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                failures.append(exc)

        threads = [threading.Thread(target=writer, args=(name,)) for name in ("moda", "modb")]
        threads.append(threading.Thread(target=reader))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert failures == []
        assert set(service.cells["7"]["modules"]) == {"Moda", "Modb"}
        assert len(service.committed_alerts()) == 2
