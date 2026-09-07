# EdgeWARN-Core

## Project Overview
EdgeWARN-Core is the mixed Python and Node.js backend for the EdgeWARN analysis pipeline and the EWMRS rendering service. The repository ingests operational weather datasets, processes storm-cell products, renders GUI layers, and serves generated artifacts through REST APIs.

Current codebase capabilities include:
- Shared real-time ingest orchestration for EdgeWARN analysis and EWMRS rendering
- EdgeWARN storm-cell detection, optional tracking/lineage, multi-source integration, CTAM analytics, alert generation, and API index updates
- EWMRS raster rendering, tile generation, colormap delivery, and WPC surface-analysis serving
- Historical reprocessing via `src/process_historical.py`
- Filesystem-first runtime using a configurable base directory, with remote ingestion from NOAA/AWS/HTTP sources

The current package version defined in `package.json` is **3.0.0**.

## Technology Stack
- **API Services**: Node.js with Express.js and ES modules
- **Core Processing**: Python 3.13 in the `EdgeWARN-dev` Conda environment
- **Scientific/Data Libraries**: NumPy, SciPy, xarray, rasterio/rioxarray, shapely, scikit-image, cfgrib
- **Storage Model**: Local runtime filesystem (`data/`, `gui/`, `wpc/`) backed by AWS S3, HTTPS, and NOAA feeds for ingestion
- **Testing**: Jest + Supertest for Node APIs, pytest for Python modules/integration
- **Package Management**: npm and conda

## Environment Setup

### Prerequisites
- Conda or Miniconda
- npm
- git-scm

### Installation Process
1. **Clone the repository**
   ```bash
   git clone https://www.github.com/ewsofficial/EdgeWARN-Core
   cd EdgeWARN-Core
   ```

2. **Create and activate the Conda environment**
   ```bash
   conda env create -f environment.yml
   conda activate EdgeWARN-dev
   ```

3. **Install Node.js dependencies**
   ```bash
   npm install
   ```

### Runtime Base Directory
Most generated data is written outside the repository into a runtime base directory.

Default locations are:
- **Linux/macOS**: `~/EdgeWARN_input`
- **Windows**: `C:\EdgeWARN_input`

Current components support the following overrides:
- **Python CLI**: `--base_dir` / `--base-dir`
- **EdgeWARN API**: `--base-dir` or `EDGEWARN_BASE_DIR`
- **EWMRS API**: `--base_dir` or `BASE_DIR`

## Running the Application

### Node.js API Services
Run these commands from the repository root:

```bash
npm run api       # Start the unified API (default port 5000)
npm run debug:api # Start the unified API in debug mode (port 3001)
```

### Real-Time Services (Python)

Three independently operable services run from the `src` directory:

```bash
cd src
# Primary EdgeWARN service (latency-sensitive analysis cycle):
python run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300
# EWMRS/accessory service (renders, GOES ABI, METAR/NWS/WPC, record consumption):
python run_ewmrs.py
# NEXRAD service (Level-II ingest + rendering):
python run_nexrad.py
```

`run_edgewarn.py` owns MRMS selection/ingest, scan-time GLM, detection,
integration, CTAM, cycle state, and publishes durable `mrms-ready`/
`rap-ready` records that `run_ewmrs.py` consumes. `run.py` remains as a
deprecated thin alias for `run_edgewarn.py`.

Primary optional flags:
- `--lat_limits`
- `--lon_limits`
- `--base_dir` / `--base-dir`
- `--config-dir`
- `--profile`
- `--disable-ctam`
- `--disable-tracking`
- `--disable-polygon-expansion`
- `--disable-goes`
- `--mrms-core-only`
- `--refl-threshold`
- `--min-seed-percentage`
- `--drop-offset`

EWMRS service flags: `--base_dir`/`--base-dir`, `--config-dir`, `--profile`,
`--disable-metar`, `--disable-nws`, `--disable-wpc`, `--disable-goes`.
NEXRAD service flags: `--base_dir`/`--base-dir`, `--config-dir`.

