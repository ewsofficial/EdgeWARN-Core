# EdgeWARN API Compatibility Endpoints

The live API is implemented in `src/api/` and exposes `/api/v3` as its primary
contract. The v2 routes described here are compatibility adapters, not a
separate service under `src/EdgeWARN/api`.

For backing file schemas, see `docs/api/data_keys.md`.

## API Overview

- Base URL: `/api/v2`
- Response format: JSON
- Version behavior:
  - `2.x` when `NODE_ENV=production` (`api.yaml`
    `server.production_version_label`)
  - the `package.json` version otherwise, currently `3.0.0`

Every route in this document is a compatibility adapter. Each response carries
`Deprecation: true` and `Link: </api/v3/openapi.json>; rel="deprecation"`, and
none of them sets `Cache-Control` — the tuned cache lifetimes in
`api.yaml` `cache_control_max_age` apply to `/api/v3` only. The v3 router's
query-parameter allowlist is likewise not applied here.

### Error responses

Failures raised inside the shared services are `ArtifactError`s and reach the
one global handler, which answers `application/problem+json` with
`Cache-Control: no-store` and a `requestId` member. The status is derived from
the error code, not chosen per route:

| Code | Status | Cause |
| --- | --- | --- |
| `NOT_FOUND` | `404` | missing file or directory |
| `INVALID_ARTIFACT` | `503` | artifact over its size limit, or failing a format invariant |
| `IN_PROGRESS` | `503` | artifact unparseable, typically a partially written JSON file |
| `INVALID_PATH` | `400` | rejected identifier, timestamp, traversal attempt, or symlink |

A malformed on-disk JSON file therefore surfaces as `503`, not `500`; `500` is
reserved for an unexpected non-`ArtifactError` throw. The one place a
compatibility route builds its own body is the mutually-exclusive-parameter
rejection on the alert routes, described below.

## Root Endpoints

### GET /

Returns unified-service banner metadata. This is served by the unified app, not
by the v2 adapter, so it carries no `Deprecation` header.

Response:

```json
{
  "service": "EdgeWARN Unified API",
  "version": "3.0.0",
  "links": {
    "api": "/api/v3",
    "openapi": "/api/v3/openapi.json"
  }
}
```

### GET /api/v2

Returns API metadata and route map.

Response:

```json
{
  "message": "EdgeWARN API v2",
  "version": "3.0.0",
  "endpoints": {
    "features": {
      "cells": "/api/v2/features/cells[?id={int}]",
      "timestamps": "/api/v2/features/timestamps[?timestamp={YYYYMMDD-HHMMSS}]",
      "alerts": {
        "official": "/api/v2/features/alerts/official[?id={id}|timestamp={YYYYMMDD-HHMMSS}]",
        "edgewarn": "/api/v2/features/alerts/edgewarn[?id={id}|timestamp={YYYYMMDD-HHMMSS}]"
      }
    },
    "data": {
      "metar": "/api/v2/data/metar[?timestamp={YYYYMMDD-HHMMSS}]"
    }
  }
}
```

## Feature Endpoints

### GET /api/v2/features/cells

Query:

- `id` (optional): positive integer

Behavior:

- Without `id`: returns `cell_index.json` IDs
- With `id`: returns `cells/{id}.json`

Responses:

- `200` list mode: `number[]` (the `cellIds` member of the index)
- `200` id mode: JSON object from file (passthrough)
- `200` list mode fallback when `cell_index.json` is absent: `[]`
- `400`: `id` is not a positive integer
- `404`: cell file not found

### GET /api/v2/features/timestamps

Query:

- `timestamp` (optional): `YYYYMMDD-HHMMSS`

Behavior:

- Without `timestamp`: returns `stormcell_index.json` timestamps
- With `timestamp`: returns `stormcells_{timestamp}.json`

Responses:

- `200` list mode: `string[]` (the `timestamps` member of the index)
- `200` list mode fallback when `stormcell_index.json` is absent: `[]`
- `200` timestamp mode: stormcell JSON payload (passthrough)
- `400`: invalid timestamp
- `404`: snapshot not found

### GET /api/v2/features/alerts/official

### GET /api/v2/features/alerts/edgewarn

Query (mutually exclusive):

- `timestamp` (optional): `YYYYMMDD-HHMMSS`
- `id` (optional): alert identifier string

Behavior:

- Without params: returns available snapshot timestamps
- With `timestamp`: returns snapshot `alerts` array from `{timestamp}.json`
- With `id`: returns a specific alert payload from `ids/{safe_id}.json`

Responses:

- `200` list mode: `string[]`
- `200` timestamp mode (`official`): array of official alert summaries such as:
  - `id`, `name`, `urn_oid`, `effective`, `expires`, `severity`, `geometry`
- `200` timestamp mode (`edgewarn`): array of EdgeWARN alert summaries such as:
  - `id`, `severity`
- `200` timestamp mode when the snapshot file is absent: `[]`
- `200` id mode: returns the stored alert object, with an automatic unwrap to the nested `feature` payload when one is present. The unwrap applies uniformly to both `official` and `edgewarn` endpoints — official records always carry a `feature`, while edgewarn records typically do not and so return as-is.
- `400` when `timestamp` and `id` are both supplied, and only in that case, the
  legacy envelope is returned instead of `application/problem+json`:

```json
{
  "success": false,
  "error": {
    "code": "INVALID_INPUT",
    "message": "Parameters timestamp and id cannot be specified at the same time"
  }
}
```

Every other failure on these routes uses the shared problem+json handler.

Validation:

- `timestamp` and `id` cannot be sent together
- `timestamp` must match `YYYYMMDD-HHMMSS` and pass calendar validation
- `id` must match `^[A-Za-z0-9_.:-]{1,200}$`; colons are rewritten to `_` when
  resolving the on-disk `ids/{safe_id}.json` name
- the `{source}` path segment must be exactly `official` or `edgewarn`; anything
  else is a `400`

Timestamp-mode note:

- The API returns the snapshot file's `alerts` array only. It does not return the wrapper object stored on disk.

## Data Endpoints

### GET /api/v2/data/metar

Query:

- `timestamp` (optional): `YYYYMMDD-HHMMSS`

Behavior:

- Without `timestamp`: lists timestamps derived from `METAR_YYYYMMDD-HHz.json`
- With `timestamp`: reads matching hourly file and wraps as:

```json
{
  "type": "metar",
  "timestamp": "YYYYMMDD-HHMMSS",
  "data": []
}
```

Responses:

- `200` list mode: `string[]`, newest first. Each hourly file contributes one
  `YYYYMMDD-HH0000` value, so the minutes and seconds are always zero.
- `200` timestamp mode: wrapper object above. Only the `YYYYMMDD-HH` prefix
  selects the file; the minutes and seconds in the request are echoed back in
  `timestamp` but do not narrow the result.
- `400`: invalid timestamp
- `404`: hourly METAR file not found

## Other Routes

## EWMRS Render Products

The unified API's EWMRS compatibility adapter exposes tiled GUI products through:

- `GET /renders/get-items`
- `GET /renders/fetch?product={product}`
- `GET /renders/download?product={product}&timestamp={YYYYMMDD-HHMMSS}`
- `GET /renders/tile?product={product}&timestamp={YYYYMMDD-HHMMSS}[&x={int}&y={int}]`
- `GET /renders/tile-info?product={product}`

For `/renders/tile`, supplying both `x` and `y` returns a PNG tile; omitting both returns the valid tile coordinates listed in the timestamp folder's `index.json`.

GOES products exposed through those routes include:

- scalar ABI folders `GOES_ABI_C01` through `GOES_ABI_C16`

Behavior notes:

- ABI single-channel products are generated from staged `ABI-L1b-RadC` channels on the GOES CONUS `EPSG:3857` tile grid.
- Missing or time-misaligned channels skip only the affected layer; other GOES products continue rendering.
- Current GOES renders are tile-first; `/renders/download` only resolves the legacy flat PNG naming contract when such files exist.
- Listing mode reads the timestamp folder's `index.json` and never scans the
  directory for filenames. It looks for a `tiles` array, which is the legacy
  key. Schema-version-2 indexes written by the current renderers publish
  `chunks` instead, so against a freshly rendered product this route reports
  `tiles: []` with a valid `tile_grid` rather than failing. That is not an error
  condition; it means the product has no PNG tiles to list.

See `docs/api/ewmrs_api_endpoints.md` for the full EWMRS route contracts and `docs/core/goes_pipeline.md` for the ingest-to-render flow.

New clients should use the unified API chunk contract:

- `GET /api/v3/render-products/{productId}/snapshots/{timestamp}/chunks`
- `GET /api/v3/render-products/{productId}/snapshots/{timestamp}/chunks/{x}/{y}`

The second resource is `application/octet-stream`, not PNG. It returns exact
scalar float16 value chunks (gzip-compressed, `NaN` no-data) with top-to-bottom
rows and bottom-left chunk-grid coordinates.
The index's sparse `chunks` list is authoritative; omitted chunks are
transparent. Legacy `/renders/download` and `/renders/tile` remain PNG-only
and return missing-artifact responses when no compatibility PNG exists.

## EWMRS RAP Uint16 Products

The EWMRS service also exposes RAP Uint16 array outputs from `<BASE_DIR>/gui/RAP` through:

- `GET /rap/layers`
- `GET /rap/mappings`
- `GET /rap/fetch?layer={layer}`
- `GET /rap/metadata?layer={layer}&timestamp={YYYYMMDD-HHMM00}`
- `GET /rap/data?layer={layer}&timestamp={YYYYMMDD-HHMM00}`

RAP timestamp folders are minute-aligned as `YYYYMMDD-HHMM00`. `/rap/data` returns raw little-endian `uint16` bytes with `65535` reserved as no-data. Clients should use the matching `/rap/metadata` response for shape, scale, units, and GRIB metadata. RAP layer names are the on-disk folders under `gui/RAP`, such as `Temperature_2m`, `CAPE_0-3km`, or `UWind_925mb`.

## EWMRS WPC Surface Analysis

The EWMRS service exposes WPC surface-analysis GeoJSON artifacts through:

- `GET /wpc/fetch?type=sfc`
- `GET /wpc/download?type=sfc&timestamp={YYYYMMDD-HH0000}`

These routes read analysis-hour files from `<BASE_DIR>/wpc/surface_analysis/wpc_sfc_{timestamp}.geojson`, where `timestamp` uses the form `YYYYMMDD-HH0000`. See `docs/api/ewmrs_api_endpoints.md` for full response semantics.

### GET /health

Response:

```json
{
  "status": "OK",
  "timestamp": "2026-01-01T00:00:00.000Z"
}
```

### GET /healthz

Response:

```json
{
  "ok": true
}
```

Both are fixed-body compatibility stubs that inspect nothing. The live checks
are `/health/live` and `/health/ready`; only `/health/ready` touches the
filesystem, returning `503` when any of the `data`, `gui`, or `wpc` roots is
missing.

## Service Visibility (`SERVICE_NOT_ENABLED`)

Each Python runtime service publishes an atomic heartbeat beneath
`<BASE_DIR>/state/realtime/services/<name>.json`; the filenames are the
canonical service registry: `edgewarn`, `ewmrs`, and `nexrad`. A heartbeat is
classified as one of:

| State | Meaning |
| --- | --- |
| `active` | fresh heartbeat within the `api.yaml` `server.service_stale_after_seconds` threshold |
| `stale` | heartbeat present but expired — the service crashed, hung, or was killed without cleanup |
| `disabled` | no heartbeat file — never started or intentionally omitted |
| `degraded` | active but reporting degraded children; degraded services still serve requests |
| `unsupported-schema` | file exists but fails schema validation |

