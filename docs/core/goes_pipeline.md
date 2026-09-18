# GOES Pipeline

This document describes the current GOES ABI ingest, readiness, rendering, and
API-serving flow used by EWMRS.

## Scope

The GOES pipeline in this repository currently supports:

- decoupled realtime ingest of GOES ABI CONUS radiance data
- local readiness checks for EWMRS rendering
- single-channel ABI rendering for channels `C01` through `C16`
- tile-first GUI output for the EWMRS API

EdgeWARN integration still treats GOES differently from EWMRS rendering: EWMRS GOES readiness is based on locally staged ABI render inputs, while EdgeWARN integration separately checks GLM availability.

## Source Data

Realtime GOES ingest is owned by the EWMRS service (`run_ewmrs.py`) and configured through `src/common/ingest/mrms/config.py`. Scan-time GLM for integration remains a primary-service (`run_edgewarn.py`) input.

- Bucket: `noaa-goes19`
- ABI product: `ABI-L1b-RadC`
- GLM product: `GLM-L2-LCFA`
- ABI channel coverage: `C01` through `C16`

Each ABI channel is staged into its own runtime directory under the configured base directory. Those staged files are the source of truth for later readiness checks and rendering.

## Generated Products

### Single-Channel Products

These products are written to GUI folders exposed through the EWMRS API as `product` values.

| API product | Internal render name | Derived field |
| --- | --- | --- |
| `GOES_ABI_C01_Reflectance` | `GOES_ABI_C01_Reflectance` | Reflectance |
| `GOES_ABI_C02_Reflectance` | `GOES_ABI_C02_Reflectance` | Reflectance |
| `GOES_ABI_C03_Reflectance` | `GOES_ABI_C03_Reflectance` | Reflectance |
| `GOES_ABI_C04_Reflectance` | `GOES_ABI_C04_Reflectance` | Reflectance |
| `GOES_ABI_C05_Reflectance` | `GOES_ABI_C05_Reflectance` | Reflectance |
| `GOES_ABI_C06_Reflectance` | `GOES_ABI_C06_Reflectance` | Reflectance |
| `GOES_ABI_C07_BrightnessTemp` | `GOES_ABI_C07_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C08_BrightnessTemp` | `GOES_ABI_C08_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C09_BrightnessTemp` | `GOES_ABI_C09_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C10_BrightnessTemp` | `GOES_ABI_C10_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C11_BrightnessTemp` | `GOES_ABI_C11_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C12_BrightnessTemp` | `GOES_ABI_C12_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C13_BrightnessTemp` | `GOES_ABI_C13_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C14_BrightnessTemp` | `GOES_ABI_C14_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C15_BrightnessTemp` | `GOES_ABI_C15_BrightnessTemp` | Brightness temperature |
| `GOES_ABI_C16_BrightnessTemp` | `GOES_ABI_C16_BrightnessTemp` | Brightness temperature |

## Realtime Flow

### 1. Background ingest

`run_ewmrs.py` supervises a dedicated `goes_loop()` child that:

1. builds the full ABI channel spec list from `get_abi_radc_channel_specs()`
2. downloads GOES files on a 60-second poll cadence
3. prefers async ingest and falls back to sync ingest only when the async call raises an exception
4. marks the GOES cycle active while staging is in progress

This loop is decoupled from the shared MRMS ingest cycle so GOES ingest does not block MRMS detection or MRMS-backed rendering.

### 2. Readiness checks

Local GOES readiness is implemented in `src/common/pipeline/goes_readiness.py`.

Current behavior:

- readiness is computed from the configured EWMRS GOES scalar layer set, which currently spans channels `C01` through `C16`
- the helper selects the nearest eligible staged file per channel whose scan window is within `20` minutes of the target time; it does not simply choose the newest file
- GOES filenames that encode `s..._e...` scan windows are treated as valid across the whole scan interval, not as a single instant
- if any configured ABI channel is missing locally, EWMRS GOES readiness fails for that cycle

This makes the decoupled GOES render phase wait for a complete local ABI set before rendering begins.

### 3. Render task scheduling

Since the decomposition, `goes_render_loop()` is a poll-based EWMRS-owned loop: each poll it pins the nearest complete local ABI set into an exact-path manifest and renders it once per distinct input selection.

- ingest and render share the EWMRS service's process tree, so rendering can optionally pause background ingest if `goes_coordination.pause_ingest_during_render` is enabled
- no cross-process render queue exists; the primary never schedules GOES renders

## Render Pipeline

`src/EWMRS/pipeline.py` drives GOES rendering through `run_goes_render_pipeline()`, which delegates each configured single-channel layer to the shared `run_render_pipeline()` and runs a constrained GUI cleanup afterwards.

### Projection and grid

GOES products are reprojected from the native ABI fixed grid into a CONUS-focused `EPSG:3857` target grid.

