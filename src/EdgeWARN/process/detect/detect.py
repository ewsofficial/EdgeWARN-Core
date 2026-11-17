from EdgeWARN.process.detect.config import DetectionConfig
from EdgeWARN.process.detect.tools.utils import DetectionDataHandler
from EdgeWARN.process.detect.tools.gatemapper import GateMapper
from EdgeWARN.process.detect.tools.save import CellDataSaver
from concurrent.futures import ThreadPoolExecutor
from util.io import IOManager
import util.file as fs
import gc
from util.performance import tracker as perf_tracker


def detect_cells(
    radar_path,
    ps_path,
    preciptype_path,
    io_manager,
    lat_min,
    lat_max,
    lon_min,
    lon_max,
    *,
    return_probsevere=False,
    return_datasets=False,
    disable_polygon_expansion=False,
    detection_config,
    radar_obj=None,
    ps_obj=None,
    preciptype_obj=None,
):
    handler = DetectionDataHandler(
        radar_path,
        ps_path,
        preciptype_path,
        io_manager,
        lat_min,
        lat_max,
        lon_min,
        lon_max,
        radar_obj=radar_obj,
        ps_obj=ps_obj,
        preciptype_obj=preciptype_obj,
    )

    def _load_with_timing(timer_name, loader):
        perf_tracker.start(timer_name)
        try:
            return loader()
        finally:
            perf_tracker.stop(timer_name)

    with ThreadPoolExecutor(max_workers=detection_config.dataset_load_max_workers) as executor:
        radar_future = executor.submit(_load_with_timing, "Detection - Load Radar", handler.load_subset)
        ps_future = executor.submit(_load_with_timing, "Detection - Load ProbSevere", handler.load_probsevere)
        preciptype_future = executor.submit(_load_with_timing, "Detection - Load PrecipType", handler.load_preciptype)

        radar_ds = radar_future.result()
        ps_ds = ps_future.result()
        preciptype_ds = preciptype_future.result()

    if radar_ds is None:
        io_manager.write_error(f"Failed to load/subset radar data from {radar_path}")
        if return_datasets:
            empty_context = (None, None, None)
            if return_probsevere:
                return [], None, empty_context
            return [], empty_context
        if return_probsevere:
            return [], None
        return []

    if preciptype_ds is None:
         io_manager.write_warning("Failed to load precipitation type data, stratiform discrimination will be limited")

    if disable_polygon_expansion:
        io_manager.write_info("Polygon expansion disabled: using original ProbSevere geometries for cell footprints")
        saver = CellDataSaver(
            None,
            radar_ds,
            None,
            None,
            ps_ds,
            preciptype_ds,
            use_probsevere_geometry=True,
        )

        perf_tracker.start("Detection - Create Entry")
        entries = saver.create_entry()
        perf_tracker.stop("Detection - Create Entry")

        if return_datasets:
            dataset_context = (radar_ds, ps_ds, preciptype_ds)
            if return_probsevere:
                return entries, ps_ds, dataset_context
            return entries, dataset_context

        if return_probsevere:
            return entries, ps_ds

        return entries

    mapper = GateMapper(radar_ds, ps_ds, io_manager, detection_config)


    perf_tracker.start("Detection - Map Gates")
    mapped_ds = mapper.map_gates_to_polygons()
    perf_tracker.stop("Detection - Map Gates")
    
    perf_tracker.start("Detection - Expand Gates")
    expanded_ds = mapper.expand_gates(mapped_ds)
    perf_tracker.stop("Detection - Expand Gates")

    perf_tracker.start("Detection - BBox")
    bboxes = mapper.draw_bbox(expanded_ds)
    perf_tracker.stop("Detection - BBox")

    saver = CellDataSaver(
        bboxes,
        radar_ds,
        mapped_ds,
        expanded_ds,
        ps_ds,
        preciptype_ds,
    )

    # Pass physics grids to create_entry for scalar extraction
    perf_tracker.start("Detection - Create Entry")
    entries = saver.create_entry()
    perf_tracker.stop("Detection - Create Entry")


    if return_datasets:
        dataset_context = (radar_ds, ps_ds, preciptype_ds)
        if return_probsevere:
            return entries, ps_ds, dataset_context
        return entries, dataset_context

    if return_probsevere:
        return entries, ps_ds

    return entries


if __name__ == "__main__":
    from common.ingest.mrms.config import get_registry
    registry = get_registry()
    if registry is not None:
        for product in ("MergedReflectivityQCComposite_00.50", "ProbSevere", "PrecipFlag_00.00"):
            registry.require(product)
    radar_path = fs.latest_files(fs.MRMS_COMPOSITE_DIR, 1)[-1]
    ps_path = fs.latest_files(fs.MRMS_PROBSEVERE_DIR, 1)[-1]
    pt_path = fs.latest_files(fs.MRMS_PRECIPTYP_DIR, 1)[-1]
    io_manager = IOManager("[CellDetection]")
    lat_min, lat_max = 35.0, 38.0
    lon_min, lon_max = 283.0, 285.0

    detect_cells(
        radar_path,
        ps_path,
        pt_path,
        io_manager,
        lat_min,
        lat_max,
        lon_min,
        lon_max,
        detection_config=DetectionConfig.from_yaml(),
    )
