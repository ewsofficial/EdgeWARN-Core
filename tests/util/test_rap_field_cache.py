"""RAPPointExtractor's decoded-field cache must not change extracted values."""

import os
import time

import eccodes
import numpy as np
import pytest

from util.grib_loader import RAPPointExtractor


def _write_rap(path, seed, *, levels=(500, 700, 850)):
    """A small multi-message GRIB2 file shaped like the RAP catalog's inputs."""
    rng = np.random.default_rng(seed)
    with open(path, "wb") as handle:
        messages = [("t", "isobaricInhPa", level) for level in levels]
        messages += [("u", "isobaricInhPa", level) for level in levels]
        messages += [("2t", "heightAboveGround", 2), ("t", "isobaricInhPa", levels[0])]
        for short_name, type_of_level, level in messages:
            gid = eccodes.codes_grib_new_from_samples("regular_ll_pl_grib2")
            try:
                for key, value in (
                    ("Ni", 41), ("Nj", 31),
                    ("latitudeOfFirstGridPointInDegrees", 45.0),
                    ("latitudeOfLastGridPointInDegrees", 30.0),
                    ("longitudeOfFirstGridPointInDegrees", 250.0),
                    ("longitudeOfLastGridPointInDegrees", 270.0),
                    ("iDirectionIncrementInDegrees", 0.5),
                    ("jDirectionIncrementInDegrees", 0.5),
                    ("typeOfLevel", type_of_level),
                    ("level", level),
                    ("shortName", short_name),
                ):
                    eccodes.codes_set(gid, key, value)
                eccodes.codes_set_values(gid, rng.uniform(200.0, 310.0, 41 * 31))
                eccodes.codes_write(gid, handle)
            finally:
                eccodes.codes_release(gid)
    return str(path)


PRODUCTS = [
    {"var": "t", "filter": {"typeOfLevel": "isobaricInhPa"}, "levels": [500, 700, 850],
     "key_template": "temp.t{level}"},
    {"var": "u", "var_aliases": ["UGRD", "u"], "filter": {"typeOfLevel": "isobaricInhPa"},
     "levels": [700, 850], "key_template": "wind.u{level}"},
    {"var": "2t", "filter": {"typeOfLevel": "heightAboveGround", "level": 2}, "key": "t2m"},
    {"var": "missing", "filter": {"typeOfLevel": "surface"}, "key": "absent"},
]

CELLS = {1: (35.0, -97.0), "b": (40.2, -100.3), 3: (31.0, -91.1), 4: (44.9, -109.9), 5: (35.0, -97.0)}


@pytest.fixture
def rap_file(tmp_path):
    return _write_rap(tmp_path / "rap.grib2", 7)


def test_cached_and_uncached_extraction_match_the_original(rap_file, tmp_path):
    expected = _reference_extract_batch(RAPPointExtractor(rap_file), PRODUCTS, CELLS)
    cache_dir = tmp_path / "cache"

    uncached = RAPPointExtractor(rap_file).extract_batch(PRODUCTS, CELLS)
    first = RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS)
    assert sorted(path.suffix for path in cache_dir.iterdir()) == [".json", ".npy"]
    second = RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS)

    for result in (uncached, first, second):
        assert result == expected
        assert list(result) == list(expected)
        assert all(type(value) is float for row in result.values() for value in row.values())


def test_cache_serves_new_cells_without_rescanning(rap_file, tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS)
    other_cells = {"x": (33.3, -95.5), "y": (42.0, -104.0)}
    expected = _reference_extract_batch(RAPPointExtractor(rap_file), PRODUCTS, other_cells)

    def no_scan(*_args, **_kwargs):
        raise AssertionError("cache miss")

    monkeypatch.setattr(RAPPointExtractor, "_scan_fields", no_scan)
    assert RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, other_cells) == expected


def test_changed_source_file_is_rescanned(rap_file, tmp_path):
    cache_dir = tmp_path / "cache"
    RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS)
    _write_rap(rap_file, 8)
    stat = os.stat(rap_file)
    os.utime(rap_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))

    expected = _reference_extract_batch(RAPPointExtractor(rap_file), PRODUCTS, CELLS)
    assert RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS) == expected


def test_corrupt_cache_entry_falls_back_to_scanning(rap_file, tmp_path):
    cache_dir = tmp_path / "cache"
    RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS)
    for path in cache_dir.glob("*.npy"):
        path.write_bytes(b"not an array")

    expected = _reference_extract_batch(RAPPointExtractor(rap_file), PRODUCTS, CELLS)
    assert RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS) == expected


