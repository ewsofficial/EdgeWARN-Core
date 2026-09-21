from datetime import datetime, timezone
import asyncio

from common.pipeline.coordinator import run_staged_ingest_cycle
import common.pipeline.coordinator as coordinator
from common.ingest.manifest import staged_input_from_path
from common.ingest.mrms.downloader import DownloadBatchResult
import common.ingest.synoptic.downloader as synoptic_downloader
import common.ingest.synoptic.main as synoptic_main


def _batch(tmp_path, timestamp, product):
    path = tmp_path / product / f"MRMS_{product}_{timestamp:%Y%m%d-%H%M%S}.grib2"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return DownloadBatchResult(
        attempted=(product,),
        downloaded=(
            staged_input_from_path(
                product,
                path,
                source="test",
                family="mrms",
            ),
        ),
        failed=(),
    )


def test_run_staged_ingest_cycle_preserves_staged_readiness(monkeypatch, tmp_path):
    call_order = []
    callbacks = []

    async def fake_detection(dt, max_entries=10, remove_old_files=True):
        await asyncio.sleep(0.01)
        call_order.append("detection")
        return _batch(tmp_path, dt, "Detection")

    async def fake_mrms_integration(dt, max_entries=10, remove_old_files=True):
        await asyncio.sleep(0.03)
        call_order.append("mrms_integration")
        return _batch(tmp_path, dt, "Integration")

    async def fake_goes(dt, max_entries=10, hour_lookback=3):
        await asyncio.sleep(0.04)
        call_order.append("goes")
        return _batch(tmp_path, dt, "GOES")

    async def fake_rap(dt):
        await asyncio.sleep(0.05)
        call_order.append("rap")
        rap_path = tmp_path / "RAP.20260317-20z.awp130pgrbf00.grib2"
        rap_path.write_bytes(b"grib")
        return rap_path

    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", fake_detection)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", fake_mrms_integration)
    monkeypatch.setattr(coordinator, "download_all_goes_files_async", fake_goes)
    monkeypatch.setattr(coordinator, "download_rap_async", fake_rap)

    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)

    state = asyncio.run(
        run_staged_ingest_cycle(
            dt,
            lambda msg: None,
            on_detection_ready=lambda current_state: callbacks.append((
                "detection",
                current_state.detection_inputs_ready,
                current_state.ewmrs_mrms_inputs_ready,
                current_state.ewmrs_goes_inputs_ready,
                current_state.edgewarn_integration_inputs_ready,
            )),
            on_ewmrs_mrms_ready=lambda current_state: callbacks.append((
                "ewmrs_mrms",
                current_state.detection_inputs_ready,
                current_state.ewmrs_mrms_inputs_ready,
                current_state.ewmrs_goes_inputs_ready,
                current_state.edgewarn_integration_inputs_ready,
            )),
            on_ewmrs_goes_ready=lambda current_state: callbacks.append((
                "ewmrs_goes",
                current_state.detection_inputs_ready,
                current_state.ewmrs_mrms_inputs_ready,
                current_state.ewmrs_goes_inputs_ready,
                current_state.edgewarn_integration_inputs_ready,
            )),
            on_edgewarn_integration_ready=lambda current_state: callbacks.append((
                "integration",
                current_state.detection_inputs_ready,
                current_state.ewmrs_mrms_inputs_ready,
                current_state.ewmrs_goes_inputs_ready,
                current_state.edgewarn_integration_inputs_ready,
            )),
        )
    )

    assert call_order == ["detection", "mrms_integration", "goes", "rap"]
    assert [name for name, *_ in callbacks] == ["detection", "ewmrs_mrms", "ewmrs_goes", "integration"]
    assert callbacks[0] == ("detection", True, False, False, False)
    assert callbacks[1] == ("ewmrs_mrms", True, True, False, False)
    assert callbacks[2] == ("ewmrs_goes", True, True, True, True)
    assert callbacks[3] == ("integration", True, True, True, True)
    assert state.detection_inputs_ready is True
    assert state.ewmrs_mrms_inputs_ready is True
    assert state.ewmrs_goes_inputs_ready is True
    assert state.edgewarn_integration_inputs_ready is True


