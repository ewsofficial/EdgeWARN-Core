"""
CTAM Pipeline Entry Point

Provides a single entry point for CTAM processing on storm cell data:

- The reserved built-in StormProb module runs in-process through the host
  service boundary and always runs first.
- External modules are discovered from manifests below ``ctam_modules/`` and
  executed out of process in dependency order through the internal API v1.
"""

import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from collections import Counter
from EdgeWARN.alerts import AlertManager
from . import discovery, readiness
from common.ingest.manifest import CycleInputManifest



def _run_phase1_discovery_dry_run(
    cells: List[Dict[str, Any]],
    timestamp: str,
    json_path: Optional[str],
    input_manifest: Optional[CycleInputManifest],
) -> None:
    """Discover external modules and record per-cycle readiness. Launches nothing.

    This pre-execution pass must never block the pipeline, so every failure
    here is caught and logged rather than raised.
    """
    try:
        started_at = datetime.now(timezone.utc)
        catalog = readiness.build_catalog(
            cells=cells,
            timestamp=timestamp,
            stormcell_path=json_path,
            input_manifest=input_manifest,
        )
        discovery_result = discovery.discover_modules()

        evaluations = {}
        for module in discovery_result.modules:
            if module.manifest is None:
                continue
            evaluations[module.module_id] = readiness.evaluate_requirements(
                module.manifest, catalog
            )

        status = readiness.cycle_status(
            catalog=catalog,
            discovery=discovery_result,
            evaluations=evaluations,
            state=readiness.CYCLE_STATE_REQUIREMENTS_EVALUATED,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        readiness.write_cycle_status(status)
        print(
            f"[CTAM] Discovery: root={discovery_result.root} "
            f"root_present={discovery_result.root_present} "
            f"runnable={len(discovery_result.runnable)}/{len(discovery_result.modules)}"
        )
    except Exception as e:
        print(f"[CTAM] Discovery/readiness pass failed: {e}")


def _run_external_modules(cells, timestamp, json_path, input_manifest, *, disabled=False):
    """Execute only already-discovered manifests; legacy built-ins stay separate."""
    if not timestamp or disabled:
        return cells
    try:
        from .runner import ExternalModuleRunner
        catalog = readiness.build_catalog(cells=cells, timestamp=timestamp, stormcell_path=json_path, input_manifest=input_manifest)
        discovered = discovery.discover_modules()
        manifests = {item.module_id: item.manifest for item in discovered.runnable if item.manifest is not None}
        if not manifests:
            return cells
        runner = ExternalModuleRunner(catalog=catalog, cells=cells, manifests=manifests)
        results = runner.run()
        result_states = {result.module_id: result.state for result in results}
        evaluations = {module_id: readiness.evaluate_requirements(manifest, catalog) for module_id, manifest in manifests.items()}
        required_failed = any(manifests[result.module_id].required and result.state not in {"completed", "skipped_missing_requirements"} for result in results)
        readiness.write_cycle_status(readiness.cycle_status(
            catalog=catalog, discovery=discovered, evaluations=evaluations,
            state=readiness.CYCLE_STATE_FAILED if required_failed else readiness.CYCLE_STATE_COMPLETED,
            started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
            module_states=result_states,
        ))
        for result in results:
            print(f"[CTAM] External module {result.module_id!r}: {result.state} ({result.duration_seconds:.3f}s)")
            if result.state != "completed" and manifests[result.module_id].required:
                raise RuntimeError(f"required external CTAM module {result.module_id!r} {result.state}: {result.reason}")
        # Only sealed transactions have made it into the runner's working set.
        by_id = runner.transactions.cells
        return [by_id.get(str(cell.get("id")), cell) for cell in cells]
    except Exception as exc:
        print(f"[CTAM] External module execution failed: {exc}")
        return cells


def _run_builtin_stormprob(cells):
    """Run the reserved built-in before any discovered external module.

    StormProb is deliberately not obtained from the import-time registry here:
    an operator-installed manifest cannot shadow it and every data dependency
    crosses the same narrow host-service boundary.
    """
    from .builtins import BuiltinStormProbAdapter, StormProbCycleService
    from EdgeWARN.stormprob.onnx_runtime import BATCH_SIZE
    adapter = BuiltinStormProbAdapter(StormProbCycleService())
    success_count = error_count = alert_count = 0
    for batch_start in range(0, len(cells), BATCH_SIZE):
        batch = cells[batch_start:batch_start + BATCH_SIZE]
        try:
            adapter.run_batch(batch)
        except Exception as exc:
            for cell in batch:
                cell.setdefault("modules", {})[adapter.name] = {
                    "status": "error", "error": str(exc)}
        for cell_idx, cell in enumerate(batch, batch_start):
            success_count += int(cell.get("modules", {}).get(adapter.name, {}).get("status") == "success")
            error_count += int(cell.get("modules", {}).get(adapter.name, {}).get("status") != "success")
            try:
                alert_count += adapter.publish_alerts(adapter.alerts(cell))
            except Exception as exc:
                print(f"[CTAM]   Cell {cell_idx + 1}/{len(cells)}: StormProb alerts FAILED: {exc}")
    return success_count, error_count, alert_count


def run_ctam(
    cells: List[Dict[str, Any]],
    timestamp: Optional[str] = None,
    *,
    json_path: Optional[str] = None,
    input_manifest: Optional[CycleInputManifest] = None,
    disable_ctam_modules: bool = False,
) -> List[Dict[str, Any]]:
    """
    Run CTAM on the provided storm cells.

    The reserved built-in StormProb module runs first, in-process. Discovered
    external modules then execute out of process in dependency order through
    the internal API; only their sealed transactions reach the working set.

    Args:
        cells: List of storm cell dictionaries, each with 'properties' key.
        timestamp: Optional scan timestamp (e.g. YYYYMMDD-HHMMSS) to save as an alert snapshot.
        json_path: Optional path to this cycle's stormcell snapshot, used to pin
            the cycle file catalog.
        input_manifest: Optional pinned CycleInputManifest used to build the
            cycle file catalog.

    Returns:
        The list of cells with 'modules' populated by each completed module.
    """
    start_time = time.time()
    if timestamp:
        _run_phase1_discovery_dry_run(cells, timestamp, json_path, input_manifest)

    # Clean up expired alerts before running modules
    # This prevents expired alerts from piling up on disk
    try:
        AlertManager.cleanup_expired()
    except Exception as e:
        print(f"[CTAM] Failed to clean up expired alerts: {e}")
    
    print("[CTAM] Starting CTAM pipeline...")
    print("[CTAM] Built-in modules: ['StormProb']")
    print(f"[CTAM] Processing {len(cells)} storm cell(s)...")
    
    # Step 1: Run cell-based modules
    
    cell_success_count, cell_error_count, builtin_alert_count = _run_builtin_stormprob(cells)
    
    stormprob_status_counts = {}
    stormprob_alert_eligibility_counts = {
        True: 0,
        False: 0,
        None: 0,
    }
    stormprob_alert_outcome_counts = Counter()
    stormprob_alert_blocker_counts = Counter()

    for cell in cells:
        stormprob_result = cell.get("modules", {}).get("StormProb")
        if not stormprob_result:
            continue

        status = stormprob_result.get("status", "unknown")
        stormprob_status_counts[status] = stormprob_status_counts.get(status, 0) + 1

        eligibility = stormprob_result.get("can_generate_alerts")
        if eligibility is True:
            stormprob_alert_eligibility_counts[True] += 1
        elif eligibility is False:
            stormprob_alert_eligibility_counts[False] += 1
        else:
            stormprob_alert_eligibility_counts[None] += 1

        alert_outcome = stormprob_result.get("alert_outcome")
        if alert_outcome:
            stormprob_alert_outcome_counts[alert_outcome] += 1

        for blocker in stormprob_result.get("alert_blockers", []):
            stormprob_alert_blocker_counts[str(blocker)] += 1

    if stormprob_status_counts:
        status_summary = ", ".join(
            f"{status}={count}" for status, count in sorted(stormprob_status_counts.items())
        )
        eligibility_summary = (
            f"true={stormprob_alert_eligibility_counts[True]}, "
            f"false={stormprob_alert_eligibility_counts[False]}, "
            f"none={stormprob_alert_eligibility_counts[None]}"
        )
        print(
            "[CTAM] StormProb summary: "
            f"status[{status_summary}] can_generate_alerts[{eligibility_summary}]"
        )
        if stormprob_alert_outcome_counts:
            outcome_summary = ", ".join(
                f"{name}={count}" for name, count in sorted(stormprob_alert_outcome_counts.items())
            )
            print(f"[CTAM] StormProb alert outcomes: {outcome_summary}")
        if stormprob_alert_blocker_counts:
            blocker_summary = ", ".join(
                f"{name}={count}" for name, count in sorted(stormprob_alert_blocker_counts.items())
            )
            print(f"[CTAM] StormProb alert blockers: {blocker_summary}")
    
    total_elapsed = time.time() - start_time
    print(f"[CTAM] Pipeline complete: {cell_success_count} built-in success, {cell_error_count} built-in error(s), {builtin_alert_count} alert(s) in {total_elapsed:.3f}s")
    
    # Generate timestamp snapshot of active alerts if provided
    cells = _run_external_modules(
        cells,
        timestamp,
        json_path,
        input_manifest,
        disabled=disable_ctam_modules,
    )

    if timestamp:
        try:
            AlertManager.create_snapshot(timestamp)
        except Exception as e:
            print(f"[CTAM] Failed to create alert snapshot for {timestamp}: {e}")

    return cells
