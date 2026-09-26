from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import common.ingest.synoptic.downloader as synoptic_downloader


DT = datetime(2026, 7, 26, 13, 6, tzinfo=timezone.utc)
FILE_PATTERN = "rap.t{hour:02d}z.awp130pgrbf00.grib2"
DIR_PATTERN = "rap.{date}"
GRIB2 = b"GRIB\x00\x00\x00\x02" + (24).to_bytes(8, "big") + b"data7777"


async def _download(tmp_path, **kwargs):
    return await synoptic_downloader.download_synoptic(
        DT,
        "bucket-name",
        FILE_PATTERN,
        DIR_PATTERN,
        Path(tmp_path),
        dataset_name="RAP",
        max_age_minutes=kwargs.pop("max_age_minutes", 180),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_current_and_previous_missing_selects_second_previous(
    monkeypatch, mock_io_manager, tmp_path
):
    async_calls = []

    async def fake_async(current_dt, *_args):
        async_calls.append(current_dt)
        if current_dt.hour != 11:
            raise FileNotFoundError("missing")
        _, local_path = synoptic_downloader._build_synoptic_s3_params(
            current_dt, FILE_PATTERN, DIR_PATTERN, tmp_path
        )
        local_path.write_bytes(GRIB2)
        return local_path

    def fake_sync(*_args):
        raise AssertionError("definitive async 404 must not receive a sync retry")

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_sync", fake_sync)

    result = await _download(tmp_path)

    assert result.name == "RAP.20260726-11z.awp130pgrbf00.grib2"
    assert [candidate.hour for candidate in async_calls] == [13, 12, 11]
    selection_log = mock_io_manager.write_info.call_args_list[-1].args[0]
    assert "analysis=2026-07-26T11:00:00+00:00" in selection_log
    assert "age_minutes=126" in selection_log


@pytest.mark.asyncio
async def test_valid_local_fallback_avoids_network(
    monkeypatch, mock_io_manager, tmp_path
):
    local_path = tmp_path / "RAP.20260726-12z.awp130pgrbf00.grib2"
    local_path.write_bytes(GRIB2)
    async_calls = []

    async def fake_async(current_dt, *_args):
        async_calls.append(current_dt)
        raise FileNotFoundError("missing")

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)
    monkeypatch.setattr(
        synoptic_downloader,
        "download_synoptic_sync",
        lambda *_args: pytest.fail("sync should not run"),
    )

    result = await _download(tmp_path)

    assert result == local_path
    assert [candidate.hour for candidate in async_calls] == [13]
    assert "source=local" in mock_io_manager.write_info.call_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_invalid_local_file_proceeds_to_remote(
    monkeypatch, mock_io_manager, tmp_path
):
    local_path = tmp_path / "RAP.20260726-13z.awp130pgrbf00.grib2"
    local_path.write_bytes(b"")

    async def fake_async(current_dt, *_args):
        assert current_dt.hour == 13
        local_path.write_bytes(GRIB2)
        return local_path

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)

    result = await _download(tmp_path)

    assert result == local_path
    assert local_path.read_bytes() == GRIB2
    assert "Ignoring invalid local RAP file" in (
        mock_io_manager.write_warning.call_args_list[0].args[0]
    )


@pytest.mark.asyncio
async def test_truncated_cached_grib_is_removed_before_remote_retry(
    monkeypatch, mock_io_manager, tmp_path
):
    local_path = tmp_path / "RAP.20260726-13z.awp130pgrbf00.grib2"
    local_path.write_bytes(GRIB2[:-4])

    async def fake_async(current_dt, *_args):
        assert current_dt.hour == 13
        assert not local_path.exists()
        local_path.write_bytes(GRIB2)
        return local_path

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)

    assert await _download(tmp_path) == local_path
    assert local_path.read_bytes() == GRIB2


