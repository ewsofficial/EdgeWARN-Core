"""Domain-wide swept StormProb probability field: raster, format, publication."""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from EdgeWARN.ctam.builtins.stormprob import BuiltinStormProbAdapter, StormProbCycleService
from EdgeWARN.stormprob import field, onnx_runtime, postprocess
from EdgeWARN.stormprob.database import StormProbRepository, field_path
from EdgeWARN.stormprob.records import build_observation_record


def _naive_swept(radii, centres, east, north):
    """Per-member, per-instant reference over one 1D axis pair."""
    radii = radii[0]
    centres = centres[0]
    blocks = [(radii[:, 0], centres[:, 0])]
    for lead in range(3):
        for fraction in (np.float32(1 / 3), np.float32(2 / 3), np.float32(1)):
            remaining = np.float32(1) - fraction
            blocks.append((radii[:, lead] * remaining + radii[:, lead + 1] * fraction,
                           centres[:, lead] * remaining + centres[:, lead + 1] * fraction))
    members = np.zeros((postprocess.ENSEMBLE_SIZE, north.size, east.size), dtype=bool)
    east_grid, north_grid = np.meshgrid(east, north, indexing="xy")
    for member_radii, member_centres in blocks:
        for member in range(postprocess.ENSEMBLE_SIZE):
            dx = east_grid - member_centres[member, 0]
            dy = north_grid - member_centres[member, 1]
            angle = np.mod(np.arctan2(dy, dx), np.float32(2 * math.pi)) * np.float32(
                64 / (2 * math.pi))
            floor = np.floor(angle)
            lo = floor.astype(np.int64) % 64
            hi = (lo + 1) % 64
            fraction = angle - floor
            profile = member_radii[member]
            interpolated = profile[lo] + (profile[hi] - profile[lo]) * fraction
            members[member] |= np.hypot(dx, dy) <= interpolated
    packed = np.zeros((north.size, east.size), dtype=np.uint32)
    for member in range(postprocess.ENSEMBLE_SIZE):
        packed |= members[member].astype(np.uint32) << np.uint32(member)
    return packed


def test_swept_member_mask_matches_naive_reference():
    rng = np.random.default_rng(11)
    radii = rng.uniform(2.0, 12.0, (1, 20, 4, 64)).astype(np.float32)
    centres = rng.uniform(-20.0, 20.0, (1, 20, 4, 2)).astype(np.float32)
    axis = np.arange(-30, 31, 1.0, dtype=np.float32)
    mask = postprocess.swept_member_mask(radii, centres, axis, axis)
    np.testing.assert_array_equal(mask, _naive_swept(radii, centres, axis, axis))


def test_reach_bucketing_matches_the_naive_reference_on_a_wide_spread():
    rng = np.random.default_rng(23)
    scale = rng.uniform(1.0, 30.0, (1, 20, 1, 1)).astype(np.float32)
    radii = scale * rng.uniform(0.85, 1.0, (1, 20, 4, 64)).astype(np.float32)
    centres = rng.uniform(-60.0, 60.0, (1, 20, 4, 2)).astype(np.float32)
    axis = np.arange(-140, 141, 1.0, dtype=np.float32)
    reach = radii[0].reshape(postprocess.ENSEMBLE_SIZE, -1).max(axis=1)
    assert len(postprocess._reach_buckets(reach)) > 1
    mask = postprocess.swept_member_mask(radii, centres, axis, axis)
    np.testing.assert_array_equal(mask, _naive_swept(radii, centres, axis, axis))


def test_split_bands_and_reused_workspace_match_the_naive_reference(monkeypatch):
    """Tiny bands and a workspace reused across patch sizes keep the raster exact."""
    rng = np.random.default_rng(29)
    small_axis = np.arange(-30, 31, 1.0, dtype=np.float32)
    wide_axis = np.arange(-70, 71, 1.0, dtype=np.float32)
    cases = []
    for axis in (wide_axis, small_axis, wide_axis):
        radii = rng.uniform(2.0, 25.0, (1, 20, 4, 64)).astype(np.float32)
        centres = rng.uniform(-25.0, 25.0, (1, 20, 4, 2)).astype(np.float32)
        cases.append((radii, centres, axis))
    expected = [_naive_swept(radii, centres, axis, axis) for radii, centres, axis in cases]
    # One row of a narrow bucket per band forces every band-boundary path.
    monkeypatch.setattr(postprocess, "_RASTER_BAND_ELEMENTS", 1)
    for (radii, centres, axis), reference in zip(cases, expected):
        mask = postprocess.swept_member_mask(radii, centres, axis, axis)
        np.testing.assert_array_equal(mask, reference)


