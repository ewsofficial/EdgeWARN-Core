"""Phase 4 wiring test: durable handoff published by the primary cycle.

Drives ``run_primary_cycle_once`` with a stubbed EdgeWARN worker and
monkeypatched downloaders, asserting that mrms-ready.json and rap-ready.json
are committed alongside the in-memory release events, that a failed phase
publishes nothing, and that republication of the same cycle is idempotent.
The cycle is primary-only now; EWMRS consumption is covered by
``tests/integration/handoff/test_ewmrs_consumer.py``.
"""

import multiprocessing
from datetime import datetime, timezone

import pytest

import common.pipeline.coordinator as coordinator
import util.runtime.cycle as cycle_module
from common.ingest.manifest import staged_input_from_path
from common.ingest.mrms.downloader import DownloadBatchResult
from util.runtime.cycle import PrimaryCycleConfig, run_primary_cycle_once
from util.runtime.handoff import (
    canonical_cycle_id,
    iter_committed_records,
    phase_record_path,
    read_phase_record,
    shadow_validate_phase_record,
)


DT = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)


def _batch(tmp_path, timestamp, product):
    if product == "Detection":
        from common.ingest.mrms.core_contract import PROTECTED_IDS
        return DownloadBatchResult(tuple(sorted(PROTECTED_IDS)), tuple(
            _batch(tmp_path, timestamp, p).downloaded[0] for p in sorted(PROTECTED_IDS)), ())
    path = tmp_path / "staged" / product / f"MRMS_{product}_{timestamp:%Y%m%d-%H%M%S}.grib2"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return DownloadBatchResult(
        attempted=(product,),
        downloaded=(staged_input_from_path(product, path, source="test", family="mrms"),),
        failed=(),
    )


def _stub_worker(log_queue, shared_state, *_args, **_kwargs):
    # Publish the same terminal contract a real EdgeWARN worker would.
    shared_state["edgewarn_stage"] = {
        "status": "completed",
        "produced_artifacts": [],
        "errors": [],
    }
    return None


@pytest.fixture()
def stubbed_workers(monkeypatch, tmp_path):
    import util.file as fs
    monkeypatch.setattr(fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cycle_module, "edgewarn_cycle_worker", _stub_worker)


def _config(tmp_path, *, handoff_enabled=True):
    return PrimaryCycleConfig(
        lat_limits=(20.0, 55.0),
        lon_limits=(230.0, 300.0),
        profile=False,
        disable_ctam=False,
        disable_ctam_modules=False,
        disable_tracking=False,
        disable_polygon_expansion=False,
        refl_threshold=20.0,
        min_seed_percentage=10.0,
        drop_offset=0.0,
        config_dir=None,
        goes_enabled=False,
        mrms_core_only=False,
        base_dir=str(tmp_path),
        handoff_enabled=handoff_enabled,
    )


def _patch_downloaders(monkeypatch, tmp_path):
    async def fake_detection(dt, max_entries=10, remove_old_files=True):
        return _batch(tmp_path, dt, "Detection")

    def sync_detection(*args, **kwargs):
        raise RuntimeError("sync fallback must not run when async succeeded")

    async def fake_mrms_integration(dt, max_entries=10, remove_old_files=True):
        return _batch(tmp_path, dt, "Integration")

    async def fake_rap(dt):
        rap_path = tmp_path / "rap" / f"RAP.{dt:%Y%m%d-%H}z.awp130pgrbf00.grib2"
        rap_path.parent.mkdir(parents=True, exist_ok=True)
        rap_path.write_bytes(b"grib")
        return rap_path

    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", fake_detection)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files", sync_detection)
    monkeypatch.setattr(
        coordinator.mrms_ingest, "download_integration_files_async", fake_mrms_integration
    )
    monkeypatch.setattr(coordinator, "download_rap_async", fake_rap)
    # Phase 4: the coordinator no longer runs the RAP Uint16 conversion at all.
    monkeypatch.setattr(
        "EWMRS.pipeline.run_rap_uint16_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("conversion must not run in the primary")),
    )