@pytest.mark.asyncio
async def test_exhausted_404_search_attempts_each_key_once(
    monkeypatch, mock_io_manager, tmp_path
):
    keys = []

    async def fake_async(current_dt, *_args):
        key, _ = synoptic_downloader._build_synoptic_s3_params(
            current_dt, FILE_PATTERN, DIR_PATTERN, tmp_path
        )
        keys.append(key)
        raise FileNotFoundError(key)

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)
    monkeypatch.setattr(
        synoptic_downloader,
        "download_synoptic_sync",
        lambda *_args: pytest.fail("sync should not run after 404"),
    )

    with pytest.raises(synoptic_downloader.SynopticUnavailableError) as exc_info:
        await _download(tmp_path)

    assert keys == [
        "rap.20260726/rap.t13z.awp130pgrbf00.grib2",
        "rap.20260726/rap.t12z.awp130pgrbf00.grib2",
        "rap.20260726/rap.t11z.awp130pgrbf00.grib2",
    ]
    assert all(attempt.failure == "not_found" for attempt in exc_info.value.attempts)
    assert "180-minute analysis-age limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_async_transport_failure_uses_sync_once(
    monkeypatch, mock_io_manager, tmp_path
):
    sync_calls = []

    async def fake_async(*_args):
        raise RuntimeError("connection reset")

    def fake_sync(current_dt, *_args):
        sync_calls.append(current_dt)
        _, local_path = synoptic_downloader._build_synoptic_s3_params(
            current_dt, FILE_PATTERN, DIR_PATTERN, tmp_path
        )
        local_path.write_bytes(GRIB2)
        return local_path

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_sync", fake_sync)

    result = await _download(tmp_path)

    assert result.name == "RAP.20260726-13z.awp130pgrbf00.grib2"
    assert len(sync_calls) == 1
    assert "source=s3_sync" in mock_io_manager.write_info.call_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_authentication_failure_is_distinguished(
    monkeypatch, mock_io_manager, tmp_path
):
    async def fake_async(*_args):
        raise RuntimeError("AccessDenied: invalid credential signature")

    def fake_sync(*_args):
        raise RuntimeError("Forbidden")

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_sync", fake_sync)

    with pytest.raises(synoptic_downloader.SynopticUnavailableError) as exc_info:
        await _download(tmp_path, max_age_minutes=6)

    assert len(exc_info.value.attempts) == 1
    assert exc_info.value.attempts[0].failure == "authentication"
    assert "=authentication" in str(exc_info.value)


def test_candidate_age_boundary_is_inclusive():
    dt = datetime(2026, 7, 26, 13, 6, tzinfo=timezone.utc)

    within_126 = list(synoptic_downloader._eligible_analysis_times(dt, 126))
    within_125 = list(synoptic_downloader._eligible_analysis_times(dt, 125))

    assert [candidate.hour for candidate in within_126] == [13, 12, 11]
    assert [candidate.hour for candidate in within_125] == [13, 12]


@pytest.mark.asyncio
async def test_fallback_builds_correct_month_rollover_key(
    monkeypatch, mock_io_manager, tmp_path
):
    dt = datetime(2026, 3, 1, 0, 5, tzinfo=timezone.utc)
    keys = []

    async def fake_async(current_dt, *_args):
        key, local_path = synoptic_downloader._build_synoptic_s3_params(
            current_dt, FILE_PATTERN, DIR_PATTERN, tmp_path
        )
        keys.append(key)
        if current_dt.hour == 23:
            local_path.write_bytes(GRIB2)
            return local_path
        raise FileNotFoundError(key)

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", fake_async)

    result = await synoptic_downloader.download_synoptic(
        dt,
        "bucket-name",
        FILE_PATTERN,
        DIR_PATTERN,
        tmp_path,
        dataset_name="RAP",
        max_age_minutes=180,
    )

    assert result.name == "RAP.20260228-23z.awp130pgrbf00.grib2"
    assert keys == [
        "rap.20260301/rap.t00z.awp130pgrbf00.grib2",
        "rap.20260228/rap.t23z.awp130pgrbf00.grib2",
    ]


