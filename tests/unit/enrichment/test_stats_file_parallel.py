"""The concurrent statistics-file loop must reproduce the serial loop exactly."""

import copy
import json
from collections import defaultdict
from unittest.mock import MagicMock

import threading

import eccodes
import numpy as np
import pytest
import xarray as xr

from EdgeWARN.process.integrate import pipeline
from EdgeWARN.process.integrate.integrate import StormCellIntegrator


def _write_dataset(path, seed):
    """A small MRMS-like regular lat/lon GRIB2 file (north-to-south rows)."""
    rng = np.random.default_rng(seed)
    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, value in (
            ("Ni", 101), ("Nj", 101),
            ("latitudeOfFirstGridPointInDegrees", 31.0),
            ("latitudeOfLastGridPointInDegrees", 30.0),
            ("longitudeOfFirstGridPointInDegrees", 264.0),
            ("longitudeOfLastGridPointInDegrees", 265.0),
            ("iDirectionIncrementInDegrees", 0.01),
            ("jDirectionIncrementInDegrees", 0.01),
        ):
            eccodes.codes_set(gid, key, value)
        eccodes.codes_set_values(gid, rng.uniform(-5.0, 80.0, 101 * 101))
        with open(path, "wb") as handle:
            eccodes.codes_write(gid, handle)
    finally:
        eccodes.codes_release(gid)
    return str(path)


def _write_netcdf(path, seed):
    rng = np.random.default_rng(seed)
    xr.Dataset(
        data_vars={"test_var": (["latitude", "longitude"], rng.uniform(-5.0, 80.0, (101, 101)))},
        coords={"latitude": np.linspace(30.0, 31.0, 101), "longitude": np.linspace(-96.0, -95.0, 101)},
    ).to_netcdf(path)
    return str(path)


def _cells():
    cells = []
    for index, (lat, lon) in enumerate([(30.2, -95.8), (30.5, -95.5), (30.8, -95.2), (35.0, -80.0)]):
        cells.append({
            "id": index + 1,
            "bbox": [
                [lat - 0.05, lon - 0.05],
                [lat - 0.05, lon + 0.05],
                [lat + 0.05, lon + 0.05],
                [lat + 0.05, lon - 0.05],
            ],
            "centroid": [lat, lon],
            "properties": {"existing": index, "maxVIL": -1},
        })
    # A cell with no property bag at all keeps the serial loop's key placement.
    cells.append({"id": 99, "bbox": cells[1]["bbox"], "centroid": [30.5, -95.5]})
    # A cell without geometry receives the zero results.
    cells.append({"id": 100, "properties": {}})
    return cells


def _reference_serial(integrator, cells, configs, paths):
    """The pre-parallel loop, kept verbatim apart from file selection."""
    grouped = defaultdict(list)
    for config in configs:
        grouped[config["filepath"]].append(config)
    result_cells = cells
    contexts = integrator.build_cell_contexts(result_cells)
    for filepath, group_list in grouped.items():
        selected = paths.get(filepath)
        if selected is None:
            continue
        try:
            result_cells = integrator.integrate_multi_stats(
                selected, copy.deepcopy(result_cells), group_list, cell_contexts=contexts)
        except Exception:
            pass
    return result_cells


@pytest.fixture
def stats_setup(tmp_path, monkeypatch):
    paths = {
        "A": _write_dataset(tmp_path / "a.grib2", 1),
        "B": _write_dataset(tmp_path / "b.grib2", 2),
        "C": _write_dataset(tmp_path / "c.grib2", 3),
        "N": _write_netcdf(tmp_path / "n.nc", 4),
        "MISSING": None,
    }
    configs = [
        {"name": "VIL", "filepath": "A", "key": "maxVIL", "method": "max"},
        {"name": "VIL (90th)", "filepath": "A", "key": "p90VIL", "method": "percentile", "percentile": 90},
        {"name": "Echo", "filepath": "B", "key": "maxEcho", "method": "max"},
        {"name": "Gone", "filepath": "MISSING", "key": "gone", "method": "max"},
        {"name": "Ref", "filepath": "C", "key": "maxRef", "method": "max"},
        {"name": "Ref (50th)", "filepath": "C", "key": "p50Ref", "method": "percentile", "percentile": 50},
        {"name": "Net", "filepath": "N", "key": "maxNet", "method": "max"},
        {"name": "Shared", "filepath": "N", "key": "maxVIL", "method": "max"},
    ]
    monkeypatch.setattr(pipeline, "get_datasets_config", lambda: configs)
    monkeypatch.setattr(
        pipeline, "_selected_input_path", lambda filepath, _manifest, _product=None: paths[filepath])
    return configs, paths


