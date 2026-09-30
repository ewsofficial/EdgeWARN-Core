# Independent ingest polling and incremental EWMRS rendering

Status: phases 1 and 2 implemented and verified; phases 3–8 remain pending.
The independent realtime service and consumer cutover are not yet enabled.

## 1. Required outcome

Create a separately supervised ingest process that polls for new source files
every **10 seconds**, downloads available files independently of Core processing,
and publishes durable readiness as each file becomes usable.

- Poll every enabled MRMS product, including products outside the check subset.
  A missing check modifier must never prevent acquisition of another product.
- Start the EdgeWARN detection worker for a scan once **all configured check
  modifiers are downloaded and validated locally for that scan**. Remote listing
  presence alone is insufficient.
- Preserve Core's separate integration prerequisites. Starting detection must
  not require every integration product, RAP, or GLM to have arrived.
- Make newly staged render inputs available to EWMRS during every poll/download
  pass, including while Core is processing, retrying, stopped, or waiting for a
  missing check modifier. Publish completion records immediately; do not wait
  for all downloads in the pass to finish.
- Render only the affected layers, using their actual source timestamps. A late
  layer for an already processed scan must still produce new render work.

Scope: move realtime MRMS, raw RAP, and scan-time GLM acquisition out of Core.
EWMRS retains its existing GOES ABI, METAR, NWS, and WPC loops; NEXRAD retains its
own ingest/render service. Historical processing retains its explicit staged
ingest entry point and does not consume or publish realtime queue records.

This plan is implementable against the current catalog. The adjacent
[`configurable-mrms-ingestion-plan.md`](configurable-mrms-ingestion-plan.md) is a
separate proposal, not an implemented prerequisite. If it lands first, use its
resolved product registry and protected dependency sets in the contracts below.
Do not combine its directory migration with this process split. This plan
supersedes that proposal's instruction to retain Core ownership of RAP/GLM.

## 2. Findings in the current tree

These findings come from source inspection, not a live timing experiment.

| Location | Current behavior and change needed |
| --- | --- |
| `src/util/runtime/primary_service.py:run_primary_cycle_loop` | Selects the newest common remote check-product minute, calls `run_primary_cycle_once` synchronously, then waits for supervisor ticks. Discovery is blocked by the full Core cycle; the default tick wait is another 15 seconds. |
| `src/EdgeWARN/schedule/scheduler.py:MRMSUpdateChecker` | Intersects remote timestamps with UTC/even-minute normalization and S3/HTTPS fallback. Reuse timestamp rules, but move discovery to independent per-product listings and test readiness against local validated inputs. |
| `src/util/runtime/cycle.py:run_primary_cycle_once` | Starts the worker, downloads MRMS/RAP and scan-time GLM, releases in-memory barriers, publishes EWMRS records, and waits for the worker. Split acquisition from this execution boundary. |
| `src/common/pipeline/coordinator.py:run_staged_ingest_cycle` | Starts acquisition tasks concurrently but awaits all selected source results before invoking callbacks. Moving this entire function to a timer would retain the batch barrier. |
| `src/common/ingest/mrms/main.py` and `downloader.py` | Supply source fallback, atomic staging, and `DownloadBatchResult`. Integration currently includes every enabled MRMS product outside detection. Extract per-object discovery/download completion primitives; preserve the batch API for historical callers. |
| `src/util/runtime/handoff.py` | Stores immutable `mrms-ready`/`rap-ready` records once per cycle; checkpoint selection is cycle based. Republishing the same cycle cannot represent a later layer arrival. |
| `src/util/runtime/ewmrs_consumer.py` | An MRMS record already triggers an independent best-effort scan of all layers. Its problem is trigger timing and cycle checkpoint granularity, not an aggregate MRMS readiness predicate. |
| `src/EWMRS/pipeline.py` | Supports a selected `layers` list and pinned `input_manifest` in `run_render_pipeline`; `_render_layer` reuses complete outputs. Extend these seams rather than implementing a second renderer. |
| `src/run_all.py`, `src/edgewarn_cli/run.py`, `src/util/runtime/services.py` | Launchers and the heartbeat registry currently enumerate three services. Add ingest explicitly to packaging, topology, argument ownership, and service discovery. |

Current `ingest.mrms.check_products` contains ten entries; detection membership
contains composite reflectivity, PrecipFlag, and ProbSevere. Normalize the null
ProbSevere source modifier to manifest identity `ProbSevere` consistently.
PrecipRate and lowest-altitude reflectivity remain outside the check gate and
continue to download and render when available.

## 3. Process and ownership design

```text
run_ingest.py
  10-second discovery timer -> bounded download/decode workers
                               |
                    validated atomic source files
                               |
                  durable input inventory + outbox
                         /                 \
          per-scan Core phase records     per-input render records
                      |                         |
              run_edgewarn.py              run_ewmrs.py
              sequential scans             independent layer jobs
              detection -> integration     artifacts -> indexes
```

Proposed modules and responsibilities:

| Module | Responsibility |
| --- | --- |
| New `src/run_ingest.py` | Configuration preflight, filesystem initialization, ingest service lock, heartbeat, signals, and service entry point. |
| New `src/util/runtime/ingest_service.py` | Poll cadence, bounded job dispatch, retry scheduling, completion handling, and shutdown. No scientific analysis or rendering. |
| New `src/common/ingest/inventory.py` | Validated object identities, exact paths, source times, durable acquisition state, recovery reconciliation, and retention references. |
| New `src/common/pipeline/readiness.py` | Pure per-scan dependency evaluation and immutable phase manifest construction from the inventory. No remote I/O. |
| New `src/util/runtime/ingest_handoff.py` | Versioned Core readiness records, per-input render outbox, consumer acknowledgments, and strict validation. Reuse existing atomic-write and lock utilities. |
| `src/util/runtime/primary_service.py`, `cycle.py` | Consume Core readiness, run one scan at a time, preserve truthful outcomes/retries, and publish analysis artifacts. |
| `src/util/runtime/ewmrs_consumer.py`, `src/EWMRS/pipeline.py` | Consume per-input work, resolve affected configured layers, pin source files, render and acknowledge each layer independently. |

Ingest owns all realtime source acquisition retries and source cleanup for the
moved products. Core owns analysis retries and tracking state; EWMRS owns render
retries and GUI cleanup. Neither consumer downloads a missing source. Avoid
imports from EWMRS or scientific Core modules in the ingest service: its outbox
describes source products, and EWMRS owns the product-to-layer mapping.

## 4. Ten-second polling and download behavior

1. On startup, load and validate the full catalog, acquire the ingest service
   lock, reconcile committed local inputs/outbox, and poll immediately.
2. Schedule discovery with a monotonic timer at `t0 + n * 10s`. Discovery and
   download workers must not run on the timer's execution path. Never implement
   `download_everything(); sleep(10)` or wait for Core/EWMRS acknowledgments.
3. At each tick, refresh every enabled product's bounded listing window and
   enqueue unseen eligible objects. Fetch every new object within that window,
   subject to explicit queue/backlog bounds, rather than only the latest common
   scan. Use pagination where required; a listing-depth cap must not silently
   hide newer files behind old entries.
4. Track per-product listing jobs so a slow request cannot overlap itself
   indefinitely. Tick other products on schedule; record an overrun and retry
   the slow product after its bounded timeout. Missed deadlines coalesce into
   one refresh, with no burst of catch-up timers. The 10-second requirement is
   the scheduling cadence, not a promise that a stalled network finishes in 10s.
5. Use a sliding observation-time lookback across hour/day boundaries, plus
   pending scan dependencies. Do not filter discoveries solely by Core's last
   processed scan or a global maximum source timestamp: delayed older products
   still need acquisition. Give waiting check inputs priority and reserve
   capacity for other products so neither class starves.
6. Deduplicate queued/in-flight/completed objects by canonical product, encoded
   source timestamp, and remote object identity/version where available. Map
   equivalent S3 and HTTPS observations to the same logical input after
   validation. Remember failures separately; a failed download is never a
   completed cursor advancement.
7. Download, decompress/parse, validate timestamps and payload structure, then
   atomically expose the final file. Reuse existing source fallback behavior,
   including ProbSevere JSON handling and RAP S3/NOMADS selection. Run blocking
   fallbacks/decode work in bounded owned workers, with real timeouts and joined
   shutdown; an abandoned async wrapper must not leave work running forever.
8. Persist each successful input and publish its render outbox record immediately.
   Evaluate affected Core scans after each completion. Other incomplete jobs
   in the same poll must not delay either publication.
9. At every tick reconcile newly completed inputs and uncommitted notifications,
   and atomically update poll status with counts and pending/error reasons.
   A poll with no changed source creates no duplicate render jobs. A completion
   after its originating poll ends still publishes immediately.

RAP/GLM run within this service with separate bounded capacity so a large RAP
download cannot occupy all MRMS slots. RAP keeps analysis-hour freshness and
local reuse; reusing one analysis across scans produces one render input event.
Schedule GLM for candidate scan times using the existing scan-time selection
rules. Neither auxiliary source delays a discovery tick or MRMS render event.

## 5. Readiness contracts and durable state

Add a distinct versioned namespace; do not reinterpret existing v1 phase files
or reuse EWMRS's old cycle checkpoints for the new stream:

```text
<BASE_DIR>/state/realtime/ingest/v1/
  inputs/<input-id>.json
  scans/<cycle-id>/core-start-ready.json
  scans/<cycle-id>/core-integration-ready.json
  scans/<cycle-id>/terminal.json
  render-ready/<input-id>.json
  poll-status.json
<BASE_DIR>/state/realtime/consumers/
  core-ingest-v1/...
  ewmrs-inputs-v1/...
```

Use deterministic, path-safe IDs derived from canonical identities; never use
untrusted product strings as unchecked path segments. Records include schema
version, producer/run ID, UTC publication time, effective dependency-config
fingerprint, source/product identity, exact contained path, encoded observation
time, validation evidence, and immutable input ID. Poll IDs are diagnostic;
they are not scan IDs or output timestamps.

### Core start and integration

- Define `C` as normalized check products and `D` as detection membership.
  Preflight requires nonempty `C`, enabled references, and `D ⊆ C`. Reject an
  invalid catalog before workers start instead of silently starting detection
  with absent inputs or enlarging the requested gate.
- For candidate scan `T`, start readiness is `all(valid_local_input(p, T) for
  p in C)`. Use existing scan normalization and manifest alignment tolerances,
  retaining actual source times. Never intersect an incomplete subset, use
  mtime, use a previous-history record as current, or mix arbitrary latest files.
