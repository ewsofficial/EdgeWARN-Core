"""Tests for RAP ingest policy, wrappers, and cache retention."""

import asyncio
from datetime import datetime, timezone
import os
from unittest.mock import AsyncMock, call, patch

import pytest

import common.ingest.synoptic.config as synoptic_config
import common.ingest.synoptic.main as synoptic_main


DT = datetime(2026, 7, 26, 13, 6, tzinfo=timezone.utc)


def _rap_name(hour):
    return f"RAP.20260726-{hour:02d}z.awp130pgrbf00.grib2"


@pytest.mark.asyncio
async def test_async_download_uses_same_age_policy_for_cleanup(monkeypatch):
    clean = AsyncMock()
    monkeypatch.setattr(synoptic_main, "_async_clean_rap_cache", clean)
    monkeypatch.setattr(
        synoptic_main, "_download_rap", AsyncMock(return_value="rap.grib2")
    )
    monkeypatch.setattr(synoptic_main, "get_rap_max_age_minutes", lambda: 180)

    result = await synoptic_main.download_rap_async(DT)

    assert result == "rap.grib2"
    assert clean.await_args_list == [
        call(DT, max_age_minutes=180, max_files=None),
        call(DT, max_age_minutes=180, max_files=3),
    ]


@pytest.mark.asyncio
async def test_async_download_does_not_apply_file_cap_after_failure(monkeypatch):
    clean = AsyncMock()
    monkeypatch.setattr(synoptic_main, "_async_clean_rap_cache", clean)
    monkeypatch.setattr(synoptic_main, "_download_rap", AsyncMock(return_value=None))

    result = await synoptic_main.download_rap_async(DT)

    assert result is None
    assert len(clean.await_args_list) == 1
    assert clean.await_args.kwargs["max_files"] is None