- output raster shape: `3500 x 7000`
- tile grid written by the renderer: `10 x 20`
- tile size: `350` pixels
- tile grid capacity per completed product: `200`

The renderers persist the tile grid into each product `index.json` and each timestamp folder's `index.json`. The EWMRS API falls back to the current `10 x 20` / `350px` grid for product metadata, but chunk requests still require a valid product-level index.

### Single-channel render path

For `source_type="goes_abi"` layers, the pipeline:

1. selects the staged source file for the channel
2. extracts the GOES timestamp from the filename
3. loads the calibrated `CMI` payload, with `Rad` fallback when configured
4. converts `Rad` fallback data to reflectance for `C01` through `C06`, or to brightness temperature for `C07` through `C16`; `CMI` is already calibrated
5. reprojects the normalized array into the GOES Web Mercator target grid
6. retains the scalar source values after masking
7. writes tiled float16 value chunks, updates product-level `index.json`, and writes timestamp-level `index.json`

## Output Layout

GOES GUI products are written under `<BASE_DIR>/gui`.

Examples:

```text
<BASE_DIR>/gui/GOES_ABI_C13_BrightnessTemp/
├── 20260423-124000/
│   ├── chunks/                       # gzip-compressed float16 value chunks
│   └── index.json                    # timestamp-level index
└── index.json                        # product-level index
```

The tiled render path writes only `chunks/` and the two `index.json` levels. It
does not write `metadata.json` — that file belongs to the RAP Uint16 pipeline
alone (see `docs/core/ingestion.md`).

Current product-level `index.json` format for rendered products is:

```json
{
  "schema_version": 2,
  "timestamps": ["20260423-124000"],
  "representation": "binary_chunks",
  "chunk_format": {
    "version": 2,
    "encoding": "float16",
    "file_suffix": ".f16.gz",
    "compression": "gzip",
    "data_type": "float16",
    "channels": 1,
    "value_kind": "scalar",
    "no_data": "nan",
    "bytes_per_component": 2,
    "pixel_row_order": "top_to_bottom",
    "grid_origin": "bottom_left",
    "media_type": "application/octet-stream"
  },
  "tile_grid": {
    "rows": 10,
    "cols": 20,
    "tile_size": 350
  }
}
```

Current timestamp-level `index.json` format is:

```json
{
  "schema_version": 2,
  "timestamp": "20260423-124000",
  "representation": "binary_chunks",
  "chunk_format": {
    "version": 2,
    "encoding": "float16",
    "file_suffix": ".f16.gz",
    "compression": "gzip",
    "data_type": "float16",
    "channels": 1,
    "value_kind": "scalar",
    "no_data": "nan",
    "bytes_per_component": 2,
    "pixel_row_order": "top_to_bottom",
    "grid_origin": "bottom_left"
  },
  "tile_grid": {
    "rows": 10,
    "cols": 20,
    "tile_size": 350
  },
  "chunks": [[0, 0], [1, 3], [2, 6]]
}
```

The available-coordinate array is named `chunks`, not `tiles`. `chunk_format` is
a wire-format invariant sourced from `ewmrs_render.yaml`, not an operator knob;
`media_type` appears at the product level only. `no_data` is the literal string
`"nan"`.

## API Exposure

The unified API's EWMRS compatibility adapter serves GOES products through:

- `GET /renders/get-items`
- `GET /renders/fetch?product={product}`
- `GET /renders/download?product={product}&timestamp={YYYYMMDD-HHMMSS}`
- `GET /renders/tile?product={product}&timestamp={YYYYMMDD-HHMMSS}[&x={int}&y={int}]`
- `GET /renders/tile-info?product={product}`

Notes:

- `product` values are the full GUI folder names shown in the tables above; shortened names such as `GOES_ABI_C01` are not API aliases
- binary clients should use `/api/v3/render-products/{productId}/snapshots/{timestamp}/chunks`
  and `/chunks/{x}/{y}`; chunks are gzip-compressed float16 value chunks with metadata in
  schema-version-2 `index.json`
- the timestamp index's sparse `chunks` array, sorted by y then x, is the
  authority for available coordinates; omitted chunks are fully transparent
- visualization palettes are client-owned; the API does not publish a
  colormap catalog
- legacy `/renders/download` and `/renders/tile` return `410 Gone` and point
  clients to the v3 chunk resource

See `docs/api/ewmrs_api_endpoints.md` for route-level behavior.

## Failure Semantics

The GOES pipeline requires a complete aligned input set before starting a render pass. Failures inside an eligible layer are isolated instead of failing the entire pass.

- a missing scalar channel prevents readiness for the pass
- poll-based input-selection deduplication avoids a separate stale task queue
- cleanup runs after GOES rendering and remains constrained to the configured GUI base directory

This keeps MRMS record consumption moving in the EWMRS service even when some GOES inputs are late or absent.