def test_naive_and_non_utc_times_normalize_to_utc():
    naive = datetime(2026, 7, 26, 13, 6)
    offset = datetime.fromisoformat("2026-07-26T09:06:00-04:00")

    assert synoptic_downloader._as_utc(naive) == DT
    assert synoptic_downloader._as_utc(offset) == DT


@pytest.mark.asyncio
async def test_s3_404_uses_nomads_for_same_hour(monkeypatch, mock_io_manager, tmp_path):
    calls = []

    async def missing_s3(current_dt, *_args):
        calls.append(("s3", current_dt.hour))
        raise FileNotFoundError("S3 object missing")

    async def nomads(key, local_path, base_url):
        calls.append(("nomads", key))
        assert base_url == "https://nomads.example/rap/prod"
        local_path.write_bytes(GRIB2)
        return local_path

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", missing_s3)
    monkeypatch.setattr(
        synoptic_downloader,
        "download_synoptic_sync",
        lambda *_args: pytest.fail("S3 404 should skip synchronous S3"),
    )
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_https_async", nomads)

    result = await _download(tmp_path, https_base_url="https://nomads.example/rap/prod")

    assert result.name == "RAP.20260726-13z.awp130pgrbf00.grib2"
    assert calls == [
        ("s3", 13),
        ("nomads", "rap.20260726/rap.t13z.awp130pgrbf00.grib2"),
    ]
    assert "source=nomads_https" in mock_io_manager.write_info.call_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_nomads_failure_advances_hour_and_reports_both_sources(
    monkeypatch, mock_io_manager, tmp_path
):
    calls = []

    async def missing_s3(current_dt, *_args):
        calls.append(("s3", current_dt.hour))
        raise FileNotFoundError("missing")

    async def missing_nomads(key, *_args):
        calls.append(("nomads", key))
        raise FileNotFoundError(key)

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", missing_s3)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_https_async", missing_nomads)

    with pytest.raises(synoptic_downloader.SynopticUnavailableError) as exc_info:
        await _download(
            tmp_path,
            max_age_minutes=66,
            https_base_url="https://nomads.example/rap/prod",
        )

    assert calls == [
        ("s3", 13),
        ("nomads", "rap.20260726/rap.t13z.awp130pgrbf00.grib2"),
        ("s3", 12),
        ("nomads", "rap.20260726/rap.t12z.awp130pgrbf00.grib2"),
    ]
    assert all(a.failure == a.https_failure == "not_found" for a in exc_info.value.attempts)
    assert "https://nomads.example/rap/prod/rap.20260726" in str(exc_info.value)


@pytest.mark.asyncio
async def test_async_s3_transport_then_sync_failure_uses_nomads(
    monkeypatch, mock_io_manager, tmp_path
):
    calls = []

    async def failed_async(*_args):
        calls.append("s3_async")
        raise RuntimeError("connection reset")

    def failed_sync(*_args):
        calls.append("s3_sync")
        raise RuntimeError("connection reset")

    async def nomads(_key, local_path, _base_url):
        calls.append("nomads")
        local_path.write_bytes(GRIB2)
        return local_path

    monkeypatch.setattr(synoptic_downloader, "io_manager", mock_io_manager)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_async", failed_async)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_sync", failed_sync)
    monkeypatch.setattr(synoptic_downloader, "download_synoptic_https_async", nomads)

    result = await _download(tmp_path, https_base_url="https://nomads.example/rap/prod")

    assert result.exists()
    assert calls == ["s3_async", "s3_sync", "nomads"]


@pytest.mark.asyncio
async def test_rap_wrapper_enables_nomads(monkeypatch):
    download = AsyncMock(return_value="rap.grib2")
    monkeypatch.setattr(synoptic_downloader, "download_synoptic", download)

    assert await synoptic_downloader.download_rap(DT) == "rap.grib2"
    assert download.await_args.kwargs["https_base_url"] == (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rap/prod"
    )
