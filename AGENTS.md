# EdgeWARN-Core Agent Guide

## Scope and instruction precedence

This file applies to the entire repository. Many source and test subtrees have
more specific `AGENTS.md` files; read and follow every applicable guide before
editing a file. The closest guide to the file being changed takes precedence
when instructions differ.

## What this repository is

EdgeWARN-Core 3.0.1 is the backend for the EdgeWARN weather platform. It is a
mixed Python and Node.js system with four independently deployable runtime
surfaces:

- **Core (Python):** MRMS/RAP/GLM ingestion, storm-cell detection, optional
  tracking and lineage, multi-source enrichment, CTAM execution, StormProb,
  alerts, and API index publication.
- **EWMRS (Python):** MRMS and GOES raster rendering, RAP Uint16 products,
  tiling, METAR/NWS/WPC accessories, and GUI artifact cleanup.
- **NEXRAD (Python):** Level-II discovery, ingest, parsing, retention, and
  compressed polar rendering.
- **Unified API (Node.js):** an Express service that reads runtime artifacts
  from the filesystem and exposes only the versioned v3 API.

The Python services are separate processes, not threads inside one monolith.
They coordinate through atomic records beneath `<BASE_DIR>/state/realtime/`.
Core publishes cycle phase records, EWMRS consumes them and maintains a
checkpoint, and all services publish heartbeats used by the API service gate.

The Node API is a separate deployment and is not started by the Python Docker
image or by `edgewarn run`.

## Technology and dependency authorities

- Python 3.13 in the Conda environment named `EdgeWARN`
- NumPy, SciPy, xarray, rasterio/rioxarray, Shapely, scikit-image, cfgrib,
  ONNX Runtime, aiohttp/aioboto3, and related scientific/geospatial packages
- Node.js with Express, ES modules, Helmet, CORS, compression, and rate limiting
- pytest for Python and Jest/Supertest for Node.js
- `environment.yml` is the Python runtime dependency authority
- `package.json` and `package-lock.json` are the Node dependency authorities
- `pyproject.toml` defines Python packaging and the `edgewarn` console command

Do not add Python runtime dependencies to `pyproject.toml`; package installs use
`--no-deps` so Conda remains authoritative.

## Setup

From the repository root:

```bash
conda env create -f environment.yml
conda activate EdgeWARN
python -m pip install --no-deps -e .
npm install
edgewarn --version
```

The editable install is required when exercising the package command. Imports
and tests must remain compatible with `pythonpath = src`.

## Supported runtime commands

### Deployment-facing Python command

`edgewarn run` validates the complete configuration tree before starting any
worker or initializing the runtime filesystem.

```bash
edgewarn run                  # Core + EWMRS + NEXRAD
edgewarn run core             # Core only
edgewarn run ewmrs            # Core producer + EWMRS consumer
edgewarn run nexrad           # NEXRAD ingest + rendering
edgewarn run core --config-path /etc/edgewarn/config
```

Use repeatable, worker-scoped JSON arrays to forward service arguments without
shell parsing:

```bash
edgewarn run ewmrs \
  --args core '["--lat_limits", "20", "55"]' \
  --args ewmrs '["--disable-wpc"]'
```

Valid worker names are `core`, `ewmrs`, and `nexrad`. The wrapper owns topology
and configuration-path flags, so those flags cannot be forwarded in `--args`.

### Direct Python entry points

Run source entry points from `src/`:

```bash
cd src
python run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300
python run_ewmrs.py
python run_nexrad.py
python run_all.py --services edgewarn,ewmrs,nexrad
```

- `run_edgewarn.py` owns the latency-sensitive analysis cycle.
- `run_ewmrs.py` owns render/accessory work and consumes Core readiness records.
- `run_nexrad.py` supervises both NEXRAD ingest and render children.
- `run_all.py` is a thin subprocess supervisor; it does no scientific work.
- `run.py` is retired and exits with migration instructions.

Common service flags include `--base-dir`/`--base_dir`, `--config-dir`, and
`--profile`. Feature flags use `argparse.BooleanOptionalAction`, so both
`--disable-*` and `--no-disable-*` forms are meaningful. Consult
`src/util/cli.py` and the entry point rather than duplicating parser definitions.

### Historical processing

```bash
cd src
python process_historical.py \
  --start 2024-01-01T00:00:00 \
  --end 2024-01-01T01:00:00 \
  --lat 20 55 --lon -130 -60
```

Historical processing finds the best MRMS scan around each requested step and
reuses the Core pipeline with historical configuration and optional CTAM,
tracking, and polygon-expansion controls.

### Unified Node API

Run from the repository root:

```bash
npm run api                  # port 5000 by default
npm run debug:api            # port 3001 by default
```

Compatibility scripts `api:edgewarn` and `api:ewmrs` only launch the same
unified server and emit a deprecation warning. The supported surfaces are:

- `/api/v3`
- `/api/v3/openapi.json`
- `/health/live`
- `/health/ready`

Legacy v1/v2, `/features`, `/data`, `/renders/*`, `/wpc/*`, `/colormaps`,
`/rap/*`, `/nexrad/*`, `/health`, and `/healthz` routes are removed. Preserve
their expected 404 behavior unless a public API decision explicitly changes it.

