"""
Fast GRIB2 loaders using eccodes bindings directly.
Bypasses cfgrib indexing overhead for MRMS and RAP GRIB files.
"""

import hashlib
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path

import eccodes
import numpy as np
import xarray as xr

eccodes.codes_grib_multi_support_on()

# Bump when the on-disk layout of RAPPointExtractor's field cache changes.
_RAP_FIELD_CACHE_VERSION = 1
# Entries kept, including the one just written: the current hour and the last.
_RAP_FIELD_CACHE_KEEP = 2


@lru_cache(maxsize=1)
def eccodes_thread_safe() -> bool:
    """Whether this ecCodes build serializes its shared state across threads.

    Concurrent decodes are only attempted when the library reports the
    ``ECCODES_THREADS`` feature; an unknown build is treated as unsafe.
    """
    try:
        features = eccodes.codes_get_features()
    except Exception:
        return False
    return "ECCODES_THREADS" in str(features).split()


@lru_cache(maxsize=1)
def _cv2():
    """OpenCV, imported on first use; ``None`` when it is not installed."""
    try:
        import cv2
    except ImportError:
        return None
    return cv2


@lru_cache(maxsize=32)
def _png_lut(bits: int, reference: float, binary_scale: int, decimal_scale: int) -> np.ndarray:
    """float32 value for every packed code, scaled as ecCodes' grid_png does."""
    codes = np.arange(2 ** bits, dtype=np.float64)
    lut = (((codes * 2.0 ** binary_scale) + reference) * 10.0 ** -decimal_scale).astype(np.float32)
    lut.flags.writeable = False
    return lut


def _section7_payload(message: bytes):
    """Return the data section's payload, or ``None`` unless exactly one exists."""
    if len(message) < 16 or message[:4] != b"GRIB" or message[7] != 2:
        return None
    offset = 16
    payload = None
    while offset + 4 <= len(message):
        if message[offset:offset + 4] == b"7777":
            return payload
        if offset + 5 > len(message):
            return None
        length = int.from_bytes(message[offset:offset + 4], "big")
        number = message[offset + 4]
        if length < 5 or offset + length > len(message):
            return None
        if number == 7:
            if payload is not None:
                return None
            payload = message[offset + 5:offset + length]
        offset += length
    return None


def _decode_png_values(gid, ni: int, nj: int):
    """Decode a PNG-packed (template 5.41) field without ecCodes' float64 pass.

    Returns the ``(nj, ni)`` float32 grid ``load_grib_fast`` would otherwise
    produce, or ``None`` when the message is not an 8- or 16-bit, bitmap-free
    PNG field (or OpenCV is unavailable), so the caller uses ecCodes instead.
    """
    cv2 = _cv2()
    if cv2 is None:
        return None
    try:
        if eccodes.codes_get_long(gid, "dataRepresentationTemplateNumber") != 41:
            return None
        if eccodes.codes_get_long(gid, "bitmapPresent") != 0:
            return None
        bits = eccodes.codes_get_long(gid, "bitsPerValue")
        if bits not in (8, 16):
            return None
        reference = eccodes.codes_get_double(gid, "referenceValue")
        binary_scale = eccodes.codes_get_long(gid, "binaryScaleFactor")
        decimal_scale = eccodes.codes_get_long(gid, "decimalScaleFactor")
        payload = _section7_payload(eccodes.codes_get_message(gid))
        if payload is None:
            return None
        image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_UNCHANGED)
    except Exception:
        return None
    expected = np.uint8 if bits == 8 else np.uint16
    if image is None or image.dtype != expected or image.ndim != 2 or image.size != ni * nj:
        return None
    lut = _png_lut(int(bits), float(reference), int(binary_scale), int(decimal_scale))
    return lut[image].reshape(nj, ni)


