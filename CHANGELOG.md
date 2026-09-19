# Changelog

## [3.0.0] 2026-09-20

### Added
- Unified, secured `/api/v3` service for EdgeWARN and EWMRS artifacts, with an
  OpenAPI contract, cursor-based collection responses, problem-detail errors,
  request IDs, conditional caching, and weather, analysis, render, radar, RAP,
  and WPC resources, including native WPC GeoJSON delivery. It adds
  configured security headers, strict proxy/origin handling, rate limiting,
  safe access logging, and artifact-path containment; consolidates the former
  Node servers; and serves only `/api/v3` data endpoints (legacy `/api/v2`,
  `/renders/*`, `/wpc/*`, `/colormaps`, `/health`, `/healthz`, `/rap/*`,
  `/nexrad/*`, `/api/v1`, `/features`, and `/data` paths are removed and
  return HTTP 404).
- EWMRS binary chunk delivery: renders now publish gzip-compressed float16
  source-value chunks with versioned indexes and metadata for client-owned
  styling and GOES RGB composition.
- Schema-validated YAML configuration catalogs for runtime, historical,
  processing, ingest, rendering, API, filesystem, and tracking settings.
  Deployments can select a complete catalog tree with `--config-dir` or
  `EDGEWARN_CONFIG_DIR` and inspect effective configuration provenance. The
  catalogs are the sole source of the corresponding operational defaults.
- External CTAM module support with manifest discovery, declared-input
  readiness checks, a loopback internal API and SDK, cycle-scoped transactions,
  ordered alert publication, persistent journals, and per-module outcome
  reporting. StormProb now runs as a built-in module through the same host
  boundary.
- Coherent cycle input manifests and transactional runtime-artifact publication
  to preserve consistent detection, integration, and rendering outputs.
- Production dependency-audit and SBOM npm scripts.
- Standalone realtime service entry points: `run_edgewarn.py` (primary),
  `run_ewmrs.py` (accessory), `run_nexrad.py` (NEXRAD), and `run_all.py`
  (optional all-services supervisor) with exact flag routing, signal
  forwarding, and bounded teardown; `run.py` is retained as a deprecated
  alias.
- Durable cross-service handoff: `mrms-ready`/`rap-ready` phase records and
  consumer checkpoints with atomic publication, per-cycle shadow validation,
  and per-phase durable checkpoints; the EWMRS consumer renders MRMS/RAP from
  exact committed paths rather than in-memory queues.
- Canonical realtime service-name registry with an atomic heartbeat schema and
  route-family dependency map; API route families (analysis, render, RAP, WPC,
  radar) are gated behind their owning service heartbeat and return
  `SERVICE_NOT_ENABLED` when that service is inactive.
- NEXRAD service with a canonical heartbeat, single-instance lock, and an
  optional cross-process primary-activity lease (default off).
- StormProb forecast engine replacing StormCast as the built-in CTAM module,
  with ONNX runtime inference, phased input-collection/database feature
  sources, rollout gates, batched inference with tightened public output, and
  calibrated adaptive forecast geometry.
- Installable EdgeWARN package commands with a topology-aware runner, safe
  configuration mutation, an interactive configuration TUI, Compose runtime
  base-directory support, containerized runtime delivery (including Docker
  runtime configuration, NWS zone assets, StormProb model assets, and
  rotatelogs log volume handling), and All-Origins-Allowed CORS policy support.
- NLDN lightning render products served through the API, with 1-minute NLDN
  ingest replacing the 5-minute NLDN source.
- CTAM public module route registration published through the v3 API, with
  operator CTAM modules wired into containers via a read-only mount.
- Configurable EWMRS worker cap with simplified worker memory configuration.

### Changed
- StormProb emits `tstm_wind: "false"` when no wind assessment is available.
- The primary cycle now performs primary-only work and publishes durable
  records as the sole cross-service handoff; the GOES render loop is a
  poll-based EWMRS-owned cycle, RAP is an EWMRS-owned artifact, and NEXRAD GUI
  rendering, retention, and cleanup moved into the NEXRAD service package.
