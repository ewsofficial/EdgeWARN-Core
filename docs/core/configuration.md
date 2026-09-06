# Configuration reference

EdgeWARN loads a complete, schema-validated `config/` tree before either root
process starts work. Copy the entire tree when deploying; individual YAML files
are not standalone configuration units.

Discovery is `--config-dir`, then `EDGEWARN_CONFIG_DIR`, then the repository's
`config/` directory. Runtime base directories are independently resolved as
`--base-dir` / `--base_dir`, then `EDGEWARN_BASE_DIR`, then legacy `BASE_DIR`,
then `filesystem.yaml`. All catalog edits require a process restart.

The CTAM external-module discovery root (`run.ctam_module_dir` in
`runtime.yaml`, default `ctam_modules`, resolved against the repository root) is
independently overridable as `--ctam-module-dir`, then
`EDGEWARN_CTAM_MODULE_DIR`, then the YAML value. See
`docs/ctam/module-manifest.md`.

| File | Owner and operator-facing scope |
| --- | --- |
| `runtime.yaml` | Realtime run bounds, feature switches, retry and supervisor timing. |
| `historical.yaml` | Historical scan bounds, cadence, and throttling. |
| `filesystem.yaml` | Platform base-directory defaults, cleanup retention, colormap lookup. |
| `detection.yaml` | Cell-detection thresholds, masks, expansion, and retention. |
| `lineage.yaml` | Tracking and lineage matching controls. |
| `integration.yaml` | Dataset sources, statistics, rounding, and RAP products. |
| `scheduler.yaml` | MRMS update-selection and scheduling policy. |
| `api_index.yaml` | Generated EdgeWARN index/snapshot retention. |
| `ingest.yaml` | MRMS/GOES ingest products, source keys, and retention. |
| `nexrad.yaml` | NEXRAD discovery, parsing, grouping, and output selection. |
| `synoptic_rap.yaml` | RAP source discovery, freshness, and request policy. |
| `wpc.yaml` | WPC surface-analysis sources and artifact naming. |
| `metar.yaml` | METAR source, parsing, and retention settings. |
| `nws.yaml` | NWS alert and zone-sync sources, headers, and retry policy. |
| `ewmrs_render.yaml` | MRMS/GOES render-layer inputs and render settings. |
| `ewmrs_pipeline.yaml` | EWMRS processing, cleanup, and render scheduling; the `rap_uint16` section holds RAP Uint16 conversion layers and encoding metadata. |
| `api.yaml` | Unified API network, security, limits, artifact, and query policy. |
| `kalman.yaml` | Kalman filter, assignment, and tracking parameters. |

Each file has a matching `config/schema/*.schema.json`; the schema gives the
accepted types and numeric ranges. Validate an installation before starting it:

```bash
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

The GUI renderer writes float16 chunk artifacts and JSON indexes under
`<BASE_DIR>/gui`; PNG routes are compatibility endpoints only where legacy PNG
artifacts exist.

## Package command

Install the command into the active `EdgeWARN-dev` environment without asking
pip to resolve runtime dependencies:

```bash
python -m pip install --no-deps -e .
```

All package-run modes validate this entire catalog before any child starts:

```bash
edgewarn run                                      # core + EWMRS + NEXRAD
edgewarn run core                                 # primary only
edgewarn run ewmrs                                # primary + EWMRS
edgewarn run nexrad                               # NEXRAD ingest + render
edgewarn run core --config-path /etc/edgewarn/config
edgewarn run ewmrs \
  --args core '["--lat_limits", "20", "55"]' \
  --args ewmrs '["--disable-wpc"]'
```

`--args WORKER JSON_ARGV` accepts only a JSON array of strings and routes it to
one selected worker without shell parsing. The EWMRS mode includes its primary
producer dependency, and the NEXRAD mode includes both ingest and rendering.

## Validated edits

Use a filename stem, dotted leaf path, and one YAML scalar:

```bash
edgewarn configure ewmrs_pipeline.workers.budget_mb.goes 2048
edgewarn configure --config-path /etc/edgewarn/config \
  runtime.run.disable_nexrad true
```

The command validates the whole existing tree, locks and re-reads it, preserves
round-trip YAML details, validates the proposed document, atomically replaces
the target with its permission bits intact, and validates the full on-disk tree
again. Invalid paths, collections, tags, aliases, schema violations, symlink
escapes, and read-only files do not produce a partial edit.

Without a dotted assignment, `edgewarn configure` opens a TTY-only two-screen
editor. Choose a file, then choose a leaf; `Ctrl+S` validates and saves, `Esc`
returns to the previous screen, and `q` quits when no editor is open. A
validation error remains visible without changing the file. Noninteractive
containers must use the dotted form.

Package-command status `0` means success or clean shutdown, `1` means a worker
or write/rollback failure, and `2` means usage or configuration validation
failure. Production containers mount this directory read-only; only the
administrative `edgewarn configure` container should mount it read-write. See
`INSTALLATION.md` and `compose.yaml` for the complete container commands.
