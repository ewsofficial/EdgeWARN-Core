# Changelog

## [3.1.0] 2026-11-17

3.1.0 is a breaking release. It retires the EWMRS rendering service and its
accessory products. Read *Breaking: EWMRS retirement* and *Upgrade and rollback*
before deploying. EWMRS work developed during the 3.1.0 cycle never shipped.

### Changed: StormProb joint model (`stormprob/v2`)
- Replaced the radial model and isotonic calibrator with a joint radial/centroid
  ensemble. Probability is the raw 20-member fraction at the 0.25 threshold; the
  calibrator asset is no longer shipped. Forecast rows use `stormprob/v2`;
  leads with an empty contour are persisted as `no-polygon`, including when the
  module reports `partial`. `stormprob.audit` evaluates `stormprob/v2` rows only.

### Breaking: EWMRS retirement
- Removed the EWMRS service/package, raster chunk and RAP Uint16 production,
  render consumers/outbox/checkpoints, and GOES ABI/METAR/WPC accessories.
  NWS alert fetching is retained and moves to ingest (see below).
- Removed `edgewarn run ewmrs`, `--args ewmrs`, `run_ewmrs.py`,
  `--disable-ewmrs`, `EDGEWARN_DISABLE_EWMRS`, `--render-consumer`, and
  `--disable-metar/wpc/goes`. Unknown/retired topology errors point here.
  Core's existing scan-time GLM control is now `--disable-glm` with
  `runtime.run.disable_glm`; GLM enrichment remains supported.
- `all` now starts ingest, Core, and NEXRAD; `core` starts ingest and Core.
  MRMS-core-only mode disables RAP/GLM/NEXRAD. Core detection, tracking, CTAM,
  StormProb, alerts, and NEXRAD serialization remain supported. Official
  NWS alerts are still fetched (now by ingest) and matched/read.
- Removed `/api/v3/render-products`, `/api/v3/models/rap`,
  `/api/v3/analyses/wpc`, and `/api/v3/observations/metar` families and their
  binary/JSON readers. GET/HEAD and unsupported methods return 404 problems.
  OpenAPI/API contract version advances to 3.1.0. Legacy files cannot restore
  these routes. Health/service discovery lists ingest, edgewarn, nexrad only.
- Deleted `ewmrs_pipeline`, `ewmrs_render`, `metar`, and `wpc` YAML
  catalogs/schemas, ABI channel settings, accessory flags/cadences, and API
  render settings. The complete catalog now contains 14 YAML files, including
  `nws`.
  NEXRAD render cadence/retention/worker limits moved to `nexrad.gui` and its
  display mapping to `nexrad.variable_colormaps` without changing values.
- NWS active-alert fetching now runs as a background thread in the ingest
  service, every `runtime.background_intervals.nws_seconds` (60 by default), so it
  never blocks MRMS/RAP/GLM acquisition. `nws.yaml`, `edgewarn sync-nws-zones`,
  `run.disable_nws`, and `--disable-nws` (ingest; `--mrms-core-only` also disables
  it) are retained. Missing zone assets stop ingest startup unless NWS is disabled.
- The Docker image now downloads NWS zone geometry at build time (the build fails
  without it) and ingest (all deployments) exits at startup when zone assets are
  unavailable unless NWS fetching is disabled. Building requires network access to api.weather.gov.

### Upgrade and rollback

Stop all Python services and the API. Deploy code and the complete 14-file
configuration tree together; restart only after both validators pass:
`npm run validate-config` and `PYTHONPATH=src python -m common.config.validate`.
Remove retired CLI flags from service units and rename the GLM control.
Run `edgewarn sync-nws-zones --apply` if NWS zone assets are not yet present.
Set `api.server.enabled_services` to your deployed topology. Retired configuration
names fail validation; do not carry their catalogs or schemas into the new tree.

For rollback, stop new workers first and restore previous code and the complete
previous configuration tree together. Resume from Core/NEXRAD state. Never
translate render acknowledgments into Core success or invent legacy outputs.

### Optional operator cleanup

Existing operational artifacts and gitignored zone assets are left untouched.
No startup migration automatically purges retired data. With services stopped,
inspect the following paths beneath your resolved runtime root before removing
any of them: `gui/<layer>` (list below), `wpc/surface_analysis`,
`state/realtime/consumers/ewmrs-inputs-v1`, legacy `consumers/ewmrs-*.json`,
`state/realtime/services/ewmrs.json`, and `state/realtime/ingest/v1/render-ready`
and `render-plan`. Keep `gui/NEXRAD`, Core consumer state, and input pins.

Retired GUI folders:

`GOES_ABI_C01_Reflectance`, `GOES_ABI_C02_Reflectance`, `GOES_ABI_C03_Reflectance`, `GOES_ABI_C04_Reflectance`, `GOES_ABI_C05_Reflectance`, `GOES_ABI_C06_Reflectance`, `GOES_ABI_C07_BrightnessTemp`, `GOES_ABI_C08_BrightnessTemp`, `GOES_ABI_C09_BrightnessTemp`, `GOES_ABI_C10_BrightnessTemp`, `GOES_ABI_C11_BrightnessTemp`, `GOES_ABI_C12_BrightnessTemp`, `GOES_ABI_C13_BrightnessTemp`, `GOES_ABI_C14_BrightnessTemp`, `GOES_ABI_C15_BrightnessTemp`, `GOES_ABI_C16_BrightnessTemp`, `MRMS_EchoTop18`, `MRMS_EchoTop30`, `MRMS_FLASH`, `MRMS_MESH`, `MRMS_MergedAzShear_0-2kmAGL`, `MRMS_MergedAzShear_3-6kmAGL`, `MRMS_MergedReflectivityQC`, `MRMS_MergedRhoHV`, `MRMS_NLDN_CG_001min_AvgDensity`, `MRMS_PrecipFlag`, `MRMS_PrecipRate`, `MRMS_ProbSevere`, `MRMS_QPE`, `MRMS_ReflectivityAt0C`, `MRMS_ReflectivityAtLowestAltitude`, `MRMS_ReflectivityAtM15C`, `MRMS_ReflectivityAtM5C`, `MRMS_RotationTrack30min`, `MRMS_VII`, `MRMS_VIL`, `MRMS_VILDensity`, `RAP`, `maps`, `overlay_manifest.json`.

After inspecting those folders, an explicit cleanup example is:

```bash
runtime_root=/absolute/path/to/your/runtime
# Populate this list only with the inspected retired GUI folders listed above.
retired_layers=(RAP MRMS_QPE GOES_ABI_C13_BrightnessTemp)
for layer in "${retired_layers[@]}"; do
  rm -rf -- "$runtime_root/gui/$layer"
done
rm -rf -- "$runtime_root/wpc/surface_analysis" \
  "$runtime_root/state/realtime/consumers/ewmrs-inputs-v1" \
  "$runtime_root/state/realtime/services/ewmrs.json" \
  "$runtime_root/state/realtime/ingest/v1/render-ready" \
  "$runtime_root/state/realtime/ingest/v1/render-plan"
```

Earlier release notes retain retired names; live code, guides,
configuration, tests, and packaging contain no references to the retired name.

### Added
- Added a configurable MRMS product registry. Composite reflectivity, PrecipFlag,
  and ProbSevere are protected products used for discovery and mandatory MRMS
  readiness; the other 18 products are configurable additions. A packaged Core
  contract defines the protected products.
- Added the ingest v2 schema (`config/schema/ingest.v2.schema.json`), selected
  when `ingest.schema_version` is 2, and shipped `ingest.yaml` as schema
  version 2. Integration statistics accept a `product` field that uses the upstream product identity in place of a raw `filepath`.
- Added `runtime.run.disable_stormprob` and the `--disable-stormprob` /
  `--no-disable-stormprob` flags.
- Added `edgewarn migrate-mrms` to convert a v1 configuration and its raw MRMS
  directories to v2. It supports a dry run, `--apply`, `--resume`, and
  `--rollback`, and records progress in `<config-root>/.mrms-migration/journal.json`.
  Python workers and the Node API refuse to start while the journal is
  incomplete or corrupt.
- Added `scripts/qualify_mrms_release.py` to reproduce the offline MRMS
  acquisition measurements.
- Added an independently supervised ingest service (`src/run_ingest.py`) that
  owns realtime MRMS, raw RAP, and scan-time GLM acquisition. It polls on a
  fixed deadline grid, uses bounded executors and per-object retry with bounded
  backoff, and gives check inputs admission priority. `ingest` joins the Python
  and Node heartbeat registries as diagnostic liveness only and does not gate
  any API route.
- Added a durable ingest inventory and readiness handoff, a dedicated ingest
  producer descriptor (schema 2), and a replay path for committed inputs.
- Added a configurable MRMS acquisition budget (default 8 concurrent downloads
  and a 30-second optional-product deadline) so optional products can no longer
  hold up mandatory callbacks.
- Added CTAM and StormProb preflight checks at startup, before workers start.
- Added an effective MRMS registry descriptor. Readers require the producer's
  identity, contract version, registry fingerprint, and a fresh heartbeat from
  the same run before they consume MRMS inputs.
