import json
import copy
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import util.file as fs
from common.ingest.manifest import CycleInputManifest
from util.io import IOManager
from util.performance import tracker as perf_tracker

from EdgeWARN.process.detect.tools.save import CellDataSaver
from EdgeWARN.process.integrate.config import get_datasets_config, probsevere_field_map
from EdgeWARN.process.integrate.integrate import StormCellIntegrator
from EdgeWARN.process.integrate.integrate_glm import integrate_glm
from EdgeWARN.process.integrate.integrate_rap import get_rap_output_roots, integrate_rap
from EdgeWARN.process.integrate.utils import StatFileHandler

io_manager = IOManager("[CellIntegration]")
_GLM_OUTPUT_KEYS = {"GLM_FLASH_COUNT", "GLM_TOTAL_ENERGY"}
_AZSHEAR_SUPPORT_ENABLED = False


def _run_step(step_name, action):
    try:
        perf_tracker.start(step_name)
        result = action()
        perf_tracker.stop(step_name)
        return result
    except Exception:
        if step_name in perf_tracker.active_timers:
            perf_tracker.stop(step_name)
        raise


def _selected_input_path(filepath, input_manifest):
    if input_manifest is not None:
        record = input_manifest.latest_for_directory(filepath)
        return record.local_path if record is not None else None

    latest_files = fs.latest_files(filepath, 1)
    return latest_files[-1] if latest_files else None


def _selection_label(input_manifest):
    return "pinned" if input_manifest is not None else "latest"


def _integrate_dataset_groups(integrator, cells, input_manifest=None):
    grouped_configs = defaultdict(list)
    for config in get_datasets_config():
        grouped_configs[config["filepath"]].append(config)

    result_cells = cells
    cell_contexts = integrator.build_cell_contexts(result_cells)
    for filepath, group_list in grouped_configs.items():
        name_list = [c["name"] for c in group_list]
        name_str = ", ".join(name_list)

        try:
            selected_file = _selected_input_path(filepath, input_manifest)
            if selected_file is None:
                io_manager.write_warning(f"No files found for {name_str} at {filepath}, skipping")
                continue

            io_manager.write_info(
                f"Using {_selection_label(input_manifest)} input for "
                f"{name_str}: {selected_file}"
            )

            # Manifest records intentionally retain ``Path`` values so their
            # identity is immutable through the cycle.  The statistics
            # integrator predates that contract and determines its reader via
            # ``str.endswith``; convert only at this legacy boundary.
            result_cells = _run_step(
                f"Integration - {name_str}",
                lambda: integrator.integrate_multi_stats(
                    str(selected_file),
                    result_cells,
                    group_list,
                    cell_contexts=cell_contexts,
                ),
            )
            io_manager.write_debug(f"Integration completed for {name_str}")

        except Exception as e:
            io_manager.write_error(f"Failed to integrate {name_str} data: {e}")

    return result_cells


def _integrate_azshear(integrator, cells, input_manifest=None):
    if not _AZSHEAR_SUPPORT_ENABLED:
        io_manager.write_info("AzShear support feature integration disabled")
        return cells

    try:
        low_file = _selected_input_path(fs.MRMS_AZSHEARLOW_DIR, input_manifest)
        mid_file = _selected_input_path(fs.MRMS_AZSHEARMID_DIR, input_manifest)
        if low_file and mid_file:
            io_manager.write_info(f"Integrating AzShear support features for {len(cells)} cells")
            return _run_step(
                "Integration - AzShear Features",
                lambda: integrator.integrate_azshear_features(
                    str(low_file),
                    str(mid_file),
                    cells,
                ),
            )

        io_manager.write_warning("AzShear feature extraction skipped due to missing low/mid AzShear files")
    except Exception as e:
        io_manager.write_error(f"Failed to integrate AzShear support features: {e}")

    return cells


