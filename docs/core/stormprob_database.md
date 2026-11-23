# StormProb database (input schema v1)

The EdgeWARN publication process writes `<BASE_DIR>/data/stormprob/stormprob.sqlite3`.
It uses SQLite WAL, foreign keys, a five-second busy timeout, and transactional
cycle writes. The pre-CTAM pending-observation commit and the later forecast/
projection publication are currently separate transaction boundaries; they are
not one atomic transaction. Inference and other readers use read-only
connections. The database is the source of model features; generated
`stormcells_*.json` and `cells/<id>.json` are compatibility projections for the
API and existing external CTAM file scopes. Those projections omit the private
`stormprob` input record. Do not delete old JSON during this rollout unless the
migration/retention policy explicitly permits it; realtime cleanup can remove
inactive files.

`cycles` stores the normalized UTC analysis time, selected source manifest,
publication state, and a recovery projection. `cell_observations` stores stable
cell identity, lineage, full-precision centroid and polygon, geometry status,
readiness, and a legacy-shaped projection. `feature_values` stores a 135-value
ordered float32 vector, the `stormprob-input/v1` schema and order checksum, raw
named values, units, source analysis times, and quality flags. `radial_profiles`
stores 64 float32 kilometer radii and polygon log area. `forecasts` has one row
per cell, analysis time, lead (15/30/45/60 minutes), and model version. The
packaged `stormprob/v2` joint model is deployed. Pending companion rows can still
explicitly report `not-computed/model-not-deployed` or
`skipped/input-not-ready`; they are not predictions. Forecast rows can be
inserted through `commit_cycle(..., forecasts=...)` after inference.

Centroids and detection polygons are `[latitude, longitude]` in the source
domain. Operational forecast polygons use GeoJSON `[longitude, latitude]` and
are adaptive 4–12 point envelopes spanning the source and predicted polygons,
with a 1 km metric buffer. Numeric vectors are little-endian IEEE 754 float32 blobs of exactly
135 or 64 elements. SQL numeric fields must be finite; missing model channels
use the pinned `-999` sentinel. JSON serialization rejects `NaN` and infinity;
non-finite legacy projection numbers become `null`.

The writer upserts `(cell_id, analysis_time, feature_schema_version)` and
`(cell_id, analysis_time, lead_minutes, model_version)` idempotently. Reprocessing
defaults to `forecast_policy="invalidate"`: it removes forecasts from deployed
model versions for the touched observation before replacing inputs. The
explicit `"preserve"` option keeps them, for controlled replay only. No
forecast is silently reused after input revision. Model tensors are rebuilt by
`StormProbRepository.model_inputs` solely from committed `feature_values`,
`radial_profiles`, and observation centroids; the 30-row window is chronological
and left-padded.

Realtime publication prunes a cell's StormProb forecasts and observations once
its newest observation is older than
`api_index.stormprob_inactive_cell_max_age_minutes` (20 minutes by default),
independently of compatibility cell-file retention. Feature values and radial
profiles cascade with their observation. Historical runs do not prune, and pruning is skipped when StormProb or CTAM is disabled. After
deletion the database is vacuumed so freed pages are returned to the
filesystem and the file shrinks on disk. Pruned IDs are removed from stored cycle projections, and old cycle
rows are deleted once no retained observation or forecast references them;
cycles shared with retained cells remain with the pruned IDs removed.

The packaged StormProb ONNX graphs use a fixed batch size of 128. During each
CTAM cycle, ready cells are processed in chunks of up to 128, with zero padding
for the last chunk. Skipped or invalid cells do not occupy a model slot; each
successful output is mapped back to its original cell before operational
geometry postprocessing and alert handling. `inference_duration_ms` records a cell's
elapsed time through input preparation, shared batch execution, and its own
postprocessing, so it is not the isolated ONNX kernel time. Re-exporting the
graphs requires the pinned checkpoints and `scripts/export_stormprob_batch128.py`.