@pytest.mark.parametrize("workers", [1, 2, 8])
def test_parallel_stats_files_match_serial_loop(stats_setup, monkeypatch, workers):
    configs, paths = stats_setup
    monkeypatch.setattr(pipeline, "stats_file_workers", lambda: workers)
    cells = _cells()
    original = copy.deepcopy(cells)

    expected = _reference_serial(StormCellIntegrator(MagicMock()), copy.deepcopy(cells), configs, paths)
    actual = pipeline._integrate_dataset_groups(StormCellIntegrator(MagicMock()), cells)

    # json.dumps without sort_keys also pins key insertion order.
    assert json.dumps(actual) == json.dumps(expected)
    assert cells == original, "input cells must not be mutated"


def test_failed_file_is_skipped_like_the_serial_loop(stats_setup, monkeypatch):
    configs, paths = stats_setup
    monkeypatch.setattr(pipeline, "stats_file_workers", lambda: 3)
    integrator = StormCellIntegrator(MagicMock())
    real = integrator.integrate_multi_stats

    def flaky(path, cells, group_list, cell_contexts=None):
        if path == paths["B"]:
            raise RuntimeError("boom")
        return real(path, cells, group_list, cell_contexts=cell_contexts)

    integrator.integrate_multi_stats = flaky
    result = pipeline._integrate_dataset_groups(integrator, _cells())

    assert all("maxEcho" not in cell.get("properties", {}) for cell in result)
    assert all("maxVIL" in cell["properties"] and "maxRef" in cell["properties"]
               for cell in result if "properties" in cell)


def test_no_selected_files_returns_input_cells(monkeypatch):
    monkeypatch.setattr(pipeline, "get_datasets_config", lambda: [
        {"name": "VIL", "filepath": "A", "key": "maxVIL", "method": "max"}])
    monkeypatch.setattr(pipeline, "_selected_input_path", lambda *_args: None)
    cells = _cells()

    assert pipeline._integrate_dataset_groups(StormCellIntegrator(MagicMock()), cells) is cells


def test_netcdf_files_stay_on_the_calling_thread(stats_setup, monkeypatch):
    _configs, paths = stats_setup
    monkeypatch.setattr(pipeline, "stats_file_workers", lambda: 4)
    integrator = StormCellIntegrator(MagicMock())
    real = integrator.integrate_multi_stats
    threads = {}

    def spy(path, cells, group_list, cell_contexts=None):
        threads[path] = threading.current_thread()
        return real(path, cells, group_list, cell_contexts=cell_contexts)

    integrator.integrate_multi_stats = spy
    pipeline._integrate_dataset_groups(integrator, _cells())

    assert threads[paths["N"]] is threading.current_thread()
    assert any(threads[paths[name]] is not threading.current_thread() for name in "ABC")


def test_unsafe_eccodes_build_falls_back_to_serial(stats_setup, monkeypatch):
    monkeypatch.setattr(pipeline, "stats_file_workers", lambda: 4)
    monkeypatch.setattr(pipeline, "eccodes_thread_safe", lambda: False)
    integrator = StormCellIntegrator(MagicMock())
    real = integrator.integrate_multi_stats
    threads = set()

    def spy(path, cells, group_list, cell_contexts=None):
        threads.add(threading.current_thread())
        return real(path, cells, group_list, cell_contexts=cell_contexts)

    integrator.integrate_multi_stats = spy
    pipeline._integrate_dataset_groups(integrator, _cells())

    assert threads == {threading.current_thread()}
