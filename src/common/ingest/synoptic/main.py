from common.ingest.replay import guard_cleanup
import asyncio
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import re

from common.ingest.synoptic.downloader import download_rap as _download_rap
from common.ingest.synoptic.config import (
    get_rap_max_age_minutes,
    rap_date_format,
    rap_filename_regex,
    rap_max_files,
)
import util.file as fs


@lru_cache(maxsize=None)
def _rap_filename_re() -> re.Pattern[str]:
    """Compiled lazily, and memoized because cleanup parses every cached file.

    Compiling at import would read the catalog before a ``--config-dir`` could
    be resolved.
    """
    return re.compile(rap_filename_regex())


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_rap_analysis_time(path: Path) -> datetime | None:
    match = _rap_filename_re().match(path.name)
    if match is None:
        return None
    try:
        # "%H" pairs with the 2-digit `hour` capture group in `filename_regex`.
        return datetime.strptime(
            f"{match.group('date')}{match.group('hour')}", rap_date_format() + "%H"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@guard_cleanup
def clean_rap_cache(
    reference_time: datetime,
    *,
    max_age_minutes: int,
    max_files: int | None,
) -> int:
    """Prune RAP files by encoded analysis time, not filesystem mtime."""
    rap_dir = Path(fs.RAP_DIR)
    if not fs._is_safe_directory(rap_dir, allow_logical_inside=True):
        fs.io_manager.write_error(
            f"SAFETY ERROR: Attempting to clean {rap_dir} which is not inside {fs.BASE_DIR}"
        )
        return 0

    reference_time = _as_utc(reference_time)
    kept = []
    removed = 0
    if not rap_dir.exists():
        return removed

    for path in rap_dir.iterdir():
        if not path.is_file() or path.suffix.lower() == ".idx":
            continue
        analysis_time = parse_rap_analysis_time(path)
        if analysis_time is None:
            fs.io_manager.write_warning(
                f"Ignoring unrecognized RAP cache file during cleanup: {path.name}"
            )
            continue
        age_minutes = (reference_time - analysis_time).total_seconds() / 60
        if age_minutes < 0 or age_minutes > max_age_minutes:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                fs.io_manager.write_error(
                    f"Could not delete RAP cache file {path.name}: {exc}"
                )
        else:
            kept.append((analysis_time, path))

    if max_files is not None and len(kept) > max_files:
        kept.sort(key=lambda item: item[0], reverse=True)
        for _, path in kept[max_files:]:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                fs.io_manager.write_error(
                    f"Could not delete RAP cache file {path.name}: {exc}"
                )
    return removed


async def _async_clean_rap_cache(reference_time, *, max_age_minutes, max_files):
    # RAP retains only a handful of files. Keep this small directory scan on
    # the cycle thread so asyncio.run() does not retain a default-executor
    # worker during tandem-cycle teardown.
    return clean_rap_cache(
        reference_time,
        max_age_minutes=max_age_minutes,
        max_files=max_files,
    )


async def download_rap_async(dt: datetime, *, cleanup=True, preserve_existing=False):
    """
    Async version of download_rap.
    Cleans up RAP files before and after downloading so the RAP directory stays bounded.
    """
    max_age_minutes = get_rap_max_age_minutes()
    if cleanup:
        await _async_clean_rap_cache(
            dt, max_age_minutes=max_age_minutes, max_files=None,
        )
    result = await _download_rap(dt, **({"preserve_existing": True} if preserve_existing else {}))
    if result and cleanup:
        await _async_clean_rap_cache(
            dt,
            max_age_minutes=max_age_minutes,
            max_files=rap_max_files(),
        )
    return result


def download_rap(dt: datetime):
    """
    Public API to download a RAP file for a given datetime.
    Handles the async loop if necessary.
    Enforces RAP analysis-age and file-count retention.
    """
    try:
        # Check if there is a running event loop
        loop = asyncio.get_running_loop()
    except RuntimeError:
        max_age_minutes = get_rap_max_age_minutes()
        clean_rap_cache(
            dt,
            max_age_minutes=max_age_minutes,
            max_files=None,
        )
        # If no loop, run with asyncio.run
        result = asyncio.run(_download_rap(dt))
        if result:
            clean_rap_cache(
                dt,
                max_age_minutes=max_age_minutes,
                max_files=rap_max_files(),
            )
        return result
    else:
        # If loop exists, we can't use asyncio.run
        return loop.create_task(download_rap_async(dt))

if __name__ == "__main__":
    # Test with current time or specific timestamp
    import sys
    from util.io import IOManager
    
    io_manager = IOManager("[RAPTest]")
    test_dt = datetime.now()
    io_manager.write_info(f"Running RAP download test (Synoptic Refactor) for {test_dt}")
    
    result = download_rap(test_dt)
    if result:
        io_manager.write_info(f"Test successful: {result}")
    else:
        io_manager.write_error("Test failed")


async def acquire_rap_input(dt: datetime):
    """Return a validated analysis identity; defer all deletion to inventory.

    Reusing an analysis for another scan returns the same input_id. Producers
    must key notifications by this ID, not by the requesting scan timestamp.
    """
    from common.ingest.manifest import CycleInputManifest, staged_input_from_path
    from common.ingest.mrms.acquisition import validate_payload
    from common.ingest.objects import CommittedInput
    directory = Path(fs.RAP_DIR)
    if not directory.resolve().is_relative_to(Path(fs.BASE_DIR).resolve()):
        raise ValueError("RAP directory escapes runtime base directory")
    before = set(directory.glob("*.grib2"))
    result = await download_rap_async(dt, cleanup=False, preserve_existing=True)
    if not result:
        raise RuntimeError("RAP acquisition returned no usable file")
    path = Path(result)
    if path.is_symlink() or not path.resolve().is_relative_to(Path(fs.BASE_DIR).resolve()):
        raise ValueError("RAP input escapes runtime base directory")
    analysis_time = parse_rap_analysis_time(path)
    if analysis_time is None:
        raise ValueError("RAP filename does not encode an analysis time")
    digest = validate_payload(path, 'conus_grib2')
    record = staged_input_from_path('RAP', path, source='synoptic', family='rap',
                                    analysis_time=analysis_time)
    errors = CycleInputManifest(dt, (record,)).validate_alignment()
    if errors:
        raise ValueError('; '.join(errors))
    return CommittedInput(record, digest, str(path), reused=path in before)