def test_hand_case_covers_axis_from_seven_to_sixty_eight():
    radii = np.full((1, 20, 4, 64), 8.0, np.float32)
    centres = np.zeros((1, 20, 4, 2), np.float32)
    centres[0, :, :, 0] = np.asarray([15.0, 30.0, 45.0, 60.0], np.float32)
    east = np.arange(-10, 75, 1.0, dtype=np.float32)
    north = np.zeros(1, dtype=np.float32)
    counts = np.bitwise_count(postprocess.swept_member_mask(radii, centres, east, north))[0]
    covered = east[counts == postprocess.ENSEMBLE_SIZE]
    assert covered.min() == 7.0
    assert covered.max() == 68.0
    assert np.all(counts[east < 6.5] == 0)
    assert counts[np.searchsorted(east, 0.0)] == 0


def test_radii_are_floored_at_the_model_minimum():
    radii = np.full((1, 20, 4, 64), -3.0, np.float32)
    centres = np.zeros((1, 20, 4, 2), np.float32)
    axis = np.asarray([-0.5, 0.0, 0.5], dtype=np.float32)
    counts = np.bitwise_count(postprocess.swept_member_mask(radii, centres, axis, axis))
    assert counts[1, 1] == postprocess.ENSEMBLE_SIZE
    assert counts[:, 0].max() == 0
    assert counts[:, 2].max() == 0


def test_ray_index_stays_in_range_just_below_zero_angle():
    """A tiny negative angle rounds to 64 rays if the wrap happens in float32."""
    radii = np.full((1, postprocess.ENSEMBLE_SIZE, 4, 64), 10.0, np.float32)
    centres = np.zeros((1, postprocess.ENSEMBLE_SIZE, 4, 2), np.float32)
    east = np.asarray([1.0], dtype=np.float32)
    north = np.asarray([-1e-9], dtype=np.float32)
    mask = postprocess.swept_member_mask(radii, centres, east, north)
    assert mask.shape == (1, 1)
    assert int(np.bitwise_count(mask)[0, 0]) == postprocess.ENSEMBLE_SIZE


def test_sweep_instants_follow_the_configured_step():
    radii = np.zeros((1, postprocess.ENSEMBLE_SIZE, 4, 64), np.float32)
    centres = np.zeros((1, postprocess.ENSEMBLE_SIZE, 4, 2), np.float32)
    assert postprocess.SWEEP_STEP_MINUTES == 5
    assert len(postprocess.SWEEP_FRACTIONS) == 3
    assert len(list(postprocess._sweep_instants(radii, centres))) == 10


def _outputs():
    mean = np.zeros((1, 4, 33), np.float32)
    mean[..., 0] = 5.0
    residual = np.zeros((1, 4, 2), np.float32)
    residual[0, :, 0] = 1.0
    return {
        "coefficient_mean": mean,
        "coefficient_log_std": np.zeros((1, 4, 33), np.float32),
        "residual_motion_mps": residual,
        "centroid_offset_km": np.zeros((1, 4, 2), np.float32),
        "centroid_cholesky": np.full((1, 4, 2, 2), 0.5, np.float32),
    }


def _cell(when="2024-05-01T12:00:00"):
    cell = {"id": 101, "timestamp": when, "centroid": [35.0, 265.0],
            "bbox": [[34.99, 264.99], [34.99, 265.01],
                     [35.01, 265.01], [35.01, 264.99]],
            "properties": {"wind_field": {"u850": 5.0, "v850": 2.0}}}
    cell["stormprob"] = {"observation": build_observation_record(cell)}
    return cell


def _domain(rows=100, cols=100):
    return field.Domain(30.0, 260.0, 30.0 + rows / 100, 260.0 + cols / 100)


