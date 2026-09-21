import re
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from common.ingest.manifest import StagedInput, staged_input_from_path
from common.ingest.mrms.config import (
    get_goes_max_entries,
    get_goes_modifiers,
    get_mrms_modifiers,
    goes_bucket,
    goes_cleanup_max_age_minutes,
    goes_hour_lookback,
    mrms_bucket,
    mrms_filename_prefix,
    mrms_probsevere_start_after,
    normalize_goes_modifier,
)
from common.ingest.mrms.s3_sync import FileFinder, FileDownloader
from common.ingest.mrms.s3_async import AsyncFileFinder, AsyncFileDownloader
from common.ingest.mrms.https_client import HttpsFileFinder, HttpsFileDownloader
from common.ingest.mrms.parse import parse_mrms_bucket_path, parse_goes_bucket_path
from common.ingest.mrms.utils import merge_glm_files, extract_timestamp
import util.file as fs
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
import aioboto3
from botocore import UNSIGNED
from botocore.client import Config
from common.ingest.aws_async_compat import ensure_aiobotocore_endpoint_compat

from util.io import IOManager, PerformanceTimer
from util.performance import tracker as perf_tracker
import uuid

io_manager = IOManager("[Ingest]")


def _write_netcdf_atomically(dataset, destination: Path) -> None:
    """Publish a completed NetCDF file without exposing a partial artifact.

    The scan-time GLM task runs concurrently with the staged ingest cycle.
    Readers must therefore never observe the final merged filename until the
    writer has closed it completely.  Write to a sibling temporary path and
    atomically replace the destination only after ``to_netcdf`` succeeds.
    """
    destination = Path(destination)
    temporary = destination.with_name(f".{destination.name}.part")
    temporary.unlink(missing_ok=True)
    try:
        dataset.to_netcdf(temporary, engine="netcdf4")
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _narrow_mrms_lookup(dt, region, modifier):
    """Return the ``(prefix, start_after)`` pair that narrows one MRMS listing.

    Two mutually exclusive ways to avoid listing a whole day:

    * A modifier means the filename carries the hour, so appending
      :func:`mrms_filename_prefix` to the day prefix makes S3 itself exclude every
      other hour, and no ``StartAfter`` marker is needed.
    * ProbSevere has no modifier and separates its date and hour with an
      underscore, so the prefix cannot be extended that way; it gets an explicit
      marker one lookback behind the target instead.

    The sync and async download paths shared neither the code nor a comment
    explaining the asymmetry, and each carried its own copy of both grammars.
    """
    prefix = parse_mrms_bucket_path(dt, region, modifier)
    if modifier is not None:
        return f"{prefix}{mrms_filename_prefix(dt, modifier)}", None
    return prefix, f"{prefix}{mrms_probsevere_start_after(dt)}"


@dataclass(frozen=True)
class DownloadBatchResult:
    attempted: tuple[str, ...]
    downloaded: tuple[StagedInput, ...]
    failed: tuple[str, ...]

    def __post_init__(self):
        invalid = [
            type(record).__name__
            for record in self.downloaded
            if not isinstance(record, StagedInput)
        ]
        if invalid:
            raise TypeError(
                "DownloadBatchResult.downloaded requires StagedInput records; "
                f"received {', '.join(invalid)}"
            )

    @property
    def downloaded_products(self) -> tuple[str, ...]:
        return tuple(record.product for record in self.downloaded)

    @property
    def successful(self) -> bool:
        return (
            bool(self.attempted)
            and not self.failed
            and set(self.downloaded_products) == set(self.attempted)
        )


def _staged_record(product, path, *, source, family="mrms"):
    if path is None:
        return None
    return staged_input_from_path(
        product,
        Path(path),
        source=source,
        family=family,
    )


async def _download_mrms_https_async(dt, region, modifier, outdir, downloader):
    """Try the same requested MRMS time on HTTPS after any S3 miss/failure."""
    label = _mrms_modifier_label(modifier)
    try:
        urls = await HttpsFileFinder(dt, io_manager).find_files(region, modifier)
        if not urls:
            return label, None
        path = await HttpsFileDownloader(dt, io_manager).download_matching(urls, outdir)
        if path is not None and Path(path).suffix == ".gz":
            path = await downloader.async_decompress_file(Path(path))
        return label, _staged_record(label, path, source="https")
    except Exception as exc:
        io_manager.write_error(f"HTTPS fallback failed for {label}: {exc}")
        return label, None