def load_grib_fast(filepath: str) -> xr.Dataset:
    try:
        with open(filepath, "rb") as f:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                raise ValueError("No GRIB message found in file")

            try:
                ni = eccodes.codes_get_long(gid, "Ni")
                nj = eccodes.codes_get_long(gid, "Nj")
                lat0 = eccodes.codes_get_double(gid, "latitudeOfFirstGridPointInDegrees")
                lon0 = eccodes.codes_get_double(gid, "longitudeOfFirstGridPointInDegrees")
                latN = eccodes.codes_get_double(gid, "latitudeOfLastGridPointInDegrees")
                lonN = eccodes.codes_get_double(gid, "longitudeOfLastGridPointInDegrees")
                lats = np.linspace(lat0, latN, nj, dtype=np.float32)
                lons = np.linspace(lon0, lonN, ni, dtype=np.float32)
                vals = _decode_png_values(gid, ni, nj)
                if vals is None:
                    vals = eccodes.codes_get_double_array(gid, "values")
                    vals = vals.reshape(nj, ni).astype(np.float32)

                try:
                    name = eccodes.codes_get_string(gid, "shortName")
                    if not name or name == "unknown":
                        name = "unknown"
                except Exception:
                    name = "unknown"

                da = xr.DataArray(vals, coords={"latitude": lats, "longitude": lons}, dims=("latitude", "longitude"), name=name)
                return da.to_dataset()
            finally:
                eccodes.codes_release(gid)
    except Exception as e:
        raise RuntimeError(f"Fast GRIB load failed for {filepath}: {e}") from e


