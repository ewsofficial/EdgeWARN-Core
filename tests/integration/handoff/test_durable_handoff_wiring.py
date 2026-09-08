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
def stubbed_workers(monkeypatch):
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
    assert {"Detection", "Integration"} <= products
    # The committed exact paths are the ones actually staged this cycle.
    assert shadow_validate_phase_record(mrms_record) == ()
    assert shadow_validate_phase_record(rap_record) == ()
    # The EWMRS stage no longer exists on the primary outcome.
    assert set(outcome.stages) == {"ingest", "edgewarn"}
    assert outcome.completed


def test_failed_mrms_phase_publishes_no_mrms_ready_record(stubbed_workers, monkeypatch, tmp_path):
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
    # The MRMS phase failed, so no successful mrms-ready record may exist,
    # but the independently validated RAP phase still commits its record.
    assert all(record is None for _, record in iter_committed_records(tmp_path, "mrms-ready"))
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
    assert not (tmp_path / "state" / "realtime").exists()


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