def _download_mrms_https_sync(dt, region, modifier, outdir, downloader):
    label = _mrms_modifier_label(modifier)
    try:
        urls = HttpsFileFinder(dt, io_manager).find_files_sync(region, modifier)
        if not urls:
            return label, None
        path = HttpsFileDownloader(dt, io_manager).download_matching_sync(urls, outdir)
        if path is not None and Path(path).suffix == ".gz":
            path = downloader.decompress_file(Path(path))
        return label, _staged_record(label, path, source="https")
    except Exception as exc:
        io_manager.write_error(f"HTTPS fallback failed for {label}: {exc}")
        return label, None


def _goes_staged_records(goes_spec, paths, *, source):
    spec = normalize_goes_modifier(goes_spec)
    label = _get_goes_spec_label(spec)
    return tuple(
        _staged_record(label, path, source=source, family="goes")
        for path in (paths or ())
        if path is not None
    )


def _mrms_modifier_label(modifier):
    return modifier if modifier else "ProbSevere"


def _record_perf_metric(perf_maps, phase, label, elapsed_ms):
    if perf_maps is None:
        return
    perf_maps.setdefault(phase, {})[str(label)] = round(float(elapsed_ms), 2)


def _format_perf_summary(values):
    if not values:
        return ""
    slowest_label, slowest_ms = max(values.items(), key=lambda item: item[1])
    samples = tuple(values.values())
    return (
        f"count={len(samples)}, "
        f"min={min(samples):.2f}, "
        f"max={max(samples):.2f}, "
        f"avg={fmean(samples):.2f}, "
        f"slowest={slowest_label}"
    )


def _emit_perf_maps(trace_id, perf_maps):
    if not perf_maps:
        return
    for phase in ("lookup_ms", "download_ms", "decompress_ms"):
        values = perf_maps.get(phase)
        if values:
            io_manager.write_perf(f"[{trace_id}] {phase}: {_format_perf_summary(values)}")


def _log_batch_summary(summary_label, downloaded, failed):
    io_manager.write_info(
        f"{summary_label} download summary: downloaded={len(downloaded)}, failed={len(failed)}"
    )
    if failed:
        io_manager.write_warning(f"{summary_label} failed products: {', '.join(failed)}")


def _should_log_per_product_success(attempted_count):
    return attempted_count <= 5


def _get_goes_spec_label(goes_spec):
    return goes_spec.label if goes_spec.channel_id else goes_spec.product


def _goes_search_max_entries():
    """The GOES listing depth, deliberately independent of the MRMS depth.

    GLM publishes roughly 180 objects an hour, so the MRMS depth of 10 would
    never list back far enough to reach the requested scan. Every GOES caller
    reads this value rather than supplying one per call.

    Read per call, not at import, so a `--config-dir` that a spawned accessory
    resolves after this module is imported is still honored.
    """
    return get_goes_max_entries()


def _get_goes_bucket_paths(dt, product, hour_lookback=None):
    """The hourly bucket prefixes one GOES lookup walks, newest hour first.

    The single resolution site for ``hour_lookback``. Every GOES entry point
    passes its parameter straight through, so ``None`` there means "no caller
    narrowed the window" and the catalog value applies here -- one owner, and
    still overridable per call.
    """
    if hour_lookback is None:
        hour_lookback = goes_hour_lookback()

    return [
        parse_goes_bucket_path(dt, product, hour_offset=hour_offset)
        for hour_offset in range(hour_lookback)
    ]


def _filter_goes_files_for_spec(file_list, goes_spec, trace_id=None):
    if not goes_spec.filename_matcher:
        return file_list

    matcher = re.compile(goes_spec.filename_matcher)
    filtered_files = [
        (s3_path, timestamp)
        for s3_path, timestamp in file_list
        if matcher.search(s3_path)
    ]

    if not filtered_files:
        prefix = f"[{trace_id}] " if trace_id else ""
        io_manager.write_warning(
            f"{prefix}No files matched {goes_spec.channel_id} for GOES product {goes_spec.product}"
        )

    return filtered_files


def _cleanup_goes_outdir_sync(goes_spec, max_age_minutes=None):
    """Run pre-download cleanup for a GOES product output directory (sync path)."""
    if max_age_minutes is None:
        max_age_minutes = goes_cleanup_max_age_minutes()

    try:
        outdir = goes_spec.outdir
        fs.clean_old_files(outdir, max_age_minutes=max_age_minutes, max_files=goes_spec.max_files)
    except Exception as e:
        label = _get_goes_spec_label(goes_spec)
        io_manager.write_warning(f"[GOES:{label}] Pre-download cleanup failed for {outdir}: {e}")


