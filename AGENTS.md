# EdgeWARN-Core Agent Guide

## Scope and precedence

This guide applies to the repository. Read all applicable subtree AGENTS.md
files before editing; the closest guide takes precedence.

## Runtime ownership

Four deployable surfaces:

- Ingest (Python): MRMS/RAP/scan-time GLM acquisition, validated inventory,
  immutable Core readiness, and reference-aware retention.
- Core (Python): detection, tracking/lineage, multi-source enrichment, CTAM,
  StormProb, alerts, and filesystem API publication.
- NEXRAD (Python): Level-II discovery, acquisition, parsing, retention, and
  compressed polar serialization.
- Unified API (Node): filesystem readers and versioned v3 HTTP endpoints.

Python services are separate processes. Ingest publishes durable readiness
under `<BASE_DIR>/state/realtime/ingest/v1/`; Core consumes exact pinned inputs.
All services publish heartbeats and own single-instance locks. The Node API
is deployed separately from the processing image and `edgewarn run`.

## Dependencies and setup

Use Python 3.13 in the EdgeWARN Conda environment. `environment.yml` owns Python
runtime dependencies; do not add them to pyproject.toml. `package.json` and its
lockfile own Node dependencies. `pyproject.toml` owns packaging and the console
command. Imports/tests must work with `pythonpath = src`.

```bash
conda env create -f environment.yml
conda activate EdgeWARN
python -m pip install --no-deps -e .
npm install
edgewarn --version
```

## Supported commands

```bash
edgewarn run                  # Ingest + Core + NEXRAD
edgewarn run core             # Ingest + Core
edgewarn run ingest           # Ingest only
edgewarn run nexrad           # NEXRAD ingest + rendering
edgewarn run core --config-path /etc/edgewarn/config \
  --args core '["--lat_limits", "20", "55"]'
```

Worker names are ingest, core, nexrad. JSON argv arrays contain strings and
cannot override wrapper-owned topology/configuration flags. Direct source
entrypoints are run_ingest.py, run_edgewarn.py, run_nexrad.py, and run_all.py,
run from src/. run.py is retired. Use util/cli.py instead of duplicating parsers.
Boolean feature switches support --no-* forms. --disable-glm controls Core
lightning acquisition. Historical processing reuses Core with an isolated
historical runtime root and bounded timestamp selection.

`npm run api` starts port 5000; `npm run debug:api` uses port 3001. Supported
surfaces are /api/v3, /api/v3/openapi.json, /health/live, /health/ready. Removed
legacy and retired render/RAP/WPC/METAR routes return RFC 9457 404 responses.

## Configuration and filesystem

The 13 catalogs are api, api_index, detection, filesystem, historical, ingest,
integration, kalman, lineage, nexrad, runtime, scheduler, synoptic_rap. Each has
a schema in config/schema/. Deploy the complete tree and restart after edits.
Both runtimes use these catalogs and supported overlays; avoid hard-coded defaults.

Configuration root: --config-dir (package --config-path), EDGEWARN_CONFIG_DIR,
then installation discovery. Runtime root: --base-dir/--base_dir,
EDGEWARN_BASE_DIR, legacy BASE_DIR, filesystem.yaml. Defaults are
~/EdgeWARN_input on POSIX and C:\EdgeWARN_input on Windows.