Before cutover, inspect legacy files and then import them:

```bash
cd src
python -m EdgeWARN.stormprob.migrate --base-dir /path/to/runtime --dry-run
python -m EdgeWARN.stormprob.migrate --base-dir /path/to/runtime
```

The importer reads `data/stormcells/stormcells_*.json` first and
`data/cells/*.json` second (excluding `cell_index.json`). It checks entry
counts, identities, parseable timestamps, and SHA-256 hashes. Each source file
commits atomically; identical reruns skip it, and changed source bytes fail
closed. Legacy source files are retained. Fields absent from legacy JSON stay
missing with the Phase 1 sentinel/quality policy. The importer runs SQLite
integrity and foreign-key checks after completion. To roll back the runtime,
restore the previous JSON/API projection
and stop reading the new database; leave the StormProb database and model
assets intact for investigation.

At the start of a valid detection cycle, recovery first completes prepared CTAM
JSON journals; this is not a general service-startup recovery gate. A
journal produced by the database-enabled pipeline records its committed cycle
dependency, so recovery refuses to expose JSON if that cycle is missing from
SQLite. It then recreates missing public snapshots and cell histories from
cycles marked `projection_state=pending` and rebuilds API indexes. Successfully
published cycles are marked `published`, so intentional retention cleanup does
not resurrect old JSON. Detection writes its intermediate snapshot for
integration but does not add it to the public index; integration updates the
index after the database and final JSON commit. Recovery includes pending
cycles in the index only after their files have been restored; the projection
state changes to `published` after the index write. This preserves the existing
external CTAM history read scope while its contract migrates. CTAM
readiness still validates the derived JSON file because the current external
module file descriptor schema promises a readable file; that contract must be
updated before the projection is removed.

Forecast geometry failures are recorded per lead as `status: error` with
`reason: geometry-failed`. The module reports `partial` and retains successful
leads. Alerts require successful 15- and 30-minute leads; tracking requires a
successful 15-minute lead. Sampled rays are floored at 0.1 km about each
ensemble member's own centre. They never create opposite-direction boundary
points.

## Swept probability field artifacts

Publication writes one swept field per cycle to
`<BASE_DIR>/data/stormprob/fields/stormprob_field_<cycle>.bin.gz`. It is a
filesystem artifact, not a SQLite table, and holds no cross-scan state. The
format is `EWSPv2U1`: a 64-byte little-endian plain header (magic,
`analysis_unix_ms`, the 15/60-minute window, ensemble size 20, contributing cell
count, flags, domain `(first_lat, first_lon, dlat, dlon)`, crop origin, and crop
shape) followed by a gzip container (level 6, `mtime = 0`) of row-major `uint8`
member counts. The grid matches MRMS: values sit at 0.01° cell centres, row 0 is
the northernmost row, rows step south by `dlat` and columns step east by `dlon`.
`first_lat`/`first_lon` is the centre of the domain's north-west pixel and the
crop origin is the centre of the crop's north-west pixel, so the default
`[20, 55] × [230, 300]` domain starts at 54.995 N, 230.005 E like the MRMS CONUS
grid and every field pixel overlays one MRMS pixel. Counts are integers 0–20 and probability
is `count / 20`; there is no calibration and no float plane. The crop is the
union of the cell patches, processed in row bands capped by a pixel budget (at
most 256 rows) so a full-crop array is never allocated; a scan with no ready
cells writes only the header. The artifact is written only after the cycle's
JSON snapshots are committed, and `field_index.json` lists canonical
`YYYYMMDD-HHMMSS` cycle keys -- the same keys as `stormcell_index.json` and the
artifact file names. Cell
patches live on `cell["stormprob"]["field_patch"]` until publication and are
never persisted, so the public cell projection is unchanged. Field files are
retained by scan age through `APIIndexManager`
(`detection.stormcell_cleanup_max_age_minutes`) and listed in
`data/stormprob/fields/field_index.json`.