### Historical Processing (Python)
Run from the `src` directory:

```bash
cd src
python process_historical.py --start 2024-01-01T00:00:00 --end 2024-01-01T01:00:00 --lat 20 55 --lon -130 -60
```

`process_historical.py` iterates minute-by-minute through a requested time range, finds the best available MRMS timestamp near each step, and runs the historical EdgeWARN pipeline with optional CTAM/tracking controls.

Supported historical flags include:
- `--start`
- `--end`
- `--lat`
- `--lon`
- `--base_dir` / `--base-dir`
- `--profile`
- `--disable-ctam`
- `--disable-tracking`
- `--disable-polygon-expansion`
- `--refl-threshold`
- `--min-seed-percentage`
- `--drop-offset`

### Testing
Always activate the `EdgeWARN-dev` environment before running Python tests.

#### Node.js Tests
```bash
npm test
npm run test:watch
npm run test:coverage
```

#### Python Tests
```bash
python -m pytest tests/
```

Notes:
- Jest is configured for the Node API test suite under `tests/api/`
- Pytest uses `pythonpath = src` and ignores `tests/api/`

## Key Project Structure
```
EdgeWARN-Core/
├── src/
│   ├── common/                         # Shared ingestion implementations and tandem coordination
│   │   ├── ingest/                     # MRMS, NWS, synoptic, METAR, and WPC ingestion
│   │   └── pipeline/coordinator.py     # Shared staged-ingest coordinator for EdgeWARN + EWMRS
│   ├── EdgeWARN/
│   │   ├── api/                        # Express API server, config, routes, and file utilities
│   │   ├── alerts/                     # EdgeWARN alert schema and alert manager
│   │   ├── api_integration/            # API index/snapshot management helpers
│   │   ├── ctam/                       # CTAM host, built-ins, and module API
│   │   ├── ingest/                     # Compatibility re-exports for shared ingest modules
│   │   ├── process/
│   │   │   ├── detect/                 # Storm-cell detection, tracking, Kalman, lineage, and save tools
│   │   │   └── integrate/              # GLM/RAP/stat integration, history, and integration utilities
│   │   ├── schedule/                   # Scheduler and MRMS update checking
│   │   ├── __init__.py                 # Public EdgeWARN Python exports
│   │   └── pipeline.py                 # EdgeWARN realtime/historical orchestration helpers
│   ├── EWMRS/
│   │   ├── api/                        # Express API for renders, tiles, WPC, and colormaps
│   │   ├── render/                     # Rendering, reprojection, and tiling utilities
│   │   ├── colormaps.json              # Colormap definitions used by rendered products
│   │   ├── pipeline.py                 # Render pipeline and GUI cleanup logic
│   │   └── scheduler.py                # EWMRS scheduling helpers
│   ├── NEXRAD/                         # NEXRAD GUI serialization, retention, and render loop
│   ├── util/                           # Filesystem, I/O, GRIB, release, handler, and performance utilities
│   ├── run_edgewarn.py                 # Primary EdgeWARN service entry point
│   ├── run_ewmrs.py                    # EWMRS/accessory service entry point
│   ├── run_nexrad.py                   # NEXRAD service entry point
│   ├── run.py                          # Deprecated thin alias for run_edgewarn.py
│   └── process_historical.py           # Historical reprocessing entry point
├── tests/
│   ├── api/                            # Jest/Supertest coverage for Node APIs
│   ├── benchmarks/                     # Python performance and benchmark tests
│   ├── core/                           # Python tests for EdgeWARN, EWMRS, ingest, process, and schedule modules
│   ├── integration/                    # Cross-module and tandem pipeline integration tests
│   ├── unit/                           # Focused regression/unit tests
│   └── util/                           # Utility module tests
├── docs/
│   ├── api/                            # EdgeWARN API documentation
│   ├── core/                           # Ingest, detection, and integration architecture notes
│   └── ctam/                           # CTAM framework and module documentation
├── assets/
│   ├── EdgeWARN.png                    # Project branding
│   ├── EWS_logo_072025.png             # Organization branding
│   └── nws_zones/                      # Zone geometry assets (gitignored; downloaded on first run by geomapper._ensure_zone_assets)
├── config/
│   └── kalman.yaml                     # Tracking and Kalman filter configuration
├── plans/                              # Design notes and implementation plans
├── package.json                        # Node scripts and API dependencies
├── environment.yml                     # Conda environment definition
├── pytest.ini                          # Pytest discovery, markers, and defaults
├── jest.config.js                      # Jest configuration for API tests
└── INSTALLATION.md                     # Setup and execution guide
```

