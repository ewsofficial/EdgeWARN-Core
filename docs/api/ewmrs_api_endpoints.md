# EWMRS Compatibility Endpoints

These legacy EWMRS routes are compatibility adapters mounted by the unified
`src/api/` service. New clients should use the `/api/v3` resources documented in
`docs/api/api_endpoints.md`.

## API Overview

- Base URL: `/`
- Response format: JSON for metadata/list routes, PNG for render downloads/tiles, raw binary for RAP Uint16 arrays

There is no separate EWMRS process, port, or configuration surface. These routes
share the unified service's runtime settings, documented in
`docs/api/api_implementation.md`:

- Default port `5000`; debug port `3001` via `--debug-server`
- Base directory: `--base-dir` (or `--base_dir`), then `EDGEWARN_BASE_DIR`, then
  `BASE_DIR`, then `config/filesystem.yaml`
- Rate limits are the unified ones — `40` requests per second and `2000` per
  minute from `config/api.yaml`, overridable with `RATE_LIMIT_MAX_SEC` and
  `RATE_LIMIT_MAX_MIN`, where `0` disables a window. There are no
  `--ewmrs-rate-limit-*` flags and no `EWMRS_`-prefixed environment variables.

Every route below sets `Deprecation: true` and
`Link: </api/v3/openapi.json>; rel="deprecation"`. None of them sets
`Cache-Control`; the tuned cache lifetimes apply to `/api/v3` only. Error
responses use the shared `application/problem+json` handler with the status
mapping in `docs/api/api_endpoints.md`, except where a route builds its own body
as noted.

## Root Endpoints

### GET /

There is no EWMRS-specific root document. `GET /` is the unified service banner
described in `docs/api/api_endpoints.md`; it advertises `/api/v3` and the OpenAPI
document, and deliberately does not disclose the resolved base or GUI
directories.

### GET /healthz

Response:

```json
{
  "ok": true
}
```

A fixed body that inspects nothing. Use `/health/ready` for an actual readiness
signal.

## Render Endpoints

### GET /renders/get-items

Returns available render product folders present in `<BASE_DIR>/gui` that are known by the API mapping.

Response:

- `200`: `string[]`

Currently mapped products include MRMS layers and GOES products.

- `CompRefQC`, `EchoTop18`, `EchoTop30`, `RALA`, `Ref0C`, `RefM5C`, `RefM15C`
- `PrecipRate`, `QPE_01H`, `VIL`, `VILDensity`, `VII`, `MESH`, `NLDN`
- `AzShearLow`, `AzShearMid`
- `GOES_ABI_C01`, `GOES_ABI_C02`, `GOES_ABI_C03`, `GOES_ABI_C04`, `GOES_ABI_C05`, `GOES_ABI_C06`
- `GOES_ABI_C07`, `GOES_ABI_C08`, `GOES_ABI_C09`, `GOES_ABI_C10`, `GOES_ABI_C11`, `GOES_ABI_C12`
- `GOES_ABI_C13`, `GOES_ABI_C14`, `GOES_ABI_C15`, `GOES_ABI_C16`

### GET /renders/fetch?product={product}

Returns available timestamps for a product from `index.json`.

Behavior:

- Accepts both index formats:
  - old format: `string[]`
  - new format: `{ "timestamps": string[], "tile_grid": { ... } }`

Responses:

- `200`: `string[]`
- `200`: `[]` when the mapped product has no `index.json` yet
- `404`: missing, unknown, or path-like `product` — this route answers `404`
  rather than `400` for an absent parameter, because the lookup is by legacy ID
  and an empty value simply fails to resolve

### GET /renders/download?product={product}&timestamp={YYYYMMDD-HHMMSS}

Resolves a rendered PNG in the legacy non-tiled naming format when that file exists:

- `<GUI_DIR>/<product>/<file_prefix>_{timestamp}.png`

This legacy route remains PNG-only. Current GOES and MRMS renderers publish
binary float16 value chunks through the unified v3 `/api/v3/render-products/.../chunks`
resources; a missing compatibility PNG returns `404` rather than binary bytes
under the PNG contract. In practice no current renderer writes these flat PNGs,
so this route is `404` for freshly rendered products. Responses carry
`Deprecation: true` and the successor-version `Link`; no `Sunset` header is sent.

Responses:

- `200`: PNG image
- `400`: missing `product` or `timestamp`, unknown product, or invalid timestamp
- `404`: file not found

