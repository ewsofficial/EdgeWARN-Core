# Ingestion Architecture

EdgeWARN-Core uses a filesystem-first ingest model. Remote products are
selected and staged under the configured runtime base directory, then
consumed by the service that owns the corresponding processing or rendering
work. Shared implementations live under `src/common/`; `src/EdgeWARN/ingest/`
is a compatibility re-export layer.

The [configurable MRMS Phase 1 contract and impact inventory](configurable-mrms-phase1.md)
records the current 21-product baseline, three-product protected set,
raw-path migration, and implementation ownership. The shipped ingest catalog
uses version 2 with 18 optional additions and three code-owned protected inputs. Core startup now audits enabled CTAM declarations and StormProb
dependencies before initializing the runtime filesystem.

An enabled external CTAM module must write `requires = []` or declare
`[[requires]]` selectors. Required disabled MRMS products stop Core at startup;
enabled but unavailable products block only that module for the cycle. A
required `previous` selector needs a retention window at least as long as its
declared `max_age_seconds`. Changes to module declarations or ingestion
eligibility require a restart.

StormProb has a separate all-input gate after the final optional input and
enrichment snapshot. Missing required MRMS/RAP inputs or model features stop
the whole Core process with a nonzero status before forecast or alert
publication. Set `runtime.run.disable_stormprob: true` or use
`--disable-stormprob` to skip its dependency and asset checks, inference, and
new publication while keeping external CTAM modules available. `--disable-ctam`
disables both; `--disable-ctam-modules` disables only external modules.

## Service ownership

```text
run_ingest.py
  MRMS + raw RAP + scan-time GLM acquisition
    -> validated inventory + per-input render-ready -> run_ewmrs.py
    -> core-start-ready / core-integration-ready / core-final-ready -> run_edgewarn.py

run_edgewarn.py
  local readiness consumer: detection, integration, CTAM, alerts, publication

run_ewmrs.py
  per-input MRMS rendering + RAP Uint16 conversion
  GOES ABI ingest/render + METAR, NWS, WPC accessories

run_nexrad.py
  Level-II discovery/download/parse/staging + polar rendering
```

`edgewarn run` starts all four Python services. `edgewarn run core` starts
ingest and Core; `edgewarn run ewmrs` starts ingest, Core, and EWMRS.
Direct consumers require a separately managed `run_ingest.py` using the same
config root, runtime root, and dependency options. Ingest and EWMRS can run
without Core using `run_all.py --services ingest,ewmrs`.

## Realtime readiness and rendering

The ingest service commits validated inputs and durable records beneath
`<BASE_DIR>/state/realtime/ingest/v1/`. Core starts detection only after all
check inputs are ready locally, then waits for integration and final snapshots.
The snapshots pin exact paths and timestamps through processing and retention.

EWMRS runs `InputRenderConsumer`, consuming each `render-ready` notification
independently of Core readiness. It persists per-layer plans and acknowledgments,
retries failed layers separately, and releases the input reference after all
mapped layers have a durable terminal disposition. Each RAP analysis fans out to
the configured RAP layers. Scan-time GLM is a Core integration input with an
explicit no-mapping acknowledgment; GOES ABI is acquired/rendered separately.

Realtime no longer writes `state/realtime/ingest-reports` or the legacy
`cycles/<cycle-id>/{mrms-ready,rap-ready}.json` records. Repository readers do
not depend on the retired report directory; external tooling needs an operator
audit. Legacy handoff primitives remain available for migration tooling.

## Historical staged MRMS/RAP cycle

Historical processing retains `common.pipeline.coordinator.run_staged_ingest_cycle`
and its async acquisition with synchronous fallback. It returns a `CycleState`
with an immutable `CycleInputManifest`: exact local paths, product/family/source
identity, encoded analysis times, validation, and current/previous selections.
Historical reports remain under `state/historical/ingest-reports`.

## RAP rendering

RAP Uint16 conversion is EWMRS-owned derived processing. For each accepted RAP
`render-ready` notification, configured layers are written as:

```text
<BASE_DIR>/gui/RAP/<outdir>/<YYYYMMDD-HHMM00>/data.u16
<BASE_DIR>/gui/RAP/<outdir>/<YYYYMMDD-HHMM00>/metadata.json
```

`data.u16` contains the full `Ni * Nj` little-endian grid. Metadata identifies
the layer, source file, timestamp, shape/grid, dtype, byte order, scale,
missing-value sentinel, units, matched GRIB keys, and optional layer metadata.
`outdir` is the configured output directory and may differ from the layer
name; `colormap_key` is internal renderer metadata, not an API layer name.

Historical processing uses the same shared staged coordinator with EWMRS and
GOES disabled. `src/process_historical.py` selects the best available MRMS
cycle minute by minute, runs detection from the returned manifest, and runs
integration only when its manifest inputs are available.

## MRMS

`src/common/ingest/mrms/` provides the MRMS catalog, timestamp selection,
async/synchronous S3 and HTTPS download paths, decompression/parsing, atomic
staging, and cleanup. Important entry points are:

- `download_detection_files_async(dt, ...)`
- `download_integration_files_async(dt, ...)`
- `download_all_files_async(dt, ...)`
- `download_detection_files(dt, ...)` and `download_integration_files(dt, ...)`
  for synchronous fallback paths

Detection and integration modifiers are deliberately separate. A structured
`DownloadBatchResult` reports attempted, downloaded, and failed products;
readiness requires every requested product to be present and successful.
For each MRMS product, an S3 listing miss, failed S3 fetch, or S3 processing
error triggers an HTTPS lookup/download for the requested timestamp before
that product is reported unavailable. The HTTPS matcher accepts the exact
minute or the configured narrow timestamp window; it does not substitute an
arbitrarily newer file. Both source paths stage files atomically.
GRIB2 acquisition validates every message's framing and decodes its values
with ecCodes before publication; undecodable downloads are quarantined and
the source fallback remains eligible. The coordinator applies the same
validation to local previous observations, trying older history when the
newest prior file is corrupt. Unusable history is omitted from the CTAM
manifest rather than reported ready.
`max_entries` and `remove_old_files` default to the runtime/catalog settings,
so configuration remains the source of truth unless a caller explicitly
overrides them. Cleanup is restricted to configured runtime directories.

PrecipRate is excluded from the MRMS scan-discovery readiness subset because
its upstream latency can delay selection of an otherwise usable cycle. It
remains in the full ingest catalog and the integration/render paths, so the
selected cycle still downloads it, computes `maxPrecipRate`, and serves the
`MRMS_PrecipRate` EWMRS product.

MergedReflectivityAtLowestAltitude is also excluded from scan-discovery
readiness to avoid waiting for that layer. It remains in the full ingest
catalog for downstream use.

ProbSevere is a distinct MRMS-family product with its own bucket-path and JSON
handling. Its product identity must be preserved in manifests and downstream
processing.

## GOES ABI and scan-time GLM

GOES ABI is an EWMRS-owned background pipeline. The supervised GOES ingest loop
polls `noaa-goes19` for `ABI-L1b-RadC` channel files, preferring async download
with sync fallback. It stages channels locally; the poll-based render loop
selects a complete configured ABI set (currently C01-C16) near the target
time and renders each distinct input selection once. A scan-window filename is
valid across its encoded start/end interval, and readiness requires every
configured channel.

GOES ABI source files are staged in configured per-channel runtime directories.
Single-channel GUI products are written under `<BASE_DIR>/gui` as tiled
float16/gzip chunks with schema-version-2 product and timestamp indexes. RGB
composites are derived client-side; they are not staged or rendered server-side.
See `docs/core/goes_pipeline.md` for the render and API representation.

GLM uses the same NOAA GOES-19 source family (`GLM-L2-LCFA`) but has different
ownership and readiness semantics: the realtime primary downloads scan-time
GLM when enabled and gates EdgeWARN integration on a valid pinned GLM input.
It is not part of the EWMRS ABI render loop.

## RAP / Synoptic