## Configuration model

The application loads one schema-validated configuration catalog consisting of
18 YAML files in `config/`, each paired with `config/schema/*.schema.json`:

```text
api              api_index        detection       ewmrs_pipeline
ewmrs_render     filesystem       historical      ingest
integration      kalman           lineage         metar
nexrad           nws              runtime         scheduler
synoptic_rap     wpc
```

Copy and deploy the entire `config/` tree; individual files are not standalone.
All Python workers and the Node API must resolve settings from this catalog and
the supported overlay layer rather than introducing new hard-coded defaults.

Configuration-root precedence for direct services is `--config-dir`, then
`EDGEWARN_CONFIG_DIR`, then installation/repository discovery. The package
command calls the same concept `--config-path`. Runtime base-directory
precedence is CLI `--base-dir`/`--base_dir`, then `EDGEWARN_BASE_DIR`, then the
legacy `BASE_DIR`, then `filesystem.yaml`.

Validate configuration from both runtimes after catalog or schema changes:

```bash
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

For operator edits, use the validated, atomic configuration editor:

```bash
edgewarn configure ewmrs_pipeline.workers.budget_mb.goes 2048
edgewarn configure --config-path /etc/edgewarn/config
```

Configuration changes require process restart. Keep schema, defaults, CLI/env
overlays, documentation, and baseline tests synchronized.

## Runtime filesystem contract

The runtime base directory is the source of truth for generated artifacts. Its
platform defaults come from `config/filesystem.yaml` (`~/EdgeWARN_input` on
POSIX and `C:\EdgeWARN_input` on Windows). Do not write generated data into the
repository unless a test explicitly uses a temporary repository-local fixture.

Important top-level paths are:

```text
<BASE_DIR>/
├── data/                     # raw/derived weather data, cells, alerts, StormProb DB
├── gui/                      # MRMS/GOES float16, RAP Uint16, NEXRAD .bin.gz products
├── wpc/surface_analysis/     # WPC GeoJSON products
└── state/realtime/
    ├── cycles/               # durable per-cycle phase records
    ├── consumers/            # consumer checkpoints
    ├── leases/               # primary activity lease
    └── services/             # service heartbeats and process locks
```

Filesystem writes that become visible to another process must remain atomic.
Cleanup must be constrained to the resolved base directory. Preserve existing
binary formats, schema versions, filenames, and readiness order; the Python
writers and Node readers form a cross-language contract.

## Current source layout

```text
src/
├── api/                      # unified Express v3 API, OpenAPI, routes, services, repositories
├── common/
│   ├── config/               # Python catalog loading, validation, and overlays
│   ├── ingest/               # primary MRMS/NEXRAD/NWS/synoptic/WPC implementations
│   └── pipeline/             # staged ingest coordination
├── edgewarn_cli/             # edgewarn run/configure/sync-nws-zones
├── EdgeWARN/
│   ├── alerts/               # EdgeWARN alert schema and manager
│   ├── api_integration/      # filesystem API index/snapshot publication
│   ├── ctam/                 # module discovery, host, SDK, transactions, built-ins
│   ├── ingest/               # compatibility-facing imports and a few adapters
│   ├── process/detect/       # detection, Kalman tracking, lineage, save tools
│   ├── process/integrate/    # GLM/RAP/AzShear/statistical enrichment
│   ├── schedule/             # scan selection and scheduling
│   ├── stormprob/            # ONNX inference, records, SQLite repository, migration/audit
│   └── pipeline.py           # reusable Core orchestration helpers
├── EWMRS/                    # render pipeline, RAP encoding, transforms, tiling
├── NEXRAD/                   # NEXRAD GUI serialization and render loop
├── util/                     # filesystem, CLI, I/O, GRIB, release, and shared helpers
│   └── runtime/              # service lifecycles, handoff, heartbeats, workers
├── run_edgewarn.py
├── run_ewmrs.py
├── run_nexrad.py
├── run_all.py
└── process_historical.py
```

Additional repository surfaces:

- `models/stormprob/` contains versioned ONNX models and normalization assets.
- `assets/nws_zones/` is gitignored operational data. Source deployments must
  run `edgewarn sync-nws-zones --apply` before enabling NWS-dependent EWMRS
  work; the Docker image normally bundles a snapshot.
- `benchmarks/` contains opt-in performance and memory workloads.
- `scripts/` contains diagnostics, plotting, asset sync, and verification tools.
- `docker/`, `Dockerfile`, and `compose.yaml` define the Python processing image
  and administrative configuration/zone-sync profiles.
- `docs/api/`, `docs/core/`, and `docs/ctam/` contain the public contracts and
  architecture documentation.

Do not refer to removed `src/EdgeWARN/api`, `src/EWMRS/api`, or
`src/EdgeWARN/core` packages. The live HTTP implementation is `src/api`, and
shared ingest implementations belong in `src/common/ingest`.

## Development rules

### Pipeline and scientific changes

- Preserve the staged flow: detection inputs first, EWMRS render readiness
  second, and Core integration readiness last.
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
- Changes to EWMRS float16 chunks, RAP Uint16 fields, NEXRAD gzip payloads, or
  indexes require coordinated writer, reader, fixture, and documentation work.

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