async def _cleanup_goes_outdir_async(goes_spec, trace_id, max_age_minutes=None):
    """Run pre-download cleanup for a GOES product output directory (async path)."""
    if max_age_minutes is None:
        max_age_minutes = goes_cleanup_max_age_minutes()

    try:
        outdir = goes_spec.outdir
        await fs.async_clean_old_files(outdir, max_age_minutes=max_age_minutes, max_files=goes_spec.max_files)
    except Exception as e:
        label = _get_goes_spec_label(goes_spec)
        io_manager.write_warning(
            f"[{trace_id}] [GOES:{label}] Async pre-download cleanup failed for {outdir}: {e}"
        )


def _cleanup_goes_specs_sync(goes_specs, max_age_minutes=None):
    for goes_spec in goes_specs:
        _cleanup_goes_outdir_sync(goes_spec, max_age_minutes=max_age_minutes)
    if goes_specs:
        io_manager.write_info(f"GOES pre-download cleanup completed for {len(goes_specs)} products")


async def _cleanup_goes_specs_async(goes_specs, trace_id, max_age_minutes=None):
    if not goes_specs:
        return
    await asyncio.gather(
        *[
            _cleanup_goes_outdir_async(goes_spec, trace_id, max_age_minutes=max_age_minutes)
            for goes_spec in goes_specs
        ]
    )
    io_manager.write_info(f"[{trace_id}] GOES pre-download cleanup completed for {len(goes_specs)} products")

async def download_all_files_async_internal(dt, max_entries, target_modifiers=None):
    """Internal async function that handles the actual download operations"""
    trace_id = f"INGEST-{uuid.uuid4().hex[:8]}"
    
    # Create shared async S3 client for all operations
    ensure_aiobotocore_endpoint_compat()
    async with aioboto3.Session().client("s3", config=Config(signature_version=UNSIGNED)) as s3:
        with PerformanceTimer(io_manager, "MRMS_Ingest_Total", trace_id):
            perf_maps = {"lookup_ms": {}, "download_ms": {}, "decompress_ms": {}}
            
            # Create async tasks for all modifiers
            tasks = []
            attempted = []
            for region, modifier, outdir in get_mrms_modifiers():
                if target_modifiers is not None and modifier not in target_modifiers:
                    continue
                attempted.append(_mrms_modifier_label(modifier))
                task = download_modifier_async(
                    region, modifier, outdir, dt, max_entries, s3, trace_id, perf_maps=perf_maps
                )
                tasks.append(task)
            
            downloaded = []
            failed = []
            log_per_product = _should_log_per_product_success(len(attempted))
            for task in asyncio.as_completed(tasks):
                label, record = await task
                if record is not None:
                    downloaded.append(record)
                    if log_per_product:
                        io_manager.write_info(f"Downloaded: {label}")
                else:
                    failed.append(label)
            
            _log_batch_summary("MRMS", downloaded, failed)
            _emit_perf_maps(trace_id, perf_maps)
            return DownloadBatchResult(
                attempted=tuple(attempted),
                downloaded=tuple(downloaded),
                failed=tuple(failed),
            )

