# EdgeWARN-Core

**Live weather data. Operational analysis. Ready for the map.**

EdgeWARN Core powers the [EdgeWARN weather platform](https://edgewarn.ashk3000.com/). Its Python pipelines ingest operational weather data, analyze storm cells, and render radar and forecast layers. A separate Node.js service exposes generated products through a unified REST API.

## Quick start — Docker

Pull the prebuilt processing image:

```bash
docker pull ewsofficial/edgewarn:latest
```

Start Core analysis, EWMRS, and NEXRAD with persistent data and logs:

```bash
docker run -d --name edgewarn --restart unless-stopped \
  -v edgewarn-runtime:/var/lib/edgewarn \
  -v edgewarn-logs:/var/log/edgewarn \
  ewsofficial/edgewarn:latest
```

The image bundles Python dependencies, configuration, models, and an NWS zone snapshot. Live ingestion requires network access. **The Node.js API is a separate deployment and is not started by this image.** For custom configuration, Compose, and specialized service modes, see [INSTALLATION.md](INSTALLATION.md#containers).

## Inside the pipeline

| Component | Role |
| --- | --- |
| Core | MRMS/RAP ingest; storm-cell detection, optional tracking/lineage, CTAM analytics, and alert generation. |
| EWMRS | Weather raster rendering and tiling; GOES ABI, METAR/NWS, and WPC products. |
| NEXRAD | Level-II ingest and radar rendering. |
| API v3 | Versioned, file-backed access to generated products at `/api/v3`. Legacy API routes have been removed. |

The three Python services coordinate through durable runtime records. EWMRS requires the primary Core producer; NEXRAD starts both ingest and rendering. Historical reprocessing is also supported.

## Develop from source

Requires Conda or Miniconda, Node.js/npm, and Git. Docker users can skip this section.

```bash
git clone https://github.com/ewsofficial/EdgeWARN-Core.git
cd EdgeWARN-Core
git switch version-test/3.0.0
conda env create -f environment.yml
conda activate EdgeWARN
python -m pip install --no-deps -e .
npm install
edgewarn --version
```

The branch command selects v3.0.0 while the default branch tracks an earlier release. `environment.yml` is the runtime dependency authority; `--no-deps` avoids duplicate pip resolution.

### Processing

```bash
edgewarn run                  # All three services
edgewarn run core             # Core only
edgewarn run ewmrs            # Core producer + EWMRS
edgewarn run nexrad           # NEXRAD ingest + rendering
edgewarn run core --config-path /etc/edgewarn/config
```

Use repeatable `--args WORKER JSON_ARGV` flags to forward a JSON array of strings to just one worker. The supported workers are `core`, `ewmrs`, and `nexrad`. See [INSTALLATION.md](INSTALLATION.md#running-real-time-services) for examples, direct source entry points, and options.

### Unified API

Run from the repository root:

```bash
npm run api
# Debug mode: npm run debug:api
```

The API defaults to port `5000` (`3001` for debug). Its schema is available at `/api/v3/openapi.json`; health checks are at `/health/live` and `/health/ready`. See [the API v3 contract](docs/api/unified_v3.md).

### Configuration

```bash
edgewarn configure ewmrs_pipeline.workers.budget_mb.goes 2048
edgewarn configure --config-path /etc/edgewarn/config
npm run validate-config
```

The interactive configuration editor requires a terminal. Select a file and leaf value, use `Ctrl+S` to validate and save, `Esc` to go back, and `q` to quit. A complete alternate configuration tree can be selected with `--config-dir` or `EDGEWARN_CONFIG_DIR`.

### Historical reprocessing

From `src/`:

```bash
python process_historical.py --start 2024-01-01T00:00:00 --end 2024-01-01T01:00:00 --lat 20 55 --lon -130 -60
```

Storm-cell artifacts are saved to `<BASE_DIR>/data/stormcells/` with timestamped filenames.

## Runtime storage

Default data directory: `~/EdgeWARN_input` on Linux/macOS or `C:\EdgeWARN_input` on Windows; Docker uses `/var/lib/edgewarn`. Override with `--base-dir` or `EDGEWARN_BASE_DIR`. Production configuration should be mounted read-only; see [INSTALLATION.md](INSTALLATION.md) for administrative editing, NWS zone synchronization, logging, and deployment options.

## Testing

```bash
npm test
npm run test:coverage
python -m pytest
```

**Version 3.0.0** · [Installation](INSTALLATION.md) · [Configuration](docs/core/configuration.md) · [Changelog](CHANGELOG.md)