`src/common/ingest/synoptic/` stages RAP files for EdgeWARN integration. The
selection is local-first and walks backward through eligible UTC analysis hours
from the requested scan. The default maximum analysis age is 180 minutes and
can be overridden with `EDGEWARN_RAP_MAX_AGE_MINUTES`. Freshness is determined
from the analysis timestamp encoded in the RAP filename, not filesystem mtime.

For each eligible hour, a definitive S3 404 skips the synchronous S3 attempt;
other S3 failures receive one synchronous S3 attempt. If S3 cannot provide a
valid file, RAP downloads the same analysis from the configured NOAA NOMADS
HTTPS path before considering an older hour. NOMADS downloads are streamed,
validated as complete GRIB2 files, and published atomically under the same
local filename as S3 files. When the search window is exhausted, the readiness
error includes the configured limit and failures from both sources. RAP cleanup
uses the same encoded-time policy and retains at most the newest three eligible
analyses under `<BASE_DIR>/data/RAP`.

## METAR

`src/common/ingest/metar.py` ingests hourly Aviation Weather METAR cycle files,
using a cached station database for coordinates. It parses reports, enriches
station locations, filters to configured CONUS bounds, writes hourly snapshots,
and cleans old snapshots:

```text
<BASE_DIR>/data/METAR/METAR_YYYYMMDD-HHz.json
```

The EWMRS service runs the async entry point on the configured hourly boundary;
sync and async paths share the same parsing/output behavior.

## NWS alerts

`src/common/ingest/nws/` downloads the active-alert feed from the configured
National Weather Service URL, filters blocklisted event types, maps UGC zones
to geometry, and updates a deduplicated registry. It reconciles the registry
against the current active ID set and applies expiration/TTL cleanup while
writing timestamp snapshots for API serving.

NWS zone assets are an operator-managed prerequisite. Run
`edgewarn sync-nws-zones --apply` before enabling NWS on a new runtime; EWMRS
fails preflight if enabled NWS ingest has no usable zone assets. The loop polls
the active feed on its configured interval. Its registry and timestamp
snapshots are stored under `<BASE_DIR>/data/Alerts/official/` (raw downloads,
when retained, are under `<BASE_DIR>/data/NWS_Raw/`).

## NEXRAD Level II

`src/common/ingest/nexrad/` owns independent realtime Level-II processing:
volume discovery, station filtering, chunked S3 download, VCP probing,
stream/boundary handling, parsing, worker-pool execution, staged writes, and
retention. `run_nexrad.py` supervises both the ingest and render loops; it does
not depend on the MRMS/RAP phase records.

Staged Level-II data and manifests live under:

```text
<BASE_DIR>/data/NEXRAD_Level2/<SITE>/...
<BASE_DIR>/data/NEXRAD_Level2/manifests/...
```

The NEXRAD renderer polls those local staged outputs and writes gzip-compressed
polar intermediates under:

```text
<BASE_DIR>/gui/NEXRAD/<SITE>/<ELEVATION>/
  <SITE>_<PRODUCT>_<ELEVATION>_<YYYYMMDD-HHMMSS>.bin.gz
```

Discovery, chunk-listing, downloads, full scans, and worker liveness are
bounded and observable; stale worker heartbeats cause restart handling. The
resulting files are served by the EWMRS/NEXRAD API adapters documented in
`docs/api/ewmrs_api_endpoints.md`.

## WPC surface analysis

`src/common/ingest/wpc/` downloads the latest valid coded surface analysis,
using fixed WPC analysis hours and a previous-analysis fallback. It parses the
coded product, converts fronts/troughs and pressure centers to GeoJSON, writes
the latest artifact, writes timestamped copies for the current and previous
analysis, and removes old timestamped files:

```text
<BASE_DIR>/wpc/surface_analysis/latest.geojson
<BASE_DIR>/wpc/surface_analysis/wpc_sfc_YYYYMMDD-HH0000.geojson
```

TLS certificate verification is required for WPC downloads. The EWMRS WPC
loop runs on the configured analysis boundary and exposes these artifacts
through the WPC API routes.

## Runtime layout

All generated data is rooted at the resolved `<BASE_DIR>`:

```text
<BASE_DIR>/
├── data/      # MRMS, RAP, METAR, NWS alerts, NEXRAD, and other staged data
├── gui/       # MRMS/GOES/RAP/NEXRAD render artifacts and indexes
├── state/     # realtime cycle records, consumer checkpoints, service state
└── wpc/       # WPC surface-analysis GeoJSON
```

The primary CLI accepts `--base_dir`/`--base-dir`; the API and accessory
services resolve their supported base-directory flags/environment settings
through their service parsers. Do not introduce repository-local output paths:
the runtime filesystem is the source of truth for staged inputs, durable
handoff, rendered artifacts, and API visibility.

The registry acquisition and path implementation is documented in
[Configurable MRMS phase 4](configurable-mrms-phase4.md), including explicit
startup directory creation, bounded acquisition, staging, validation, and
quarantine. The shipped catalog activates this implementation.

[Configurable MRMS phase 5](configurable-mrms-phase5.md) separates mandatory
readiness from optional MRMS completion, preserves phase snapshots, and protects
active raw inputs across processes. Historical CLI runs now use the isolated
`<BASE_DIR>/historical` runtime root.

### Configurable MRMS consumer agreement (phase 7)

For development ingest v2 catalogs, integration statistics and EWMRS layers
resolve their `product` identities through the effective registry. Legacy raw
aliases remain accepted during the migration period. Disabled entries appear
in `get_datasets_config(include_inactive=True)` and
`get_mrms_file_list(include_inactive=True)` with `active: false`, an
`ingestion-disabled` reason, and no resolved raw path. Execution lists omit
those entries, including when an old raw directory still exists.

Pinned Core MRMS inputs are selected by family, full product identity, and
current/previous role. Missing current inputs never fall back to an arbitrary
latest file. EWMRS continues to select each eligible layer independently and
preserves its source timestamp, GUI identity, payload, and retained history.
Adding raw ingestion does not register a public API product.

After successful preflight and acquiring its service lock, Core atomically
publishes `state/realtime/services/edgewarn-mrms-registry.json`. The descriptor
contains schema/contract versions, enabled identities, registry fingerprint,
and the Core run ID; it contains no authoritative raw paths. EWMRS checks it
against its own registry and the existing Core heartbeat on each MRMS pass.
Missing, mismatched, stale, or previous-run state pauses MRMS scanning and
checkpoint advancement. RAP consumption and unrelated GOES/accessory work
continue. Matching configuration and a live matching Core run automatically
resume pending MRMS work. Restart services after configuration changes.

See [Phase 8 migration and qualification](configurable-mrms-phase8.md) for upgrade, rollback, and offline resource measurements.

## Independent ingest foundations (phases 1–2)

The current services still use the staged cycle coordinator. The independent
service, inventory/outbox, and per-layer consumer cutover are later phases of
`plans/independent-ingest-incremental-rendering-plan.md`.

`resolve_dependencies()` in `common.ingest.mrms.core_contract` freezes the
registry's full manifest IDs and dependency policy. The default check and
detection sets are composite reflectivity, PrecipFlag, and `ProbSevere` (the
legacy null modifier). Empty checks, disabled references, and detection outside
the check set fail preflight. There are currently no additional mandatory MRMS
integration products. Enabled RAP and scan-time GLM remain separate integration
requirements; `mrms_core_only` disables both and `disable_goes` disables GLM.
Previous detection history is the latest validated strictly earlier observation,
when available; it is pinned independently from current checks.

Optional MRMS acquisition still has the configured 30-second default deadline;
the staged coordinator retains its additional single HTTPS-timeout teardown
allowance (10 seconds by default). CTAM receives a distinct immutable final
snapshot: the unchanged detection/history and integration selections, plus
aligned successful optional current inputs and validated optional history.
Every optional product has a terminal outcome, including unavailable/expired
ones. Late arrivals after that boundary must not revise this snapshot. They can
still supply independent rendering in the later inventory phase. StormProb's
existing preflight and fatal per-cycle source/feature checks remain authoritative;
an optional acquisition failure does not make a required StormProb feature
optional. Missing final optional completion is also fatal when both CTAM and
StormProb are enabled.

