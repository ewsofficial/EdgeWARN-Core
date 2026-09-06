# EdgeWARN Core Installation and Runtime

## Requirements

- Conda or Miniconda
- npm
- git-scm

## Setup

1. Clone the repository:

```bash
git clone https://www.github.com/ewsofficial/EdgeWARN-Core
cd EdgeWARN-Core
```

2. Create and activate the Python environment:

```bash
conda env create -f environment.yml
conda activate EdgeWARN
```

3. Install Node.js dependencies:

```bash
npm install
```

4. Register the Python package command in the active environment:

```bash
python -m pip install --no-deps -e .
edgewarn --version
```

`environment.yml` is the sole runtime dependency authority. `--no-deps`
ensures pip only installs the EdgeWARN package and its `edgewarn` console
entry point; Conda supplies Python and every runtime dependency. Use the same
flag for editable and wheel installs.

### Deploying editable configuration

The YAML configuration tree is deployment state and is not installed inside
the Python wheel. Container and service images should copy the complete tree,
including its schemas, to an operator-editable location:

```bash
install -d /etc/edgewarn
cp -R config /etc/edgewarn/config
```

Select that copy with `--config-path /etc/edgewarn/config`. Keeping it outside
site-packages permits atomic updates and read-only production mounts without
modifying the installed wheel.

## Runtime Base Directory

Most generated data is written outside the repository into a base directory.

Defaults:

- Linux/macOS: `~/EdgeWARN_input`
- Windows: `C:\EdgeWARN_input`

The unified API resolves the same platform default when no override is set.

`config/filesystem.yaml` is the sole authority for these defaults. Precedence
is CLI (`--base-dir` or `--base_dir`), then `EDGEWARN_BASE_DIR`, then legacy
`BASE_DIR`, then YAML. The legacy spellings remain supported for compatibility.

## Configuration tree

The application validates all 18 YAML documents and their schemas before
starting workers or an HTTP listener. Select a deployed tree with `--config-dir
/path/to/config` or `EDGEWARN_CONFIG_DIR=/path/to/config`; otherwise discovery
walks up from the installed source tree. Copy the whole `config/` directory,
including `config/schema/`, for deployment.

```bash
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

See `docs/core/configuration.md` for the authoritative owner of each setting.

Overrides:

- Python CLIs (all real-time services, `process_historical.py`): `--base_dir` or `--base-dir`
- Unified API: `--base-dir` or `EDGEWARN_BASE_DIR`
- `--base_dir` and `BASE_DIR` remain temporary compatibility aliases
- RAP maximum analysis age: `EDGEWARN_RAP_MAX_AGE_MINUTES` (non-negative
  integer minutes; default `180`)

The RAP setting controls both local-cache eligibility and the bounded backward
search of NOAA RAP analysis hours. Analysis timestamps, rather than download
times or filesystem modification times, determine freshness.

## Running API Services

Run from repository root:

```bash
npm run api
npm run debug:api
```

- Unified API default port: `5000`
- Unified API debug mode port: `3001`

Current API surfaces:

- v3: `/api/v3` and `/api/v3/openapi.json`
- Health: `/health/live`, `/health/ready`
- Legacy EdgeWARN and EWMRS paths remain compatibility adapters during migration

CLI and environment overrides:

- Base directory: `--base-dir <path>` or `EDGEWARN_BASE_DIR`
- Port override: `PORT`; debug mode: `--debug-server`
- Rate-limit env vars: `RATE_LIMIT_MAX_SEC`, `RATE_LIMIT_MAX_MIN`
- Browser and proxy policy: `ALLOWED_ORIGINS`, `TRUST_PROXY_IPS`

See `docs/api/unified_v3.md` for migration details and the complete contract.

## Running Real-Time Services

### Package command

`edgewarn run` is the deployment-facing supervisor. It validates every YAML
document and matching schema before starting any child process or initializing
the runtime filesystem.

| Command | Selected services |
| --- | --- |
| `edgewarn run` | Primary EdgeWARN, EWMRS/accessories, and NEXRAD |
| `edgewarn run core` | Primary EdgeWARN only |
| `edgewarn run ewmrs` | Primary EdgeWARN producer followed by EWMRS/accessories |
| `edgewarn run nexrad` | NEXRAD Level-II ingest and NEXRAD rendering |

The EWMRS consumer requires products from the primary service, so `ewmrs`
intentionally starts both. NEXRAD's launcher owns both of its supervised
children; it is not ingest-only.

Use `--config-path` to select a complete deployed tree. Forward launcher flags
as a repeatable, worker-scoped JSON array so quoting is preserved and no
argument is broadcast to unrelated workers:

```bash
edgewarn run core --config-path /etc/edgewarn/config
edgewarn run core \
  --args core '["--lat_limits", "20", "55", "--disable-ctam"]'
