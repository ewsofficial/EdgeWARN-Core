# Ingestion Architecture

EdgeWARN-Core uses shared ingest modules in `src/common/ingest` so EdgeWARN and EWMRS can consume the same staged data products.

## Module Layout

```text
src/common/ingest/
├── mrms/                  # MRMS + GOES discovery/download and staging
├── nws/                   # NWS active alert ingest + registry/snapshots
├── synoptic/              # RAP ingest
├── nexrad/                # NEXRAD Level II ingest, parser, and pipeline
├── metar.py               # METAR ingest and parsing
├── wpc/                   # WPC surface analysis ingest and GeoJSON conversion
└── aws_async_compat.py    # AWS async/sync compatibility shim used by ingest helpers
```

## Staged Shared Ingest

`src/common/pipeline/coordinator.py` (`run_staged_ingest_cycle`) drives staged readiness for the primary cycle:

1. Detection inputs ready (MRMS detection subset) — released to the EdgeWARN worker immediately
2. Render MRMS inputs ready (detection + MRMS integration subset) — committed as a durable `mrms-ready` record for the EWMRS service
3. Raw RAP validated — committed as a durable `rap-ready` record
4. Base EdgeWARN integration inputs ready (both MRMS groups + raw RAP)
5. EdgeWARN integration inputs ready (base inputs + scan-time GLM when enabled)

Since the decomposition, cross-service handoff is durable: `run_edgewarn.py`
publishes the immutable phase records under
`<BASE_DIR>/state/realtime/cycles/<cycle-id>/`, and `run_ewmrs.py` consumes
them in order, rendering from each record's exact pinned paths with per-phase
consumer checkpoints. GOES ABI ingest and rendering run entirely inside the
EWMRS service as a poll-based loop over locally staged ABI files; NEXRAD runs
entirely as its own service. The RAP Uint16Array conversion is an
EWMRS-owned artifact executed by the consumer after it accepts a rap-ready
record.

## MRMS + GOES

`src/common/ingest/mrms/main.py` provides async-first ingestion with sync fallback paths.

Key entry points:

- `download_all_files_async(dt, max_entries=None, remove_old_files=None)`
- `download_detection_files_async(dt, ...)`
- `download_integration_files_async(dt, ...)`
- `download_ewmrs_files_async(dt, ...)`
- `download_all_files(dt, ...)` (async wrapper with sync fallback)

`max_entries` and `remove_old_files` default to `None` on every entry point so
the catalogs stay the single owner. They resolve to
`runtime.yaml` `cycle.ingest_max_entries` and `ingest.yaml`
`mrms.remove_old_files` respectively; a caller-supplied value still wins.

Notes:

- Detection and integration modifiers are staged separately
- GOES ingestion can run as part of the full ingest cycle or as a decoupled background loop in realtime mode
- GOES ABI staging uses `ABI-L1b-RadC` channel files from `noaa-goes19`; GLM staging remains a separate modifier path
- GOES RGB composites are not staged or rendered server-side; they are derived client-side from the staged ABI channel set
- Cleanup is constrained to configured runtime directories

See `docs/core/goes_pipeline.md` for the end-to-end GOES readiness and render flow.

## NWS Alerts

`src/common/ingest/nws/main.py` downloads active alerts from `https://api.weather.gov/alerts/active`, applies GeoMapper processing, and updates the alert registry.

Before starting a pipeline that ingests NWS alerts, synchronize the required
zone assets with `edgewarn sync-nws-zones --apply`. The pipeline fails with a
clear preflight error if no zone assets are available. The command wraps the NWS
asset maintenance utility and supports:

- `--assets-dir`
- `--zone-types`
- `--timeout-seconds`
- `--max-retries`
- `--max-workers`
- `--pause-seconds`
- `--progress` / `--no-progress`
- `--apply` (without it, the command performs a dry run)
- `--report-path`
- `--config-path`

Key behavior:

- Blocklist filtering for non-target event types
- Deduplicated alert tracking with `first_seen`, `last_seen`, and `expires`
- Timestamp snapshot generation for API serving
- TTL cleanup for stale registry and snapshot files

Primary entry points:

- `download_alerts(dt)`
- `download_alerts_async(dt)`

## RAP / Synoptic

`src/common/ingest/synoptic/main.py` stages RAP files for integration.
When the EWMRS service consumes a committed rap-ready record, it runs the RAP Uint16Array conversion pipeline for configured layers.

