from common.ingest.mrms.config import (
    get_mrms_modifiers,
    get_registry,
    get_goes_modifiers,
    mrms_cleanup_max_age_minutes,
    mrms_remove_old_files,
)
from common.ingest.mrms.downloader import (
    download_all_files_async_internal,
    download_all_files_sync_fallback, download_files_sync_fallback,
    download_all_goes_files,
    download_all_goes_files_async
)
from common.ingest.mrms.pipeline import (
    get_output_dirs,
    run_ingestion_pipeline,
    run_with_async_fallback,
)
from common.config.loader import load_config
from util.io import IOManager
import util.file as fs
import asyncio

io_manager = IOManager("[Ingest]")


def _ingest_max_entries():
    """The S3 listing depth for MRMS ingest.

    Owned by ``runtime.yaml cycle.ingest_max_entries`` rather than by
    ``ingest.yaml``: the coordinator resolves the same key for the callers
    that pass one, so a literal default here would be a second owner that
    disagreed the moment an operator raised the catalog value.
    """
    return load_config("runtime")["cycle"]["ingest_max_entries"]


def _cleanup_kwargs():
    return {"max_age_minutes": mrms_cleanup_max_age_minutes()}


def _resolve_ingest_args(max_entries, remove_old_files):
    """Fill in whichever of the two an entry point's caller left unspecified."""
    return (
        _ingest_max_entries() if max_entries is None else max_entries,
        mrms_remove_old_files() if remove_old_files is None else remove_old_files,
    )


def get_detection_modifiers():
    registry = get_registry()
    if registry is not None:
        return [p.source_modifier for p in registry.for_phase("detection")]
    return list(load_config("ingest")["mrms"]["membership_lists"]["detection"])


def get_integration_modifiers():
    detection_mods = set(get_detection_modifiers())
    return [mod for _, mod, _ in get_mrms_modifiers() if mod not in detection_mods]


def get_enrichment_modifiers():
    """Enabled statistics inputs, separate from download-only additions."""
    registry = get_registry()
    if registry is None:
        return get_integration_modifiers()
    from common.ingest.mrms.core_contract import LEGACY_ALIASES
    products = {
        entry.get("product") or LEGACY_ALIASES.get(entry.get("filepath"))
        for entry in load_config("integration", config_dir=fs.MRMS_CONFIG_DIR)["stats_datasets"]
    }
    return [p.source_modifier for p in registry.products if not p.protected and p.product_id in products]


def get_other_modifiers():
    enrichment = set(get_enrichment_modifiers())
    return [p for p in get_integration_modifiers() if p not in enrichment]


def get_ewmrs_modifiers():
    from EWMRS.render.config import get_file_list

    render_dirs = {item["filepath"] for item in get_file_list()}
    return [
        modifier
        for _, modifier, outdir in get_mrms_modifiers()
        if outdir in render_dirs
    ]


def get_ewmrs_support_modifiers():
    detection_mods = set(get_detection_modifiers())
    return [mod for mod in get_ewmrs_modifiers() if mod not in detection_mods]

async def download_all_files_async(dt, max_entries=None, remove_old_files=None):
    max_entries, remove_old_files = _resolve_ingest_args(max_entries, remove_old_files)
    mrms_modifiers = get_mrms_modifiers()
    goes_modifiers = get_goes_modifiers()
    cleanup_dirs = get_output_dirs(mrms_modifiers, goes_modifiers=goes_modifiers)

    results = await run_ingestion_pipeline(
        io_manager=io_manager,
        async_downloads=[
            download_all_files_async_internal(dt, max_entries),
            download_all_goes_files_async(dt),
        ],
        cleanup_dirs=cleanup_dirs if remove_old_files else (),
        cleanup_async=fs.async_clean_old_files,
        cleanup_kwargs=_cleanup_kwargs(),
    )
    return results[0]

async def download_detection_files_async(dt, max_entries=None, remove_old_files=None):
    """Downloads only files strictly required for detection phase."""
    max_entries, remove_old_files = _resolve_ingest_args(max_entries, remove_old_files)
    mrms_modifiers = get_mrms_modifiers()
    detection_mods = get_detection_modifiers()
    cleanup_dirs = get_output_dirs(
        mrms_modifiers,
        include_modifiers=detection_mods,
        include_goes=False,
    )

    results = await run_ingestion_pipeline(
        io_manager=io_manager,
        async_downloads=[
            download_all_files_async_internal(dt, max_entries, target_modifiers=detection_mods),
        ],
        cleanup_dirs=cleanup_dirs if remove_old_files else (),
        cleanup_async=fs.async_clean_old_files,
        cleanup_kwargs=_cleanup_kwargs(),
        # Detection readiness is defined by its three downloads; background
        # housekeeping must not delay the EdgeWARN worker barrier.
        wait_for_cleanup=False,
    )
    return results[0]

async def download_integration_files_async(dt, max_entries=None, remove_old_files=None):
    """Downloads MRMS integration products, excluding detection products."""
    max_entries, remove_old_files = _resolve_ingest_args(max_entries, remove_old_files)
    mrms_modifiers = get_mrms_modifiers()
    detection_mods = get_detection_modifiers()
    integration_mods = get_integration_modifiers()
    cleanup_dirs = get_output_dirs(
        mrms_modifiers,
        exclude_modifiers=detection_mods,
        include_goes=False,
    )

    results = await run_ingestion_pipeline(
        io_manager=io_manager,
        async_downloads=[
            download_all_files_async_internal(dt, max_entries, target_modifiers=integration_mods),
        ],
        cleanup_dirs=cleanup_dirs if remove_old_files else (),
        cleanup_async=fs.async_clean_old_files,
        cleanup_kwargs=_cleanup_kwargs(),
    )
    return results[0]


async def download_ewmrs_files_async(dt, max_entries=None, remove_old_files=None):
    """Downloads the MRMS products required by the EWMRS render pipeline."""
    max_entries, remove_old_files = _resolve_ingest_args(max_entries, remove_old_files)
    mrms_modifiers = get_mrms_modifiers()
    render_mods = get_ewmrs_support_modifiers()
    cleanup_dirs = get_output_dirs(
        mrms_modifiers,
        include_modifiers=render_mods,
        include_goes=False,
    )

    await run_ingestion_pipeline(
        io_manager=io_manager,
        async_downloads=[
            download_all_files_async_internal(dt, max_entries, target_modifiers=render_mods),
        ],
        cleanup_dirs=cleanup_dirs if remove_old_files else (),
        cleanup_async=fs.async_clean_old_files,
        cleanup_kwargs=_cleanup_kwargs(),
    )

def download_all_files(dt, max_entries=None, remove_old_files=None):
    max_entries, remove_old_files = _resolve_ingest_args(max_entries, remove_old_files)
    def sync_fallback():
        result = download_all_files_sync_fallback(dt, max_entries)
        download_all_goes_files(dt)
        return result

    return run_with_async_fallback(
        io_manager=io_manager,
        async_runner=lambda: download_all_files_async(dt, max_entries, remove_old_files),
        sync_fallback=sync_fallback,
    )


def download_detection_files(dt, max_entries=None):
    """Sync fallback scoped to the detection MRMS products."""
    max_entries, _ = _resolve_ingest_args(max_entries, None)
    return download_files_sync_fallback(dt, max_entries, get_detection_modifiers())


def download_integration_files(dt, max_entries=None):
    """Sync fallback scoped to the integration MRMS products."""
    max_entries, _ = _resolve_ingest_args(max_entries, None)
    return download_files_sync_fallback(dt, max_entries, get_integration_modifiers())