def test_member_wise_union_counts_members_not_cells():
    domain = _domain()
    first = np.full((3, 3), (1 << 10) - 1, dtype=np.uint32)
    second = np.full((3, 3), ((1 << 20) - 1) ^ ((1 << 10) - 1), dtype=np.uint32)
    payload = field.encode_field(
        [{"row": 10, "col": 10, "mask": first},
         {"row": 11, "col": 11, "mask": second}],
        "2024-05-01T12:00:00Z", domain=domain)
    counts = field.decode_field(payload)["member_count"]
    assert counts[1, 1] == 20
    assert counts[0, 0] == 10
    assert counts[3, 3] == 10
    assert counts[3, 0] == 0
    assert field.decode_header(payload)["cell_count"] == 2


def test_format_round_trip_is_deterministic_and_rows_step_south():
    domain = _domain()
    mask = np.zeros((4, 5), dtype=np.uint32)
    mask[:] = (1 << 20) - 1
    patches = [{"row": 40, "col": 60, "mask": mask}]
    payload = field.encode_field(patches, "2024-05-01T12:00:00Z", domain=domain)
    assert payload.startswith(field.MAGIC)
    assert payload == field.encode_field(patches, "2024-05-01T12:00:00Z", domain=domain)
    decoded = field.decode_field(payload)
    assert decoded["window_start_minutes"] == 15
    assert decoded["window_end_minutes"] == 60
    assert decoded["ensemble_size"] == 20
    assert decoded["analysis_unix_ms"] == 1714564800000
    # Header coordinates are cell centres: the north-west domain pixel and the
    # north-west crop pixel (row 40 counts south from 31.0 N).
    assert decoded["domain_bounds"] == pytest.approx((30.995, 260.005, 0.01, 0.01), rel=1e-6)
    assert decoded["crop_origin"] == pytest.approx((30.595, 260.605), rel=1e-6)
    assert decoded["crop_shape"] == (4, 5)
    np.testing.assert_array_equal(decoded["member_count"], np.full((4, 5), 20, np.uint8))
    # Row 0 is the northernmost row, so a crop below it has a lower origin.
    assert decoded["crop_origin"][0] < decoded["domain_bounds"][0]


def test_quiet_scan_writes_only_the_header():
    payload = field.encode_field([], "2024-05-01T12:00:00Z", domain=_domain())
    assert len(payload) == field.HEADER_SIZE == 64
    decoded = field.decode_field(payload)
    assert decoded["crop_shape"] == (0, 0)
    assert decoded["flags"] & field.FLAG_ACTIVE_CROP == 0
    assert decoded["member_count"].shape == (0, 0)
    assert decoded["cell_count"] == 0


def test_each_field_is_built_from_one_scan_only():
    domain = _domain()
    mask = np.full((2, 2), 1 << 3, dtype=np.uint32)
    patch = {"row": 5, "col": 5, "mask": mask}
    with_field = field.encode_field([patch], "2024-05-01T12:00:00Z", domain=domain)
    without_field = field.encode_field([], "2024-05-01T12:05:00Z", domain=domain)
    assert field.decode_field(with_field)["member_count"].max() == 1
    assert field.decode_field(without_field)["crop_shape"] == (0, 0)
    # Rebuilding the later scan cannot inherit the earlier scan's coverage.
    assert field.decode_field(
        field.encode_field([], "2024-05-01T12:05:00Z", domain=domain)
    )["member_count"].size == 0


def test_patch_outside_the_domain_is_rejected():
    with pytest.raises(ValueError, match="escapes"):
        field.encode_field([{"row": 99, "col": 0, "mask": np.ones((5, 1), np.uint32)}],
                           "2024-05-01T12:00:00Z", domain=_domain())


def test_field_path_and_artifact_name_share_the_canonical_cycle_key():
    assert field_path("2024-05-01T12:00:00Z").name == "stormprob_field_20240501-120000.bin.gz"
    assert field_path("20240501-120000").name == field_path("2024-05-01T12:00:00+00:00").name
    assert field.cycle_key("2024-05-01T12:00:00Z") == "20240501-120000"


def test_banded_combine_matches_full_plane_reference():
    domain = _domain(rows=300, cols=4)
    tall = np.zeros((255, 2), dtype=np.uint32)
    tall[:] = 0b11
    straddling = np.zeros((11, 2), dtype=np.uint32)
    straddling[:] = 0b1100
    patches = [{"row": 0, "col": 0, "mask": tall},
               {"row": 250, "col": 1, "mask": straddling}]
    decoded = field.decode_field(
        field.encode_field(patches, "2024-05-01T12:00:00Z", domain=domain))["member_count"]
    reference = np.zeros((261, 3), dtype=np.uint32)
    reference[0:255, 0:2] |= tall
    reference[250:261, 1:3] |= straddling
    np.testing.assert_array_equal(decoded, np.bitwise_count(reference).astype(np.uint8))
    assert decoded[250, 1] == 4
    assert decoded[100, 0] == 2
    assert decoded[100, 2] == 0