async def download_modifier_async(region, modifier, outdir, dt, max_entries, s3_client, parent_trace_id=None, perf_maps=None):
    """Internal async version of download_modifier using aioboto3 for non-blocking S3 operations"""
    # Enforce minute-precision dt
    dt = dt.replace(second=0, microsecond=0)
    
    # Use parent trace ID or generate new one
    trace_id = parent_trace_id or f"MOD-{uuid.uuid4().hex[:8]}"
    modifier_name = _mrms_modifier_label(modifier)

    s3_bucket = mrms_bucket()
    finder = AsyncFileFinder(dt, s3_bucket, max_entries, io_manager, s3_client=s3_client)
    downloader = AsyncFileDownloader(dt, s3_bucket, io_manager, s3_client=s3_client)

    perf_tracker.start(f"Ingest - MRMS - {modifier_name}")
    try:
        bucket_path, start_after = _narrow_mrms_lookup(dt, region, modifier)

        # Async file lookup (S3)
        lookup_started_at = asyncio.get_running_loop().time()
        perf_tracker.start(f"Ingest - MRMS - {modifier_name} - Lookup")
        file_list = await finder.async_lookup_files(bucket_path, start_after=start_after)
        perf_tracker.stop(f"Ingest - MRMS - {modifier_name} - Lookup")
        _record_perf_metric(perf_maps, "lookup_ms", modifier_name, (asyncio.get_running_loop().time() - lookup_started_at) * 1000)

        if not file_list:
            io_manager.write_warning(f"[{trace_id}] No S3 file for {modifier_name} at {dt}; trying HTTPS")
            result = await _download_mrms_https_async(dt, region, modifier, outdir, downloader)
            perf_tracker.stop(f"Ingest - MRMS - {modifier_name}")
            return result
        
        # Download most recent file asynchronously (S3)
        downloaded = None
        download_started_at = asyncio.get_running_loop().time()
        perf_tracker.start(f"Ingest - MRMS - {modifier_name} - Download")
        downloaded = await downloader.async_download_matching(file_list, outdir)
        perf_tracker.stop(f"Ingest - MRMS - {modifier_name} - Download")
        _record_perf_metric(perf_maps, "download_ms", modifier_name, (asyncio.get_running_loop().time() - download_started_at) * 1000)
            
        staged_path = downloaded
        if downloaded:
            if downloaded.suffix == ".gz":
                decompress_started_at = asyncio.get_running_loop().time()
                staged_path = await downloader.async_decompress_file(downloaded)
                _record_perf_metric(perf_maps, "decompress_ms", modifier_name, (asyncio.get_running_loop().time() - decompress_started_at) * 1000)
        else:
            io_manager.write_warning(f"[{trace_id}] S3 fetch failed for {modifier_name}; trying HTTPS")
            result = await _download_mrms_https_async(dt, region, modifier, outdir, downloader)
            perf_tracker.stop(f"Ingest - MRMS - {modifier_name}")
            return result

        if staged_path is None:
            result = await _download_mrms_https_async(dt, region, modifier, outdir, downloader)
            perf_tracker.stop(f"Ingest - MRMS - {modifier_name}")
            return result
        
        perf_tracker.stop(f"Ingest - MRMS - {modifier_name}")
        return (
            modifier_name,
            _staged_record(
                modifier_name,
                staged_path,
                source="s3_async",
            ),
        )
    
    except Exception as e:
        io_manager.write_error(f"[{trace_id}] S3 processing failed for {modifier_name}: {e}")
        result = await _download_mrms_https_async(dt, region, modifier, outdir, downloader)
        perf_tracker.stop(f"Ingest - MRMS - {modifier_name}")
        return result

def download_files_sync_fallback(dt, max_entries, target_modifiers=None):
    """Sync fallback for a selected MRMS phase (or all products)."""
    # Multithread MRMS downloads
    mrms_modifiers_list = [
        spec for spec in get_mrms_modifiers()
        if target_modifiers is None or spec[1] in target_modifiers
    ]
    downloaded = []
    failed = []
    log_per_product = _should_log_per_product_success(len(mrms_modifiers_list))
    with ThreadPoolExecutor(max_workers=len(mrms_modifiers_list) + 2) as executor:
        futures = [
            executor.submit(download_modifier_sync, region, modifier, outdir, dt, max_entries)
            for region, modifier, outdir in mrms_modifiers_list
        ]

        for future in as_completed(futures):
            label, record = future.result()
            if record is not None:
                downloaded.append(record)
                if log_per_product:
                    io_manager.write_info(f"Downloaded: {label}")
            else:
                failed.append(label)
    _log_batch_summary("MRMS", downloaded, failed)
    return DownloadBatchResult(
        attempted=tuple(_mrms_modifier_label(modifier) for _, modifier, _ in mrms_modifiers_list),
        downloaded=tuple(downloaded),
        failed=tuple(failed),
    )


def download_all_files_sync_fallback(dt, max_entries):
    """Backward-compatible full-MRMS sync fallback."""
    return download_files_sync_fallback(dt, max_entries)

