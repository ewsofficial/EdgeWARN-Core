# EdgeWARN-Core

EdgeWARN powers the weather platform with storm-cell analysis and NEXRAD radar
products. Independent Python services acquire weather inputs, analyze cells,
and serialize radar data. A separate Node.js API serves published artifacts.

## Quick start

```bash
conda env create -f environment.yml
conda activate EdgeWARN
python -m pip install --no-deps -e .
npm install
edgewarn --version
edgewarn run
```

`environment.yml` owns Python runtime dependencies. The editable install uses
`--no-deps` so Conda remains authoritative.

| Command | Services |
| --- | --- |
| `edgewarn run` | Ingest, Core, NEXRAD |
| `edgewarn run core` | Ingest, Core |
| `edgewarn run ingest` | MRMS/RAP/scan-time GLM acquisition |
| `edgewarn run nexrad` | Level-II acquisition and polar serialization |

Forward worker arguments as JSON arrays:

```bash
edgewarn run core --config-path /etc/edgewarn/config \
  --args core '["--lat_limits", "20", "55"]'
```

Workers are `ingest`, `core`, and `nexrad`. Core provides detection,
tracking/lineage, enrichment, CTAM, StormProb, alerts, and API publication.
The ingest producer publishes durable pinned readiness records independently.

## Containers and API

The processing image runs the Python services and includes configuration and
models. Persist `/var/lib/edgewarn` and `/var/log/edgewarn`:

```bash
docker run -d --name edgewarn --restart unless-stopped \
  -v edgewarn-runtime:/var/lib/edgewarn \
  -v edgewarn-logs:/var/log/edgewarn \
  ewsofficial/edgewarn:latest
npm run api
```

The Node API is deployed separately. It defaults to port 5000 and serves
`/api/v3`, `/api/v3/openapi.json`, `/health/live`, and `/health/ready`.
See [API v3](docs/api/unified_v3.md) and [installation](INSTALLATION.md).

## Configuration and storage

Deploy all 13 YAML catalogs and their schemas together. Changes require restart.

```bash
edgewarn configure runtime.ingest.disk_budget_mb 8192
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

Runtime files live under `~/EdgeWARN_input` on POSIX or `C:\EdgeWARN_input` on
Windows; override with `--base-dir` or `EDGEWARN_BASE_DIR`. Data and state are
outside the repository. `gui/NEXRAD` holds radar products.

## Historical processing and tests

```bash
cd src
python process_historical.py --start 2024-01-01T00:00:00 --end 2024-01-01T01:00:00 --lat 20 55 --lon -130 -60
```

With the EdgeWARN environment active, run `python -m pytest`, `npm test`, and
`npm run test:coverage`. Network and benchmark checks are opt-in.

[Configuration](docs/core/configuration.md) · [Breaking changes and operator cleanup](CHANGELOG.md)
