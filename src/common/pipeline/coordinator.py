"""Shared staged-ingest coordination for tandem EdgeWARN and EWMRS execution."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass, field, replace
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
    detection_manifest: CycleInputManifest | None = None
    integration_manifest: CycleInputManifest | None = None
    ctam_manifest: CycleInputManifest | None = None
    optional_inputs_complete: bool = False
    ingest_report: dict = field(default_factory=dict)
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
    on_optional_complete: Optional[StateCallback] = None,
) -> CycleState:
    """Release mandatory phases before draining the bounded optional batch.

    Each callback owns a distinct state and immutable manifest. Final optional
    records never replace the reserved selection made for detection.
    """
    if max_entries is None:
        max_entries = load_config("runtime")["cycle"]["ingest_max_entries"]
    registry = mrms_ingest.get_registry()
    expected = {modifier or "ProbSevere" for modifier in mrms_ingest.get_detection_modifiers()}
    settings = load_config("ingest")["mrms"] if registry is None else json.loads(registry.normalized_config_json)
    deadline = settings.get("downloads", {}).get(
        "optional_timeout_seconds", settings["ncep_https"]["sync_timeout_seconds"]
    )
    state = CycleState(timestamp=dt)
    tasks = []

    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    def emit(callback):
        if callback is not None:
            callback(replace(state, errors=dict(state.errors)))

    async def optional():
        # v2 acquisition already owns per-product S3/HTTPS fallback. A second
        # whole-batch sync retry would reset its deadline and block callbacks.
        try:
            async with asyncio.timeout(deadline + (settings["ncep_https"]["sync_timeout_seconds"] if registry else 0)):
                return await mrms_ingest.download_integration_files_async(dt, max_entries)
        except Exception as exc:
            log(f"WARN: Optional MRMS acquisition terminal: {exc}")
            return None

    detection_task = start(_safe_ingest(
        "MRMS Detection", log, mrms_ingest.download_detection_files_async,
        mrms_ingest.download_detection_files, dt, max_entries, require_result=True,
    ))
    optional_task = start(optional())
    rap_task = start(_ingest_rap(dt, log)) if include_rap else None
    goes_task = start(_safe_ingest(
        "GOES", log, download_all_goes_files_async, download_all_goes_files,
        dt, require_result=True,
    )) if include_goes else None
    try:
        detection = await detection_task
        records = _batch_records(detection)
        manifest = CycleInputManifest(cycle_time=dt, inputs=records)
        present = {r.product for r in manifest.current_inputs(family="mrms")}
        state.detection_inputs_ready = (
            bool(expected) and expected <= present and _batch_succeeded(detection)
            and not manifest.validate_alignment()
        )
        if not state.detection_inputs_ready:
            state.errors["detection_ingest"] = "Detection inputs unavailable"
        records = (*records, *_previous_detection_records(records))
        state.input_manifest = state.detection_manifest = CycleInputManifest(cycle_time=dt, inputs=records)
        emit(on_detection_ready)
        state.ewmrs_mrms_inputs_ready = include_ewmrs
        emit(on_ewmrs_mrms_ready)

        rap_path, rap_error = await rap_task if rap_task else (None, None)
        rap_records = ()
        if rap_path:
            analysis_time = parse_rap_analysis_time(Path(rap_path))
            if analysis_time is not None:
                rap_records = (staged_input_from_path("RAP", rap_path, source="synoptic", family="rap", analysis_time=analysis_time),)
        state.rap_inputs_ready = not include_rap or bool(rap_records) and not CycleInputManifest(cycle_time=dt, inputs=rap_records).validate_alignment()
        if not state.rap_inputs_ready:
            state.errors["rap_ingest"] = rap_error or "RAP inputs unavailable"
        records = (*records, *rap_records)
        state.input_manifest = CycleInputManifest(cycle_time=dt, inputs=records)
        # Optional MRMS products never form a base Core barrier.
        state.mrms_integration_inputs_ready = state.detection_inputs_ready
        state.edgewarn_integration_inputs_ready = state.detection_inputs_ready and state.rap_inputs_ready
        emit(on_base_integration_ready)

        goes = await goes_task if goes_task else None
        goes_records = _batch_records(goes)
        goes_ok = not include_goes or (_batch_succeeded(goes) and not CycleInputManifest(cycle_time=dt, inputs=goes_records).validate_alignment())
        if not goes_ok:
            state.errors["goes_ingest"] = "GOES inputs unavailable"
        records = (*records, *goes_records)
        state.input_manifest = state.integration_manifest = CycleInputManifest(cycle_time=dt, inputs=records)
        state.ewmrs_goes_inputs_ready = include_ewmrs and include_goes and goes_ok
        emit(on_ewmrs_goes_ready)
        state.edgewarn_integration_inputs_ready &= goes_ok
        if not state.edgewarn_integration_inputs_ready:
            state.errors["edgewarn_integration_ingest"] = "EdgeWARN integration inputs unavailable"
        emit(on_edgewarn_integration_ready)

        optional_result = await optional_task
        optional_records = tuple(r for r in _batch_records(optional_result)
            if r.product not in expected and r.role == "current"
            and not CycleInputManifest(cycle_time=dt, inputs=(r,)).validate_alignment())
        history_anchors = list(optional_records)
        if registry is not None:
            # A previous-only consumer can use history even when this cycle's
            # optional download is absent. Anchors select history only and are
            # never included as observations in a manifest.
            present_optional = {record.product for record in optional_records}
            for spec in registry.products:
                if spec.protected or spec.product_id in present_optional:
                    continue
                history_anchors.append(StagedInput(
                    product=spec.product_id,
                    path=str(spec.directory / "history-anchor.grib2"),
                    analysis_time=state.integration_manifest.cycle_time,
                    source="selection-anchor", family="mrms", validated=False,
                ))
        state.input_manifest = state.ctam_manifest = state.integration_manifest.with_inputs(
            (*optional_records, *_previous_detection_records(tuple(history_anchors))))
        state.optional_inputs_complete = True
        outcomes = {r.product: asdict(r) for r in getattr(optional_result, "product_results", ())
                    if r.status != "not_requested"}
        ready_records = {r.product: r for r in optional_records}
        ready_products = set(ready_records)
        for modifier in mrms_ingest.get_integration_modifiers():
            product = modifier or "ProbSevere"
            record = ready_records.get(product)
            outcomes.setdefault(product, {
                "product": product,
                "status": "ready" if record else "unavailable",
                "requested_time": state.ctam_manifest.cycle_time.isoformat(),
                "registry_fingerprint": registry.fingerprint if registry else None,
                "source": record.source if record else None,
                "analysis_time": record.analysis_time.isoformat() if record else None,
                "path": record.path if record else None,
                "reason": None if record else "Optional acquisition failed, expired, or returned no valid current input",
            })
            if outcomes[product]["status"] == "ready" and product not in ready_products:
                outcomes[product].update(status="failed", reason="Input alignment validation failed")
        state.ingest_report = {
            "schema_version": 1, "cycle_time": state.ctam_manifest.cycle_time.isoformat(),
            "registry_fingerprint": registry.fingerprint if registry else None,
            "optional_outcomes": outcomes,
            "snapshots": {name: getattr(state, name + "_manifest").as_dict()
                          for name in ("detection", "integration", "ctam")},
        }
        emit(on_optional_complete)
        return state
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
    """Pin one prior encoded-time observation for each selected product."""
    previous = []
    for current in current_records:
        candidates = []
        try:
            for path in current.local_path.parent.iterdir():
                if (not path.is_file() or path == current.local_path
                        or path.suffix != current.local_path.suffix
                        or not (path.name.startswith("MRMS_" + current.product + "_")
                                or current.product == "ProbSevere" and "PROBSEVERE" in path.name.upper())):
                    continue
                analysis_time = parse_file_analysis_time(path)
                if analysis_time is None or analysis_time >= current.analysis_time:
                    continue
                candidates.append((analysis_time, path))
        except OSError:
            continue

        if not candidates:
            continue
        analysis_time, path = max(candidates, key=lambda item: (item[0], str(item[1])))
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
