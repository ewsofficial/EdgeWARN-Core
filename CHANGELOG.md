# Changelog

## [Unreleased] — Retire EWMRS (breaking)

- Removed the EWMRS service/package, raster chunk and RAP Uint16 production,
  render consumers/outbox/checkpoints, and GOES ABI/METAR/NWS/WPC accessories.
  NWS zone synchronization and accessory configuration are removed.
- Removed `edgewarn run ewmrs`, `--args ewmrs`, `run_ewmrs.py`,
  `--disable-ewmrs`, `EDGEWARN_DISABLE_EWMRS`, `--render-consumer`, and
  `--disable-metar/nws/wpc/goes`. Unknown/retired topology errors point here.
  Core's existing scan-time GLM control is now `--disable-glm` with
  `runtime.run.disable_glm`; GLM enrichment remains supported.
- `all` now starts ingest, Core, and NEXRAD; `core` starts ingest and Core.
  MRMS-core-only mode disables RAP/GLM/NEXRAD. Core detection, tracking, CTAM,
  StormProb, alerts, and NEXRAD serialization remain supported. Published
  official alerts can still be matched/read, but there is no built-in writer.
- Removed `/api/v3/render-products`, `/api/v3/models/rap`,
  `/api/v3/analyses/wpc`, and `/api/v3/observations/metar` families and their
  binary/JSON readers. GET/HEAD and unsupported methods return 404 problems.
  OpenAPI/API contract version advances to 3.1.0. Legacy files cannot restore
  these routes. Health/service discovery lists ingest, edgewarn, nexrad only.
- Deleted `ewmrs_pipeline`, `ewmrs_render`, `metar`, `nws`, and `wpc` YAML
  catalogs/schemas, ABI channel settings, accessory flags/cadences, and API
  render settings. The complete catalog now contains 13 YAML files.
  NEXRAD render cadence/retention/worker limits moved to `nexrad.gui` and its
  display mapping to `nexrad.variable_colormaps` without changing values.
- Deleted product-specific tests, fixtures, benchmarks, and plotting scripts;
  retained Core/GLM/NEXRAD contracts and added retirement regressions.

### Upgrade and rollback

Stop all Python services and the API. Deploy code and the complete 13-file
configuration tree together; restart only after both validators pass:
`npm run validate-config` and `PYTHONPATH=src python -m common.config.validate`.
Remove retired CLI flags from service units and rename the GLM control.
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

Historical plans and release notes retain retired names; live code, guides,
configuration, tests, and packaging contain no references to the retired name.

## [3.1.0] 2026-10-05

### Added
- Added a configurable MRMS product registry. Composite reflectivity, PrecipFlag,
  and ProbSevere are protected products used for discovery and mandatory MRMS
  readiness; the other 18 products are configurable additions. A packaged Core
  contract defines the protected products.
- Added the ingest v2 schema (`config/schema/ingest.v2.schema.json`), selected
  when `ingest.schema_version` is 2, and shipped `ingest.yaml` as schema
  version 2. Integration statistics and MRMS render layers accept a `product`
  field that uses the upstream product identity in place of a raw `filepath`.
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
- Added an effective MRMS registry that Core publishes after preflight. EWMRS
  requires its identity, contract version, fingerprint, and a fresh heartbeat
  from the same run before it consumes MRMS inputs.
- Added MRMS registry documentation: `docs/core/configurable-mrms-phase*.md`
  and updates to `docs/core/ingestion.md`, `configuration.md`, and `integration.md`.

### Changed
- Core no longer performs source acquisition in realtime. It validates a
  committed start record and waits locally for integration readiness. It selects
  the oldest complete check set from durable local readiness, records skipped or
  overflowed scans explicitly, and no longer holds the input lease for a whole
  cycle. It also stops publishing the legacy render triggers and the old
  producer descriptor.
- EWMRS now renders each committed input independently from the per-input render
  outbox. Work is keyed by input ID, layer ID, and render configuration
  fingerprint, and runs in a persistent bounded render pool. Each layer is
  acknowledged only after its artifacts and product index are published and
  validated. Layers fail and retry independently, and RAP conversion can be
  restricted to a selected layer set.
- Shared RAP product-index updates are serialized between processes and sorted
  descending, so a late older completion can never move a product's latest
  timestamp backward.
- Realtime MRMS ingest phases are now independent and replayable.
  Integration and EWMRS references use full upstream product identities, while
  GUI paths, binary payloads, and API v3 product identities are unchanged.
- Detection and integration read MRMS inputs through validated manifest records
  by family and product, with explicit previous-scan roles and no fallback to
  arbitrary latest files.
- Disabled MRMS products are filtered out before path resolution. Integration
  warns once per disabled statistic. `MRMS_*_DIR` Python aliases remain
  supported through the 3.x series.
- Raw MRMS directories are renamed to canonical names (ten renames, eleven
  unchanged) by the migration command.

### Fixed
- StormProb pruning is skipped when StormProb or CTAM is disabled.
- StormProb inference is skipped for predicted-only cells. StormProb records an
  explicit skipped forecast for them, and they no longer fail the whole cycle.

### Testing
- Added tests for the MRMS registry, the ingest v2 catalog and migration, ingest
  and Core readiness consumers, EWMRS layer pooling and the RAP Uint16 pipeline,
  CTAM/StormProb preflight, replay, and release qualification.