def download_modifier_sync(region, modifier, outdir, dt, max_entries):
    """Internal sync version of download_modifier for fallback"""
    # Enforce minute-precision dt
    dt = dt.replace(second=0, microsecond=0)

    s3_bucket = mrms_bucket()
    finder = FileFinder(dt, s3_bucket, max_entries, io_manager)
    downloader = FileDownloader(dt, s3_bucket, io_manager)
    modifier_name = _mrms_modifier_label(modifier)

    try:
        bucket_path, start_after = _narrow_mrms_lookup(dt, region, modifier)

        file_list = finder.lookup_files(bucket_path, start_after=start_after)

        if not file_list:
            io_manager.write_warning(f"No S3 file for {modifier_name} at {dt}; trying HTTPS")
            return _download_mrms_https_sync(dt, region, modifier, outdir, downloader)
        
        # Download most recent file that matches the target minute
        downloaded = downloader.download_matching(file_list, outdir)
        staged_path = downloaded
        if downloaded and downloaded.suffix == ".gz":
            staged_path = downloader.decompress_file(downloaded)
        else:
            if not downloaded:
                io_manager.write_warning(f"S3 fetch failed for {modifier_name}; trying HTTPS")
                return _download_mrms_https_sync(dt, region, modifier, outdir, downloader)
        if staged_path is None:
            return _download_mrms_https_sync(dt, region, modifier, outdir, downloader)
    
    except Exception as e:
        io_manager.write_error(f"S3 processing failed for {modifier_name}: {e}")
        return _download_mrms_https_sync(dt, region, modifier, outdir, downloader)
    return (
        modifier_name,
        _staged_record(
            modifier_name,
            staged_path,
            source="s3_sync",
        ),
    )

# ==================== GOES-19 Download Functions ====================

def download_goes_product(goes_spec, dt, hour_lookback=None, preloaded_files=None):
    """
    Download a specific GOES-19 product.

    Args:
        goes_spec: GOES ingest specification or legacy ``(product, outdir)`` tuple
        dt (datetime): Target datetime (UTC, timezone-aware)
        hour_lookback (int): Hours to look back; None uses goes.hour_lookback.

    Returns:
        Path: Path to downloaded file, or None if failed
    """
    # Enforce minute-precision dt
    # dt = dt.replace(second=0, microsecond=0) # Allow seconds for sliding window

    goes_spec = normalize_goes_modifier(goes_spec)
    product = goes_spec.product
    outdir = goes_spec.outdir
    label = _get_goes_spec_label(goes_spec)

    s3_bucket = goes_bucket()
    finder = FileFinder(dt, s3_bucket, _goes_search_max_entries(), io_manager)
    downloader = FileDownloader(dt, s3_bucket, io_manager)
    
    try:
        all_files = preloaded_files
        if all_files is None:
            bucket_paths = _get_goes_bucket_paths(dt, product, hour_lookback)
            # Lookup files across all paths (FileFinder handles the loop and max_entries check)
            all_files = finder.lookup_files(bucket_paths)
        
        if not all_files:
            io_manager.write_warning(f"No files found for GOES product {label} at {dt}")
            return None

        all_files = _filter_goes_files_for_spec(all_files, goes_spec)
        if not all_files:
            return []
        
        
        # Download all matching files
        if goes_spec.is_glm:
            downloaded_files = downloader.download_all_matching(all_files, outdir)
        else:
            downloaded = downloader.download_matching(all_files, outdir)
            downloaded_files = [downloaded] if downloaded else []
        

        
        if downloaded_files:
            processed_files = []
            for downloaded in downloaded_files:
                # Decompress if .gz
                if downloaded.suffix == ".gz":
                    decompressed = downloader.decompress_file(downloaded)
                    if decompressed:
                        processed_files.append(decompressed)
                    else:
                        processed_files.append(downloaded)
                else:
                    processed_files.append(downloaded)
            
            # Check if we need to merge GLM files
            if goes_spec.is_glm and len(processed_files) > 1:
                io_manager.write_info(f"Merging {len(processed_files)} GLM files...")
                merged_ds = merge_glm_files(processed_files, io_manager)
                
                if merged_ds:
                    # Find the newest timestamp among the files
                    try:
                        timestamps = [extract_timestamp(str(f)) for f in processed_files]
                        newest_ts = max(timestamps)
                        ts_str = newest_ts.strftime('%Y%m%d-%H%M%S')
                    except Exception as e:
                        io_manager.write_warning(f"Could not extract timestamps for naming, using target dt: {e}")
                        ts_str = dt.strftime('%Y%m%d-%H%M%S')

                    # Create a merged filename
                    # Format: OR_{product}_merged_YYYYMMDD-HHMMSS.nc
                    merged_filename = f"OR_{product}_merged_{ts_str}.nc"
                    merged_path = outdir / merged_filename
                    
                    try:
                        _write_netcdf_atomically(merged_ds, merged_path)
                        io_manager.write_info(f"Saved merged GLM file to: {merged_path}")
                        merged_ds.close()
                        
                        # Delete individual files after successful merge
                        for f in processed_files:
                            try:
                                f.unlink()
                            except Exception as del_e:
                                io_manager.write_warning(f"Failed to delete {f}: {del_e}")
                        io_manager.write_debug(f"Deleted {len(processed_files)} individual GLM files")
                        
                        # Return only the merged file path
                        return [merged_path]
                    except Exception as e:
                        io_manager.write_error(f"Failed to save merged GLM file: {e}")
                        merged_ds.close()
                        # Fallback to returning individual files? Or fail?
                        # Let's return individual files as fallback
                        return processed_files
                else:
                    io_manager.write_error("GLM merge failed, returning individual files")
                    return processed_files

            return processed_files
        else:
            io_manager.write_error(f"Failed to download GOES {label} file")
            return []
    
    except Exception as e:
        io_manager.write_error(f"Failed to process GOES {label} - {e}")
        return []