### GET /renders/tile?product={product}&timestamp={YYYYMMDD-HHMMSS}[&x={int}&y={int}]

Supports two modes:

- image mode when both `x` and `y` are supplied
- listing mode when both `x` and `y` are omitted

Image mode downloads a compatibility tile PNG from:

- `<GUI_DIR>/<product>/<timestamp>/tile_{x}_{y}.png`

Tile bounds are validated from the timestamp-level `index.json` `tile_grid` when present, with fallback to the product-level `index.json` `tile_grid`.

Current renderer-written GOES and MRMS products persist:

- `rows=10`
- `cols=20`
- `tile_size=350`

If product-level `index.json` is missing, the route falls back to defaults `rows=10`, `cols=20`, `tile_size=350` for coordinate validation.

Listing mode reads `<GUI_DIR>/<product>/<timestamp>/index.json` and filters
out-of-bounds coordinates. It does not scan the timestamp directory for tile
filenames.

The key it reads is `tiles`, which is the legacy shape below. Schema-version-2
indexes written by the current renderers publish `chunks` instead, so a freshly
rendered product returns a valid `tile_grid` with an empty `tiles` array rather
than an error — there are no PNG tiles to list. Use the v3 `/chunks` resource for
those products.

Legacy timestamp-level tile index format:

```json
{
  "tiles": [[0, 0], [1, 3], [2, 6]],
  "tile_grid": {
    "rows": 10,
    "cols": 20,
    "tile_size": 350
  }
}
```

Listing response:

```json
{
  "product": "CompRefQC",
  "timestamp": "20260317-200000",
  "tile_grid": {
    "rows": 10,
    "cols": 20,
    "tile_size": 350
  },
  "tiles": [[0, 0], [1, 3], [2, 6]]
}
```

Responses:

- `200`: PNG image in image mode, JSON tile listing in listing mode
- `400`: missing `product` or `timestamp`, unknown product, invalid timestamp,
  out-of-bounds coordinates, or only one of `x`/`y` supplied
- `404`: missing tile file, or missing timestamp `index.json`

### GET /renders/tile-info?product={product}

Returns tile-grid metadata and timestamps for a product.

Responses:

- `200`:

```json
{
  "product": "CompRefQC",
  "rows": 10,
  "cols": 20,
  "tile_size": 350,
  "timestamps": ["20260317-200000"]
}
```

- `200`: default tile-grid metadata with `timestamps: []` when the mapped product has no `index.json` yet
- `404`: missing or unknown `product`

The `rows`, `cols`, and `tile_size` values come from the product-level
`index.json` `tile_grid` when it is present and within
`api.yaml` `render_defaults.grid_maxima`; an out-of-range grid is discarded in
favor of the `render_defaults.grid` values rather than raising.

## NEXRAD Endpoints

NEXRAD render intermediates are served from `<BASE_DIR>/gui/NEXRAD`.

Runtime layout:

```text
<BASE_DIR>/gui/NEXRAD/<SITE>/<ELEVATION>/<SITE>_<PRODUCT>_<ELEVATION>_<YYYYMMDD-HHMMSS>.bin.gz
```

Allowed products:

- `DBZH`
- `VRADH`
- `WRADH`
- `PHIDP`
- `CCORH`
- `RHOHV`
- `ZDR`

This list is `config/api.yaml` `validation.radar_products`, an allowlist checked
before any file lookup. `CCORH` is servable but deliberately uncolored: it has no
entry in `config/ewmrs_render.yaml` `nexrad_gui.variable_colormaps`, which colors
only the other six. Clients requesting `CCORH` get valid data with no published
palette.

Security and validation rules:

- Site identifiers are normalized to uppercase and must be exactly 4 alphanumeric characters.
- Timestamps must use `YYYYMMDD-HHMMSS` and pass calendar/time validation.
- Elevations must match the regex `^\d{1,3}(?:\.\d{1,2})?$`. The regex is
  deliberately wider than what is produced: the labels actually written are the
  canonical bins in `config/nexrad.yaml` `selection.canonical_elevation_bins`,
  currently `0.5`, `0.9`, `1.3`, `1.8`, `2.4`, `3.1`, and `4.0`.
- Product names must match the exact allowlist above.
- Path traversal attempts and malformed parameters are rejected.
- Unsafe on-disk directory names are ignored during listing.

