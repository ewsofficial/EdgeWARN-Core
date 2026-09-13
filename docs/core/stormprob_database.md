# StormProb database (input schema v1)

The EdgeWARN publication process writes `<BASE_DIR>/data/stormprob/stormprob.sqlite3`.
It uses SQLite WAL, foreign keys, a five-second busy timeout, and one `BEGIN
IMMEDIATE` transaction per cycle. Inference and other readers use read-only
connections. The database is the source of model features; generated
`stormcells_*.json` and `cells/<id>.json` are compatibility projections for the
API and existing external CTAM file scopes. Those projections omit the private
`stormprob` input record. Do not delete old JSON during this rollout.

`cycles` stores the normalized UTC analysis time, selected source manifest,
publication state, and a recovery projection. `cell_observations` stores stable
cell identity, lineage, full-precision centroid and polygon, geometry status,
readiness, and a legacy-shaped projection. `feature_values` stores a 135-value
ordered float32 vector, the `stormprob-input/v1` schema and order checksum, raw
named values, units, source analysis times, and quality flags. `radial_profiles`
stores 64 float32 kilometer radii and polygon log area. `forecasts` has one row
per cell, analysis time, lead (15/30/45/60 minutes), and model version. Until
the models are deployed, the four `stormprob-pending/v1` rows explicitly report
`not-computed/model-not-deployed` or `skipped/input-not-ready`; they are not
predictions. A later real-model row can be inserted in the same cycle
transaction via `commit_cycle(..., forecasts=...)`.

Centroids and detection polygons are `[latitude, longitude]` in the source
domain. The forecast polygon contract will use GeoJSON `[longitude, latitude]`
in Phase 4. Numeric vectors are little-endian IEEE 754 float32 blobs of exactly
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

The packaged StormProb ONNX graphs use a fixed batch size of 128. During each
CTAM cycle, ready cells are processed in chunks of up to 128, with zero padding
for the last chunk. Skipped or invalid cells do not occupy a model slot; each
successful output is mapped back to its original cell before contour
postprocessing and alert handling. `inference_duration_ms` records a cell's
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
missing with the Phase 1 sentinel/quality policy. A pre-import SQLite backup is
written under `data/stormprob/backups/`; the seven newest managed backups are
retained. The importer runs SQLite integrity and foreign-key checks after
completion. To roll back the runtime, restore the previous JSON/API projection
and stop reading the new database; leave the StormProb database and model
assets intact for investigation.

Successful realtime publication also makes one online SQLite backup per UTC
day under the same managed backup directory and retains the seven newest. A
backup failure is logged without changing the already committed cycle.

On runtime startup, recovery first completes prepared CTAM JSON journals. A
journal produced by the database-enabled pipeline records its committed cycle
dependency, so recovery refuses to expose JSON if that cycle is missing from
SQLite. It then recreates missing public snapshots and cell histories from
cycles marked `projection_state=pending` and rebuilds API indexes. Successfully
published cycles are marked `published`, so intentional retention cleanup does
not resurrect old JSON. Detection writes its intermediate snapshot for
integration but does not add it to the public index; integration updates the
index after the database and final JSON commit. This preserves the
existing external CTAM history read scope while its contract migrates. CTAM
readiness still validates the derived JSON file because the current external
module file descriptor schema promises a readable file; that contract must be
updated before the projection is removed.
