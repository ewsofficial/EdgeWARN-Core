import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from common.pipeline import coordinator
from common.ingest.mrms.downloader import DownloadBatchResult
from common.ingest.manifest import staged_input_from_path


def _run_cycle(logs, **kwargs):
    return asyncio.run(
        coordinator.run_staged_ingest_cycle(
            datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc),
            logs.append,
            include_goes=False,
            **kwargs,
        )
    )


def _record(tmp_path, product, timestamp):
    path = tmp_path / f"MRMS_{product}_{timestamp:%Y%m%d-%H%M%S}.grib2"
    path.write_bytes(b"data")
    return staged_input_from_path(
        product,
        path,
        source="test",
        family="mrms",
    )


def _complete_batch(tmp_path, product, timestamp):
    return DownloadBatchResult(
        attempted=(product,),
        downloaded=(_record(tmp_path, product, timestamp),),
        failed=(),
    )


def test_tandem_cycle_publishes_rap_record_without_converting_it(tmp_path):
    """Phase 4: the coordinator only stages raw RAP; the EWMRS service converts.

    The Uint16 conversion moved to ``run_ewmrs.py``'s consumer, which acts on
    committed rap-ready records. The coordinator's job ends at a validated
    manifest entry and a truthful rap_inputs_ready flag.
    """
    logs = []
    rap_file = tmp_path / "RAP.20260427-13z.awp130pgrbf00.grib2"
    rap_file.write_bytes(b"grib")

    timestamp = datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc)
    with patch.object(coordinator.mrms_ingest, "download_detection_files_async", new=AsyncMock(return_value=_complete_batch(tmp_path, "Detection", timestamp))), \
         patch.object(coordinator.mrms_ingest, "download_integration_files_async", new=AsyncMock(return_value=_complete_batch(tmp_path, "Integration", timestamp))), \
         patch.object(coordinator, "download_rap_async", new=AsyncMock(return_value=rap_file)), \
         patch("EWMRS.pipeline.run_rap_uint16_pipeline") as mock_convert:
        state = _run_cycle(logs)

    mock_convert.assert_not_called()
    assert state.rap_inputs_ready is True
    assert state.input_manifest.latest_for_product("RAP").local_path == rap_file
    assert "rap_ingest" not in state.errors


def test_tandem_cycle_rejects_partial_detection_batch(tmp_path):
    logs = []
    rap_file = tmp_path / "RAP.20260427-13z.awp130pgrbf00.grib2"
    rap_file.write_bytes(b"grib")
    partial = DownloadBatchResult(
        attempted=("Composite", "PrecipFlag"),
        downloaded=(
            _record(
                tmp_path,
                "Composite",
                datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc),
            ),
        ),
        failed=("PrecipFlag",),
    )
    complete = DownloadBatchResult(
        attempted=("EchoTop",),
        downloaded=(
            _record(
                tmp_path,
                "EchoTop",
                datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc),
            ),
        ),
        failed=(),
    )

    with patch.object(coordinator.mrms_ingest, "download_detection_files_async", new=AsyncMock(return_value=partial)), \
         patch.object(coordinator.mrms_ingest, "download_detection_files", return_value=partial), \
         patch.object(coordinator.mrms_ingest, "download_integration_files_async", new=AsyncMock(return_value=complete)), \
         patch.object(coordinator, "download_rap_async", new=AsyncMock(return_value=rap_file)), \
         patch("EWMRS.pipeline.run_rap_uint16_pipeline", return_value={}):
        state = _run_cycle(logs)

    assert state.detection_inputs_ready is False
    assert state.errors["detection_ingest"] == "Detection inputs unavailable"
    # EWMRS is triggered for every cycle and independently scans the local
    # source directory of each layer; detection completeness gates Core only.
    assert state.ewmrs_mrms_inputs_ready is True