Entry points:

- `download_rap_async(dt)`
- `download_rap(dt)`

RAP selection is local-first and searches backward from the requested UTC
analysis hour. Candidates must be no older than the configured analysis-age
limit, which defaults to 180 minutes and can be overridden with
`EDGEWARN_RAP_MAX_AGE_MINUTES`. The limit is measured from the requested scan
timestamp to the analysis timestamp encoded in the RAP filename; filesystem
modification time is not used as a freshness signal.

Definitive S3 404 responses advance to the next eligible analysis without
repeating the same key through the synchronous client. Transport or
authentication failures may receive one synchronous source fallback. A selected
file is logged with its analysis timestamp, age, source, and local path. If the
window is exhausted, the RAP readiness error records the configured limit and
the result for every checked S3 key.

RAP cache cleanup uses the same encoded analysis timestamps and staleness
policy. It retains at most the newest three eligible analyses under
`<BASE_DIR>/data/RAP`.

RAP Uint16Array conversion is configured by the `rap_uint16` section of
`config/ewmrs_pipeline.yaml`, read through the accessors in
`src/EWMRS/rap/config.py`. It writes one raw little-endian `data.u16` file per
configured data layer:

```text
<BASE_DIR>/gui/RAP/<outdir>/<YYYYMMDD-HHMM00>/data.u16
<BASE_DIR>/gui/RAP/<outdir>/<YYYYMMDD-HHMM00>/metadata.json
```

The path segment is the layer's `outdir`, which is deliberately distinct from
its `name`: the layer named `RAP_Temperature_2m` writes to `Temperature_2m`.
`outdir` is relative to `<BASE_DIR>/gui/RAP`.

Each `data.u16` contains the full `Ni * Nj` grid from one matched RAP GRIB
message. Each `metadata.json` records the layer name, timestamp, source file,
array shape, grid, dtype, byte order, scale, missing-value sentinel, units, and
the matched GRIB keys needed to reconstruct and render values from a browser
`Uint16Array`. `colormap_key` and `description` are written only when the layer
declares them, so consumers must treat both as optional.

RAP `colormap_key` values are stable and discoverable through `GET /colormaps`.
They are *not* layer names: several layers share one key, so `RAP_Temperature_2m`
resolves to `RAP_Temperature_LL` and both 10 m wind components resolve to
`RAP_Wind_LL`. Colormap definitions follow NOAA/SPC/GEMPAK lineage where
practical machine-readable standards are available and use documented project
fallbacks for remaining variables.

## METAR

`src/common/ingest/metar.py` ingests hourly METAR cycle files, parses reports, enriches station coordinates from the station cache, filters to CONUS bounds, and writes hourly JSON snapshots.

Entry points:

- `ingest_metars()`
- `ingest_metars_async()`

Output file pattern:

- `METAR_YYYYMMDD-HHz.json`

## NEXRAD Level II

`src/common/ingest/nexrad` manages realtime Level II discovery, chunked S3 download, parsing, and staged output under `<BASE_DIR>/data/NEXRAD_Level2`.

Current components include:

- `main.py` / `service.py`: realtime ingest entry points
- `coordinator.py`: volume coordination
- `s3_chunks.py`, `s3_async.py`, `worker_pool.py`, `worker.py`: download and worker execution
- `parser.py`, `models.py`, `writer.py`: Level II parsing and staged artifact writing
- `pipeline/`: station filtering, volume discovery, pending-volume models, and a standalone `__main__.py`

EWMRS polls staged NEXRAD outputs and writes gzip-compressed polar intermediate fields to:

```text
<BASE_DIR>/gui/NEXRAD/<SITE>/<ELEVATION>/<SITE>_<PRODUCT>_<ELEVATION>_<YYYYMMDD-HHMMSS>.bin.gz
```

Those files are served through the EWMRS `/nexrad` routes documented in `docs/api/ewmrs_api_endpoints.md`.

## WPC Surface Analysis

`src/common/ingest/wpc/main.py` fetches coded surface analysis, parses it, converts it to GeoJSON, and writes:

- `latest.geojson`
- timestamped `wpc_sfc_YYYYMMDD-HH0000.geojson` artifacts

Entry points:

- `fetch_surface_analysis(dt=None, save_timestamped=False)`
- `run_wpc_ingest()`
