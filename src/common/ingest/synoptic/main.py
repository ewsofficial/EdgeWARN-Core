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


async def download_rap_async(dt: datetime):
    """
    Async version of download_rap.
    Cleans up RAP files before and after downloading so the RAP directory stays bounded.
    """
    max_age_minutes = get_rap_max_age_minutes()
    await _async_clean_rap_cache(
        dt,
        max_age_minutes=max_age_minutes,
        max_files=None,
    )
    result = await _download_rap(dt)
    if result:
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