def _run_cycle(tmp_path, manager):
    return run_primary_cycle_once(DT, manager, config=_config(tmp_path))


def test_cycle_publishes_mrms_and_rap_ready_records(stubbed_workers, monkeypatch, tmp_path):
    _patch_downloaders(monkeypatch, tmp_path)
    with multiprocessing.Manager() as manager:
        outcome = _run_cycle(tmp_path, manager)

    mrms_record = read_phase_record(
        phase_record_path(tmp_path, canonical_cycle_id(DT), "mrms-ready")
    )
    rap_record = read_phase_record(
        phase_record_path(tmp_path, canonical_cycle_id(DT), "rap-ready")
    )
    assert mrms_record is not None and mrms_record.success
    assert rap_record is not None and rap_record.success
    products = {staged.product for staged in mrms_record.inputs}
    from common.ingest.mrms.core_contract import PROTECTED_IDS
    assert PROTECTED_IDS <= products
    assert "Integration" not in products
    # The committed exact paths are the ones actually staged this cycle.
    assert shadow_validate_phase_record(mrms_record) == ()
    assert shadow_validate_phase_record(rap_record) == ()
    # The EWMRS stage no longer exists on the primary outcome.
    assert set(outcome.stages) == {"ingest", "edgewarn"}
    assert outcome.completed


def test_failed_mrms_phase_still_publishes_ewmrs_cycle_trigger(stubbed_workers, monkeypatch, tmp_path):
    _patch_downloaders(monkeypatch, tmp_path)

    async def failing_detection(dt, max_entries=10, remove_old_files=True):
        raise RuntimeError("S3 unavailable")

    def failing_sync(*args, **kwargs):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(
        coordinator.mrms_ingest, "download_detection_files_async", failing_detection
    )
    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files", failing_sync)

    with multiprocessing.Manager() as manager:
        outcome = _run_cycle(tmp_path, manager)

    assert outcome.completed is False
    # mrms-ready is a cycle trigger, not an aggregate input-readiness claim.
    # EWMRS will independently scan whatever layer sources are available.
    assert any(
        record is not None and record.success
        for _, record in iter_committed_records(tmp_path, "mrms-ready")
    )
    rap_records = iter_committed_records(tmp_path, "rap-ready")
    assert any(record is not None and record.success for _, record in rap_records)


def test_missing_rap_source_publishes_no_rap_ready_record(stubbed_workers, monkeypatch, tmp_path):
    _patch_downloaders(monkeypatch, tmp_path)

    async def missing_rap(dt):
        return tmp_path / "rap" / "does-not-exist.grib2"

    monkeypatch.setattr(coordinator, "download_rap_async", missing_rap)

    with multiprocessing.Manager() as manager:
        _run_cycle(tmp_path, manager)

    # MRMS rendering may proceed, but the raw-RAP phase was not validated, so
    # no successful rap-ready record may exist.
    assert all(record is None for _, record in iter_committed_records(tmp_path, "rap-ready"))


def test_republication_of_same_cycle_is_idempotent(stubbed_workers, monkeypatch, tmp_path):
    _patch_downloaders(monkeypatch, tmp_path)
    with multiprocessing.Manager() as manager:
        _run_cycle(tmp_path, manager)
        _run_cycle(tmp_path, manager)

    records = iter_committed_records(tmp_path, "mrms-ready")
    assert [cycle_id for cycle_id, _ in records] == [canonical_cycle_id(DT)]
    record_dir = tmp_path / "state" / "realtime" / "cycles" / canonical_cycle_id(DT)
    assert sorted(p.name for p in record_dir.iterdir()) == [
        "mrms-ready.json",
        "rap-ready.json",
    ]


