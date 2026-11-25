"""The vectorized radial profile must be bit-identical to the per-ray loop."""

import math

import numpy as np
import pytest

from EdgeWARN.stormprob.geometry import MAX_RADIUS_KM, N_RAYS, polygon_to_radial_profile


def _reference(local_en, n_angles=N_RAYS, max_radius_km=MAX_RADIUS_KM):
    """The original per-ray, per-edge loop, kept verbatim."""
    points = np.asarray(local_en, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 3:
        return np.zeros(n_angles, dtype=np.float32)
    angles = 2.0 * math.pi * np.arange(n_angles) / n_angles
    result = np.zeros(n_angles, dtype=np.float32)
    rolled = np.roll(points, -1, axis=0)
    for index, angle in enumerate(angles):
        dx, dy = math.cos(angle), math.sin(angle)
        best = 0.0
        for start, end in zip(points, rolled):
            edge_x = end[0] - start[0]
            edge_y = end[1] - start[1]
            denominator = dx * edge_y - dy * edge_x
            if abs(denominator) < 1e-10:
                continue
            radius = (start[0] * edge_y - start[1] * edge_x) / denominator
            fraction = (start[0] * dy - start[1] * dx) / denominator
            if radius >= 0.0 and 0.0 <= fraction <= 1.0 and radius > best:
                best = float(radius)
        result[index] = min(best, max_radius_km)
    return result


def _assert_identical(points, **kwargs):
    with np.errstate(all="ignore"):  # the loop warns on non-finite input
        expected = _reference(points, **kwargs)
    actual = polygon_to_radial_profile(points, **kwargs)
    assert actual.dtype == expected.dtype == np.float32
    assert actual.tobytes() == expected.tobytes()


def _star(rng, vertices, scale):
    angles = np.sort(rng.uniform(0, 2 * np.pi, vertices))
    radii = rng.uniform(0.2, 1.0, vertices) * scale
    center = rng.normal(0, scale * 0.3, 2)
    return np.column_stack([np.cos(angles) * radii, np.sin(angles) * radii]) + center


@pytest.mark.parametrize("seed", range(40))
def test_random_polygons_match_loop(seed):
    rng = np.random.default_rng(seed)
    vertices = int(rng.choice([3, 4, 7, 25, 120, 400]))
    scale = float(rng.choice([0.5, 5.0, 40.0, 150.0]))
    _assert_identical(_star(rng, vertices, scale))
    # Arbitrary (self-intersecting, possibly not enclosing the origin) rings.
    _assert_identical(rng.normal(0, scale, (vertices, 2)))


@pytest.mark.parametrize("points", [
    [[0, 0], [1, 0], [0, 1]],                       # origin on a vertex
    [[-1, -1], [1, -1], [1, 1], [-1, 1]],            # axis-aligned square
    [[0, -1], [0, 1], [0, 2]],                       # collinear through origin
    [[1, 1], [2, 2], [3, 3]],                        # collinear, away from origin
    [[-0.0, 0.0], [0.0, -0.0], [-0.0, -0.0]],        # signed zeros
    [[1e-12, 0], [0, 1e-12], [-1e-12, 0]],           # near-zero denominators
    [[500, 500], [-500, 500], [-500, -500], [500, -500]],  # clipped at max radius
    [[np.nan, 0], [1, 1], [0, 1]],
    [[np.inf, 0], [1, 1], [0, 1]],
    [[1, 2]],                                        # too few points
    [[1, 2, 3], [4, 5, 6], [7, 8, 9]],               # wrong shape
])
def test_degenerate_polygons_match_loop(points):
    _assert_identical(points)


@pytest.mark.parametrize("n_angles", [0, 1, 3, 64, 360])
def test_other_ray_counts_and_limits_match_loop(n_angles):
    rng = np.random.default_rng(n_angles)
    points = _star(rng, 50, 30.0)
    _assert_identical(points, n_angles=n_angles)
    _assert_identical(points, n_angles=n_angles, max_radius_km=10.0)