edgewarn run ewmrs \
  --args core '["--lat_limits", "20", "55"]' \
  --args ewmrs '["--disable-wpc"]'
edgewarn run nexrad --args nexrad '["--profile"]'
```

Each `JSON_ARGV` value must be an array containing only strings. A worker may
appear once and only when its topology is selected. The wrapper rejects
configuration and topology flags inside forwarded arrays because it injects
the single resolved configuration path itself.

### Direct source commands

Three independently operable services run from `src/`. Start each in its own
shell, service unit, or container; all of them share the configured runtime
base directory.

```bash
cd src
# Primary EdgeWARN service (latency-sensitive analysis cycle):
python run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300
# EWMRS/accessory service (renders, GOES ABI, METAR/NWS/WPC):
python run_ewmrs.py
# NEXRAD service (Level-II ingest + rendering):
python run_nexrad.py
```

An optional supervisor starts any subset with one command (it performs no
ingest, rendering, or coordination work itself):

```bash
python run_all.py                                # all three services
python run_all.py --services edgewarn,ewmrs      # a subset
```

`run.py` is retired and exits with instructions rather than silently starting
only the primary service. Use `run_all.py` for all services or the explicit
service commands above.

### Primary flags

- `--lat_limits <LAT_MIN> <LAT_MAX>` default `20 55`
- `--lon_limits <LON_MIN> <LON_MAX>` default `230 300`
- `--base_dir` / `--base-dir`
- `--config-dir`
- `--profile`
- `--disable-ctam`
- `--ctam-module-dir`
- `--list-ctam-modules`
- `--check-ctam-modules`
- `--disable-tracking`
- `--disable-polygon-expansion`
- `--disable-goes` (disables scan-time GLM)
- `--mrms-core-only`
- `--refl-threshold`
- `--min-seed-percentage`
- `--drop-offset`

### EWMRS and NEXRAD flags

- Both accept `--base_dir` / `--base-dir`, `--config-dir`, and `--profile`.
- EWMRS additionally accepts `--disable-metar`, `--disable-nws`,
  `--disable-wpc`, and `--disable-goes` (ABI ingest/render).

Notes:

- The primary normalizes `--lon_limits` into the `0-360` domain internally.
- `--mrms-core-only` runs MRMS-only primary behavior and implies disabling
  every non-primary component.
- Every `--disable-*` / `--profile` switch defaults from `runtime.yaml` when
  omitted and accepts a `--no-` form to re-enable.
- Each service publishes an atomic heartbeat under
  `<BASE_DIR>/state/realtime/services/<name>.json`; the unified Node API uses
  these to answer requests whose owning service is not active with a
  structured `SERVICE_NOT_ENABLED` error instead of stale artifacts.

## Editing Configuration

A noninteractive edit names the YAML file without `.yaml`, traverses a dotted
leaf path, and parses the new value as one YAML scalar:

```bash
edgewarn configure ewmrs_pipeline.workers.budget_mb.goes 2048
edgewarn configure --config-path /etc/edgewarn/config \
  runtime.run.disable_nexrad true