async def _download_goes_product_async(
    goes_spec,
    dt,
    hour_lookback,
    s3_client,
    parent_trace_id=None,
    perf_maps=None,
    preloaded_files=None,
):
    """
    Async version of download_goes_product.

    Internal async function for downloading a single GOES product using aioboto3.
    """
    goes_spec = normalize_goes_modifier(goes_spec)
    product = goes_spec.product
    outdir = goes_spec.outdir
    label = _get_goes_spec_label(goes_spec)
    trace_id = parent_trace_id or f"GOES-{uuid.uuid4().hex[:8]}"

    s3_bucket = goes_bucket()
    finder = AsyncFileFinder(
        dt, s3_bucket, _goes_search_max_entries(), io_manager, s3_client=s3_client
    )
    downloader = AsyncFileDownloader(dt, s3_bucket, io_manager, s3_client=s3_client)
    
    perf_tracker.start(f"Ingest - GOES - {label}")
    try:
        all_files = preloaded_files
        if all_files is None:
            bucket_paths = _get_goes_bucket_paths(dt, product, hour_lookback)
            # Lookup files across all paths (AsyncFileFinder handles the loop and max_entries check)
            lookup_started_at = asyncio.get_running_loop().time()
            perf_tracker.start(f"Ingest - GOES - {label} - Lookup")
            all_files = await finder.async_lookup_files(bucket_paths)
            perf_tracker.stop(f"Ingest - GOES - {label} - Lookup")
            _record_perf_metric(perf_maps, "lookup_ms", label, (asyncio.get_running_loop().time() - lookup_started_at) * 1000)
        
        if not all_files:
            io_manager.write_warning(f"[{trace_id}] No files found for GOES product {label} at {dt}")
            perf_tracker.stop(f"Ingest - GOES - {label}")
            return None

        all_files = _filter_goes_files_for_spec(all_files, goes_spec, trace_id=trace_id)
        if not all_files:
            perf_tracker.stop(f"Ingest - GOES - {label}")
            return []
        
        
        # Download all matching files
        download_started_at = asyncio.get_running_loop().time()
        perf_tracker.start(f"Ingest - GOES - {label} - Download")
        if goes_spec.is_glm:
            downloaded_files = await downloader.async_download_all_matching(all_files, outdir)
        else:
            downloaded = await downloader.async_download_matching(all_files, outdir)
            downloaded_files = [downloaded] if downloaded else []
        perf_tracker.stop(f"Ingest - GOES - {label} - Download")
        _record_perf_metric(perf_maps, "download_ms", label, (asyncio.get_running_loop().time() - download_started_at) * 1000)
        
        if downloaded_files:
            processed_files = []
            decompress_tasks = []
            
            # Helper to handle decompression result mapping
            async def _decompress_wrapper(f):
                if f.suffix == ".gz":
                        decompress_started_at = asyncio.get_running_loop().time()
                        res = await downloader.async_decompress_file(f)
                        _record_perf_metric(
                            perf_maps,
                            "decompress_ms",
                            label,
                            (asyncio.get_running_loop().time() - decompress_started_at) * 1000,
                        )
                        return res if res else f
                return f

            # Gather all decompression tasks
            results = await asyncio.gather(*[_decompress_wrapper(f) for f in downloaded_files])
            processed_files = list(results)
            
            # Check if we need to merge GLM files
            if goes_spec.is_glm and len(processed_files) > 1:
                io_manager.write_info(f"[{trace_id}] Merging {len(processed_files)} GLM files (Async)...")
                
                # merge_glm_files is synchronous, so we offload it to a thread pool
                loop = asyncio.get_running_loop()
                # merge_glm_files is synchronous, so we offload it to a thread pool
                loop = asyncio.get_running_loop()
                with PerformanceTimer(io_manager, f"Merge_GLM", trace_id):
                    perf_tracker.start(f"Ingest - GOES - {label} - Merge")
                    merged_ds = await loop.run_in_executor(None, merge_glm_files, processed_files, io_manager)
                    perf_tracker.stop(f"Ingest - GOES - {label} - Merge")
                
                if merged_ds:
                    # Find the newest timestamp among the files
                    try:
                        timestamps = [extract_timestamp(str(f)) for f in processed_files]
                        newest_ts = max(timestamps)
                        ts_str = newest_ts.strftime('%Y%m%d-%H%M%S')
                    except Exception as e:
                        io_manager.write_warning(f"[{trace_id}] Could not extract timestamps for naming, using target dt: {e}")
                        ts_str = dt.strftime('%Y%m%d-%H%M%S')

                    merged_filename = f"OR_{product}_merged_{ts_str}.nc"
                    merged_path = outdir / merged_filename
                    
                    try:
                        # NetCDF serialization is synchronous; publish only
                        # after the temporary file is complete and closed.
                        _write_netcdf_atomically(merged_ds, merged_path)
                        io_manager.write_info(f"[{trace_id}] Saved merged GLM file to: {merged_path}")
                        merged_ds.close()
                        
                        # Delete individual files after successful merge
                        for f in processed_files:
                            try:
                                f.unlink()
                            except Exception as del_e:
                                io_manager.write_warning(f"[{trace_id}] Failed to delete {f}: {del_e}")
                        io_manager.write_debug(f"[{trace_id}] Deleted {len(processed_files)} individual GLM files")
                        
                        perf_tracker.stop(f"Ingest - GOES - {label}")
                        return [merged_path]
                    except Exception as e:
                        io_manager.write_error(f"[{trace_id}] Failed to save merged GLM file: {e}")
                        merged_ds.close()
                        perf_tracker.stop(f"Ingest - GOES - {label}")
                        return processed_files
                else:
                    io_manager.write_error(f"[{trace_id}] GLM merge failed, returning individual files")
                    perf_tracker.stop(f"Ingest - GOES - {label}")
                    return processed_files

            perf_tracker.stop(f"Ingest - GOES - {label}")
            return processed_files
        else:
            io_manager.write_error(f"[{trace_id}] Failed to download GOES {label} file")
            perf_tracker.stop(f"Ingest - GOES - {label}")
            return []
    
    except Exception as e:
        io_manager.write_error(f"[{trace_id}] Failed to process GOES {label} - {e}")
        perf_tracker.stop(f"Ingest - GOES - {label}")
        return []

    perf_tracker.stop(f"Ingest - GOES - {label}")