class RAPPointExtractor:
    def __init__(self, filepath: str, cache_dir=None):
        self.filepath = filepath
        self.cache_dir = cache_dir

    @staticmethod
    def _get_product_vars(product: dict) -> list[str]:
        """Return all supported GRIB short-name aliases for a product."""
        var_aliases = product.get("var_aliases")
        if var_aliases is not None:
            return list(var_aliases)

        var = product["var"]
        if isinstance(var, (list, tuple, set)):
            return list(var)
        return [var]

    def extract(self, products: list, cell_coords: dict) -> dict:
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
        lats = np.array([cell_coords[cid][0] for cid in cell_ids])
        lons = np.array([cell_coords[cid][1] for cid in cell_ids])
        lons_360 = np.where(lons < 0, lons + 360, lons)
        results = {}
        matched_keys = set()

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

                    cell_values = {}
                    for i, cid in enumerate(cell_ids):
                        try:
                            nearest = eccodes.codes_grib_find_nearest(gid, lats[i], lons_360[i])
                            cell_values[cid] = nearest[0].value if nearest and len(nearest) > 0 else None
                        except Exception:
                            cell_values[cid] = None

                    results[output_key] = cell_values
                    matched_keys.add(output_key)
                    if len(matched_keys) == len(wanted):
                        break
                finally:
                    eccodes.codes_release(gid)

        return results

    def _wanted_keys(self, products: list) -> dict:
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
        return wanted

    def _scan_fields(self, wanted: dict):
        """Decode every wanted field once: ``(keys, grid_lats, grid_lons, values)``.

        ``grid_lats``/``grid_lons`` come from the first matched message, with
        longitudes already in 0-360, and ``values`` lists the full decoded
        arrays in file order. Returns ``None`` when nothing matched.
        """
        keys = []
        values = []
        matched_keys = set()
        grid_lats = grid_lons = None

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

                    if grid_lats is None:
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

                    keys.append(output_key)
                    values.append(eccodes.codes_get_double_array(gid, "values"))
                    matched_keys.add(output_key)
                    if len(matched_keys) == len(wanted):
                        break
                finally:
                    eccodes.codes_release(gid)

        if not keys:
            return None
        return keys, grid_lats, grid_lons, values

    def _cache_entry(self, wanted: dict):
        """Cache file stem for this exact source file and product catalog."""
        stat = os.stat(self.filepath)
        identity = json.dumps(
            {
                "version": _RAP_FIELD_CACHE_VERSION,
                "path": os.path.realpath(self.filepath),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "wanted": sorted([list(map(str, key)), value] for key, value in wanted.items()),
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return Path(self.cache_dir) / f"rap-{digest}"

    @staticmethod
    def _load_cached_fields(stem: Path):
        meta_path = stem.with_suffix(".json")
        data_path = stem.with_suffix(".npy")
        if not meta_path.is_file() or not data_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        keys = meta["keys"]
        stack = np.load(data_path, mmap_mode="r", allow_pickle=False)
        if stack.dtype != np.float64 or stack.shape != (len(keys) + 2, meta["points"]):
            return None
        return keys, stack

    def _store_cached_fields(self, stem: Path, keys, grid_lats, grid_lons, values) -> None:
        stem.parent.mkdir(parents=True, exist_ok=True)
        stack = np.vstack([np.asarray(grid_lats, dtype=np.float64),
                           np.asarray(grid_lons, dtype=np.float64)]
                          + [np.asarray(row, dtype=np.float64) for row in values])
        token = f".{stem.name}.{os.getpid()}.{uuid.uuid4().hex}"
        data_tmp = stem.parent / (token + ".npy.tmp")
        meta_tmp = stem.parent / (token + ".json.tmp")
        try:
            with open(data_tmp, "wb") as handle:
                np.save(handle, stack, allow_pickle=False)
            meta_tmp.write_text(json.dumps({"keys": list(keys), "points": int(stack.shape[1])}),
                                encoding="utf-8")
            # The data file lands first; a reader needs both, and the stem is
            # unique to this source file and catalog, so contents never differ.
            os.replace(data_tmp, stem.with_suffix(".npy"))
            os.replace(meta_tmp, stem.with_suffix(".json"))
        finally:
            for leftover in (data_tmp, meta_tmp):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        self._prune_cache(stem)

    @staticmethod
    def _prune_cache(current: Path) -> None:
        """Keep the current entry and the newest other one; drop the rest."""
        directory = current.parent
        newest = {}
        for path in list(directory.glob("rap-*.json")) + list(directory.glob("rap-*.npy")):
            stem = path.with_suffix("")
            if stem == current:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            newest[stem] = max(mtime, newest.get(stem, mtime))
        entries = sorted(((mtime, stem) for stem, mtime in newest.items()), reverse=True)
        stale_cutoff = time.time() - 3600
        for _mtime, stem in entries[_RAP_FIELD_CACHE_KEEP - 1:]:
            for path in (stem.with_suffix(".json"), stem.with_suffix(".npy")):
                try:
                    path.unlink()
                except OSError:
                    pass
        for leftover in directory.glob(".rap-*.tmp"):
            try:
                if leftover.stat().st_mtime < stale_cutoff:
                    leftover.unlink()
            except OSError:
                pass

    def extract_batch(self, products: list, cell_coords: dict) -> dict:
        from scipy.spatial import cKDTree

        eccodes.codes_grib_multi_support_on()
        wanted = self._wanted_keys(products)

        cell_ids = list(cell_coords.keys())
        cell_lats = np.array([cell_coords[cid][0] for cid in cell_ids])
        cell_lons = np.array([cell_coords[cid][1] for cid in cell_ids])
        cell_lons_360 = np.where(cell_lons < 0, cell_lons + 360, cell_lons)
        cell_points = np.column_stack((cell_lats, cell_lons_360))

        # The hourly RAP file is re-read by every two-minute cycle, each in a
        # fresh process, so the decoded wanted fields are kept on disk keyed by
        # the file's identity. Any cache problem falls back to a direct scan.
        stem = None
        if self.cache_dir is not None:
            try:
                stem = self._cache_entry(wanted)
                cached = self._load_cached_fields(stem)
            except Exception:
                cached = None
            if cached is not None:
                keys, stack = cached
                tree = cKDTree(np.column_stack((stack[0], stack[1])))
                _, nearest_indices = tree.query(cell_points)
                sampled = np.asarray(stack[2:, nearest_indices])
                del stack
                return {
                    key: {cid: float(value) for cid, value in zip(cell_ids, row)}
                    for key, row in zip(keys, sampled)
                }

        fields = self._scan_fields(wanted)
        if fields is None:
            return {}
        keys, grid_lats, grid_lons, values = fields
        if stem is not None:
            try:
                self._store_cached_fields(stem, keys, grid_lats, grid_lons, values)
            except Exception:
                pass

        tree = cKDTree(np.column_stack((grid_lats, grid_lons)))
        _, nearest_indices = tree.query(cell_points)
        return {
            key: {cid: float(vals[idx]) if idx is not None else None
                  for cid, idx in zip(cell_ids, nearest_indices)}
            for key, vals in zip(keys, values)
        }