## Runtime Output Layout
At runtime, the code expects a base directory that typically looks like this:

```text
<BASE_DIR>/
├── data/      # Ingested MRMS/GOES/RAP/METAR/NWS data, stormcells, cells, alerts
├── gui/       # EWMRS RGBA binary chunks, schema-versioned indexes, and colormap assets
└── wpc/       # WPC-derived surface analysis GeoJSON artifacts
```

## Development Guidelines

### Python Development
- Use Python 3.13 with the `EdgeWARN-dev` Conda environment
- Keep imports and module paths compatible with `pythonpath = src`
- Follow existing logging patterns based on `IOManager`, `TimestampedOutput`, and queue-backed workers
- Add or update pytest coverage for new processing behavior, especially in `tests/core/`, `tests/integration/`, or `tests/unit/`
- Prefer vectorized or streaming approaches for large meteorological datasets

### Node.js Development
- Use ES modules and existing Express router patterns
- Preserve current API security layers such as `helmet`, `cors`, `compression`, and rate limiting
- Keep route changes aligned with the documented endpoint contracts in `docs/api/`
- Add or update Jest/Supertest coverage in `tests/api/`

### Data and Pipeline Development
- `src/common/ingest/` is the primary ingest implementation surface; `src/EdgeWARN/ingest/` currently exists as a compatibility re-export layer
- Preserve the tandem readiness flow in the shared ingest coordinator: detection inputs first, EWMRS render readiness second, EdgeWARN integration readiness last
- Treat the runtime base directory as the source of truth for generated artifacts; avoid introducing hard-coded repository-local output paths
- When changing detection or integration behavior, consider downstream impacts on CTAM, alerts, API indexes, and GUI render availability

### API Development
- The unified API exposes `/api/v3`, `/api/v3/openapi.json`, `/health/live`, and `/health/ready`
- Legacy `/api/v2`, `/renders/*`, `/wpc/*`, `/colormaps`, `/healthz`, `/rap/*`, and `/nexrad/*` paths remain compatibility adapters during migration
- Legacy v1-style `/features` and `/data` routes return HTTP 410
- Document public API changes in `docs/api/api_endpoints.md` and related docs

### Configuration Management
- Keep `config/kalman.yaml` aligned with tracking logic in `src/EdgeWARN/process/detect/kalman/`
- Prefer environment variables or supported CLI flags for runtime configuration
- Be mindful that EdgeWARN and EWMRS Node services use slightly different base-directory override names today

### Performance Optimization
- Prefer async ingest paths with sync fallback, matching the existing ingestion architecture
- Reuse caches/history where appropriate, but reset them safely across time gaps or failure states
- Keep cleanup logic constrained to the configured runtime base directory
- Profile Python-heavy changes with the existing performance tracker or targeted benchmark tests

### Documentation Synchronization
- Update documentation when public APIs, runtime behavior, directory structure, or major pipeline stages change
- Do not refer to a non-existent `src/EdgeWARN/core/` package; the active code is organized directly under `src/EdgeWARN/` and `src/common/`

### Committing Guidelines
- Always follow the contributing guidelines in `CONTRIBUTING.md`
- Each commit message must use one of the documented prefixes from `CONTRIBUTING.md`, followed by a `:` character