### GET /nexrad

Returns active radar site folders that contain at least one valid NEXRAD GUI file.

Runtime layout:

```text
<BASE_DIR>/gui/NEXRAD/<SITE>/<ELEVATION>/<SITE>_<PRODUCT>_<ELEVATION>_<YYYYMMDD-HHMMSS>.bin.gz
```

EWMRS populates these files by polling local ingest outputs under
`<BASE_DIR>/data/NEXRAD_Level2` every `config/ewmrs_pipeline.yaml`
`nexrad_gui.poll_interval_seconds` (`30`), considering only artifacts newer than
`nexrad_gui.retention_minutes` (`120`). If a same-timestamp GUI file already
exists for an elevation artifact, that artifact is skipped instead of being
re-rendered. Nothing is deleted on that timer — it is an input-freshness bound,
not a retention sweep.

Responses:

- `200`: `string[]`
- `200`: `[]` when `<BASE_DIR>/gui/NEXRAD` does not exist

Example:

```json
["KTLH", "KTLX"]
```

### GET /nexrad/{site}

Returns valid elevations for one radar site mapped to their available timestamps. Timestamps are parsed from NEXRAD GUI filenames rather than timestamp-named directories.

Responses:

- `200`: object mapping elevation labels to timestamp arrays, newest first. An
  elevation directory with no recognizable field files is omitted entirely.
- `400`: invalid site parameter
- `404`: site not found

Example:

```json
{
  "0.5": ["20260512-004336"],
  "0.9": ["20260512-004336"],
  "1.3": ["20260512-004753"]
}
```

### GET /nexrad/{site}/{timestamp}/{elevation}?product={PRODUCT}

Downloads the requested gzip-compressed NEXRAD binary field file.

Responses:

- `200`: gzip binary payload
- `400`: invalid site, timestamp, elevation, or product parameter
- `404`: site/timestamp/elevation/product file not found

Response headers include:

- `Content-Type: application/gzip`
- `Content-Disposition: attachment; filename="<SITE>_<TIMESTAMP>_<ELEVATION>_<PRODUCT>.bin.gz"`

Note that the attachment filename orders the parts as
`SITE_TIMESTAMP_ELEVATION_PRODUCT`, while the on-disk name is
`SITE_PRODUCT_ELEVATION_TIMESTAMP`. The v3 equivalent,
`/api/v3/radar-sites/{siteId}/scans/{timestamp}/elevations/{elevation}/products/{productId}`,
sends no `Content-Disposition` but does send an `ETag` and `Cache-Control`.

## RAP Uint16 Endpoints

RAP Uint16 outputs are served from `<BASE_DIR>/gui/RAP`. These routes intentionally remain separate from `/renders/*` because RAP outputs are raw arrays plus metadata, not PNG renders or tiles.

Runtime layout:

```text
<BASE_DIR>/gui/RAP/<LayerFolder>/index.json
<BASE_DIR>/gui/RAP/<LayerFolder>/<YYYYMMDD-HHMM00>/data.u16
<BASE_DIR>/gui/RAP/<LayerFolder>/<YYYYMMDD-HHMM00>/metadata.json
```

`data.u16` contains raw little-endian `uint16` values. The reserved missing/no-data value is `65535`. Clients must fetch `metadata.json` to decode the array shape, scale, units, and GRIB metadata. `colormap_key` and `description` are present only when the layer declares them, so treat both as optional — `MSLP_Surface`, for instance, has no `colormap_key`.

Layer names are the on-disk folder names, for example `Temperature_2m`,
`CAPE_0-3km`, `UWind_925mb`, `SRH-0-1km`, or
`LiftedIndex_Surface_500-1000mb`. Each is the layer's `outdir` from
`config/ewmrs_pipeline.yaml` `rap_uint16`, which is deliberately distinct from
its `name`: the layer named `RAP_Temperature_2m` publishes under
`Temperature_2m`.

### GET /rap/layers

Returns available RAP layer folders under `<BASE_DIR>/gui/RAP` that contain an `index.json` file.

Responses:

- `200`: `string[]`
- `500`: read/server failure

Example:

```json
["CAPE_0-3km", "Temperature_2m", "UWind_925mb"]
```

### GET /rap/fetch?layer={layer}

Returns available RAP timestamps for a layer from `<BASE_DIR>/gui/RAP/<layer>/index.json`.