def _integrate_probsevere(integrator, cells, input_manifest=None):
    try:
        selected_file = _selected_input_path(
            fs.MRMS_PROBSEVERE_DIR,
            input_manifest,
        )
        if selected_file is None:
            io_manager.write_warning("No ProbSevere files found, skipping ProbSevere integration")
            return cells

        with open(selected_file, "r") as f:
            probsevere_data = json.load(f)
        io_manager.write_info(
            f"Using {_selection_label(input_manifest)} ProbSevere input: "
            f"{selected_file}"
        )

        cells = _run_step(
            "Integration - ProbSevere",
            lambda: integrator.integrate_probsevere(probsevere_data, cells),
        )
        io_manager.write_debug("Successfully integrated ProbSevere data")
    except Exception as e:
        io_manager.write_error(f"Failed to integrate ProbSevere data: {e}")

    return cells


def _integrate_glm(cells, input_manifest=None):
    try:
        io_manager.write_info(f"Integrating GLM data for {len(cells)} cells")
        selected_file = _selected_input_path(fs.GOES_GLM_DIR, input_manifest)
        if selected_file:
            io_manager.write_info(
                f"Using {_selection_label(input_manifest)} GLM input: "
                f"{selected_file}"
            )
            cells = _run_step("Integration - GLM", lambda: integrate_glm(cells, selected_file))
            io_manager.write_debug("Successfully integrated GLM data")
        else:
            io_manager.write_warning("No GLM files found, skipping GLM integration")

    except Exception as e:
        io_manager.write_error(f"Failed to integrate GLM data: {e}")

    return cells


def _integrate_rap(cells, input_manifest=None):
    try:
        io_manager.write_info(f"Integrating RAP data for {len(cells)} cells")
        selected_file = _selected_input_path(fs.RAP_DIR, input_manifest)
        if selected_file:
            io_manager.write_info(
                f"Using {_selection_label(input_manifest)} RAP input: "
                f"{selected_file}"
            )
            cells = _run_step("Integration - RAP", lambda: integrate_rap(cells, selected_file, io_manager))
            io_manager.write_debug("Successfully integrated RAP data")
        else:
            io_manager.write_warning("No RAP files found, skipping RAP integration")
    except Exception as e:
        io_manager.write_error(f"Failed to integrate RAP data: {e}")

    return cells


def _clone_cells_for_worker(cells, preserve_properties=False):
    worker_cells = []
    for cell in cells:
        worker_cell = {"id": cell.get("id"), "properties": {}}
        for key in ("bbox", "centroid", "timestamp"):
            if key in cell:
                worker_cell[key] = copy.deepcopy(cell[key])

        if preserve_properties:
            worker_cell["properties"] = copy.deepcopy(cell.get("properties", {}))

        worker_cells.append(worker_cell)

    return worker_cells


def _stringify_cell_id(cell):
    cell_id = cell.get("id")
    if cell_id is None:
        return None
    return str(cell_id)


def _extract_patch_for_keys(worker_cells, owned_keys):
    patch = {}
    for cell in worker_cells:
        cell_id = _stringify_cell_id(cell)
        if cell_id is None:
            continue

        props = cell.get("properties", {})
        delta = {key: props[key] for key in owned_keys if key in props}
        if delta:
            patch[cell_id] = delta

    return patch


def _merge_property_value(existing_value, incoming_value):
    if isinstance(existing_value, dict) and isinstance(incoming_value, dict):
        for key, value in incoming_value.items():
            if key in existing_value:
                existing_value[key] = _merge_property_value(existing_value[key], value)
            else:
                existing_value[key] = copy.deepcopy(value)
        return existing_value

    return copy.deepcopy(incoming_value)


def _merge_property_patch(result_cells, patch):
    if not patch:
        return result_cells

    cell_index = {}
    for cell in result_cells:
        cell_id = _stringify_cell_id(cell)
        if cell_id is not None:
            cell_index[cell_id] = cell

    for cell_id, delta in patch.items():
        target_cell = cell_index.get(cell_id)
        if target_cell is None:
            io_manager.write_warning(f"Worker patch returned unknown cell id {cell_id}, ignoring")
            continue

        target_props = target_cell.setdefault("properties", {})
        for key, value in delta.items():
            if key in target_props:
                target_props[key] = _merge_property_value(target_props[key], value)
            else:
                target_props[key] = copy.deepcopy(value)

    return result_cells