- Publish `core-start-ready` once, with the complete check set and pinned
  previous detection inputs selected under existing history rules. Core
  starts detection when this record is available and its worker is idle.
- Evaluate integration independently using current integration membership,
  RAP when enabled, and scan-time GLM when enabled. Publish
  `core-integration-ready` once after all those dependencies validate, including
  the same current and previous detection selections from the start record.
  A later input must never revise an already committed phase manifest.
- A worker can finish detection while waiting for integration. Replace its
  network-dependent shared-state producer with a local record watcher that
  validates and installs the integration snapshot before releasing the barrier.
  Waiting for data does not consume a Core processing retry attempt.
- Preserve `mrms-core-only` and GOES/GLM disable semantics consistently in the
  producer and consumer fingerprint. Do not report disabled RAP/GLM as missing.
- Give incomplete scans a configured deadline. On expiry publish a terminal
  reason listing missing inputs; wake any waiting worker and record the scan as
  explicitly abandoned without reporting success. Later arrivals still render.
- Keep analysis sequential and timestamps increasing for tracking/lineage.
  Process pending scans oldest first within the configured backlog; block a
  newer scan behind an older candidate only until the older deadline/cap expires.
  Persist skip decisions. A late start record older than the processing cursor
  is explicitly skipped, never used to rewind tracking. Preserve time-gap reset
  behavior and `CycleStateStore`'s distinction between success and abandonment.

The integration set is currently larger than `C`. This plan does not quietly
make those extra inputs optional. The separate configurable-MRMS proposal may
change that dependency set; adoption requires its own scientific regression
coverage. Detection must still begin at `C` readiness in either implementation.

### EWMRS input notifications and acknowledgment

- Publish one immutable `render-ready` record per committed input identity,
  regardless of check completion, Core phase state, or Core success. Nonrendered
  products are explicitly acknowledged as having no configured layer mapping.
- Every newly downloaded layer in every poll therefore has durable work. Two
  products with the same scan time have different IDs. A late arrival for the
  same scan cannot be hidden by a cycle checkpoint or a maximum timestamp.
- Map an input to zero or more enabled layers in the EWMRS consumer. Queue jobs
  by `(input-id, layer-id, render-config-fingerprint)`, preserving actual source
  times and raw RAP analysis time. Persist per-layer acknowledgments; one failed
  layer must not block unrelated layers or falsely acknowledge the input.
- Call the existing renderer with just those layers and a pinned manifest;
  never replace the notified file with a directory's newer `latest` file.
  Reuse complete outputs after validating their expected chunks/metadata/index.
  A `None` result from `_render_layer` is a failed job even without an exception.
- Use bounded long-lived render workers and drain notifications while workers
  run. Avoid constructing an entire all-layer process pool per file and avoid
  waiting for all jobs in one poll before accepting the next poll's jobs.
- Acknowledge only after usable artifacts and their indexes are committed.
  Restart after output publication but before acknowledgment reuses the complete
  output. This provides retryable delivery and idempotent publication; it does
  not promise that a crashed process executed a render only once.
- Keep per-layer retry/backoff/terminal status. Explicitly expire excess backlog
  by configured age/count and log input/layer IDs; do not silently drop work.
  Older completed jobs must not move a product's latest timestamp backward.

These independent render notifications intentionally may precede Core detection
readiness. This replaces the old cross-service detection-then-render ordering;
Core itself still runs detection before integration. Do not route render events
through the old ordered callback chain.

### Recovery and retention

The file rename, inventory write, and outbox commit are separate atomic units.
Implement reconciliation for every crash window: a valid file without inventory
is revalidated/adopted, inventory without an outbox is republished, and an outbox
without acknowledgment is retried. Revalidate existing local files on adoption;
mere existence is not proof of a valid download. Never mark a download failed
solely because notification publication failed and then redownload it repeatedly.

Pin inputs referenced by pending/active Core phases, previous detection history,
and unacknowledged render jobs. Synchronize selection/pin publication with
cleanup using a short shared lock so deletion cannot race a new reference.
Move MRMS and RAP cleanup out of per-batch acquisition into inventory-aware
maintenance; audit GLM cleanup too. The RAP three-analysis cap must yield to
active references. Cleanup stays contained within the resolved base directory.

Bound pending work by configured windows, job counts, and disk budget. When a
limit is reached, persist explicit expiry/backpressure status before releasing
references; never delete an active worker's input. Compact acknowledged work
only after its rediscovery window closes, retaining dedupe watermarks/tombstones
as needed. Garbage-collect owned stale temporary files after restart.

For corrected upstream content at an already committed product/time, do not
overwrite pinned bytes. Detect the identity conflict and report it explicitly;
supporting same-timestamp revisions in public render caches is a separate
contract change, not an implicit consequence of polling faster.

## 6. Configuration, launchers, and service visibility

Add schema-backed settings to existing catalogs, with no new catalog file:

| Setting group | Planned ownership/default policy |
| --- | --- |
| `scheduler.ingest_poll_seconds` | Default `10`; the sole realtime discovery cadence. |
| `scheduler` discovery window/depth | Reuse existing lookback/depth where possible; add bounded pagination and listing timeout controls if current controls are insufficient. |
| `runtime.ingest` | Download/list/decode concurrency, queue limits, retry bounds, incomplete-scan deadline, retention/disk budget, and reconciliation interval. Document units and select defaults against existing source timeouts and retention windows in step 1. |
| `runtime` consumer timing | Default Core readiness and EWMRS notification checks to at most one second; don't leave Core behind the current 15-second supervisor wait. |
| `ewmrs_pipeline` | Reuse existing render CPU/memory budgets; add per-layer pending/retry limits. |