edgewarn configure runtime.profiling.perf_tracker null
edgewarn configure runtime.run.ctam_module_dir '"ctam_modules"'
edgewarn configure 'ingest.mrms.ncep_https.directory_map.EchoTop_18_00\.50' '"EchoTop_18"'
```

Escape a literal `.` or `\` in a mapping key with a leading backslash. The
interactive editor preserves these path segments automatically.

Only scalar replacement is supported. Before replacement, the command locks
the configuration root, revalidates the complete tree, checks the target path,
and validates the proposed document. It preserves comments, ordering,
permissions, and final-newline behavior, writes with flush/`fsync` plus atomic
replace, then revalidates from disk. A failed post-write check restores the
same-process backup. Symlink escapes and read-only targets are rejected.

Running without a dotted assignment opens the interactive editor only when
both stdin and stdout are terminals:

```bash
edgewarn configure --config-path /etc/edgewarn/config
```

The first screen selects one of the registered files; the second lists each
leaf's path, value, type, and schema constraints. Select a row and edit it with
`Ctrl+S`; schema errors stay on screen and do not change disk. `Esc` navigates
back and `q` quits when an editor is not open. For automation and noninteractive
containers, always use the dotted assignment form.

### Package command exit statuses

| Status | Meaning |
| --- | --- |
| `0` | Help/version, successful edit, or clean signal-driven shutdown |
| `1` | Child startup/runtime failure, forced shutdown, or write/rollback I/O failure |
| `2` | Usage, forwarded-argument, config-root, YAML, or schema validation error |

Child-specific failures are logged with the worker name; the supervisor returns
`1` instead of exposing the child's implementation-specific status.

## Containers

Build the image and start the default all-service topology:

```bash
docker build -t edgewarn-core:3.0.0 .
export EDGEWARN_HOST_BASE_DIR=/srv/edgewarn/runtime
export EDGEWARN_NWS_ASSETS_DIR=/srv/edgewarn/nws-zones
docker run --rm --name edgewarn \
  -v "$EDGEWARN_HOST_BASE_DIR:/var/lib/edgewarn" \
  -v "$PWD/config:/etc/edgewarn/config:ro" \
  -v "$EDGEWARN_NWS_ASSETS_DIR:/etc/edgewarn/assets/nws_zones:ro" \
  edgewarn-core:3.0.0
```

The image installs a built wheel and has this exec-form process contract:

```dockerfile
ENTRYPOINT ["edgewarn"]
CMD ["run", "--config-path", "/etc/edgewarn/config"]
```

Override the complete `CMD` to select a specialized topology:

```bash
docker run --rm \
  -v edgewarn-runtime:/var/lib/edgewarn \
  -v "$PWD/config:/etc/edgewarn/config:ro" \
  -v "$EDGEWARN_NWS_ASSETS_DIR:/etc/edgewarn/assets/nws_zones:ro" \
  edgewarn-core:3.0.0 run core --config-path /etc/edgewarn/config
docker run --rm \
  -v edgewarn-runtime:/var/lib/edgewarn \
  -v "$PWD/config:/etc/edgewarn/config:ro" \
  -v "$EDGEWARN_NWS_ASSETS_DIR:/etc/edgewarn/assets/nws_zones:ro" \
  edgewarn-core:3.0.0 run ewmrs --config-path /etc/edgewarn/config
docker run --rm \
  -v edgewarn-runtime:/var/lib/edgewarn \
  -v "$PWD/config:/etc/edgewarn/config:ro" \
  -v "$EDGEWARN_NWS_ASSETS_DIR:/etc/edgewarn/assets/nws_zones:ro" \
  edgewarn-core:3.0.0 run nexrad --config-path /etc/edgewarn/config