def _stats_owned_keys():
    return {config["key"] for config in get_datasets_config()}


def _support_owned_keys():
    return set(probsevere_field_map().keys()) | set(_GLM_OUTPUT_KEYS) | set(get_rap_output_roots())


def _run_enrichment_serial(
    integrator,
    cells,
    *,
    include_glm=True,
    include_rap=True,
    input_manifest=None,
):
    result_cells = cells
    result_cells = _integrate_dataset_groups(
        integrator,
        result_cells,
        input_manifest,
    )
    if _AZSHEAR_SUPPORT_ENABLED:
        result_cells = _integrate_azshear(integrator, result_cells, input_manifest)
    result_cells = _integrate_probsevere(integrator, result_cells, input_manifest)
    if include_glm:
        result_cells = _integrate_glm(result_cells, input_manifest)
    if include_rap:
        result_cells = _integrate_rap(result_cells, input_manifest)
    return result_cells


def _run_parallel_enrichment(
    integrator,
    cells,
    *,
    include_glm=True,
    include_rap=True,
    input_manifest=None,
):
    if not cells:
        return cells

    stats_keys = _stats_owned_keys()
    support_keys = _support_owned_keys()

    def run_stats_worker():
        worker_cells = _clone_cells_for_worker(cells)
        worker_result = _run_step(
            "Integration - Worker Stats",
            lambda: _integrate_dataset_groups(
                integrator,
                worker_cells,
                input_manifest,
            ),
        )
        return _extract_patch_for_keys(worker_result, stats_keys)

    def run_azshear_worker():
        worker_cells = _clone_cells_for_worker(cells, preserve_properties=True)
        worker_result = _run_step(
            "Integration - Worker AzShear",
            lambda: _integrate_azshear(
                integrator,
                worker_cells,
                input_manifest,
            ),
        )
        return _extract_patch_for_keys(worker_result, {"azshear"})

    def run_support_worker():
        worker_cells = _clone_cells_for_worker(cells)

        def _run():
            staged_cells = _integrate_probsevere(
                integrator,
                worker_cells,
                input_manifest,
            )
            if include_glm:
                staged_cells = _integrate_glm(staged_cells, input_manifest)
            if include_rap:
                staged_cells = _integrate_rap(staged_cells, input_manifest)
            return staged_cells

        worker_result = _run_step("Integration - Worker Support", _run)
        return _extract_patch_for_keys(worker_result, support_keys)

    future_order = ["stats", "support"]
    if _AZSHEAR_SUPPORT_ENABLED:
        future_order.insert(1, "azshear")
    patches = {name: {} for name in future_order}

    with ThreadPoolExecutor(max_workers=len(future_order)) as executor:
        futures = {
            "stats": executor.submit(run_stats_worker),
            "support": executor.submit(run_support_worker),
        }
        if _AZSHEAR_SUPPORT_ENABLED:
            futures["azshear"] = executor.submit(run_azshear_worker)

        for worker_name in future_order:
            try:
                patches[worker_name] = futures[worker_name].result()
                io_manager.write_debug(f"[{worker_name}] worker completed")
            except Exception as exc:
                io_manager.write_error(f"[{worker_name}] worker failed: {exc}")
                patches[worker_name] = {}

    def _merge_all():
        merged_cells = cells
        for worker_name in future_order:
            merged_cells = _merge_property_patch(merged_cells, patches[worker_name])
        return merged_cells

    return _run_step("Integration - Merge", _merge_all)