def download_goes_specs(goes_specs, dt, hour_lookback=None):
    """Download a specific list of GOES-19 products."""
    goes_modifiers_list = [normalize_goes_modifier(spec) for spec in goes_specs]
    if not goes_modifiers_list:
        return DownloadBatchResult(attempted=(), downloaded=(), failed=())

    io_manager.write_info("Starting GOES-19 downloads...")
    _cleanup_goes_specs_sync(goes_modifiers_list)

    # Use ThreadPoolExecutor for concurrent downloads
    shared_channel_files_by_product = {}
    s3_bucket = goes_bucket()
    search_max_entries = _goes_search_max_entries()

    for goes_spec in goes_modifiers_list:
        if not goes_spec.channel_id or goes_spec.product in shared_channel_files_by_product:
            continue

        finder = FileFinder(dt, s3_bucket, search_max_entries, io_manager)
        bucket_paths = _get_goes_bucket_paths(dt, goes_spec.product, hour_lookback)
        shared_channel_files_by_product[goes_spec.product] = finder.lookup_files(bucket_paths)

    downloaded = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=len(goes_modifiers_list)) as executor:
        futures = {
            executor.submit(
                download_goes_product,
                goes_spec,
                dt,
                hour_lookback,
                shared_channel_files_by_product.get(goes_spec.product)
                if goes_spec.channel_id
                else None,
            ): goes_spec
            for goes_spec in goes_modifiers_list
        }

        downloaded_records = []
        failed_labels = []
        for future in as_completed(futures):
            goes_spec = futures[future]
            label = _get_goes_spec_label(goes_spec)
            try:
                result = future.result()
                if result:
                    downloaded += 1
                    downloaded_records.extend(
                        _goes_staged_records(
                            goes_spec,
                            result,
                            source="s3_sync",
                        )
                    )
                else:
                    failed += 1
                    failed_labels.append(label)
            except Exception as e:
                failed += 1
                failed_labels.append(label)
                io_manager.write_error(f"GOES download error: {e}")

    io_manager.write_info(f"GOES download summary: downloaded={downloaded}, failed={failed}")
    return DownloadBatchResult(
        attempted=tuple(_get_goes_spec_label(spec) for spec in goes_modifiers_list),
        downloaded=tuple(downloaded_records),
        failed=tuple(failed_labels),
    )


