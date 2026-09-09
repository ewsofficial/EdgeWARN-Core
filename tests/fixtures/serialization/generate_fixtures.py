"""Regenerate the deterministic binary serialization fixtures both suites consume.

Phase 9 secondary coverage: the Node API serves bytes emitted by the *Python*
production encoders, so the committed files under ``tests/fixtures/serialization``
are the single shared source of truth for the cross-language binary contract.

Run from the repository root with the canonical Conda environment active::

    PYTHONPATH=src python tests/fixtures/serialization/generate_fixtures.py

Regeneration must stay byte-for-byte identical across runs for the small
dtype widths involved:

- the RAP ``data.u16`` write is a raw little-endian ``tobytes`` of a uint16
  array (no headers, no timestamps);
- the float16 chunk encoder pins ``mtime=0`` in the gzip header
  (``EWMRS/render/tiler.py``);
- the NEXRAD field encoder pins the same ``mtime=0`` (``NEXRAD/render.py``);
- the WPC GeoJSON uses an explicit source timestamp (no ``datetime.now()``).

``tests/integration/serialization/test_binary_contracts.py``
(``TestSharedFixtureInventory``) refuses to let the committed bytes drift from a
fresh encode; the Jest companion ``tests/api/test_binary_contracts.js`` serves
the exact same files through the real repository and ancillary stack.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from EWMRS.rap import uint16_pipeline
from EWMRS.render.tiler import save_float16_chunk
from NEXRAD import render as nexrad_render
from common.ingest.wpc.converter import parsed_to_geojson, save_geojson
from common.ingest.wpc.parser import parse_coded_surface

FIXTURES_ROOT = Path(__file__).resolve().parent
TIMESTAMP = "20260317-200000"

RENDER_FORMAT = {
    "version": 2,
    "encoding": "float16",
    "file_suffix": ".f16.gz",
    "compression": "gzip",
    "channels": 1,
    "value_kind": "scalar",
    "no_data": "nan",
    "bytes_per_component": 2,
    "pixel_row_order": "top_to_bottom",
    "grid_origin": "bottom_left",
}
TILE_GRID = {"rows": 1, "cols": 1, "tile_size": 2}

SAMPLE_SURFACE = """\
VALID 150300Z
HIGHS 1027 3380787 1024 4020950
LOWS 1008 4120720
COLD 3500800 3400790 3300780
WARM 3000850 2950840
STNRY 2800900 2750890
OCFNT 4500700 4400690
TROF 3200820 3100810
"""


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _rap_layer(outdir: Path) -> dict:
    return {
        "name": "CAPE",
        "outdir": str(outdir),
        "filter": {"typeOfLevel": "surface"},
        "scale": {"min": 0.0, "max": 100.0},
        "short_names": {"CAPE"},
        "units": "J/kg",
    }


def _rap_message() -> dict:
    return {"shortName": "CAPE", "typeOfLevel": "surface", "level": 0}


def build_rap(out_root: Path) -> None:
    """uint16 CAPE grid ``[[0, 33.3, 100], [nan, 50, 75.5]]`` plus metadata."""
    layer = _rap_layer(out_root / "RAP" / "CAPE")
    values = np.array(
        [[0.0, 33.3, 100.0], [np.nan, 50.0, 75.5]], dtype=np.float64
    )
    encoded = uint16_pipeline.scale_to_uint16(values, layer["scale"])
    layer_root = out_root / "RAP" / "CAPE" / TIMESTAMP
    data_path = layer_root / "data.u16"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(encoded.tobytes(order="C"))

    metadata = uint16_pipeline._build_metadata(
        layer=layer,
        message=_rap_message(),
        rap_path=out_root / "rap_20260317_2000.grib2",
        timestamp=TIMESTAMP,
        grid={"ni": 3, "nj": 2, "point_count": 6},
    )
    _write_json(layer_root / "metadata.json", metadata)
    _write_json(out_root / "RAP" / "CAPE" / "index.json", [TIMESTAMP])


def build_render_chunk(out_root: Path) -> None:
    """One 2x2 float16 chunk owned by a schema-v2 chunk-format index pair."""
    values = np.array(
        [[10.25, np.nan], [1.0, 2.0]], dtype=np.float16
    )
    chunk_path = (
        out_root / "render" / "CompRefQC" / TIMESTAMP / "chunks" / "chunk_0_0.f16.gz"
    )
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    save_float16_chunk(np.ascontiguousarray(values), chunk_path)

    product_index = {
        "schema_version": 2,
        "timestamps": [TIMESTAMP],
        "representation": "binary_chunks",
        "chunk_format": {**RENDER_FORMAT, "media_type": "application/octet-stream"},
        "tile_grid": TILE_GRID,
    }
    snapshot_index = {
        "schema_version": 2,
        "timestamp": TIMESTAMP,
        "representation": "binary_chunks",
        "chunk_format": RENDER_FORMAT,
        "tile_grid": TILE_GRID,
        "chunks": [[0, 0]],
    }
    _write_json(out_root / "render" / "CompRefQC" / "index.json", product_index)
    _write_json(
        out_root / "render" / "CompRefQC" / TIMESTAMP / "index.json", snapshot_index
    )


def build_nexrad_field(out_root: Path) -> None:
    """One DBZH field (2 azimuths x 3 ranges) in the GUI variable-bin layout."""
    data = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, np.nan]], dtype=np.float32)
    azimuths = np.array([0.5, 1.5], dtype=np.float32)
    ranges = np.array([125.0, 250.0, 375.0], dtype=np.float32)
    field_path = (
        out_root / "nexrad" / "KTLH" / "0.5"
        / nexrad_render.nexrad_render_variable_bin_name(
            "KTLH", TIMESTAMP, "0.5", "DBZH"
        )
    )
    field_path.parent.mkdir(parents=True, exist_ok=True)
    nexrad_render._write_nexrad_variable_bin(field_path, data, azimuths, ranges)


def build_wpc(out_root: Path) -> None:
    """GeoJSON the parser + converter emit for the fixed sample surface."""
    parsed = parse_coded_surface(SAMPLE_SURFACE)
    stamp = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    geojson = parsed_to_geojson(parsed, stamp)
    path = out_root / "wpc" / "surface_analysis" / f"wpc_sfc_{TIMESTAMP}.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_geojson(geojson, str(path))


BUILDERS = {
    "rap": build_rap,
    "render_chunk": build_render_chunk,
    "nexrad_field": build_nexrad_field,
    "wpc": build_wpc,
}


def build_all(out_root: Path) -> Path:
    out_root = Path(out_root)
    for builder in BUILDERS.values():
        builder(out_root)
    return out_root


def main() -> int:
    if FIXTURES_ROOT == Path(".").resolve():
        raise SystemExit("refusing to regenerate the repo root")
    build_all(FIXTURES_ROOT)
    generated = sorted(p for p in FIXTURES_ROOT.rglob("*") if p.is_file())
    print(f"Regenerated {len(generated)} fixture files under {FIXTURES_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())