def test_wide_crop_band_budget_bounds_the_working_set():
    domain = field.Domain(20.0, 230.0, 35.0, 300.0)
    patches = [
        {"row": 0, "col": 0, "mask": np.ones((2, 2), np.uint32)},
        {"row": domain.rows - 2, "col": domain.cols - 2, "mask": np.ones((2, 2), np.uint32)},
    ]
    assert field._band_rows(domain.cols) < field.BAND_ROWS
    import tracemalloc

    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    field.encode_field(patches, "2024-05-01T12:00:00Z", domain=domain)
    peak = tracemalloc.get_traced_memory()[1] - baseline
    tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"{peak / 1024 / 1024:.1f} MB working set"


def test_cell_patch_projects_separably_onto_the_domain_grid():
    domain = _domain()
    radii = np.full((1, 20, 4, 64), 5.0, np.float32)
    centres = np.zeros((1, 20, 4, 2), np.float32)
    patch = field.cell_patch(radii, centres, (30.5, 260.5), domain=domain)
    mask = patch["mask"]
    # Row 49 is centred at 30.505 N and column 49 at 260.495 E.
    centre_row = 49 - patch["row"]
    centre_col = 49 - patch["col"]
    assert np.bitwise_count(mask[centre_row, centre_col]) == 20
    assert mask[0, 0] == 0
    # The east axis grows with the column: a 5 km radius is gone six 0.01-degree cells east.
    assert np.bitwise_count(mask[centre_row, centre_col + 6]) == 0
    # At 30.5 N a kilometre spans more longitude cells than latitude cells.
    assert mask.shape[1] > mask.shape[0]


def test_default_domain_matches_the_mrms_conus_grid():
    domain = field.Domain(20.0, 230.0, 55.0, 300.0)
    assert (domain.rows, domain.cols) == (3500, 7000)
    # MRMS first grid point and last grid point, both cell centres.
    assert domain.bounds == pytest.approx((54.995, 230.005, 0.01, 0.01))
    assert domain.row_latitude(domain.rows - 1) == pytest.approx(20.005)
    assert domain.col_longitude(domain.cols - 1) == pytest.approx(299.995)


def test_cell_patch_rows_run_north_to_south():
    domain = _domain()
    radii = np.full((1, 20, 4, 64), 1.0, np.float32)
    centres = np.zeros((1, 20, 4, 2), np.float32)
    centres[..., 1] = 5.0  # every member sits 5 km north of the centroid
    centroid = (domain.row_latitude(50), domain.col_longitude(50))
    patch = field.cell_patch(radii, centres, centroid, domain=domain)
    rows, cols = np.nonzero(np.bitwise_count(patch["mask"]))
    covered_rows = rows + patch["row"]
    # 5 km north is about 4.5 rows, so coverage sits at lower row indices.
    assert covered_rows.max() < 50
    assert 44 <= int(np.round(covered_rows.mean())) <= 46
    assert int(np.round((cols + patch["col"]).mean())) == 50


def test_adapter_attaches_a_transient_field_patch(tmp_path, monkeypatch):
    repository = StormProbRepository(tmp_path)
    cell = _cell()
    repository.commit_cycle("cycle1", cell["timestamp"], [cell])
    monkeypatch.setattr(onnx_runtime, "load_sessions", lambda *args: (object(), object()))
    monkeypatch.setattr(onnx_runtime, "infer_pair", lambda *args, **kwargs: _outputs())
    adapter = BuiltinStormProbAdapter(StormProbCycleService(repository))
    adapter.run(cell)
    result = cell["modules"]["StormProb"]
    assert result["field_status"] == "ok"
    assert cell["stormprob"]["field_patch"]["mask"].dtype == np.uint32
    assert adapter.last_batch_timing["field_seconds"] >= 0.0