- Realtime service supervision is hardened with signal forwarding, bounded
  teardown, non-daemonic children for `ProcessPoolExecutor` spawns, and
  termination of supervised children with dead parents; Node heartbeat
  classification is aligned with the Python services.
- The monolithic runner CLI is split into ownership-scoped flag builders in
  `util/cli`, and runtime initialization and stream wrapping are moved out of
  import-time module scope.
- Refactored render product names and renamed MRMS/GOES filesystem directories
  to API product names.
- Changed the default EWMRS tile size to 700px with synchronized render chunk
  defaults.
- Renamed the Conda environment to `EdgeWARN` and updated package metadata to
  3.0.0.
- Set the default API rate limit to 100/s and 6000/min.
- Removed lagging FLASH MRMS products from the ingest catalog so `mrms-ready`
  publishes on schedule instead of stalling the EWMRS consumer.
- Simplified config TUI file selection and hid `schema_version`.
- Routed container logs through rotatelogs with a log volume.

### Removed
- Removed the bundled MorphoWind CTAM assessment.
- Removed the legacy in-process CTAM registry and grid-module execution path;
  grid analytics use the cycle-scoped external module API.
- Removed server-side GOES RGB composite rendering; clients compose RGB from
  ABI channel data delivered through EWMRS binary chunks.
- Removed PNG image/tile resources from v3; the legacy EWMRS PNG download and
  tile routes are removed (HTTP 404) with the v3 chunk successor.
- Removed legacy `/api/v2`, `/renders/*`, `/wpc/*`, `/colormaps`, `/health`,
  `/healthz`, `/rap/*`, and `/nexrad/*` endpoints (HTTP 404); legacy `/api/v1`,
  `/features`, and `/data` handlers are removed and return HTTP 404.
- Removed colormap API and catalog support, including bundled colormap assets.
- Removed the `legacyID` field from the API product catalog.
- Removed NEXRAD launch from the old runner and the dead EWMRS tandem worker
  from the render pipeline; EWMRS cleanup no longer touches NEXRAD outputs.
- Removed the bundled NWS zone artifacts from the repository; they are
  generated on first run by the geomapper instead of tracked in git.

### Fixed
- Corrected historical and single-frame processing semantics and prevented
  unavailable RAP data from stalling integration.
- Stabilized NEXRAD worker lifecycle, bounded realtime work, and improved
  recovery from stalled workers; corrected WPC ownership.
- Restored tracking assignment fallback behavior and corrected GOES render
  resampling.
- Restored effective-config reporting on normal primary startup.
- Made NEXRAD supervision and NEXRAD retention actually run; hardened lease
  release ownership and phase-record tolerance parsing.
- Restored the chunk endpoint artifact error contract.
- Prevented detection from crashing when a hail-core polygon is smaller than
  the contour-generation minimum size.
- Fixed the API crash caused by `Filehandle.createReadStream()`.

### Testing
- Added API contract, security, compatibility, and production-readiness
  coverage for the unified service.
- Added configuration catalog, schema, override, provenance, and source-boundary
  regression coverage.
- Added CTAM internal-API, manifest, readiness, transaction, publication,
  built-in/external module, and performance coverage.
- Expanded runtime, ingestion, EWMRS chunk serialization, NEXRAD supervision,
  historical-processing, and tracking regression tests and benchmarks.
- Added a PhaseTelemetry baseline harness, heartbeat scanner boundary audit,
  `run_ewmrs` CLI shadowing baseline, and daemonic-process checks for EWMRS;
  extended the realtime memory benchmark to single, direct, and launcher modes.
- Added supervisor restart and teardown robustness tests and CI hang
  self-reporting (unbuffered pytest, per-test faulthandler dumps, and a step
  timeout).
- Added StormProb engine, phase-5 rollout, adaptive geometry, and container
  packaging regression coverage.
- Added package-command, topology runner, configuration TUI, installed-command,
  container-smoke, and NWS zone asset regression coverage.
- Added CTAM public module route, cross-cycle rewrite, and container-wiring
  coverage.
- Added NLDN render, render product rename, filesystem rename, 700px tile
  default, colormap removal, and `legacyID` removal contract coverage.
