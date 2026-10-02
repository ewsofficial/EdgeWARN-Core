# Configuration reference

EdgeWARN loads a complete, schema-validated `config/` tree before either root
process starts work. Copy the entire tree when deploying; individual YAML files
are not standalone configuration units.

For the direct Python services, discovery is `--config-dir`, then
`EDGEWARN_CONFIG_DIR`, then the selected installation/repository config
directory. The package command uses `--config-path` and can fall back to its
installed shared config directory. Runtime base directories are independently resolved as
`--base-dir` / `--base_dir`, then `EDGEWARN_BASE_DIR`, then legacy `BASE_DIR`,
then `filesystem.yaml`. All catalog edits require a process restart.

`runtime.run.disable_stormprob` defaults to `false`. Direct Core and historical
commands accept `--disable-stormprob` and `--no-disable-stormprob`; an explicit
CLI value overrides YAML. `--disable-ctam` also disables StormProb, while
`--disable-ctam-modules` leaves it enabled. With StormProb enabled, startup
checks all of its configured input and model dependencies before creating
runtime directories. The configuration editor accepts the same boolean key.

The CTAM external-module discovery root (`run.ctam_module_dir` in
`runtime.yaml`, default `ctam_modules`, resolved against the parent of the
selected config directory) is
independently overridable as `--ctam-module-dir`, then
`EDGEWARN_CTAM_MODULE_DIR`, then the YAML value. See
`docs/ctam/module-manifest.md`.

| File | Owner and operator-facing scope |
| --- | --- |
| `runtime.yaml` | Realtime run bounds, feature switches, retry and supervisor timing. |
| `historical.yaml` | Historical scan bounds, cadence, and throttling. |
| `filesystem.yaml` | Platform base-directory defaults and cleanup retention. |
| `detection.yaml` | Cell-detection thresholds, masks, expansion, and retention. |
| `lineage.yaml` | Tracking and lineage matching controls. |
| `integration.yaml` | Dataset sources, statistics, rounding, and RAP products. |
| `scheduler.yaml` | MRMS update-selection and scheduling policy. |
| `api_index.yaml` | Generated EdgeWARN index/snapshot and StormProb inactive-cell retention. |
| `ingest.yaml` | MRMS/GOES ingest products, source keys, and retention. |
| `nexrad.yaml` | NEXRAD discovery, parsing, grouping, and output selection. |
| `synoptic_rap.yaml` | RAP S3 and NOMADS HTTPS sources, freshness, and request policy. |
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

The MRMS/GOES GUI renderer writes float16 chunk artifacts and JSON indexes under
`<BASE_DIR>/gui`. RAP uses Uint16 products and NEXRAD uses gzip-compressed polar
artifacts. The retired PNG compatibility routes return `410 Gone`; clients
consume the v3 render resources.

## Package command

Install the command into the active `EdgeWARN` environment without asking
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
edgewarn configure ewmrs_pipeline.workers.max_workers 4
edgewarn configure ewmrs_pipeline.workers.worker_memory_cap 384
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

### Configurable MRMS catalog

The shipped ingest document uses schema version 2; the other 17 documents retain
version 1. `mrms.products` contains 18 optional additions; the three protected
Core inputs are always enabled. Integration statistics and EWMRS layers use full
upstream `product` identities. Raw paths derive from the enabled registry; GUI
output aliases remain unchanged. Disabled entries remain visible in diagnostics
and are omitted from execution. Core publishes its registry after preflight;
EWMRS requires matching configuration and a live Core run before MRMS scanning.
Restart all services after changes. See [upgrade and qualification](configurable-mrms-phase8.md).

### Independent ingest foundation settings

Phases 1–2 add catalog controls for the later independent ingest service. These
settings do not yet switch the deployed topology or replace the existing Core
and EWMRS loops. They are editable through the normal `edgewarn configure`
path-based editor and require restart when the consuming service is deployed.
No new CLI flags or environment variables are introduced in these phases.

- `scheduler.ingest_poll_seconds`: 10 seconds, bounded to 1–300. Discovery uses
  the existing `scheduler.s3_lookback_hours` window (1 hour). That window must
  not exceed unpinned raw-input retention (`runtime.ingest.retention_minutes`,
  inheriting `ingest.mrms.cleanup_max_age_minutes`); configuration validation
  (`validate_all_configs`, `python -m common.config.validate`) rejects a longer
  window, and the ingest service also clamps its listing to retention.
- `runtime.handoff.input_lock_timeout_seconds`: 30 seconds, bounded to 1–600.
  The shared raw-input lock is a short cross-process mutex; an operation waits
  up to this long for it and then fails with `InputLockTimeout` and retries.
  Ownership locks (service single-instance locks, the primary lease) still
  fail fast, and cleanup still makes a single attempt and defers.
- `runtime.handoff.input_lock_hold_warning_seconds`: 5 seconds, bounded to
  0.1–600. A holder that keeps the raw-input lock longer logs a warning.
- `runtime.consumers.core_readiness_seconds` and
  `runtime.consumers.ewmrs_notification_seconds`: 1 second, bounded to 0.1–1.
- `runtime.ingest`: listing/download/decode/auxiliary concurrency, per-listing
  object/page limits, pending-job limits, source timeouts, retry bounds, scan
  deadlines, reconciliation, shutdown and unpinned retention/disk budgets.
  Each YAML entry documents its units, default and schema bounds.
- `ewmrs_pipeline.input_jobs`: 4096 pending jobs, 120-minute maximum age, three
  attempts, and 5–30-second retry backoff. Existing render worker CPU/memory
  controls still own execution capacity.

Nullable ingest controls inherit existing source authorities, resolved once by
`get_ingest_settings()`: MRMS listing/download concurrency inherits
`ingest.mrms.downloads.max_concurrency` (8); listing timeout inherits the MRMS
HTTPS timeout (10 seconds); MRMS job timeout is four such timeouts (40 seconds);
auxiliary timeout is four RAP NOMADS timeouts (480 seconds); unpinned retention
inherits the MRMS cleanup age (60 minutes). Decode concurrency defaults to two,
capped by resolved download concurrency. Two auxiliary slots reserve capacity
for RAP and GLM independently of MRMS. The 8192 MiB disk budget is a backpressure
limit, never permission to delete an actively referenced input.

The frozen dependency fingerprint includes the registry configuration and
RAP/GLM enablement and effective auxiliary source/freshness settings. With CTAM
and StormProb enabled it also lists every StormProb MRMS source as a mandatory
integration input, so `--disable-ctam`/`--disable-stormprob` (or
`runtime.run.disable_ctam`/`disable_stormprob`) are dependency-shared flags
that ingest, Core and EWMRS must agree on. Later producers and consumers must compare it before using
readiness. Phase-one baselines live in
`tests/config_baseline/independent_ingest_{dependencies,settings}.json`;
`tests/fixtures/ingest/source_arrivals.json` defines missing-check, delayed-layer,
reused-RAP and disabled-GLM scenarios for subsequent phases.

### Independent input rendering

Scan-time GLM is a Core integration input and receives an explicit `no-mapping`
render acknowledgment. GOES ABI acquisition/rendering remains owned by EWMRS;
GLM arrivals do not represent an ABI channel.

Each RAP analysis maps to the configured `ewmrs_pipeline.rap_uint16` layer catalog
(including templates). The consumer shares one pinned analysis and admits
per-layer work within `ewmrs_pipeline.input_jobs.pending_max_jobs`, retrying
failures independently. Size this bound for the configured catalog (currently
about 46 layers per RAP arrival). Change the configured layers/templates to
change the rendered set; do not silently omit advertised RAP products.
