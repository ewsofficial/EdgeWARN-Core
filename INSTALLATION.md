# Installation and deployment

Create the Conda environment, install the package without dependency resolution,
and install the Node dependencies from the repository root:

```bash
conda env create -f environment.yml
conda activate EdgeWARN
python -m pip install --no-deps -e .
npm install
edgewarn --version
```

`environment.yml` owns Python dependencies; `package.json` and the lockfile own
Node dependencies. Deploy the entire 13-file `config/` tree with its schemas.
The wheel also includes a configuration snapshot under `share/edgewarn`.
For operator settings, copy the tree to `/etc/edgewarn/config` and use
`--config-path` with the package command, or `--config-dir` with direct services.
`EDGEWARN_CONFIG_DIR` selects the tree when no CLI override is given.

```bash
npm run validate-config
PYTHONPATH=src python -m common.config.validate
edgewarn configure --config-path /etc/edgewarn/config
```

Configuration writes are validated and atomic. Restart processes after changes.

## Running real-time services

| Command | Services |
| --- | --- |
| `edgewarn run` | Ingest, Core, NEXRAD |
| `edgewarn run core` | Ingest, Core |
| `edgewarn run ingest` | Ingest |
| `edgewarn run nexrad` | NEXRAD ingest and rendering |

```bash
edgewarn run core --config-path /etc/edgewarn/config \
  --args core '["--lat_limits", "20", "55"]'
edgewarn run nexrad --args nexrad '["--profile"]'
```

`--args` accepts one JSON string array per selected worker: `ingest`, `core`, or
`nexrad`. The wrapper owns topology and configuration-root flags. Shared
CTAM/StormProb/GLM settings must agree between ingest and Core; contradictory
worker settings fail preflight before spawning.

Run direct source commands from `src/`, starting ingest before Core:

```bash
python run_ingest.py
python run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300
python run_nexrad.py
python run_all.py --services ingest,edgewarn,nexrad
```

These are separate processes. The supervisor forwards shutdown signals and
reaps complete process groups. Core consumes immutable local readiness;
NEXRAD runs acquisition and rendering children independently.
`--disable-glm` disables Core lightning acquisition. `--mrms-core-only` selects
MRMS detection/integration and CTAM while disabling RAP, GLM, and NEXRAD.
`--disable-stormprob` leaves external CTAM available; `--disable-ctam` disables
all CTAM execution. Boolean switches support their `--no-*` forms.

## Runtime base directory

Precedence: CLI `--base-dir`/`--base_dir`, `EDGEWARN_BASE_DIR`, legacy `BASE_DIR`,
then `filesystem.yaml` (POSIX `~/EdgeWARN_input`, Windows `C:\EdgeWARN_input`).
All generated artifacts live beneath this root. Core writes `data/`, NEXRAD
writes `data/NEXRAD_Level2` and `gui/NEXRAD`, and durable handoff/heartbeats live
under `state/realtime`. Use a shared root and a umask that allows the API to read
published artifacts. No retirement cleanup runs automatically; see
[the changelog](CHANGELOG.md) for optional operator cleanup and rollback.

## Containers

```bash
docker compose up -d --build edgewarn
docker compose run --rm edgewarn-configure
```

Compose starts ingest, Core, and NEXRAD. Mount configuration read-only for the
processing service; the administrative configure profile mounts it writable.
Persist the runtime root and circular logs. Operator CTAM modules are mounted
read-only at `/opt/edgewarn/ctam_modules`. The image bundles dependencies,
configuration, models, and the Node API. The container entrypoint starts the
API (port 5000; `EDGEWARN_API_PORT` changes the host port) beside `edgewarn run`
when `EDGEWARN_WITH_API=1`, which the image and compose set by default. If either
process exits the container stops. Set `EDGEWARN_WITH_API=0` for processing only.

## API deployment

Run `npm run api` independently (port 5000), or `npm run debug:api` (port 3001).
Set `EDGEWARN_BASE_DIR` and `EDGEWARN_CONFIG_DIR` consistently with producers.
Set `api.server.enabled_services` to the deployed Python topology. Readiness
requires `data/`, `gui/`, and healthy enabled services. See
[deployment](docs/api/deployment.md) and [API v3](docs/api/unified_v3.md).

## Historical processing and verification

Run `process_historical.py` from `src/` with `--start`, `--end`, `--lat`, and
`--lon`; it uses an isolated historical runtime root. Activate EdgeWARN before
`python -m pytest`. Use temporary base directories in tests. Run `npm test`
and `npm run test:coverage` for API contracts.