- Added MRMS registry documentation: `docs/core/configurable-mrms-phase*.md`
  and updates to `docs/core/ingestion.md`, `configuration.md`, and `integration.md`.
- Added a Docker-first quickstart. The processing image now runs the Node API
  beside the Python services by default and publishes it on port 5000; set
  `EDGEWARN_WITH_API=0` for processing only.
- Added `detection.alert_matching.snapshot_max_age_seconds`. Cell matching
  rejects snapshots older than this age and filters expired warnings.
- Added `docs/core/production_hardening.md` describing failure recovery.

### Changed
- Core no longer performs source acquisition in realtime. It validates a
  committed start record and waits locally for integration readiness. It selects
  the oldest complete check set from durable local readiness, records skipped or
  overflowed scans explicitly, and no longer holds the input lease for a whole
  cycle. It also stops publishing the legacy render triggers and the old
  producer descriptor.
- Realtime MRMS ingest phases are now independent and replayable.
  Integration references use full upstream product identities, while API v3
  product identities are unchanged.
- Detection and integration read MRMS inputs through validated manifest records
  by family and product, with explicit previous-scan roles and no fallback to
  arbitrary latest files.
- Disabled MRMS products are filtered out before path resolution. Integration
  warns once per disabled statistic. `MRMS_*_DIR` Python aliases remain
  supported through the 3.x series.
- Raw MRMS directories are renamed to canonical names (ten renames, eleven
  unchanged) by the migration command.
- Increased storm-snapshot and inactive-cell retention
  (`detection.stormcell_cleanup_max_age_minutes`) from 120 to 360 minutes and
  reduced raw MRMS retention (`ingest.mrms.cleanup_max_age_minutes`) from 60 to
  10 minutes. `scheduler.s3_lookback_hours` is no longer validated against raw
  retention.
- Cleanup never deletes the newest file in a directory or the newest observation
  of any ingest product, so a lone file is retired only after a newer one exists.
- The launcher restarts an individual failed service with `runtime.supervisor`
  backoff and restart limits while siblings keep running, and exits nonzero only
  when every service has stopped. Shutdown stops all owned process groups.
  Compose uses `restart: unless-stopped` and a read-only heartbeat health check.
- Container logs use a circular rotation budget from `runtime.logging`
  (`retention_files` files of `max_file_mb` MiB each).
- Indexes are published once per cycle. Pagination rejects a cursor whose item
  has been pruned with 400 so the client can restart the collection.

### Fixed
- Fixed a NEXRAD worker-pool recycle race: a volume that grabbed the pool just
  before `record_volume_and_maybe_recycle` retired it hit `cannot schedule new
  futures after shutdown` and logged "worker parse failed". Submission now
  retries on a fresh pool (at most 3 attempts); other errors are unchanged.
- StormProb pruning is skipped when StormProb or CTAM is disabled.
- StormProb inference is skipped for predicted-only cells. StormProb records an
  explicit skipped forecast for them, and they no longer fail the whole cycle.
- StormProb forecast geometry failures are recorded per lead as
  `reason: geometry-failed`; successful leads are retained, and nonpositive
  calibrated rays end at the forecast centroid instead of creating
  opposite-direction boundary points.
- Kalman prediction applies eastward velocity to longitude and northward velocity
  to latitude, with acceleration and process-noise axes in the same order.
  Infeasible gated Hungarian matrices retain finite partial matches.
- Raster processing exceptions produce `PROCESSING_ERROR` instead of a numeric
  measurement. Detection skips cells without finite radar centroids, and atomic
  JSON writers reject nonfinite numbers before replacing an existing artifact.
- Tracking starts fresh when its previous cycle exceeds inactive-cell retention.
- Orphaned cell-history files and committed CTAM journals are cleaned up with
  inactive cells. A corrupt CTAM journal is isolated, and a transient SQLite lock
  leaves prepared work for later recovery. Per-cycle StormProb pruning reuses
  freed SQLite pages.
- NEXRAD processing is serialized per site, cleanup yields to the site lock,
  retried downloads resume from their last committed byte, volume boundaries are
  accepted only at S-chunk headers, elevation AR2V outputs are written
  atomically, and timed-out worker pools are retired and terminated.

### Testing
- Added tests for the MRMS registry, the ingest v2 catalog and migration, ingest
  and Core readiness consumers, CTAM/StormProb preflight, replay, and release
  qualification.
- Deleted product-specific tests, fixtures, benchmarks, and plotting scripts for
  retired features; retained Core/GLM/NEXRAD contracts and added retirement
  regressions.