`discover_objects()` and `discover_objects_sync()` in
`common.ingest.mrms.discovery` list a bounded observation-time window across day
boundaries and all S3 pages. They return immutable descriptors with exact source
locators, encoded times and remote versions/ETags when available. S3 acquisition uses `VersionId` for
versions and `IfMatch` for ETags. HTTPS fallback
uses the same product/time identity. Object/page limits raise
`ListingLimitExceeded`; consumers must apply backpressure or subdivide the
window, never advance a completed cursor from an incomplete result. Sync callers
must supply an S3 client with bounded connection/read timeouts.

`acquire_object()` / `acquire_object_sync()` in `common.ingest.mrms.acquisition`
acquire an exact descriptor and use exact-time mirror fallback, existing payload
validation, quarantine and atomic publication. They return `CommittedInput`
only after the final file is usable. Its `input_id` derives from family, full
product ID, observation time and validated content digest, so mirror reuse has
one identity. Corrected content cannot replace an existing published file.
`acquire_batch()` and its sync counterpart keep historical behavior and can
report each completion through `on_committed` before the batch finishes.
Notification failures do not undo files already committed; recovery must retry
notification delivery rather than assume the source download failed.

`acquire_rap_input()` preserves analysis-hour selection and validates the returned
GRIB and age. Requests for different scans that reuse one analysis return the
same identity. `acquire_glm_inputs_for_scan()` isolates source/merge work in
staging, validates NetCDF variables and scan alignment, then publishes without
replacing existing bytes. Neither completion wrapper owns source retention.
Realtime RAP acquisition also preserves invalid existing observations for
inventory repair instead of deleting or replacing potentially referenced files.
The future inventory owns deduplication and durable notifications, including
avoiding a second render event on RAP reuse. Existing historical wrappers retain
their cleanup defaults; realtime callers must use the no-cleanup object APIs.

## Independent ingest durable contracts (phase 3)

The producer contract is implemented in `common.ingest.inventory`,
`common.pipeline.readiness`, and `util.runtime.ingest_handoff`. Service startup
and consumer cutover are scheduled for later phases of the
[independent ingest plan](../../plans/independent-ingest-incremental-rendering-plan.md).

`InputInventory.commit_input()` accepts a validated acquisition completion and
persists its identity, digest, exact source path/time, provenance, dependency
fingerprint, and producer run ID beneath `state/realtime/ingest/v1/inputs/`.
`IngestHandoff.publish_render_ready()` separately commits an immutable input
notification. Restart reconciliation adopts explicitly enumerated files through
a supplied payload validator, repairs missing notifications, and returns pending
input IDs. Publication failure leaves the acquisition committed for recovery.

`InputInventory.publish_scan()` uses the shared replay input lease to select
and pin inputs before retention can delete them. The pure `evaluate_scan()`
requires every check product in the normalized scan, selects previous history
separately, and evaluates mandatory integration and enabled RAP/GLM inputs.
Frozen auxiliary settings carry the RAP age budget. Start, integration, and
final optional-input manifests retain the earlier phase selections. Persisted
scan timing bounds optional completion across restarts; terminal records state
why an incomplete scan was abandoned. Consumers must validate dependency
membership with `validate_phase_dependencies()` and exact files with
`validate_phase_inputs()` before using a phase.

EWMRS records its enabled layer mapping with `plan_render()`. Layer dispositions
are keyed by input ID, layer ID, and render configuration fingerprint and include
attempt counts, retry eligibility, and terminal reasons. `acknowledge_input()`
requires every mapped layer to reach a terminal disposition, or explicitly
records that no layer is configured. Artifact and index verification before
successful acknowledgment belongs to the renderer consumer in phase 6.

Retention protects pending notifications, active Core selections, explicit
worker pins, and detection/optional history pins. Cleanup uses the same
nonblocking replay lease; callers retry on lock contention. Only acknowledged,
unreferenced inputs older than the caller's rediscovery cutoff are eligible.
Identity tombstones precede deletion so reconciliation can finish interrupted
cleanup and reject conflicting rediscovery. File verification caches use file
version metadata only to avoid repeated hashing; encoded observation time
remains the source-time authority.