def test_cache_keeps_only_the_two_newest_entries(tmp_path):
    cache_dir = tmp_path / "cache"
    for seed in range(4):
        path = _write_rap(tmp_path / f"rap{seed}.grib2", seed)
        RAPPointExtractor(path, cache_dir=cache_dir).extract_batch(PRODUCTS, CELLS)
        time.sleep(0.01)
    assert len(list(cache_dir.glob("rap-*.json"))) == 2
    assert len(list(cache_dir.glob("rap-*.npy"))) == 2
    assert not list(cache_dir.glob(".*.tmp"))


def test_no_matching_messages_returns_empty_and_caches_nothing(rap_file, tmp_path):
    cache_dir = tmp_path / "cache"
    products = [PRODUCTS[-1]]
    assert RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(products, CELLS) == {}
    assert not cache_dir.exists()


def test_empty_cell_set_matches_the_original(rap_file, tmp_path):
    expected = _reference_extract_batch(RAPPointExtractor(rap_file), PRODUCTS, {})
    cache_dir = tmp_path / "cache"
    for _ in range(2):
        assert RAPPointExtractor(rap_file, cache_dir=cache_dir).extract_batch(PRODUCTS, {}) == expected


def _reference_extract_batch(self, products: list, cell_coords: dict) -> dict:
    """extract_batch as it was before the field cache, kept verbatim."""
    from scipy.spatial import cKDTree

    eccodes.codes_grib_multi_support_on()
    wanted = {}
    for product in products:
        short_names = self._get_product_vars(product)
        type_of_level = product["filter"]["typeOfLevel"]
        filter_level = product["filter"].get("level")
        if "levels" in product:
            for level in product["levels"]:
                output_key = product["key_template"].format(level=level)
                for short_name in short_names:
                    wanted[(short_name, type_of_level, level)] = output_key
        else:
            for short_name in short_names:
                wanted[(short_name, type_of_level, filter_level)] = product["key"]

    cell_ids = list(cell_coords.keys())
    cell_lats = np.array([cell_coords[cid][0] for cid in cell_ids])
    cell_lons = np.array([cell_coords[cid][1] for cid in cell_ids])
    cell_lons_360 = np.where(cell_lons < 0, cell_lons + 360, cell_lons)
    cell_points = np.column_stack((cell_lats, cell_lons_360))
    results = {}
    matched_keys = set()
    tree = None
    nearest_indices = None

    with open(self.filepath, "rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                try:
                    msg_short_name = eccodes.codes_get_string(gid, "shortName")
                    msg_type_of_level = eccodes.codes_get_string(gid, "typeOfLevel")
                except Exception:
                    continue

                try:
                    msg_level = eccodes.codes_get_long(gid, "level")
                except Exception:
                    msg_level = None

                output_key = wanted.get((msg_short_name, msg_type_of_level, msg_level))
                if output_key is None:
                    output_key = wanted.get((msg_short_name, msg_type_of_level, None))
                if output_key is None or output_key in matched_keys:
                    continue

                if tree is None:
                    try:
                        grid_lats = eccodes.codes_get_double_array(gid, "latitudes")
                        grid_lons = eccodes.codes_get_double_array(gid, "longitudes")
                    except Exception:
                        ni = eccodes.codes_get_long(gid, "Ni")
                        nj = eccodes.codes_get_long(gid, "Nj")
                        lat0 = eccodes.codes_get_double(gid, "latitudeOfFirstGridPointInDegrees")
                        lon0 = eccodes.codes_get_double(gid, "longitudeOfFirstGridPointInDegrees")
                        latN = eccodes.codes_get_double(gid, "latitudeOfLastGridPointInDegrees")
                        lonN = eccodes.codes_get_double(gid, "longitudeOfLastGridPointInDegrees")
                        lats_1d = np.linspace(lat0, latN, nj)
                        lons_1d = np.linspace(lon0, lonN, ni)
                        grid_lons, grid_lats = np.meshgrid(lons_1d, lats_1d)
                        grid_lats = grid_lats.ravel()
                        grid_lons = grid_lons.ravel()

                    grid_lons = np.where(grid_lons < 0, grid_lons + 360, grid_lons)
                    tree = cKDTree(np.column_stack((grid_lats, grid_lons)))
                    _, nearest_indices = tree.query(cell_points)

                vals = eccodes.codes_get_double_array(gid, "values")
                results[output_key] = {cid: float(vals[idx]) if idx is not None else None for cid, idx in zip(cell_ids, nearest_indices)}
                matched_keys.add(output_key)
                if len(matched_keys) == len(wanted):
                    break
            finally:
                eccodes.codes_release(gid)

    return results