RAP timestamp folders are minute-aligned in the form `YYYYMMDD-HHMM00`.

The converter writes `{ "timestamps": string[], "format": "uint16",
"byte_order": "little_endian", "missing_value": 65535 }`, newest first and capped
at the configured retained-timestamp count. A bare `string[]` index is also
accepted for compatibility. Only the `timestamps` member is returned.

Responses:

- `200`: `string[]`; also `[]` when the layer folder or its `index.json` is
  absent, so a missing layer is not distinguishable from an empty one here
- `400`: missing or invalid `layer`
- `503`: `index.json` present but unparseable

### GET /rap/metadata?layer={layer}&timestamp={YYYYMMDD-HHMM00}

Returns parsed `metadata.json` for the selected layer timestamp.

Example metadata:

```json
{
  "layer": "Temperature_2m",
  "timestamp": "20260427-120000",
  "shape": [337, 451],
  "grid": {
    "ni": 451,
    "nj": 337,
    "point_count": 151987
  },
  "dtype": "uint16",
  "byte_order": "little_endian",
  "scale": {
    "min": 180.0,
    "max": 330.0
  },
  "missing_value": 65535,
  "units": "K",
  "colormap_key": "RAP_Temperature_LL",
  "grib": {
    "shortName": "2t",
    "typeOfLevel": "heightAboveGround",
    "level": 2
  }
}
```

Responses:

- `200`: metadata JSON payload
- `400`: missing/invalid `layer` or `timestamp`
- `404`: layer folder, timestamp folder, or `metadata.json` not found
- `503`: `metadata.json` present but unparseable

`colormap_key` resolves to a same-named internal renderer entry, but it is
**not** the layer name and not the folder name. Several layers share one key:
`Temperature_2m` and `Temperature_Surface` both resolve to
`RAP_Temperature_LL`, and both 10 m wind components resolve to `RAP_Wind_LL`.
Look the key up from the metadata rather than constructing it from the layer
name. RAP colormaps use NOAA/SPC/GEMPAK lineage palettes where practical source
tables exist, with documented project fallbacks for variables without a usable
standard reference.

### GET /rap/data?layer={layer}&timestamp={YYYYMMDD-HHMM00}

Streams the raw `<BASE_DIR>/gui/RAP/<layer>/<timestamp>/data.u16` bytes exactly as written by the RAP Uint16 converter.

Response headers always include:

- `Content-Type: application/octet-stream`
- `X-Data-Type: uint16`
- `X-Byte-Order: little_endian`
- `X-Missing-Value: 65535`

No `Content-Disposition` is sent; the payload is streamed inline.

When the sibling `metadata.json` is readable, these decode headers are added,
each only if its value is present and passes a range or character check:

- `X-Grid-Ni`, `X-Grid-Nj` — from `grid.ni`/`grid.nj`, falling back to `shape`
- `X-Scale-Min`, `X-Scale-Max`
- `X-Units`

A missing `metadata.json` is not an error here — the data is still served, just
without the decode headers. Any other failure reading it does fail the request.

Responses:

- `200`: raw binary `data.u16` payload
- `400`: missing/invalid `layer` or `timestamp`
- `404`: layer folder, timestamp folder, or `data.u16` not found
- `503`: `data.u16` exceeds the configured binary size limit

Decode formula:

```text
if value == 65535:
  decoded = missing
else:
  decoded = scale.min + (value / 65534) * (scale.max - scale.min)
```

## NEXRAD Polar Intermediate Endpoints

NEXRAD intermediate outputs are served from:

```text
<BASE_DIR>/gui/NEXRAD/<SITE>/<ELEVATION>/<SITE>_<VARIABLE>_<ELEVATION>_<YYYYMMDD-HHMMSS>.bin.gz
```

Each `.bin.gz` payload decompresses to:

```text
[magic bytes EWFFv1S0][azimuth_count uint32 LE][range_count uint32 LE][data float16 LE][azimuths float32 LE][ranges float32 LE]
```

Stored `data` values remain range-major with shape `[range_count, azimuth_count]`.

Elevation folder names are not a per-VCP sweep-index table. Ingest groups the
sweeps of a volume and then snaps each group's angle onto the nearest entry of
`config/nexrad.yaml` `selection.canonical_elevation_bins`, currently:

```text
0.5, 0.9, 1.3, 1.8, 2.4, 3.1, 4.0
```

