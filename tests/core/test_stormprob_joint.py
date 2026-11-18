"""Joint radial/centroid sampling, occupancy, and ONNX contract gates."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np
import pytest

from EdgeWARN.stormprob import assets, onnx_runtime, postprocess

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "stormprob_joint_parity.npz"


def _naive_occupancy(radii, centre_km=(0.0, 0.0)):
    """Single-centre full-grid reference: every member about the same centre."""
    axis = np.arange(-100, 101, dtype=np.float32)
    east, north = np.meshgrid(axis, axis, indexing="xy")
    dx, dy = east - centre_km[0], north - centre_km[1]
    angle = np.mod(np.arctan2(dy, dx), np.float32(2 * math.pi)) * np.float32(64 / (2 * math.pi))
    floor = np.floor(angle)
    lo = floor.astype(np.int64) % 64
    hi = (lo + 1) % 64
    frac = angle - floor
    distance = np.sqrt(dx * dx + dy * dy)
    out = np.zeros((4, 201, 201), np.float32)
    for lead in range(4):
        for member in range(postprocess.ENSEMBLE_SIZE):
            profile = np.maximum(radii[0, member, lead], 0.0)
            out[lead] += distance <= profile[lo] + (profile[hi] - profile[lo]) * frac
    return out / np.float32(postprocess.ENSEMBLE_SIZE)


def test_sample_joint_matches_pytorch_fixture():
    data = np.load(FIXTURE)
    radii, centres = postprocess.sample_joint(
        data["joint_mean"], data["joint_log_std"], data["joint_current_radii"],
        data["joint_base"], data["joint_offset"], data["joint_cholesky"],
        noise=(data["joint_shape_noise"], data["joint_centroid_noise"]))
    np.testing.assert_allclose(radii, data["joint_ref_radii"], atol=1e-4)
    np.testing.assert_allclose(centres, data["joint_ref_centres"], atol=1e-4)


def test_default_noise_draws_shape_then_centroid_from_one_stream():
    rng = np.random.default_rng(42)
    shape = rng.standard_normal((1, 20, 4, 33), dtype=np.float32)
    centroid = rng.standard_normal((1, 20, 4, 2), dtype=np.float32)
    args = (np.zeros((1, 4, 33)), np.zeros((1, 4, 33)), np.full((1, 64), 5.0),
            np.zeros((1, 4, 2)), np.zeros((1, 4, 2)),
            np.broadcast_to(np.eye(2, dtype=np.float32), (1, 4, 2, 2)))
    default = postprocess.sample_joint(*args)
    explicit = postprocess.sample_joint(*args, noise=(shape, centroid))
    np.testing.assert_array_equal(default[0], explicit[0])
    np.testing.assert_array_equal(default[1], explicit[1])


def test_identical_centres_match_single_centre_occupancy():
    rng = np.random.default_rng(7)
    radii = rng.uniform(2, 40, (1, 20, 4, 64)).astype(np.float32)
    centres = np.zeros((1, 20, 4, 2), np.float32)
    np.testing.assert_array_equal(postprocess.occupancy_probability(radii, centres),
                                  _naive_occupancy(radii))


def _boundary_probability(members_at_origin):
    radii = np.full((1, 20, 4, 64), 10.0, np.float32)
    # Non-origin members sit on separate lattice points, so no pixel off the
    # origin is shared by more than one member.
    lattice = [(x, y) for x in (-75, -45, 45, 75) for y in (-75, -45, 45, 75)]
    lattice += [(45, 0), (-45, 0), (0, 45), (0, -45)]
    centres = np.zeros((1, 20, 4, 2), np.float32)
    centres[0, members_at_origin:] = np.asarray(lattice[:20 - members_at_origin])[:, None]
    return postprocess.occupancy_probability(radii, centres)


def test_threshold_is_the_five_of_twenty_boundary():
    assert postprocess.THRESHOLD == 5 / postprocess.ENSEMBLE_SIZE
    assert postprocess.contour_polygon(_boundary_probability(5)[0]) is not None
    assert postprocess.contour_polygon(_boundary_probability(4)[0]) is None


def test_empty_probability_has_no_contour():
    assert postprocess.contour_polygon(np.zeros((201, 201), np.float32)) is None


def _copy_assets(tmp_path):
    directory = tmp_path / "stormprob"
    directory.mkdir()
    for name in ("manifest.json", "stormprob_radial.onnx", "stormprob_motion.onnx"):
        shutil.copy(assets.asset_dir() / name, directory / name)
    return directory


@pytest.mark.parametrize("key, model, match", [
    ("inputs", "radial", "input contract"),
    ("outputs", "radial", "output contract"),
    ("inputs", "motion", "input contract"),
])
def test_load_sessions_rejects_manifest_contract_mismatch(tmp_path, key, model, match):
    pytest.importorskip("onnxruntime")
    directory = _copy_assets(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["onnx_export"]["models"][model][key] = list(
        reversed(manifest["onnx_export"]["models"][model][key]))
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(onnx_runtime.ModelUnavailable, match=match):
        onnx_runtime.load_sessions(directory, directory / "manifest.json")


def test_load_sessions_rejects_hash_mismatch(tmp_path):
    pytest.importorskip("onnxruntime")
    directory = _copy_assets(tmp_path)
    with (directory / "stormprob_radial.onnx").open("ab") as handle:
        handle.write(b"\0")
    with pytest.raises(onnx_runtime.ModelUnavailable, match="hash-mismatched"):
        onnx_runtime.load_sessions(directory, directory / "manifest.json")


def test_onnx_and_numpy_match_pytorch_end_to_end():
    pytest.importorskip("onnxruntime")
    data = np.load(FIXTURE)
    sessions = onnx_runtime.load_sessions(assets.validate_assets(), assets.manifest_path())
    outputs = onnx_runtime.infer_pair(
        *sessions, radial_history=data["batch_radial_history"],
        statistics_history=data["batch_statistics_history"],
        current_features=data["batch_current_features"],
        history_mask=data["batch_history_mask"],
        trajectory_sequence=data["batch_trajectory_sequence"],
        trajectory_mask=data["batch_trajectory_mask"])
    for name, value in outputs.items():
        np.testing.assert_allclose(value, data[f"torch_{name}"], rtol=1e-4, atol=1e-4,
                                   err_msg=name)

    rng = np.random.default_rng(42)
    noise = (rng.standard_normal((1, 20, 4, 33), dtype=np.float32),
             rng.standard_normal((1, 20, 4, 2), dtype=np.float32))
    zero_base = np.zeros((1, 4, 2), np.float32)
    for cell in range(3):
        grids = []
        for source in (lambda name: outputs[name][cell:cell + 1],
                       lambda name: data[f"torch_{name}"][cell:cell + 1]):
            radii, centres = postprocess.sample_joint(
                source("coefficient_mean"), source("coefficient_log_std"),
                data["batch_radial_history"][cell:cell + 1, -1], zero_base,
                source("centroid_offset_km"), source("centroid_cholesky"), noise=noise)
            grids.append(postprocess.occupancy_probability(radii, centres))
        # Identical except for pixels within float rounding of a member boundary.
        assert np.count_nonzero(grids[0] != grids[1]) <= 0.001 * grids[0].size