def download_all_goes_files(dt, hour_lookback=None):
    """
    Download all configured GOES-19 products.

    Args:
        dt (datetime): Target datetime (UTC, timezone-aware)
        hour_lookback (int): Hours to look back; None uses goes.hour_lookback.
    """
    return download_goes_specs(
        get_goes_modifiers(),
        dt,
        hour_lookback=hour_lookback,
    )


async def download_goes_specs_async(goes_specs, dt, hour_lookback=None):
    """Async version: Download a specific list of GOES-19 products concurrently."""
    trace_id = f"GOES_ALL-{uuid.uuid4().hex[:8]}"
    goes_modifiers_list = [normalize_goes_modifier(spec) for spec in goes_specs]
    if not goes_modifiers_list:
        return DownloadBatchResult(attempted=(), downloaded=(), failed=())

    ensure_aiobotocore_endpoint_compat()
    async with aioboto3.Session().client("s3", config=Config(signature_version=UNSIGNED)) as s3:
        io_manager.write_info(f"[{trace_id}] Starting async GOES-19 downloads...")
        perf_maps = {"lookup_ms": {}, "download_ms": {}, "decompress_ms": {}}
        await _cleanup_goes_specs_async(goes_modifiers_list, trace_id)
        shared_channel_files_by_product = {}
        s3_bucket = goes_bucket()
        search_max_entries = _goes_search_max_entries()

        for goes_spec in goes_modifiers_list:
            if not goes_spec.channel_id or goes_spec.product in shared_channel_files_by_product:
                continue

            finder = AsyncFileFinder(
                dt, s3_bucket, search_max_entries, io_manager, s3_client=s3
            )
            bucket_paths = _get_goes_bucket_paths(dt, goes_spec.product, hour_lookback)
            lookup_started_at = asyncio.get_running_loop().time()
            shared_channel_files_by_product[goes_spec.product] = await finder.async_lookup_files(bucket_paths)
            _record_perf_metric(
                perf_maps,
                "lookup_ms",
                f"{goes_spec.product}_shared",
                (asyncio.get_running_loop().time() - lookup_started_at) * 1000,
            )

        tasks = [
            _download_goes_product_async(
                goes_spec,
                dt,
                hour_lookback,
                s3,
                trace_id,
                perf_maps=perf_maps,
                preloaded_files=(
                    shared_channel_files_by_product.get(goes_spec.product)
                    if goes_spec.channel_id
                    else None
                ),
            )
            for goes_spec in goes_modifiers_list
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        downloaded = 0
        failed = 0
        downloaded_records = []
        failed_labels = []
        for goes_spec, result in zip(goes_modifiers_list, results):
            label = _get_goes_spec_label(goes_spec)
            if isinstance(result, Exception):
                failed += 1
                failed_labels.append(label)
                io_manager.write_error(f"[{trace_id}] GOES async download error: {result}")
            elif result:
                downloaded += 1
                downloaded_records.extend(
                    _goes_staged_records(
                        goes_spec,
                        result,
                        source="s3_async",
                    )
                )
            else:
                failed += 1
                failed_labels.append(label)

        io_manager.write_info(
            f"[{trace_id}] GOES download summary: downloaded={downloaded}, failed={failed}"
        )
        _emit_perf_maps(trace_id, perf_maps)
        return DownloadBatchResult(
            attempted=tuple(
                _get_goes_spec_label(spec) for spec in goes_modifiers_list
            ),
            downloaded=tuple(downloaded_records),
            failed=tuple(failed_labels),
        )


async def download_all_goes_files_async(dt, hour_lookback=None):
    """
    Async version: Download all configured GOES-19 products concurrently.

    Args:
        dt (datetime): Target datetime (UTC, timezone-aware)
        hour_lookback (int): Hours to look back; None uses goes.hour_lookback.
    """
    return await download_goes_specs_async(
        get_goes_modifiers(),
        dt,
        hour_lookback=hour_lookback,
    )