def test_disabled_handoff_publishes_nothing(stubbed_workers, monkeypatch, tmp_path):
    _patch_downloaders(monkeypatch, tmp_path)
    with multiprocessing.Manager() as manager:
        run_primary_cycle_once(
            DT,
            manager,
            config=_config(tmp_path, handoff_enabled=False),
        )

    assert iter_committed_records(tmp_path, "mrms-ready") == []
    assert iter_committed_records(tmp_path, "rap-ready") == []
    assert not (tmp_path / "state" / "realtime" / "cycles").exists()


def test_mrms_core_only_publishes_no_rap_ready_record(stubbed_workers, monkeypatch, tmp_path):
    """Regression: mrms-core-only must not publish a rap-ready record that
    pins no RAP input -- the consumer would stall its rap phase forever."""
    _patch_downloaders(monkeypatch, tmp_path)
    with multiprocessing.Manager() as manager:
        run_primary_cycle_once(
            DT,
            manager,
            config=_config_with(tmp_path, mrms_core_only=True),
        )

    # MRMS phases still commit their records...
    mrms_records = iter_committed_records(tmp_path, "mrms-ready")
    assert any(record is not None and record.success for _, record in mrms_records)
    # ...but no RAP input was staged, so no successful rap-ready may exist.
    assert all(record is None for _, record in iter_committed_records(tmp_path, "rap-ready"))


def _config_with(tmp_path, **overrides):
    base = _config(tmp_path).__dict__
    return PrimaryCycleConfig(**{**base, **overrides})


def test_failed_detection_can_recover_without_poisoned_final_report(
    stubbed_workers, monkeypatch, tmp_path,
):
    _patch_downloaders(monkeypatch, tmp_path)
    original = coordinator.mrms_ingest.download_detection_files_async
    async def unavailable(*args):
        return DownloadBatchResult(("ProbSevere",), (), ("ProbSevere",))
    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", unavailable)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files", lambda *args: unavailable_result)
    unavailable_result = DownloadBatchResult(("ProbSevere",), (), ("ProbSevere",))
    with multiprocessing.Manager() as manager:
        assert not _run_cycle(tmp_path, manager).completed
        report = tmp_path / "state/realtime/ingest-reports" / f"{canonical_cycle_id(DT)}.json"
        assert not report.exists()
        monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", original)
        assert _run_cycle(tmp_path, manager).completed
        assert report.exists()


def _phased_worker(log_queue, shared, detection_event, integration_event, *args):
    optional_event = args[-1]
    if not detection_event.wait(2) or not integration_event.wait(2):
        raise RuntimeError("Mandatory callbacks waited for optional completion")
    shared["base_work_started"] = True
    detection = shared["detection_manifest"]
    integration = shared["integration_manifest"]
    if not optional_event.wait(2):
        raise RuntimeError("Optional completion never released")
    assert detection == shared["detection_manifest"]
    assert integration == shared["integration_manifest"]
    assert not any(r["product"] == "Integration" for r in integration["inputs"])
    assert any(r["product"] == "Integration" for r in shared["ctam_manifest"]["inputs"])
    _stub_worker(log_queue, shared)


def test_worker_starts_base_work_before_optional_and_receives_frozen_final_snapshot(
    stubbed_workers, monkeypatch, tmp_path,
):
    import asyncio
    _patch_downloaders(monkeypatch, tmp_path)
    monkeypatch.setattr(cycle_module, "edgewarn_cycle_worker", _phased_worker)
    with multiprocessing.Manager() as manager:
        shared = manager.dict()
        class SharedManager:
            def dict(self):
                return shared
        async def optional(dt, *args):
            async with asyncio.timeout(2):
                while not shared.get("base_work_started"):
                    await asyncio.sleep(.005)
            return _batch(tmp_path, dt, "Integration")
        monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", optional)
        outcome = _run_cycle(tmp_path, SharedManager())
    assert outcome.completed