Route families declare exactly one required service. Enforced families:

| Required service | Route families |
| --- | --- |
| `edgewarn` | `/api/v3/cells*`, `/api/v3/storm-snapshots*`, `/api/v3/alert-snapshots*`, `/api/v3/alerts*`, and legacy `/api/v2/features/*` adapters |
| `ewmrs` | `/api/v3/render-products*`, `/api/v3/models/rap/*`, `/api/v3/analyses/wpc/*`, `/api/v3/styles/colormaps`, and the legacy `/renders/*`, `/rap/*`, `/wpc/*`, `/colormaps` adapters |
| `nexrad` | `/api/v3/radar-sites*` and the legacy `/nexrad/*` adapters |

When the required service is not active, requests receive
`503` instead of silently serving stale artifacts.

Gated legacy responses retain the `Deprecation: true` and `Link: </api/v3/openapi.json>; rel="deprecation"` headers.

v3 routes answer with the problem+json envelope plus extension members:

```json
{
  "type": "about:blank",
  "title": "Service Not Enabled",
  "status": 503,
  "code": "SERVICE_NOT_ENABLED",
  "service": "nexrad",
  "state": "disabled",
  "lastSeen": null,
  "requestId": "…"
}
```

Legacy compatibility routes answer with the compatibility envelope:

```json
{
  "success": false,
  "error": {
    "code": "SERVICE_NOT_ENABLED",
    "message": "Required service is not active",
    "service": "nexrad",
    "state": "stale",
    "last_seen": "2026-01-01T00:00:00.000Z"
  }
}
```

`last_seen` carries the heartbeat's `updated_at` when present, so operators
can tell "turned off on purpose" (`disabled`) apart from "crashed" (`stale`).

`GET /health/ready` keeps its directory-based status contract and additionally
reports a diagnostic `services` block summarizing each canonical name's state;
it does not flip to `503` solely because an optional service is disabled.

### GET /robots.txt

Serves `text/plain` disallowing every path for every user agent.

### Legacy v1-style routes

- `/features/*`
- `/data/*`
- `/api/v1*`

All return `410 Gone` with migration guidance to `/api/v2`.

## Security and Platform Behavior

- Helmet security headers and compression are enabled. Compression skips the
  media types matched by `api.yaml` `security.compression_skip_media`, which is
  `image/*`.
- CORS is deny-all by default in **every** environment, production or not.
  `ALLOWED_ORIGINS` (or `api.yaml` `security.allowed_origins`) is an exact
  allowlist of bare `scheme://host[:port]` origins; an origin absent from it and
  a request with no `Origin` header are both refused, and `*` is rejected
  outright. There is no permissive non-production branch — that behavior
  belonged to the removed `src/EdgeWARN/api/server.js`.
- Credentialed cross-origin requests are not supported: `security.cors.credentials`
  is schema-pinned to `false`, and the allowed methods and headers can be
  narrowed by YAML but not widened.
- Global rate limiting uses two windows, both from `api.yaml` `rate_limits`:
  - `40` requests per second
  - `2000` requests per minute
- Overrides are environment variables, not CLI flags:
  - `RATE_LIMIT_MAX_SEC`
  - `RATE_LIMIT_MAX_MIN`
  - A value of `0` disables that window outright rather than making it unlimited
  - Window durations are YAML-only (`window_ms`) and take effect on restart
- Trusted reverse proxies (`TRUST_PROXY_IPS`, or `TRUST_PROXY` in
  non-production): only enable these when a stripping reverse proxy removes
  client-supplied `X-Forwarded-For`/`X-Forwarded-Proto` headers before
  forwarding. If trust is enabled on a directly exposed host, clients can
  spoof forwarded headers and bypass per-client rate limiting. The default
  (off) is correct for directly exposed deployments.
