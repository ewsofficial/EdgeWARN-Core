# Changelog

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
