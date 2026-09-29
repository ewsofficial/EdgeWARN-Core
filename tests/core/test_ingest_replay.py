"""Replay boundaries: immutable records, cleanup ownership, and phase failures."""
import asyncio
from datetime import datetime, timezone
import multiprocessing
import os

import pytest

from common.ingest.manifest import CycleInputManifest, staged_input_from_path
from common.ingest.mrms.core_contract import PROTECTED_IDS
from common.ingest.mrms.downloader import DownloadBatchResult
from common.ingest.replay import commit_ingest_report, input_lock, protect_runtime_inputs
from common.pipeline import coordinator
import util.file as fs

DT = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def batch(tmp_path, products=PROTECTED_IDS, *, role="current"):
    records = []
    for product in sorted(products):
        directory = tmp_path / product
        directory.mkdir(exist_ok=True)
        path = directory / f"MRMS_{product}_{DT:%Y%m%d-%H%M%S}.grib2"
        path.write_bytes(b"data")
        records.append(staged_input_from_path(product, path, source="test", family="mrms", role=role))
    return DownloadBatchResult(tuple(sorted(products)), tuple(records), ())


@pytest.mark.parametrize("missing", sorted(PROTECTED_IDS))
def test_each_protected_product_is_required(monkeypatch, tmp_path, missing):
    partial = batch(tmp_path, PROTECTED_IDS - {missing})
    async def detection(*args):
        return partial
    async def optional(*args):
        return DownloadBatchResult((), (), ())
    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", detection)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", optional)
    state = asyncio.run(coordinator.run_staged_ingest_cycle(DT, lambda _: None, include_goes=False, include_rap=False))
    assert not state.detection_inputs_ready
    assert not state.edgewarn_integration_inputs_ready
    assert state.optional_inputs_complete


def test_previous_cannot_satisfy_current_gate(monkeypatch, tmp_path):
    previous = batch(tmp_path, role="previous")
    async def detection(*args):
        return previous
    async def optional(*args):
        return DownloadBatchResult((), (), ())
    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", detection)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", optional)
    state = asyncio.run(coordinator.run_staged_ingest_cycle(DT, lambda _: None, include_goes=False, include_rap=False))
    assert not state.detection_inputs_ready
    assert state.input_manifest.latest_for_product(next(iter(PROTECTED_IDS))) is None


@pytest.mark.parametrize("cancel", [False, True])
def test_optional_deadline_and_cancellation_join_owned_tasks(monkeypatch, tmp_path, cancel):
    detection_batch = batch(tmp_path)
    real_load = coordinator.load_config
    def settings(name):
        result = real_load(name)
        if name == "ingest":
            return {**result, "mrms": {**result["mrms"], "downloads": {"optional_timeout_seconds": .03}}}
        return result
    monkeypatch.setattr(coordinator, "load_config", settings)
    async def run():
        entered = asyncio.Event()
        stopped = asyncio.Event()
        released = asyncio.Event()
        async def detection(*args):
            return detection_batch
        async def optional(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", detection)
        monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", optional)
        task = asyncio.create_task(coordinator.run_staged_ingest_cycle(
            DT, lambda _: None, include_goes=False, include_rap=False,
            on_edgewarn_integration_ready=lambda _: released.set()))
        await asyncio.wait_for(released.wait(), 1)
        await entered.wait()
        assert not stopped.is_set()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            state = await asyncio.wait_for(task, 1)
            assert state.optional_inputs_complete and state.edgewarn_integration_inputs_ready
        assert stopped.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())


def test_previous_selection_uses_identity_and_encoded_time(tmp_path):
    current = batch(tmp_path, {"MESH_00.50"}).downloaded[0]
    directory = current.local_path.parent
    old = directory / "MRMS_MESH_00.50_20260928-115600.grib2"
    latest = directory / "MRMS_MESH_00.50_20260928-115800.grib2"
    unrelated = directory / "MRMS_VIL_00.50_20260928-115959.grib2"
    for path in (old, latest, unrelated):
        path.write_bytes(b"data")
    os.utime(old, (2_000_000_000, 2_000_000_000))
    previous = coordinator._previous_detection_records((current,))
    assert previous[0].local_path == latest
    assert previous[0].role == "previous"