def test_sync_download_uses_analysis_time_cleanup(monkeypatch):
    clean_calls = []

    def fake_run(coroutine):
        coroutine.close()
        return "rap.grib2"

    monkeypatch.setattr(
        synoptic_main,
        "clean_rap_cache",
        lambda *args, **kwargs: clean_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(synoptic_main, "get_rap_max_age_minutes", lambda: 180)

    with patch.object(synoptic_main.asyncio, "get_running_loop", side_effect=RuntimeError), \
         patch.object(synoptic_main.asyncio, "run", side_effect=fake_run):
        result = synoptic_main.download_rap(DT)

    assert result == "rap.grib2"
    assert clean_calls == [
        ((DT,), {"max_age_minutes": 180, "max_files": None}),
        ((DT,), {"max_age_minutes": 180, "max_files": 3}),
    ]


def test_download_inside_event_loop_returns_task(monkeypatch):
    async def exercise():
        task = synoptic_main.download_rap(DT)
        assert isinstance(task, asyncio.Task)
        return await task

    monkeypatch.setattr(
        synoptic_main, "download_rap_async", AsyncMock(return_value="rap.grib2")
    )

    assert asyncio.run(exercise()) == "rap.grib2"


def test_clean_rap_cache_uses_analysis_time_not_mtime(monkeypatch, tmp_path):
    rap_dir = tmp_path / "data" / "RAP"
    rap_dir.mkdir(parents=True)
    stale = rap_dir / _rap_name(9)
    valid = rap_dir / _rap_name(11)
    stale.write_bytes(b"stale")
    valid.write_bytes(b"valid")
    now_timestamp = DT.timestamp()
    old_mtime = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(stale, (now_timestamp, now_timestamp))
    os.utime(valid, (old_mtime, old_mtime))
    monkeypatch.setattr(synoptic_main.fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(synoptic_main.fs, "RAP_DIR", rap_dir)

    removed = synoptic_main.clean_rap_cache(
        DT, max_age_minutes=180, max_files=None
    )

    assert removed == 1
    assert not stale.exists()
    assert valid.exists()


def test_clean_rap_cache_keeps_newest_three_analysis_times(monkeypatch, tmp_path):
    rap_dir = tmp_path / "data" / "RAP"
    rap_dir.mkdir(parents=True)
    for hour in (10, 11, 12, 13):
        (rap_dir / _rap_name(hour)).write_bytes(str(hour).encode())
    monkeypatch.setattr(synoptic_main.fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(synoptic_main.fs, "RAP_DIR", rap_dir)

    removed = synoptic_main.clean_rap_cache(
        DT, max_age_minutes=240, max_files=3
    )

    assert removed == 1
    assert sorted(path.name for path in rap_dir.iterdir()) == [
        _rap_name(11),
        _rap_name(12),
        _rap_name(13),
    ]


def test_clean_rap_cache_ignores_idx_and_unrecognized_files(
    monkeypatch, mock_io_manager, tmp_path
):
    rap_dir = tmp_path / "data" / "RAP"
    rap_dir.mkdir(parents=True)
    idx = rap_dir / f"{_rap_name(13)}.idx"
    unrelated = rap_dir / "README.txt"
    idx.write_text("idx")
    unrelated.write_text("keep")
    monkeypatch.setattr(synoptic_main.fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(synoptic_main.fs, "RAP_DIR", rap_dir)
    monkeypatch.setattr(synoptic_main.fs, "io_manager", mock_io_manager)

    removed = synoptic_main.clean_rap_cache(
        DT, max_age_minutes=180, max_files=3
    )

    assert removed == 0
    assert idx.exists()
    assert unrelated.exists()
    assert "Ignoring unrecognized RAP cache file" in (
        mock_io_manager.write_warning.call_args.args[0]
    )


def test_rap_age_environment_override(monkeypatch):
    monkeypatch.setenv(synoptic_config.RAP_MAX_AGE_ENV, "240")
    assert synoptic_config.get_rap_max_age_minutes() == 240


@pytest.mark.parametrize("value", ["-1", "abc", "1.5"])
def test_invalid_rap_age_environment_override(monkeypatch, value):
    monkeypatch.setenv(synoptic_config.RAP_MAX_AGE_ENV, value)
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        synoptic_config.get_rap_max_age_minutes()


@pytest.mark.asyncio
async def test_committed_rap_reuse_has_one_identity_and_defers_cleanup(tmp_path, monkeypatch):
    import base64
    from pathlib import Path
    from datetime import timedelta
    raw = base64.b64decode((Path(__file__).parents[3] / 'fixtures/weather/rap.grib2.b64').read_text())
    monkeypatch.setattr(synoptic_main.fs, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(synoptic_main.fs, 'RAP_DIR', tmp_path / 'rap')
    path = tmp_path / 'rap' / _rap_name(13)
    path.parent.mkdir()
    async def download(dt, *, preserve_existing):
        assert preserve_existing
        if not path.exists(): path.write_bytes(raw)
        return path
    monkeypatch.setattr(synoptic_main, '_download_rap', download)
    monkeypatch.setattr(synoptic_main, '_async_clean_rap_cache', AsyncMock(side_effect=AssertionError('inventory owns cleanup')))
    first = await synoptic_main.acquire_rap_input(DT)
    second = await synoptic_main.acquire_rap_input(DT + timedelta(minutes=2))
    assert first.input_id == second.input_id
    assert first.record.analysis_time == DT.replace(minute=0)
    assert not first.reused and second.reused


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['corrupt', 'stale', 'future'])
async def test_committed_rap_rejects_invalid_input(tmp_path, monkeypatch, case):
    import base64
    from pathlib import Path
    raw = base64.b64decode((Path(__file__).parents[3] / 'fixtures/weather/rap.grib2.b64').read_text())
    monkeypatch.setattr(synoptic_main.fs, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(synoptic_main.fs, 'RAP_DIR', tmp_path)
    path = tmp_path / _rap_name(9 if case == 'stale' else 14 if case == 'future' else 13)
    path.write_bytes(b'partial' if case == 'corrupt' else raw)
    monkeypatch.setattr(synoptic_main, '_download_rap', AsyncMock(return_value=path))
    with pytest.raises(ValueError):
        await synoptic_main.acquire_rap_input(DT)


@pytest.mark.asyncio
async def test_realtime_rap_preserves_corrupt_local_input_for_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(synoptic_main.fs, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(synoptic_main.fs, 'RAP_DIR', tmp_path)
    path = tmp_path / _rap_name(13)
    path.write_bytes(b'partial')
    with pytest.raises(ValueError, match='inventory owns repair'):
        await synoptic_main.acquire_rap_input(DT)
    assert path.read_bytes() == b'partial'


@pytest.mark.asyncio
async def test_realtime_rap_rejects_escaped_directory_before_acquisition(tmp_path, monkeypatch):
    base = tmp_path / 'runtime'
    base.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    directory = base / 'rap'
    directory.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(synoptic_main.fs, 'BASE_DIR', base)
    monkeypatch.setattr(synoptic_main.fs, 'RAP_DIR', directory)
    download = AsyncMock(side_effect=AssertionError('acquisition must not start'))
    monkeypatch.setattr(synoptic_main, '_download_rap', download)
    with pytest.raises(ValueError, match='directory escapes'):
        await synoptic_main.acquire_rap_input(DT)
    download.assert_not_called()