def test_raster_failure_never_changes_leads_or_alerts(tmp_path, monkeypatch):
    repository = StormProbRepository(tmp_path)
    cell = _cell()
    repository.commit_cycle("cycle1", cell["timestamp"], [cell])
    monkeypatch.setattr(onnx_runtime, "load_sessions", lambda *args: (object(), object()))
    monkeypatch.setattr(onnx_runtime, "infer_pair", lambda *args, **kwargs: _outputs())
    monkeypatch.setattr(field, "cell_patch",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("raster")))
    adapter = BuiltinStormProbAdapter(StormProbCycleService(repository))
    adapter.run(cell)
    result = cell["modules"]["StormProb"]
    assert result["field_status"] == "error"
    assert result["field_error"] == "raster"
    assert cell["stormprob"]["field_error"] == "raster"
    assert "field_patch" not in cell.get("stormprob", {})
    assert result["status"] in {"success", "partial"}
    assert len(result["leads"]) == 4


def test_publication_writes_the_gzipped_field_artifact(tmp_path, monkeypatch):
    import util.file as fs
    from EdgeWARN.process.integrate import pipeline

    monkeypatch.setattr(fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(fs, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(fs, "CELL_DIR", tmp_path / "data" / "cells")
    monkeypatch.setattr(fs, "STORMCELL_DIR", tmp_path / "data" / "stormcells")
    monkeypatch.setattr(fs, "STORMPROB_FIELDS_DIR", tmp_path / "data" / "stormprob" / "fields")
    monkeypatch.setattr(pipeline, "_update_api_indexes", lambda *args: None)
    cell = _cell()
    mask = np.zeros((3, 3), dtype=np.uint32)
    mask[:] = 0b1111
    cell["stormprob"]["field_patch"] = {"row": 100, "col": 200, "mask": mask}
    cell["modules"] = {"StormProb": {"status": "success", "leads": [], "field_status": "ok"}}
    snapshot = fs.STORMCELL_DIR / "stormcells_20240501-120000.json"
    pipeline._publish_cycle(None, cell["timestamp"], [cell], snapshot, False)
    artifact = field_path(cell["timestamp"])
    assert artifact.exists()
    decoded = field.decode_field(artifact.read_bytes())
    assert decoded["cell_count"] == 1
    assert decoded["crop_shape"] == (3, 3)
    np.testing.assert_array_equal(decoded["member_count"], np.full((3, 3), 4, np.uint8))


@pytest.mark.benchmark
def test_swept_field_latency_and_working_set():
    """Plan targets: <=3 ms/cell, <400 ms for 100 cells, <8 MB working set."""
    rng = np.random.default_rng(5)
    radii = np.full((1, 20, 4, 64), 8.0, np.float32)
    radii *= rng.uniform(0.9, 1.1, radii.shape).astype(np.float32)
    centres = np.zeros((1, 20, 4, 2), np.float32)
    centres[0, :, :, 0] = np.arange(1, 5) * 15.0
    axis = np.arange(-70, 71, 1.0, dtype=np.float32)
    postprocess.swept_member_mask(radii, centres, axis, axis)

    durations = []
    for _ in range(20):
        started = time.perf_counter()
        postprocess.swept_member_mask(radii, centres, axis, axis)
        durations.append(time.perf_counter() - started)
    median_seconds = float(np.median(durations))
    assert median_seconds <= 0.003, f"{median_seconds * 1000:.2f} ms per cell"

    domain = field.Domain(20.0, 260.0, 35.0, 280.0)
    patches = [{"row": 10 + index, "col": 10, "mask": np.zeros((140, 140), np.uint32)}
               for index in range(100)]
    scan_started = time.perf_counter()
    payload = field.encode_field(patches, "2024-05-01T12:00:00Z", domain=domain)
    scan_seconds = time.perf_counter() - scan_started
    assert scan_seconds < 0.4, f"{scan_seconds * 1000:.0f} ms for 100 cells"
    assert payload.startswith(field.MAGIC)

    import tracemalloc

    large = field.Domain(20.0, 230.0, 35.0, 250.0)
    corner_patches = [
        {"row": 0, "col": 0, "mask": np.ones((2, 2), np.uint32)},
        {"row": large.rows - 2, "col": large.cols - 2, "mask": np.ones((2, 2), np.uint32)},
    ]
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    field.encode_field(corner_patches, "2024-05-01T12:00:00Z", domain=large)
    peak = tracemalloc.get_traced_memory()[1] - baseline
    tracemalloc.stop()
    assert large.rows * large.cols > 1_500_000
    assert peak < 8 * 1024 * 1024, f"{peak / 1024 / 1024:.1f} MB working set"