```

Runtime output and configuration are separate mounts. Production should keep
configuration read-only; `edgewarn run` needs only read access. An attempted
edit fails with status `1` before replacement and leaves the file unchanged.
In Compose, set `EDGEWARN_HOST_BASE_DIR` to the host runtime directory; Compose
mounts it at `/var/lib/edgewarn` and passes that container path to the package
as `EDGEWARN_BASE_DIR` for every supervised worker.
For an intentional interactive administrative edit, use the Compose profile,
which is the only supplied service with a read-write configuration mount:

```bash
docker compose build edgewarn
docker compose --profile admin run --rm edgewarn-configure
```

`docker stop` sends `SIGTERM` directly to the exec-form `edgewarn` supervisor.
It forwards the signal to every selected service, waits up to its bounded grace
period, escalates survivors, reaps every child, and exits `0` for a clean stop.

## Running Historical Reprocessing

Run from `src/`:

```bash
python process_historical.py --start 2024-01-01T00:00:00 --end 2024-01-01T01:00:00 --lat 20 55 --lon -130 -60
```

Common optional flags:

- `--start <ISO8601>` required
- `--end <ISO8601>` required
- `--lat <LAT_MIN> <LAT_MAX>` default `20 55`
- `--lon <LON_MIN> <LON_MAX>` default `-130 -60`
- `--base_dir` / `--base-dir`
- `--config-dir`
- `--profile`
- `--disable-ctam`
- `--ctam-module-dir`
- `--list-ctam-modules`
- `--check-ctam-modules`
- `--disable-tracking`
- `--disable-polygon-expansion`
- `--refl-threshold`
- `--min-seed-percentage`
- `--drop-offset`

Historical-processing note:

- `process_historical.py` writes its stormcell products to `<BASE_DIR>/data/stormcells/stormcells_{timestamp}.json` through the normal detection and integration pipeline.

## Maintaining NWS Zone Assets

`edgewarn sync-nws-zones` refreshes `assets/nws_zones` from the NWS zone and UGC APIs in both source and installed deployments. The repository script remains a compatibility wrapper.

The `assets/nws_zones/` directory is **not** part of the repository. It must
be synchronized before starting a pipeline that ingests NWS alerts. If it is
missing, the geomapper raises an error that directs the operator to this
script. A full initial sync is roughly 8,600 zone codes at ~20 requests/second,
so allow about seven minutes the first time.

Run from repository root to refresh an already-populated tree (for example
after NWS publishes a new zone):

For a native installation:

```bash
edgewarn sync-nws-zones --apply
```

For Compose, initialize the host-mounted asset directory before starting the
default topology:

```bash
docker compose --profile admin run --rm edgewarn-sync-nws-zones
docker compose up edgewarn
```

Alternatively, enable the build-time sync switch to bundle a fresh snapshot in
the image:

```bash
EDGEWARN_SYNC_NWS_ZONES=true docker compose build edgewarn
docker compose up edgewarn
```

With BuildKit (the default builder used by current Docker Compose), the zone
download stage runs independently and in parallel with the runtime Conda
environment solve. The normal host-mounted zone directory still takes
precedence when it contains synchronized assets; the bundled snapshot is used
as a fallback when that mount is empty. The switch defaults to `false` so
ordinary builds do not contact the NWS zone APIs.

Set `EDGEWARN_NWS_ASSETS_DIR` to relocate the host asset directory; it defaults
to `./assets/nws_zones` and is mounted read-only into the runtime container.

Flags:

- `--assets-dir <path>` custom `assets/nws_zones` location
- `--zone-types <types...>` defaults to `forecast fire public county marine`
- `--timeout-seconds <int>` default `30`
- `--max-retries <int>` default `3`
- `--max-workers <int>` default `16`
- `--pause-seconds <float>` default `0.05`
- `--progress` / `--no-progress` progress output, default on
- `--apply` is accepted for compatibility; this script always writes updates
- `--report-path <path>` write the sync report JSON to a file
- `--config-dir <path>` select the `config/` tree to read defaults from

The listed defaults are owned by `config/nws.yaml` under `zone_sync`, not by the
parser, so a deployed tree can change them. `--pause-seconds` is scaled by
`--max-workers` to hold a whole-job rate: `0.05` is roughly 20 requests/second
regardless of thread count. `--apply` and `--report-path` have no YAML keys.

## Tests

Node.js tests:

```bash
npm test
npm run test:watch
npm run test:coverage
```

Python tests (with `EdgeWARN` active):

```bash
python -m pytest tests/
```
