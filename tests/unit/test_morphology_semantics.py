"""Independent invariants for representative storm-cell mask shapes."""

import numpy as np

from EdgeWARN.process.detect.tools.morphology import MorphologyEngine


def _metrics(mask):
    return MorphologyEngine.process_cell(mask, np.full(mask.shape, 50.0))


def test_empty_and_tiny_masks_have_stable_fallbacks():
    assert _metrics(np.zeros((9, 9), dtype=bool)) == {}
    tiny = np.zeros((9, 9), dtype=bool)
    tiny[3:5, 3:5] = True
    assert _metrics(tiny) == {
        "linearity": 0.0,
        "branching_factor": 0,
        "solidity": 1.0,
        "defect_max_depth": 0.0,
        "defect_bearing": 0.0,
        "aspect_ratio": 1.0,
    }


def test_straight_line_is_elongated_and_unbranched():
    mask = np.zeros((31, 31), dtype=bool)
    mask[14:17, 3:28] = True
    metrics = _metrics(mask)
    assert metrics["aspect_ratio"] == 12.0
    assert metrics["branching_factor"] == 0
    assert metrics["linearity"] > 2.0


def test_branching_structure_has_junctions_and_low_solidity():
    mask = np.zeros((31, 31), dtype=bool)
    mask[4:27, 14:17] = True
    mask[14:17, 4:27] = True
    metrics = _metrics(mask)
    assert metrics["branching_factor"] >= 4
    assert metrics["solidity"] < 0.5


def test_compact_blob_is_nearly_round_and_solid():
    y, x = np.ogrid[:31, :31]
    metrics = _metrics((x - 15) ** 2 + (y - 15) ** 2 <= 8 ** 2)
    assert metrics["aspect_ratio"] == 1.0
    assert metrics["solidity"] > 0.9
    assert metrics["branching_factor"] == 0


def test_concave_shape_is_distinguished_from_compact_blob():
    mask = np.zeros((31, 31), dtype=bool)
    mask[4:27, 4:8] = True
    mask[4:27, 23:27] = True
    mask[23:27, 4:27] = True
    metrics = _metrics(mask)
    assert metrics["solidity"] < 0.5
    assert metrics["aspect_ratio"] == 1.0
