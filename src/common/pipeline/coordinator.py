"""Shared staged-ingest coordination for tandem EdgeWARN and EWMRS execution."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from common.ingest.manifest import (
    CycleInputManifest,
    StagedInput,
    parse_file_analysis_time,
    staged_input_from_path,
)
import common.ingest.mrms.main as mrms_ingest
from common.ingest.mrms.downloader import (
    DownloadBatchResult,
    download_all_goes_files,
    download_all_goes_files_async,
)
from common.ingest.synoptic.main import download_rap_async
from common.ingest.synoptic.main import parse_rap_analysis_time
from common.config.loader import load_config


LogFunc = Callable[[str], None]
StateCallback = Callable[["CycleState"], None]


@dataclass
class CycleState:
    """Tracks staged readiness for a single shared ingest cycle."""

    timestamp: datetime
    detection_inputs_ready: bool = False
    ewmrs_mrms_inputs_ready: bool = False
    ewmrs_goes_inputs_ready: bool = False
    mrms_integration_inputs_ready: bool = False
    rap_inputs_ready: bool = False
    edgewarn_integration_inputs_ready: bool = False
    edgewarn_generated_file: str | None = None
    input_manifest: CycleInputManifest | None = None
    errors: dict[str, str] = field(default_factory=dict)


async def _safe_ingest(
    task_name: str,
    log: LogFunc,
    async_func,
    sync_fallback,
    *args,
    require_result: bool = False,
):
    try:
        result = await async_func(*args)
        if require_result and not _explicit_ingest_success(result):
            raise RuntimeError(f"{task_name} ingestion did not return a staged file path")
        log(f"INFO: Async {task_name} ingestion successful")
        return result
    except Exception as exc:
        log(f"WARN: Async {task_name} ingestion failed: {exc}. Falling back to sync.")
        try:
            # Fallback is exceptional and phase-local; run it synchronously so
            # a cycle cannot retain an executor thread during teardown.
            result = sync_fallback(*args)
            if inspect.isawaitable(result):
                result = await result
            if require_result and not _explicit_ingest_success(result):
                raise RuntimeError(f"{task_name} sync fallback did not return a staged file path")
            log(f"INFO: Sync fallback for {task_name} successful")
            return result
        except Exception as fallback_exc:
            log(f"ERROR: Both async and sync ingestion failed for {task_name}: {fallback_exc}")
            return None


def _explicit_ingest_success(result) -> bool:
    if isinstance(result, DownloadBatchResult):
        return result.successful
    return bool(result)


async def _ingest_rap(dt: datetime, log: LogFunc):
    """Run the single exhaustive RAP selection owned by the source layer."""
    try:
        result = await download_rap_async(dt)
        if not result:
            raise RuntimeError("RAP ingestion did not return a staged file path")
        log("INFO: Async RAP ingestion successful")
        return result, None
    except Exception as exc:
        reason = str(exc)
        log(f"ERROR: RAP ingestion failed: {reason}")
        return None, reason


async def run_staged_ingest_cycle(
    dt: datetime,
    log: LogFunc,
    *,
    max_entries: int | None = None,
    include_goes: bool = True,
    include_rap: bool = True,
    include_ewmrs: bool = True,
    on_detection_ready: Optional[StateCallback] = None,
    on_ewmrs_mrms_ready: Optional[StateCallback] = None,
    on_ewmrs_goes_ready: Optional[StateCallback] = None,
    on_edgewarn_integration_ready: Optional[StateCallback] = None,
    on_base_integration_ready: Optional[StateCallback] = None,
) -> CycleState:
    """Run staged shared ingest and emit readiness transitions.

    All source tasks finish before the immutable manifest is published. The
    callbacks still expose readiness in dependency order, but every callback
    observes the same exact, timestamp-validated input selection.
    """

    # Read through the loader rather than util.runtime.config: importing that
    # module pulls in util.runtime.cycle, which imports this one.
    if max_entries is None:
        max_entries = load_config("runtime")["cycle"]["ingest_max_entries"]

    state = CycleState(timestamp=dt)
    log(f"INFO: Starting shared ingest cycle for timestamp {dt}")

    detection_task = asyncio.create_task(
        _safe_ingest(
            "MRMS Detection",
            log,
            mrms_ingest.download_detection_files_async,
            mrms_ingest.download_detection_files,
            dt,
            max_entries,
            require_result=True,
        )
    )
    mrms_integration_task = asyncio.create_task(
        _safe_ingest(
            "MRMS Integration",
            log,
            mrms_ingest.download_integration_files_async,
            mrms_ingest.download_integration_files,
            dt,
            max_entries,
            require_result=True,
        )
    )
    goes_task = None
    if include_goes:
        goes_task = asyncio.create_task(
            _safe_ingest(
                "GOES",
                log,
                download_all_goes_files_async,
                download_all_goes_files,
                dt,
                require_result=True,
            )
        )
    rap_task = (
        asyncio.create_task(
            _ingest_rap(dt, log)
        )
        if include_rap
        else None
    )

    detection_result, integration_result = await asyncio.gather(
        detection_task,
        mrms_integration_task,
    )
    rap_path, rap_error = await rap_task if rap_task is not None else (None, None)
    goes_result = await goes_task if goes_task is not None else None

    records = [
        *_batch_records(detection_result),
        *_batch_records(integration_result),
    ]
    records.extend(_previous_detection_records(_batch_records(detection_result)))

    if rap_path:
        rap_analysis_time = parse_rap_analysis_time(Path(rap_path))
        if rap_analysis_time is None:
            state.errors["rap_ingest"] = (
                f"Could not parse RAP analysis timestamp from {rap_path}"
            )
        else:
            records.append(
                staged_input_from_path(
                    "RAP",
                    rap_path,
                    source="synoptic",
                    family="rap",
                    analysis_time=rap_analysis_time,
                )
            )

    if goes_result is not None:
        records.extend(_batch_records(goes_result))

    state.input_manifest = CycleInputManifest(
        cycle_time=dt,
        inputs=tuple(records),
    )
    alignment_errors = state.input_manifest.validate_alignment()
    if alignment_errors:
        state.errors["input_manifest"] = "; ".join(alignment_errors)
    else:
        log(
            "INFO: Cycle input manifest validated: "
            + ", ".join(
                f"{record.product}={record.path}@{record.analysis_time.isoformat()}"
                for record in state.input_manifest.inputs
            )
        )

    detection_ok = _batch_succeeded(detection_result) and not _manifest_family_errors(
        alignment_errors,
        "mrms",
        _batch_records(detection_result),
    )
    state.detection_inputs_ready = detection_ok
    if not detection_ok:
        state.errors["detection_ingest"] = "Detection inputs unavailable"
    if on_detection_ready is not None:
        on_detection_ready(state)

    mrms_integration_ok = _batch_succeeded(
        integration_result
    ) and not _manifest_family_errors(
        alignment_errors,
        "mrms",
        _batch_records(integration_result),
    )
    state.mrms_integration_inputs_ready = mrms_integration_ok
    if not mrms_integration_ok:
        state.errors["mrms_integration_ingest"] = "MRMS integration inputs unavailable"

    # This callback is a cycle trigger, not an aggregate MRMS readiness gate.
    # EWMRS scans each configured source directory independently and reuses or
    # renders the newest local file for that layer.  A lagging integration
    # product must not prevent unrelated, already-staged layers from rendering.
    state.ewmrs_mrms_inputs_ready = include_ewmrs
    if on_ewmrs_mrms_ready is not None:
        on_ewmrs_mrms_ready(state)

    # RAP is a source input; publish integration's non-GLM prerequisites before
    # the optional Uint16 conversion below.
    rap_ok = (
        bool(rap_path)
        and "rap_ingest" not in state.errors
        and not _manifest_family_errors(alignment_errors, "rap")
    ) if include_rap else True
    state.rap_inputs_ready = rap_ok

    if include_rap and not rap_ok:
        state.errors["rap_ingest"] = rap_error or "RAP inputs unavailable"
    state.edgewarn_integration_inputs_ready = detection_ok and mrms_integration_ok and rap_ok
    if on_base_integration_ready is not None:
        on_base_integration_ready(state)

    if goes_task is not None:
        goes_ok = _batch_succeeded(goes_result) and not _manifest_family_errors(
            alignment_errors,
            "goes",
            _batch_records(goes_result),
        )
    else:
        goes_ok = True
        log("INFO: GOES ingest is decoupled from this cycle; integration readiness does not wait for GOES availability")

    if include_goes and not goes_ok:
        state.errors["goes_ingest"] = "GOES inputs unavailable"

    # Decomposition Phase 4: the RAP Uint16 conversion is an EWMRS-owned
    # derived artifact, executed by the EWMRS service after it consumes a
    # committed rap-ready record. It no longer runs in this coordinator.

    state.ewmrs_goes_inputs_ready = (
        include_ewmrs
        and include_goes
        and goes_ok
    )
    if include_ewmrs and include_goes and not state.ewmrs_goes_inputs_ready:
        state.errors.setdefault(
            "ewmrs_goes_ingest",
            "EWMRS GOES inputs unavailable",
        )
    if on_ewmrs_goes_ready is not None:
        on_ewmrs_goes_ready(state)

    state.edgewarn_integration_inputs_ready = (
        state.edgewarn_integration_inputs_ready
        and (goes_ok or not include_goes)
    )
    if not state.edgewarn_integration_inputs_ready:
        state.errors.setdefault(
            "edgewarn_integration_ingest",
            "EdgeWARN integration inputs unavailable",
        )
    if on_edgewarn_integration_ready is not None:
        on_edgewarn_integration_ready(state)

    log(f"INFO: Shared ingest cycle finished for timestamp {dt}")
    return state


def _batch_succeeded(result) -> bool:
    """A phase batch is ready only when every requested MRMS product staged.

    Production ingest must return an explicit structured batch result.
    """
    if isinstance(result, DownloadBatchResult):
        return result.successful
    return False


def _batch_records(result) -> tuple[StagedInput, ...]:
    if not isinstance(result, DownloadBatchResult):
        return ()
    return tuple(
        record
        for record in result.downloaded
        if isinstance(record, StagedInput)
    )


def _previous_detection_records(
    current_records: tuple[StagedInput, ...],
) -> tuple[StagedInput, ...]:
    """Pin one prior encoded-time file for each detection product."""
    previous = []
    for current in current_records:
        candidates = []
        try:
            for path in current.local_path.parent.iterdir():
                if not path.is_file() or path == current.local_path:
                    continue
                analysis_time = parse_file_analysis_time(path)
                if analysis_time is None or analysis_time >= current.analysis_time:
                    continue
                candidates.append((analysis_time, path))
        except OSError:
            continue

        if not candidates:
            continue
        analysis_time, path = max(candidates, key=lambda item: item[0])
        previous.append(
            staged_input_from_path(
                current.product,
                path,
                source="local-history",
                family=current.family,
                analysis_time=analysis_time,
                role="previous",
            )
        )
    return tuple(previous)


def _manifest_family_errors(
    errors: tuple[str, ...],
    family: str,
    records: tuple[StagedInput, ...] = (),
) -> bool:
    if not errors:
        return False
    products = {record.product for record in records}
    if products:
        return any(error.split(":", 1)[0] in products for error in errors)
    family_products = {
        "rap": {"RAP"},
    }.get(family, set())
    return any(error.split(":", 1)[0] in family_products for error in errors)