def _run_ctam_if_enabled(cells, timestamp, disable_ctam, json_path=None, input_manifest=None, disable_ctam_modules=False):
    if disable_ctam:
        io_manager.write_info("CTAM module execution disabled via command-line flag")
        return cells

    try:
        from EdgeWARN.ctam.run import run_ctam

        io_manager.write_info(f"Running CTAM modules for {len(cells)} cells")
        cycle_id = timestamp
        try:
            cycle_id = datetime.fromisoformat(str(timestamp)).strftime("%Y%m%d-%H%M%S")
        except (TypeError, ValueError):
            pass
        cells = _run_step(
            "Integration - CTAM",
            lambda: run_ctam(
                cells,
                timestamp=cycle_id,
                json_path=json_path,
                input_manifest=input_manifest,
                disable_ctam_modules=disable_ctam_modules,
            ),
        )
        io_manager.write_debug("CTAM module execution completed successfully")
    except Exception as e:
        io_manager.write_error(f"Failed to run CTAM modules: {e}")

    return cells


def _save_cells(handler, timestamp, cells, json_path):
    data = CellDataSaver(None, None, None, None, None, None).create_json_structure(timestamp, cells)
    handler.write_json(data, json_path)


def _publish_cycle(handler, timestamp, cells, json_path, remove_old_cells):
    """Publish snapshot and active histories first, then make indexes visible."""
    from EdgeWARN.ctam.publication import CTAMPublicationCoordinator
    from .history import CellHistoryManager

    snapshot = CellDataSaver(None, None, None, None, None, None).create_json_structure(timestamp, cells)
    histories = CellHistoryManager(io_manager).prepare_cell_history_updates(cells)
    payloads = {json_path: snapshot, **histories}
    coordinator = CTAMPublicationCoordinator(fs.DATA_DIR / "ctam" / "transactions")
    coordinator.recover()
    coordinator.publish(payloads, publish_indexes=lambda: _update_api_indexes(cells, remove_old_cells), transaction_id=str(timestamp).replace(":", "-"))


def _update_history(cells, timestamp):
    try:
        from .history import CellHistoryManager

        history_manager = CellHistoryManager(io_manager)
        _run_step("Integration - History", lambda: history_manager.update_cell_histories(cells, timestamp))
    except Exception as e:
        io_manager.write_error(f"Failed to update cell history: {e}")


def _update_api_indexes(cells, remove_old_cells):
    try:
        from EdgeWARN.api_integration.index_manager import APIIndexManager

        api_index = APIIndexManager(io_manager, remove_old_cells=remove_old_cells)

        active_cell_ids = [cell["id"] for cell in cells if "timestamp" in cell]

        def _update():
            api_index.update_cell_index(active_cell_ids)
            api_index.cleanup_inactive_cells()

        _run_step("Integration - API Index", _update)
    except Exception as e:
        io_manager.write_error(f"Failed to update API indexes: {e}")


def main(
    json_path=None,
    remove_old_cells=None,
    disable_ctam=False,
    disable_ctam_modules=False,
    mrms_core_only=False,
    input_manifest: CycleInputManifest | None = None,
):
    handler = StatFileHandler(io_manager)
    integrator = StormCellIntegrator(io_manager)

    if json_path is None:
        raise ValueError("json_path must be provided to integration.main")

    cells, timestamp = handler.load_json(json_path)
    result_cells = cells

    result_cells = _run_parallel_enrichment(
        integrator,
        result_cells,
        include_glm=not mrms_core_only,
        include_rap=not mrms_core_only,
        input_manifest=input_manifest,
    )
    result_cells = _run_ctam_if_enabled(
        result_cells,
        timestamp,
        disable_ctam,
        json_path=json_path,
        input_manifest=input_manifest,
        disable_ctam_modules=disable_ctam_modules,
    )

    try:
        _run_step("Integration - Publication", lambda: _publish_cycle(handler, timestamp, result_cells, json_path, remove_old_cells))
    except Exception as exc:
        io_manager.write_error(f"Failed to save integrated stormcells to {json_path}: {exc}")
        raise