Keep defaults, schemas, CLI/env overlays, configuration editor support, Python/JS
validation, and catalog baseline tests synchronized. Freeze effective config per
process and compare producer/consumer dependency fingerprints before consumption.
Mismatched dependencies fail visibly; they must not silently weaken readiness.
`handoff.enabled=false` must be rejected in the new realtime topology with an
actionable diagnostic because the consumers require durable readiness.

Planned package topology (preserve existing mode coverage while adding ingest):

| Command | Processes |
| --- | --- |
| `edgewarn run` | ingest + Core + EWMRS + NEXRAD |
| `edgewarn run core` | ingest + Core |
| `edgewarn run ewmrs` | ingest + Core + EWMRS |
| `edgewarn run ingest` (new) | ingest only |
| `edgewarn run nexrad` | NEXRAD only |

Add `ingest` to `--args`, direct `run_all.py --services`, installed Python
modules, Docker/Compose launch paths, and CLI preflight. Direct
`run_edgewarn.py` becomes a consumer requiring a separately launched ingestor;
document the changed command and waiting/degraded status. Direct EWMRS can run
with ingest without Core. Keep base/config roots consistent across participants.

Move acquisition-only argument ownership to ingest and explicitly propagate
shared options such as `mrms_core_only` and GLM enablement to both participants.
Provide preflight diagnostics for conflicting worker-scoped options. Review
`run_all.resolve_services`, whose current MRMS-only filter retains only Core,
so it does not accidentally remove the required new producer.

Add the canonical `ingest` service to Python and Node heartbeat registries and
API discovery contracts. Report poll liveness separately from successful input
arrival: a quiet upstream is not a dead process. Preserve route ownership by
Core/EWMRS/NEXRAD; an ingest outage should be visible without automatically
gating valid existing artifacts behind a new global dependency. Update OpenAPI,
API docs, and Jest fixtures wherever the service enumeration becomes public.

Use independent service locks, heartbeat threads, and bounded process-group
shutdown. An ingest heartbeat is diagnostic, never proof that an input is ready.
Keep existing launcher failure policy explicit: its fail-fast shutdown may stop
all sibling services on a service crash; use separate deployments when survival
of a Core crash is required. Normal Core work must never pause ingest, and the
optional primary/NEXRAD activity lease must not throttle the new ingestor.

## 7. Implementation sequence

Read applicable subtree `AGENTS.md` files before changing implementation/tests.
Each step is a reviewable commit with the stated completion criterion.

1. **Pin requirements and fixtures.** Add deterministic staggered arrivals for
   one scan and a following scan. Characterize check vs detection vs integration
   membership, ProbSevere normalization, timestamp tolerances, history, disabled
   sources, and source timeout/retention bounds. Commit concrete defaults for
   the new resource/deadline settings and schema parity fixtures.
2. **Extract reusable acquisition primitives.** Change MRMS finder/downloader
   modules to expose per-product object listings and per-object validated
   completion. Keep historical batch wrappers and S3/HTTPS behavior. Add local
   dedupe, bounded workers, and equivalent completion semantics for sync fallback.
   Criterion: one valid file becomes observable before a delayed sibling finishes.
3. **Implement inventory, outbox, and pure readiness.** Add the modules and
   namespace in sections 3–5, reconciliation, config fingerprints, pins, and
   explicit terminal states. Criterion: late same-scan arrivals are independent
   events; no partial/misaligned file satisfies a Core or render gate.
4. **Build the ingest service.** Add the timer, independent job pools, heartbeat,
   retries, raw RAP/scan-time GLM ownership, and retention maintenance. Criterion:
   fixed-clock polls continue at 0/10/20/30 seconds during long downloads and a
   blocked Core worker; missing products remain retryable without duplicate jobs.
5. **Convert Core to consume local readiness.** Remove remote checker/download
   calls from realtime `primary_service.py` and `cycle.py`; adapt the worker's
   barriers and immutable phase snapshots in `EdgeWARN/pipeline.py`. Preserve
   `CycleOutcome`, artifact validation, retry limits, tracking order, CTAM,
   StormProb, alerts, and indexes. Criterion: start on complete checks, wait
   locally for integration, and make zero source-network calls from Core.
6. **Convert EWMRS to per-input work.** Add the new consumer, selected-layer
   adapter, persistent bounded worker lifecycle, independent acknowledgments,
   retry handling, and artifact/index validation. Criterion: a non-check layer
   renders while a check modifier is missing; another same-scan arrival renders
   later without rerendering successful unchanged layers.
7. **Wire deployment and service contracts.** Update launchers, parsers,
   packaging, container paths, Python/Node service registries, and public service
   documentation/tests. Criterion: every supported topology has one ingest owner
   where needed, consistent configuration, clean shutdown, and no orphan workers.
8. **Cut over and document.** Ship the new producer and consumers together;
   complete the recovery/retention/latency acceptance suite below and update
   operator documentation. Remove realtime calls to the batch coordinator only
   after new consumers pass, retaining its historical interface.