def test_run_staged_ingest_cycle_can_skip_goes_readiness(monkeypatch, tmp_path):
    call_order = []

    async def fake_detection(dt, max_entries=10, remove_old_files=True):
        await asyncio.sleep(0.01)
        call_order.append("detection")
        return _batch(tmp_path, dt, "Detection")

    async def fake_mrms_integration(dt, max_entries=10, remove_old_files=True):
        await asyncio.sleep(0.02)
        call_order.append("mrms_integration")
        return _batch(tmp_path, dt, "Integration")

    async def fake_goes(dt, max_entries=10, hour_lookback=3):
        call_order.append("goes")

    async def fake_rap(dt):
        await asyncio.sleep(0.03)
        call_order.append("rap")
        rap_path = tmp_path / "RAP.20260317-20z.awp130pgrbf00.grib2"
        rap_path.write_bytes(b"grib")
        return rap_path

    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", fake_detection)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", fake_mrms_integration)
    monkeypatch.setattr(coordinator, "download_all_goes_files_async", fake_goes)
    monkeypatch.setattr(coordinator, "download_rap_async", fake_rap)

    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    state = asyncio.run(
        run_staged_ingest_cycle(
            dt,
            lambda msg: None,
            include_goes=False,
        )
    )

    assert call_order == ["detection", "mrms_integration", "rap"]
    assert state.detection_inputs_ready is True
    assert state.ewmrs_mrms_inputs_ready is True
    assert state.ewmrs_goes_inputs_ready is False
    assert state.edgewarn_integration_inputs_ready is True


def test_ewmrs_cycle_trigger_does_not_wait_for_complete_integration_batch(
    monkeypatch, tmp_path
):
    async def fake_detection(*_args, **_kwargs):
        return _batch(tmp_path, dt, "Detection")

    async def missing_integration(*_args, **_kwargs):
        return DownloadBatchResult(
            attempted=("NLDN",),
            downloaded=(),
            failed=("NLDN",),
        )

    def missing_integration_sync(*_args, **_kwargs):
        return DownloadBatchResult(
            attempted=("NLDN",),
            downloaded=(),
            failed=("NLDN",),
        )

    async def fake_rap(*_args, **_kwargs):
        rap_path = tmp_path / "RAP.20260317-20z.awp130pgrbf00.grib2"
        rap_path.write_bytes(b"grib")
        return rap_path

    monkeypatch.setattr(
        coordinator.mrms_ingest, "download_detection_files_async", fake_detection
    )
    monkeypatch.setattr(
        coordinator.mrms_ingest,
        "download_integration_files_async",
        missing_integration,
    )
    monkeypatch.setattr(
        coordinator.mrms_ingest,
        "download_integration_files",
        missing_integration_sync,
    )
    monkeypatch.setattr(coordinator, "download_rap_async", fake_rap)

    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    state = asyncio.run(
        run_staged_ingest_cycle(dt, lambda _message: None, include_goes=False)
    )

    assert state.detection_inputs_ready is True
    assert state.ewmrs_mrms_inputs_ready is True
    assert state.mrms_integration_inputs_ready is False
    assert state.edgewarn_integration_inputs_ready is False


def test_second_prior_rap_analysis_releases_integration(monkeypatch, tmp_path):
    """Regression for the 2026-07-26 RAP staging outage."""
    rap_dir = tmp_path / "data" / "RAP"
    rap_dir.mkdir(parents=True)
    attempted_hours = []

    async def fake_detection(*_args, **_kwargs):
        return _batch(tmp_path, dt, "Detection")

    async def fake_mrms_integration(*_args, **_kwargs):
        return _batch(tmp_path, dt, "Integration")

    async def fake_goes(*_args, **_kwargs):
        return _batch(tmp_path, dt, "GOES")

    async def fake_remote(current_dt, *_args):
        attempted_hours.append(current_dt.hour)
        if current_dt.hour in (13, 12):
            raise FileNotFoundError("not published")
        _, local_path = synoptic_downloader._build_synoptic_s3_params(
            current_dt,
            "rap.t{hour:02d}z.awp130pgrbf00.grib2",
            "rap.{date}",
            rap_dir,
        )
        local_path.write_bytes(b"grib")
        return local_path

    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", fake_detection)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", fake_mrms_integration)
    monkeypatch.setattr(coordinator, "download_all_goes_files_async", fake_goes)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_remote)
    monkeypatch.setattr(synoptic_main.fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(synoptic_main.fs, "RAP_DIR", rap_dir)

    dt = datetime(2026, 7, 26, 13, 6, tzinfo=timezone.utc)
    state = asyncio.run(
        run_staged_ingest_cycle(
            dt,
            lambda _message: None,
            include_ewmrs=False,
        )
    )

    assert attempted_hours == [13, 12, 11]
    assert state.rap_inputs_ready is True
    assert state.edgewarn_integration_inputs_ready is True
    assert "rap_ingest" not in state.errors
    assert (rap_dir / "RAP.20260726-11z.awp130pgrbf00.grib2").exists()