```bash
edgewarn configure nexrad.gui.max_workers 8
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

Runtime artifacts live beneath the resolved base directory: data/ contains
weather inputs/cells/alerts/StormProb; gui/NEXRAD contains radar products;
state/realtime contains cycles, inventory/readiness, consumer state, leases,
heartbeats, and locks. Preserve atomic cross-process writes and constrain
cleanup to the resolved root. Existing retired files remain operator-owned;
no automatic retirement purge runs.

## Source layout

src/api owns live HTTP routes/services/repositories and OpenAPI. src/common owns
configuration, shared acquisition, inventory, and staged readiness.
src/edgewarn_cli owns run/configure/migrate-mrms. src/EdgeWARN owns analysis,
alerts, API publication, CTAM, StormProb, detection, integration, and scheduling.
src/NEXRAD owns radar serialization/rendering. src/util owns shared helpers and
runtime lifecycle/handoff. Do not reference removed legacy HTTP packages.
Models live in models/stormprob and are installed as package data files.
Benchmarks are opt-in; scripts contain diagnostics and qualification tools.

## Development rules

### Pipeline and scientific changes

- Preserve the staged flow: detection readiness first, integration readiness second,
  and final optional-input readiness last.
- Consider downstream effects on tracking/lineage, integration, StormProb,
  CTAM, alerts, snapshots/indexes, render availability, and API service gating.
- Treat timestamps, coordinate domains, projections, units, missing values, and
  array orientation as contract data. Add regression coverage for each change.
- Prefer vectorized or streaming work for large meteorological arrays. Avoid
  unbounded in-memory materialization and unbounded worker pools.
- Maintain async ingest with intentional sync fallback where the subsystem
  already provides it. Reset caches/history safely across time gaps and errors.

### API and binary-contract changes

- Use ES modules and the existing route/service/repository separation in
  `src/api`.
- Preserve request IDs, RFC 9457 problem responses, security middleware,
  service heartbeats, range/stream behavior, and path containment.
- Update `src/api/openapi/v3.yaml`, API documentation, product catalogs, and
  Jest tests together for public contract changes.
- NEXRAD gzip payloads and Core indexes require coordinated writer, reader,
  fixture, and documentation work.

### CTAM and StormProb

- CTAM external modules are operator-supplied code discovered outside the
  package; preserve manifest validation, resource limits, transactional writes,
  and the loopback internal API boundary.
- StormProb model assets live outside the Python package source tree but are
  installed through `pyproject.toml` data files. Keep manifest, model,
  normalization, feature construction, and database schema compatible.

### Logging and process behavior

- Follow the existing `IOManager`, queue-backed logging, and service heartbeat
  patterns. Avoid import-time network calls, worker startup, or filesystem
  mutation.
- Supervisors must forward shutdown signals, bound termination time, and clean
  up complete process groups. Workers must not silently outlive their owner.

## Testing and verification

Activate the `EdgeWARN` environment before Python tests.

```bash
# Python suites configured by pytest.ini
python -m pytest
python -m pytest tests/unit
python -m pytest tests/integration

# Node API suites
npm test
npm run test:coverage

# Configuration parity
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

`pytest.ini` collects `tests/core`, `tests/architecture`, `tests/integration`,
`tests/packaging`, `tests/unit`, and `tests/util`; it ignores `tests/api` because
those are Jest tests and ignores `benchmarks` because performance tests are
opt-in. Respect `network`, `slow`, and `benchmark` markers and do not make the
default suite depend on live external services.

Choose the narrowest relevant tests while iterating, then run broader suites in
proportion to the change. Notable expectations:

- Configuration changes: schemas plus architecture/catalog baseline tests
- Packaging/CLI changes: `tests/packaging` and CLI ownership/import-safety tests
- Pipeline handoff changes: integration handoff/process and runtime service tests
- Binary serialization changes: integration serialization fixtures and API tests
- Public API changes: Jest contract/service tests and OpenAPI validation

Use temporary base directories in tests. Never point cleanup, migration, or
historical-processing tests at a real operational runtime tree.

## Documentation and commits

- Update documentation whenever commands, configuration, public API behavior,
  binary formats, runtime layout, or pipeline ownership changes.
- `README.md` is the quick start; `INSTALLATION.md` is the operational guide;
  `docs/core/configuration.md` is the configuration ownership reference;
  `docs/api/unified_v3.md` and `src/api/openapi/v3.yaml` define API v3.
- Follow `CONTRIBUTING.md`. Commit subjects use a documented uppercase prefix
  followed by a colon, for example `DOC: refresh agent guidance`.
- Do not commit secrets, downloaded weather data, generated runtime artifacts,
  coverage output, or gitignored NWS zone assets.