## 8. Verification and acceptance

Use fake source listings, a controllable monotonic clock, bounded worker fixtures,
and temporary runtime directories. Default tests must not call NOAA or touch
operational data. These are implementation requirements, not tests run for this
planning-only change.

| Area | Required regression cases |
| --- | --- |
| Poll independence | Core takes 90 seconds; ticks continue every 10 seconds. One source times out; other products keep polling. No overlapping unbounded requests or post-work sleep drift. |
| Acquisition | New objects across consecutive ticks, multiple objects per product, pagination, hour/day rollover, delayed older objects, S3/HTTPS dedupe, corrupt/truncated inputs, source retry, ProbSevere JSON. |
| Core gate | Every individual missing check blocks start; remote presence without local commit blocks start; empty/misconfigured checks fail preflight; detection starts before slow non-check integration inputs, RAP, or GLM. |
| Core continuity | Sequential processing, previous-history pins, restart with pending retry, incomplete deadline, backlog expiry, late old scan, tracking gap reset, and truthful success/abandonment cursors. |
| Incremental rendering | First layer renders before final check arrival; two arrivals for the same scan create separate work; next-scan arrival during a render remains queued; unchanged input causes no new render; failures retry per layer while other layers advance. |
| Publication | Complete chunks/metadata/index required before acknowledgment; cached output reuse; output timestamp comes from source, not poll; out-of-order completion cannot regress latest; existing binary/API contracts hold. |
| Recovery | Crash before/after source rename, inventory commit, outbox commit, Core phase publication, output publication, and acknowledgment; restart reconstructs missed events without duplicate successful analysis. |
| Retention | Cleanup races with selection, long Core integration wait, RAP analysis shared across scans, render retry, previous detection history, disk/backlog pressure, and base-directory/symlink containment. |
| Lifecycle/config | Invalid config rejected before mutation; conflicting producer/consumer flags; duplicate ingestor lock; consumer starts before producer; SIGTERM during download/render; all package topologies and installed entry points. |
| Compatibility | Historical coordinator behavior; GOES ABI/accessory/NEXRAD ownership; existing v3 routes and removed-route 404s; Python/Node service/config parity. |

Extend existing suites in `tests/core/ingest/mrms`, `tests/core/schedule`,
`tests/core/test_input_manifest.py`, `tests/core/test_tandem_coordinator.py`,
`tests/integration/handoff`, `tests/integration/processes`, `tests/util`,
`tests/architecture`, and `tests/packaging`. Add focused ingest service/readiness
tests beside these suites, plus EWMRS renderer regressions and
`tests/api/test_service_registry.js` coverage.

After activating the `EdgeWARN` environment, run focused tests per step, then
the complete Python and Node suites for the coordinated runtime release. Run
both configuration validators after catalog/schema edits:

```bash
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

Deterministic end-to-end acceptance timeline (render durations are controlled
by the fixture, not promises about production hardware):

| Time | Input/processing event | Required observation |
| --- | --- | --- |
| 0s | Poll finds a renderable input for scan T, with some checks absent | Input downloads and a layer job becomes consumable; Core T has not started. |
| 10s | Poll finds more T layers, final check still absent | New jobs publish; completed layers are reused. |
| 20s | Final check for T commits; a non-check integration input remains absent | Exactly one Core detection attempt is scheduled when idle; render publication does not wait for integration. |
| 30s | Late T layer and files for T+2m arrive while Core is busy | Both download and publish render work; ingest has continued every tick. |
| 40s | Remaining T integration dependencies commit | Waiting Core receives the pinned integration manifest and proceeds. |
| Restart | EWMRS stops after publishing output but before acknowledging it | Restart validates/reuses that output and handles later same-scan arrivals. |

With idle consumers, require readiness pickup within the configured one-second
consumer interval plus bounded scheduling overhead. Record poll scheduling lag,
per-source listing/download latency, download-to-outbox latency, outbox-to-render
start, render completion, missing checks, incomplete-scan age, oldest queued job,
queue depth, retry counts, skipped jobs, and pinned disk usage. Correlate logs by
poll, product, source timestamp, input ID, and Core cycle without conflating them.

## 9. Deployment, migration, and rollback

Deploy as a coordinated internal-protocol change. Keep old v1 phase files and
checkpoints readable for an explicit drain step, but never overwrite or advance
them as if they were new per-input events. Do not run old Core acquisition and
the new ingestor simultaneously against the same runtime tree.

1. Stop old acquisition and finish/drain old Core/EWMRS work; record successful
   and abandoned Core cursors. Validate the new complete configuration tree.
2. Start the new ingestor and consumers. Reconcile valid existing source files
   into inventory; render jobs may reuse existing complete outputs. Seed Core
   from its prior truthful processing state so old scans are not reanalyzed.
3. Confirm advancing poll status, per-file outbox publication, independent render
   acknowledgments, service visibility, and the acceptance timing metrics.
4. For rollback, stop all new writers/workers first, preserve the new namespace
   for diagnosis, restore prior code/config, and resume the old pipeline from
   its verified successful/abandoned cursors and old checkpoints. New-only inputs
   can be rediscovered; never translate render acknowledgments into Core success.

Update `docs/core/ingestion.md`, `docs/core/configuration.md`, `README.md`,
`INSTALLATION.md`, service ownership guidance, CLI help, container examples, and
API service discovery documentation. Explain the 10-second cadence, separate
start/integration gates, incremental layer timestamps, standalone ingestor
requirement, wait/expiry diagnostics, and the coordinated rollback procedure.

Done when ingest continues acquiring throughout a long Core cycle, Core starts
on locally complete check modifiers, and EWMRS publishes each arriving layer
without waiting for aggregate Core readiness or losing arrivals after restart.

## 10. Concrete implementation checklist

Execute the phases below in order. Each checkbox describes an implementation
task; each phase ends with a behavior that must be demonstrated before marking
it complete. New filenames and callable names below are proposed interfaces.
Phases 1–4 establish the producer contract; phases 5–6 replace its consumers;
phase 7 connects the deployment; phase 8 qualifies the coordinated release.

**Current-tree baseline:** the configurable MRMS registry has now landed.
`config/ingest.yaml` uses schema version 2, and `MrmsRegistry.get_check_modifiers()`
currently returns the three protected discovery products. The earlier references
to ten `check_products` entries and an all-products integration barrier describe
the older catalog. Use the effective registry and current mandatory/optional
semantics when implementing these steps, as permitted in sections 1 and 5.
The required behavior remains: every effective check modifier must be locally
valid before Core detection, and every renderable committed input must produce
independent EWMRS work.

### Phase 1 — Freeze the effective dependencies and configuration

**Files:** `src/common/ingest/mrms/{registry,core_contract,config}.py`,
`src/common/pipeline/coordinator.py`, `config/{scheduler,runtime,ewmrs_pipeline}.yaml`,
their corresponding schemas, and `tests/config_baseline/`.

- [x] Build a dependency fixture from the effective registry: normalized check
  products, detection products, mandatory integration dependencies, optional
  enrichment inputs, previous-history requirements, and enabled RAP/GLM sources.
  Resolve ProbSevere to `ProbSevere` even though its source modifier is null.
- [x] Encode startup rejection for an empty check set, disabled dependencies,
  or detection products outside the check set. Use full canonical product IDs
  in readiness comparisons and fingerprints.
- [x] Record the current optional-completion behavior required by CTAM and
  StormProb. Specify which immutable final input snapshot the new producer must
  deliver; preserve optional acquisition deadlines and fatal StormProb checks.
- [x] Add `scheduler.ingest_poll_seconds: 10` and schema-backed Core/EWMRS
  consumer intervals defaulting to one second. Add the `runtime.ingest` listing,
  download, queue, retry, scan-deadline, reconciliation, and retention controls
  described in section 6. Give every control an explicit unit, default, and bound;
  derive resource defaults from existing MRMS/RAP limits rather than duplicating
  unexplained constants in the service.
- [x] Update configuration baseline fixtures and Python/Node catalog validation.
  Add deterministic source-arrival fixtures for scans T and T+2 minutes with a
  missing check, a delayed optional layer, a reused RAP analysis, and disabled GLM.

**Completion check:** both configuration validators pass; fixtures distinguish
check readiness from optional completion and reject each invalid dependency set.

### Phase 2 — Expose per-object acquisition and completion

**Files:** `src/common/ingest/mrms/{acquisition,source,s3_async,s3_sync,https_client,main}.py`,
`src/common/ingest/synoptic/`, and `src/util/runtime/goes.py`.

- [x] Define an immutable discovered-object descriptor containing canonical
  product ID, encoded observation time, source locator, and remote version when
  supplied. Add a bounded listing entry point returning every eligible object
  within the lookback window, including late objects and paginated results.
- [x] Extract a public per-object acquisition entry point from `acquisition.py`.
  Reuse its payload validation, quarantine, staging, and atomic publication;
  return a structured committed-input result only after the final file is usable.
- [x] Keep S3/HTTPS logical identity and timestamp selection consistent. Preserve
  ProbSevere JSON validation and synchronous source fallback, with the same
  completion result for both transports.
- [x] Retain `acquire_batch`, `acquire_batch_sync`, and historical wrappers by
  composing the extracted primitives. Make source cleanup ownership explicit so
  realtime calls can defer deletion to inventory maintenance while historical
  callers retain their existing isolated cleanup behavior.
- [x] Wrap existing RAP and scan-time GLM acquisition with the same committed-input
  interface. Report RAP's analysis time and GLM's validated scan alignment;
  deduplicate local RAP reuse without emitting another render event.
- [x] Extend `tests/core/ingest/mrms/test_acquisition.py` and source fallback
  coverage with a delayed sibling, duplicate S3/HTTPS observations, multiple new
  objects in one listing, and a corrupt payload.

**Completion check:** the first valid object yields a completion result while a
sibling remains blocked; a corrupt or partial file never yields that result.

**Phase 1–2 verification (2026-09-30):** the focused MRMS, RAP, GLM,
coordinator, configuration, and complete architecture selection passed 638
Python tests in the `EdgeWARN` environment. The final RAP containment check
and its full module suite passed 17 tests. Shared configuration-loader and
MRMS/catalog parity suites passed 65 Node tests. Both configuration validators
passed all 18 catalogs, and `git diff --check` passed. Async executor tests ran
outside the sandbox because sandboxed executor shutdown hangs independently of
these changes; test runtime roots remained temporary.

Implementation adds `common.ingest.mrms.discovery` and
`common.ingest.objects` alongside the listed files. Stable committed IDs and
`reused` evidence provide the deduplication seam; durable inventory/outbox
publication and render-event delivery remain phase 3 work. The existing realtime
service topology and staged callbacks remain in use until the later cutover.

### Phase 3 — Implement inventory, durable notifications, and readiness

**New files:** `src/common/ingest/inventory.py`,
`src/common/pipeline/readiness.py`, and `src/util/runtime/ingest_handoff.py`.
**Reuse:** `src/common/ingest/{manifest,replay}.py`, `src/util/atomic.py`, and
the locking/strict-validation patterns in `src/util/runtime/handoff.py`.

- [ ] Define versioned record models and strict readers/writers for the namespace
  in section 5. Include immutable input IDs, fingerprints, validation evidence,
  contained paths, source times, and producer identity. Reject incompatible
  existing records instead of overwriting their selections.
- [ ] Implement `commit_input(...)` and `publish_render_ready(...)` as separate
  recoverable operations. Key render records by input identity so two products
  or two arrival times for one scan cannot collapse into one cycle checkpoint.
- [ ] Implement pure `evaluate_scan(...)` over the inventory. Compute start
  readiness as all valid current inputs in the effective check set; resolve and
  pin previous detection history separately. Compute integration readiness from
  the dependency fixture in phase 1.
- [ ] Publish start and integration manifests once, retaining identical detection
  selections. Persist the bounded optional-completion snapshot required by the
  current CTAM/StormProb flow without mutating an earlier phase record.
- [ ] Add per-scan terminal records, persisted Core consumption state, and
  per-input/per-layer EWMRS acknowledgment records. Include retry eligibility,
  explicit expiry reasons, and the render configuration fingerprint.
- [ ] Implement `reconcile(...)` for valid files without inventory, inventory
  without notifications, and notifications without acknowledgments. Acquire
  the shared retention lock when selecting/pinning inputs and deleting eligible
  unreferenced files; integrate with existing replay protection.
- [ ] Add focused tests under `tests/integration/handoff/` for every crash window,
  timestamp mismatch, fingerprint mismatch, path escape, late same-scan arrival,
  and cleanup racing a new pin.

**Completion check:** restart recovers missing notifications; every missing
check blocks Core; two late same-scan inputs remain independently consumable.

### Phase 4 — Build the independently supervised ingest service

**New files:** `src/run_ingest.py` and `src/util/runtime/ingest_service.py`.
**Related files:** `src/util/runtime/{services,processes,mrms_registry}.py`.

- [ ] Add a side-effect-free entry point with full configuration preflight,
  resolved runtime root, ingest service lock, signal handlers, logging, and
  independent heartbeat refresh. Reconcile durable state before the first poll.
- [ ] Implement a monotonic deadline scheduler that dispatches discovery at
  0/10/20/30 seconds. Keep listing and download work outside the timer path;
  track one active listing per product and coalesce missed refreshes.
- [ ] Maintain bounded pending/in-flight/completed identities and separate
  capacity for MRMS and RAP/GLM. Prioritize missing check inputs while reserving
  capacity for other enabled products. Persist failure/backoff state without
  advancing successful acquisition cursors.
- [ ] Route every acquisition completion directly through inventory commit,
  render notification publication, and affected-scan readiness evaluation.
  Handle completion after its originating poll ends through the same path.
- [ ] Add incomplete-scan expiry, reconciliation, and retention maintenance.
  Make the RAP retention cap yield to active references; report disk/backlog
  pressure before expiring work and releasing pins.
- [ ] Transfer registry descriptor publication to the ingest service and bind
  its producer agreement to the ingest run ID/heartbeat. Define the new
  descriptor contract explicitly so old Core descriptors cannot satisfy it.
- [ ] Add fixed-clock service tests and shutdown tests under `tests/util/` and
  `tests/integration/processes/`. Stop and join owned download/decode workers
  within the configured termination bound.

**Completion check:** a blocked Core and a slow source do not stop scheduled
polls or unrelated input publication; duplicate discoveries create no extra jobs.

### Phase 5 — Convert Core into a local readiness consumer

**Files:** `src/run_edgewarn.py`, `src/util/runtime/{primary_service,cycle}.py`,
and `src/EdgeWARN/pipeline.py`.

- [ ] Replace realtime `MRMSUpdateChecker` selection with a local readiness
  reader. Select pending scans in timestamp order within the backlog/deadline
  policy and preserve `CycleStateStore` success, retry, and abandonment cursors.
- [ ] Validate the complete start record and pinned inputs before spawning the
  scan worker. Start exactly one worker when the effective check set is ready
  and Core is idle; persist late-scan skips instead of rewinding tracking.
- [ ] Remove realtime MRMS, RAP, and GLM download calls from
  `run_primary_cycle_once`. Pass the pinned detection manifest into the worker;
  preserve historical callers of the staged coordinator.
- [ ] Add a local watcher for integration readiness and the final optional-input
  snapshot. Install each validated immutable snapshot before releasing its
  corresponding worker barrier. Wake waits on terminal expiry and shutdown;
  waiting for data must not spend an analysis retry.
- [ ] Preserve downstream CTAM readiness, StormProb input validation, tracking
  gap resets, alerts, artifact checks, and truthful `CycleOutcome` handling.
  Stop publishing realtime EWMRS cycle triggers or the old producer descriptor.
- [ ] Extend Core/handoff tests with every check missing in turn, remote-only
  availability, delayed integration, worker restart, and an older late scan.
  Stub source clients to fail if realtime Core attempts acquisition.

**Completion check:** Core makes zero source-acquisition calls, starts detection
only with all local checks valid, and waits locally for later prerequisites.

### Phase 6 — Convert EWMRS to independent jobs for each input and layer

**Files:** `src/util/runtime/{ewmrs_consumer,ewmrs_service,mrms_registry}.py`,
`src/EWMRS/pipeline.py`, and `src/EWMRS/rap/uint16_pipeline.py`.

- [ ] Replace realtime MRMS/RAP cycle draining with the new input notification
  reader and acknowledgment store. Validate agreement with the ingestor so a
  stopped Core does not pause EWMRS rendering.
- [ ] Resolve each committed product to its enabled layer definitions. Persist
  jobs keyed by input ID, layer ID, and render fingerprint; acknowledge inputs
  with no mapping explicitly. Preserve the notified source path and timestamp.
- [ ] Extract the selected-layer submission path around `_render_layer` into a
  persistent bounded executor owned by the consumer. Continue accepting jobs
  while renders run, within queue limits, and preserve worker recovery/shutdown.
- [ ] Add selection of individual RAP layers to the Uint16 conversion path so
  successful RAP layers can be acknowledged separately and only failed layers
  retried. Reuse the pinned raw analysis across those jobs.
- [ ] Treat exceptions, `None`, missing chunks, missing metadata, and incomplete
  indexes as failed jobs. Serialize shared product-index updates where needed,
  prevent older completion from moving latest backward, and acknowledge only
  after output publication is complete.
- [ ] Persist per-layer retry/backoff/expiry; reuse validated complete outputs
  after restart. Release an input's render reference only once all mapped jobs
  have a durable successful or explicit terminal disposition.
- [ ] Extend `tests/integration/handoff/test_ewmrs_consumer.py` and rendering
  tests for non-check arrival before Core readiness, two arrivals for scan T,
  a T+2-minute input during a render, partial RAP success, and acknowledgment loss.

**Completion check:** each newly committed renderable input creates work without
waiting for Core or poll completion; unrelated layers advance during a failure.

### Phase 7 — Wire commands, containers, and service discovery

**Files:** `src/run_all.py`, `src/edgewarn_cli/{main,run}.py`, `src/util/cli.py`,
`src/util/runtime/services.py`, `pyproject.toml`, `Dockerfile`, `compose.yaml`,
`docker/edgewarn-entrypoint.sh`, `src/api/services/serviceRegistry.js`, and
`src/api/openapi/v3.yaml`.

- [ ] Add the `ingest` worker and `edgewarn run ingest` mode. Implement each
  topology in section 6 and forward repeatable `--args ingest` arrays without
  shell parsing. Ensure MRMS-only filtering retains its required producer.
- [ ] Assign acquisition flags to ingest; propagate shared dependency options
  to Core and ingest. Reject conflicting roots, incompatible fingerprints,
  duplicate producer topology, and disabled durable handoff before startup.
- [ ] Install `run_ingest` with the Python package and update container entry
  points and Compose configuration. Preserve complete config-tree deployment
  and preflight before runtime filesystem initialization.
- [ ] Add ingest to Python/Node heartbeat discovery and its public schema/docs.
  Preserve route ownership and existing artifact serving when ingest is down.
- [ ] Update CLI ownership, launcher, installed-command, container, and API
  service-registry tests. Include direct ingest + EWMRS with Core stopped and
  signal forwarding that leaves no child process behind.

**Completion check:** every documented command launches the intended service
set with one acquisition owner, and both runtimes agree on service discovery.

### Phase 8 — Qualify recovery, cut over, and finish operator documentation

**Files:** focused suites named above, `tests/integration/handoff/`,
`tests/integration/processes/`, `docs/core/{ingestion,configuration}.md`,
`docs/api/unified_v3.md`, `README.md`, and `INSTALLATION.md`.

- [ ] Implement the section 8 acceptance timeline as one deterministic test
  using fake sources, a controllable clock, temporary runtime roots, and bounded
  render workers. Assert both Core start timing and every expected layer job.
- [ ] Exercise restart at each publication boundary, input retention during
  long waits, incomplete-scan expiry, stale producer identity, rendering while
  Core is stopped, and shutdown during active acquisition/rendering.
- [ ] Verify existing float16, RAP Uint16, indexes, timestamps, API contracts,
  historical processing, GOES ABI, accessories, and NEXRAD remain compatible.
  Record poll lag, readiness pickup, queue depth, and publication latency.
- [ ] Run the focused tests as each phase lands. For the coordinated release,
  activate `EdgeWARN`, run the complete Python and Node suites, and run both
  configuration validators listed in section 8. Record commands and outcomes.
- [ ] Document executable stop/drain/start/rollback procedures from section 9,
  including prior Core cursors, old checkpoint retention, new-state adoption,
  and the direct-service requirement to launch ingest separately.
- [ ] Remove obsolete realtime producer wiring after the new path passes.
  Retain historical batch entry points and the explicit old-record drain path.
  Update this document's status and checklist only for completed, verified work.

**Completion check:** the acceptance timeline, recovery cases, and compatibility
checks pass, and an operator can perform the coordinated deployment and rollback
using the documented commands.