def test_ingest_report_is_idempotent_and_historical_isolated(tmp_path):
    manifest = CycleInputManifest(DT, batch(tmp_path).downloaded)
    report = {"schema_version": 1, "cycle_time": DT.isoformat(), "registry_fingerprint": "generation", "snapshots": {"ctam": manifest.as_dict()}}
    path = commit_ingest_report(tmp_path, report)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert commit_ingest_report(tmp_path, report) == path
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    local_reuse = {**report, "snapshots": {"ctam": {
        **manifest.as_dict(), "inputs": [{**r.as_dict(), "source": "local"} for r in manifest.inputs]}}}
    assert commit_ingest_report(tmp_path, local_reuse) == path
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    with pytest.raises(ValueError, match="Incompatible retry"):
        commit_ingest_report(tmp_path, {**report, "registry_fingerprint": "other"})
    history = commit_ingest_report(tmp_path, report, historical=True)
    assert history != path
    assert "historical" in history.parts
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def _attempt_cleanup(base, directory):
    fs.BASE_DIR = base
    fs.clean_old_files(directory, max_age_minutes=0, max_files=0)


def test_cycle_lease_protects_inputs_across_processes(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "BASE_DIR", tmp_path)
    directory = tmp_path / "data"
    directory.mkdir()
    path = directory / "pinned.grib2"
    path.write_bytes(b"data")
    with input_lock(tmp_path):
        process = multiprocessing.get_context("spawn").Process(target=_attempt_cleanup, args=(tmp_path, directory))
        process.start()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join()
            pytest.fail("Cleanup process did not terminate")
        assert process.exitcode == 0
        assert path.exists()
    fs.clean_old_files(directory, max_age_minutes=0, max_files=0)
    assert not path.exists()


def test_deferred_cleanup_runs_after_cycle_finishes(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "BASE_DIR", tmp_path)
    path = tmp_path / "pinned.grib2"
    path.touch()
    @protect_runtime_inputs
    def run():
        fs.clean_old_files(tmp_path, max_age_minutes=0, max_files=0)
        assert path.exists()
    run()
    assert not path.exists()


def test_previous_optional_remains_available_when_current_download_is_absent(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import json
    detection_batch = batch(tmp_path)
    directory = tmp_path / "MESH"
    directory.mkdir()
    previous = directory / "MRMS_MESH_00.50_20260928-115800.grib2"
    previous.write_bytes(b"data")
    registry = SimpleNamespace(
        fingerprint="test",
        normalized_config_json=json.dumps({"downloads": {"optional_timeout_seconds": 1},
                                           "ncep_https": {"sync_timeout_seconds": 1}}),
        products=(SimpleNamespace(protected=False, product_id="MESH_00.50", directory=directory),),
    )
    async def detection(*args):
        return detection_batch
    async def optional(*args):
        return DownloadBatchResult(("MESH_00.50",), (), ("MESH_00.50",))
    monkeypatch.setattr(coordinator.mrms_ingest, "get_registry", lambda: registry)
    monkeypatch.setattr(coordinator.mrms_ingest, "get_detection_modifiers", lambda: sorted(PROTECTED_IDS))
    monkeypatch.setattr(coordinator.mrms_ingest, "get_integration_modifiers", lambda: ["MESH_00.50"])
    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", detection)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", optional)
    state = asyncio.run(coordinator.run_staged_ingest_cycle(DT, lambda _: None, include_goes=False, include_rap=False))
    records = state.ctam_manifest.records_for_product("MESH_00.50")
    assert len(records) == 1 and records[0].role == "previous"
    assert records[0].local_path == previous
    assert state.ctam_manifest.latest_for_product("MESH_00.50") is None