These are bin *identities* with no tolerance window, so a real VCP-12 sweep at
`1.2305` degrees lands on `1.3` at a distance of `0.07` and nothing is
mis-binned. The label written into the path is that bin's value, which is why
`1.2` never appears on disk. Only the VCPs in `selection.allowed_vcps` — `12`,
`212`, and `215` — are ingested at all, and sweeps below
`selection.min_sweep_angle_deg` are dropped.

Grouping pairs a `contiguous_surveillance` sweep with its matching
`contiguous_doppler` sweep; single-elevation waveforms
(`staggered_pulse_pair`, `batch`) stand alone, and a volume naming none of these
waveforms is grouped by raw elevation instead. For a paired low elevation,
`DBZH` is persisted only from the surveillance sweep and skipped for the doppler
sweep (`src/EWMRS/render/nexrad.py`), so an elevation folder holds exactly one
`DBZH` file rather than two.

## WPC Surface Analysis Endpoints

WPC outputs are served from:

```text
<BASE_DIR>/wpc/surface_analysis/
```

The current API supports only the surface-analysis type `sfc`.

### GET /wpc/fetch?type=sfc

Lists available timestamped WPC surface-analysis GeoJSON artifacts. The route scans files matching `wpc_sfc_YYYYMMDD-HHMMSS.geojson`, ignores `latest.geojson`, and returns timestamps newest first. The ingest side only ever writes hour-aligned names (`HH0000`) —
`config/wpc.yaml` `output_filename_pattern` hardcodes the `0000` minutes and
seconds — and `update_interval_hours` is `3`, so in practice you see one file
per 3-hour analysis time. The reader itself accepts any valid time.

Responses:

- `200`: `string[]`; returns `[]` when the WPC output directory does not exist
- `400`: missing or unsupported `type`
- `500`: directory read failure

Example:

```json
["20260604-120000", "20260604-090000"]
```

### GET /wpc/download?type=sfc&timestamp={YYYYMMDD-HH0000}

Returns the parsed GeoJSON payload from:

```text
<BASE_DIR>/wpc/surface_analysis/wpc_sfc_{timestamp}.geojson
```

WPC surface-analysis artifacts are written on 3-hour analysis times, so the timestamp form is `YYYYMMDD-HH0000`.

Responses:

- `200`: GeoJSON object
- `400`: missing/unsupported `type`, missing timestamp, malformed timestamp, or resolved path escaping the WPC root
- `404`: requested timestamp file not found
- `500`: read/parse failure

## GOES Product Notes

GOES products available through the render routes are the GUI folder names below.

### Single-Channel GOES Products

| Product | Backing render |
| --- | --- |
| `GOES_ABI_C01` | `GOES_ABI_C01_Reflectance` |
| `GOES_ABI_C02` | `GOES_ABI_C02_Reflectance` |
| `GOES_ABI_C03` | `GOES_ABI_C03_Reflectance` |
| `GOES_ABI_C04` | `GOES_ABI_C04_Reflectance` |
| `GOES_ABI_C05` | `GOES_ABI_C05_Reflectance` |
| `GOES_ABI_C06` | `GOES_ABI_C06_Reflectance` |
| `GOES_ABI_C07` | `GOES_ABI_C07_BrightnessTemp` |
| `GOES_ABI_C08` | `GOES_ABI_C08_BrightnessTemp` |
| `GOES_ABI_C09` | `GOES_ABI_C09_BrightnessTemp` |
| `GOES_ABI_C10` | `GOES_ABI_C10_BrightnessTemp` |
| `GOES_ABI_C11` | `GOES_ABI_C11_BrightnessTemp` |
| `GOES_ABI_C12` | `GOES_ABI_C12_BrightnessTemp` |
| `GOES_ABI_C13` | `GOES_ABI_C13_BrightnessTemp` |
| `GOES_ABI_C14` | `GOES_ABI_C14_BrightnessTemp` |
| `GOES_ABI_C15` | `GOES_ABI_C15_BrightnessTemp` |
| `GOES_ABI_C16` | `GOES_ABI_C16_BrightnessTemp` |

Behavior notes:

- GOES products are rendered from locally staged `ABI-L1b-RadC` inputs on a CONUS `EPSG:3857` target grid.
- RGB composites are a client-side derivation and are not rendered server-side.
- See `docs/core/goes_pipeline.md` for the full ingest, readiness, and render pipeline.
