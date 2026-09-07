# EdgeWARN Core

EdgeWARN Core is the mixed Python and Node.js backend for the EdgeWARN analysis pipeline and the EWMRS rendering service.

It ingests operational weather datasets, processes storm-cell products, renders GUI layers, and serves generated artifacts through REST APIs.

## What This Repository Provides

- Three independently operable real-time services (primary EdgeWARN analysis, EWMRS/accessories, NEXRAD) coordinated through durable runtime records, plus an optional all-services supervisor
- EdgeWARN storm-cell detection, optional tracking/lineage, integration, CTAM analytics, and alert generation
- EWMRS raster rendering, tiling, WPC surface-analysis serving, and colormap delivery
- Historical reprocessing via `src/process_historical.py`
- One versioned file-backed API at `/api/v3`, with legacy EdgeWARN and EWMRS paths retained as temporary compatibility adapters

## Requirements

- Conda or Miniconda
- Node.js/npm
- git

## Installation

1. Clone the repository:

```bash
git clone https://www.github.com/ewsofficial/EdgeWARN-Core
cd EdgeWARN-Core
```

2. Create and activate the Conda environment:

```bash
conda env create -f environment.yml
conda activate EdgeWARN-dev
```

3. Install Node dependencies:

```bash
npm install
```

4. Register the Python package command in the active Conda environment:

```bash
python -m pip install --no-deps -e .
edgewarn --version
```

`environment.yml` is the runtime dependency authority; `--no-deps` prevents
pip from creating a second dependency set.

Detailed setup and runtime notes are in `INSTALLATION.md`.

## Running Services

From repository root:

```bash
npm run api
npm run debug:api
```

## Running Python Pipelines

The installed command validates the complete configuration tree before it
starts a process. It is the preferred service entry point:

```bash
edgewarn run                                      # all three services
edgewarn run core                                 # primary analysis only
edgewarn run ewmrs                                # primary producer + EWMRS
edgewarn run nexrad                               # NEXRAD ingest + rendering
edgewarn run core --config-path /etc/edgewarn/config
edgewarn run ewmrs \
  --args core '["--lat_limits", "20", "55"]' \
  --args ewmrs '["--disable-wpc"]'
```

`--args WORKER JSON_ARGV` is repeatable. `WORKER` is `core`, `ewmrs`, or
`nexrad`, and `JSON_ARGV` must be an array of strings; arguments are sent only
to that worker without shell parsing. The `ewmrs` mode always includes its
primary EdgeWARN producer dependency. The `nexrad` mode starts both Level-II
ingest and NEXRAD rendering.

Configuration can be edited as a validated YAML scalar or through a terminal
UI:

```bash
edgewarn configure ewmrs_pipeline.workers.budget_mb.goes 2048
edgewarn configure --config-path /etc/edgewarn/config \
  runtime.run.disable_nexrad true
edgewarn configure --config-path /etc/edgewarn/config  # TTY only
```

The TUI first selects a file and then displays its editable leaves. `Enter`
opens a value, `Ctrl+S` validates and saves it, `Esc` goes back, and `q` quits
when no editor is open. Exit status `0` means success or clean signal shutdown,
`1` means a worker or write/rollback failure, and `2` means invalid command,
configuration, YAML, or schema input.

The source launchers remain available for development and troubleshooting.

Three independently operable real-time services run from `src/`:

```bash
# Primary EdgeWARN service (latency-sensitive analysis cycle):
python run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300
# EWMRS/accessory service (renders, GOES ABI, METAR/NWS/WPC):
python run_ewmrs.py
# NEXRAD service (Level-II ingest + rendering):
python run_nexrad.py
```

`run_edgewarn.py` owns MRMS selection/ingest and the detection, integration,
CTAM, alert, and cycle-state work, publishing durable `mrms-ready`/
`rap-ready` records that `run_ewmrs.py` consumes. `run.py` remains as a
deprecated thin alias for `run_edgewarn.py`, and an optional
`python run_all.py` supervisor can start all three services in one command.

Key primary flags include `--disable-ctam`, `--disable-tracking`,
`--disable-polygon-expansion`, `--disable-goes`, `--mrms-core-only`,
`--refl-threshold`, `--min-seed-percentage`, and `--drop-offset`. The EWMRS
service accepts `--disable-metar`, `--disable-nws`, `--disable-wpc`, and
`--disable-goes`.

Historical processing:

```bash
python process_historical.py --start 2024-01-01T00:00:00 --end 2024-01-01T01:00:00 --lat 20 55 --lon -130 -60
```

Historical runs support `--base_dir` / `--base-dir`, `--config-dir`, `--profile`, `--disable-ctam`, `--disable-tracking`, `--disable-polygon-expansion`, `--refl-threshold`, `--min-seed-percentage`, and `--drop-offset`.

All entry points also accept `--config-dir` to select the catalog tree. The
`--disable-*` and `--profile` switches take their defaults from `runtime.yaml`
when omitted, and each accepts a `--no-` form to re-enable. The primary
service normalizes `--lon_limits` into the `0-360` domain internally.

Historical runs persist the final stormcell artifacts to `<BASE_DIR>/data/stormcells/` using the runtime timestamped filenames.

## Containers

The supplied image installs the built Python wheel and uses the registered
command directly:

```dockerfile
ENTRYPOINT ["edgewarn"]
CMD ["run", "--config-path", "/etc/edgewarn/config"]
```

`compose.yaml` mounts runtime output at `/var/lib/edgewarn` and mounts the
production configuration read-only at `/etc/edgewarn/config`. Use the
`admin`-profile configuration container for intentional read-write edits. See
`INSTALLATION.md` for build, specialized-mode, and administrative examples.

## Runtime Base Directory

Runtime output defaults to:

- Linux/macOS: `~/EdgeWARN_input`
- Windows: `C:\EdgeWARN_input`

The unified API uses the same platform default when no override is supplied.

`config/filesystem.yaml` owns these platform defaults. Resolution is CLI,
`EDGEWARN_BASE_DIR`, legacy `BASE_DIR`, then YAML. Use `--config-dir` or
`EDGEWARN_CONFIG_DIR` to select a complete alternate 18-file `config/` tree;
run `npm run validate-config` before deployment. See
`docs/core/configuration.md` for catalog ownership.

Supported overrides:

- Python CLI: `--base_dir` / `--base-dir`
- Unified API: `--base-dir` or `EDGEWARN_BASE_DIR`
- Temporary aliases: `--base_dir` and `BASE_DIR`
- RAP maximum analysis age: `EDGEWARN_RAP_MAX_AGE_MINUTES` (default `180`)

RAP ingest checks the configured runtime cache first, then searches NOAA S3
newest-to-oldest within this analysis-age limit. Freshness is based on the RAP
analysis timestamp, not the local file modification time.

The unified API honors `PORT`, `--debug-server`, `RATE_LIMIT_MAX_SEC`, and `RATE_LIMIT_MAX_MIN`. Use `ALLOWED_ORIGINS` and `TRUST_PROXY_IPS` to configure browser and proxy trust.

See `INSTALLATION.md` for the full CLI reference, including API debug and rate-limit flags plus the `scripts/sync_nws_zones.py` maintenance utility required before NWS alert ingest.

## Testing

Node:

```bash
npm test
npm run test:watch
npm run test:coverage
```

Python (with `EdgeWARN-dev` active):

```bash
python -m pytest tests/
```

## Release

Current package version: **3.0.0**

See `CHANGELOG.md` for release history.
