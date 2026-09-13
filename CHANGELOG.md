# Changelog

## [3.0.0] 2026-08-20

### Added
- Unified, secured `/api/v3` service for EdgeWARN and EWMRS artifacts, with an
  OpenAPI contract, cursor-based collection responses, problem-detail errors,
  request IDs, conditional caching, and weather, analysis, render, radar, RAP,
  WPC, and colormap resources, including native WPC GeoJSON delivery. It adds
  configured security headers, strict proxy/origin handling, rate limiting,
  safe access logging, and artifact-path containment; consolidates the former
  Node servers; and retains deprecated v2 and product-route compatibility
  adapters.
- EWMRS binary chunk delivery: renders now publish sparse RGBA artifacts and
  gzip-compressed float16 value chunks with versioned indexes and metadata for
  client-side colormapping and GOES RGB composition.
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
  colormap, radar) are gated behind their owning service heartbeat and return
  `SERVICE_NOT_ENABLED` when that service is inactive.
- NEXRAD service with a canonical heartbeat, single-instance lock, and an
  optional cross-process primary-activity lease (default off).

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

### Removed
- Removed the bundled MorphoWind CTAM assessment.
- Removed the legacy in-process CTAM registry and grid-module execution path;
  grid analytics use the cycle-scoped external module API.
- Removed server-side GOES RGB composite rendering; clients compose RGB from
  ABI channel data delivered through EWMRS binary chunks.
- Deprecated legacy EWMRS PNG routes in favor of binary chunk delivery.
- Deprecated `/api/v2`, render, WPC, colormap, health, RAP, and NEXRAD API
  routes as compatibility adapters; legacy v1-style `/features` and `/data`
  paths now return `410 Gone`.
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
