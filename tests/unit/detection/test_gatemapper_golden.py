"""expand_gates output on seeded synthetic scenes stays byte-identical.

The digests were produced by the full-grid implementation that preceded the
polygon/assigned-pixel restriction of marker, component and post-filter work,
so any change in which pixels a cell receives fails here. The scenes mix
overlapping storms, sparse label ids, NaN gates, and scattered single-pixel
polygons that the small-cluster filter rejects.
"""
import hashlib

import numpy as np
import pytest
import xarray as xr

from EdgeWARN.process.detect.config import DetectionConfig
from EdgeWARN.process.detect.tools.gatemapper import GateMapper


class _QuietIO:
    def __init__(self):
        self.debug = []

    def write_debug(self, message):
        self.debug.append(message)

    def write_warning(self, message):
        pass


def _scene(seed, size=160):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    refl = rng.normal(15.0, 8.0, (size, size))
    polygons = np.zeros((size, size), dtype=np.int32)
    for cell_id in rng.choice(np.arange(1, 500), 14, replace=False):
        cy, cx = rng.integers(10, size - 10, 2)
        radius = rng.uniform(2.0, 12.0)
        peak = rng.uniform(38.0, 70.0)
        dist = np.hypot(yy - cy, xx - cx)
        refl = np.maximum(refl, peak - (peak - 30.0) * dist / (radius * 2.5))
        polygons[dist <= radius * rng.uniform(0.3, 1.2)] = cell_id
    refl[rng.random((size, size)) < 0.01] = np.nan
    polygons[rng.random((size, size)) < 0.002] = 999
    coords = {"latitude": np.arange(size, dtype=float), "longitude": np.arange(size, dtype=float)}
    radar = xr.Dataset({"unknown": (("latitude", "longitude"), refl.astype(np.float32))}, coords=coords)
    mapped = xr.Dataset({"PolygonID": (("latitude", "longitude"), polygons)}, coords=coords)
    return radar, mapped


_GOLDEN = {
    0: (15, 5710, "69785c0ed951c357a351537db398372eaf266dd3e7b27d2f3051600d91446921"),
    1: (15, 5758, "12f7c135887bcc5fef5ce9d465e65f5af3278763516af89364f13d0e2ac68fd9"),
    2: (14, 3979, "ac9fb903fe5b3f7e7659c860d96f3e75373ae08b170882320b554c393135c50d"),
    3: (14, 3117, "e4d34d20a789b2e9c3a9e46052647f2a47417aab5c895694bf0c02a77017ff78"),
    4: (15, 4065, "ba91e84d45d1f15e52670c016bc835732578b4e740794d6948786e826ee21e5e"),
    5: (14, 2189, "43434e2d552df2799d3fcc101a894a8e2d24e5e09e117e91e6a40f4082d4bedd"),
}


@pytest.mark.parametrize("seed", sorted(_GOLDEN))
def test_expand_gates_matches_full_grid_reference(seed):
    radar, mapped = _scene(seed)
    io = _QuietIO()
    out = GateMapper(radar, None, io, DetectionConfig.from_yaml()).expand_gates(mapped)
    grid = out["PolygonID"].values

    cells, assigned, digest = _GOLDEN[seed]
    assert grid.dtype == np.int32
    assert grid.shape == mapped["PolygonID"].shape
    assert len(np.unique(grid)) - 1 == cells
    assert int(np.count_nonzero(grid)) == assigned
    assert hashlib.sha256(grid.astype("<i4").tobytes()).hexdigest() == digest
    if seed in (3, 5):
        assert "Rejecting small expanded clusters: [999]" in io.debug
