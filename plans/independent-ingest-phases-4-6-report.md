# Independent ingest and incremental rendering: phases 4–6 implementation report

> Follow-up: launcher wiring now includes ingest in package/default supervised
> topologies, retains it in MRMS-only mode, and checks producer ownership,
> shared roots, GLM options, and durable handoff before startup. EWMRS now uses
> its resolved GLM option for the dependency fingerprint. The health schema and
> operator docs are updated. This report below records the original phases 4–6
> state; phase 8 release qualification and external report-reader audits remain.
> `/health/ready` reports four services; `/health/live` has no service block.

Date: 2026-09-30
Branch: `yuchen-wei3667/decoupled-ingest`
Plan: [`plans/independent-ingest-incremental-rendering-plan.md`](plans/independent-ingest-incremental-rendering-plan.md)

## 1. What was asked and what landed

Phases 4, 5, and 6 of the independent-ingest plan were implemented, verified
against each phase's acceptance gate, and committed separately. Each phase was
finished and accepted before the next one started.

| Phase | Commit | Subject |
| --- | --- | --- |
| 4 | `43b67bca` | `FTR: add the independently supervised ingest service` |
| 5 | `46f49e2f` | `FTR: make realtime Core a local readiness consumer` |
| 6 | `df5a03f2` | `FTR: render each committed input independently in EWMRS` |
| — | `737d20b6` | `DOC: record verified ingest producer and consumer work` (plan status) |

**Nothing is deployed yet.** The three runtime surfaces now exist and are
tested, but the released command set still launches the old acquisition path.
`edgewarn run ingest`, the container/Compose paths, the OpenAPI service
enumeration, and the coordinated cutover are phases 7 and 8. Running the
existing `edgewarn run` today starts Core and EWMRS with **no producer**, and
Core will wait indefinitely for readiness records. This is the single most
important operational note in this report.

## 2. Phase 4 — the independently supervised ingest service

### 2.1 New files

**`src/util/runtime/ingest_service.py`** — the service itself. No EWMRS or
Core analysis module is imported anywhere in it, so a Core crash or a long Core
cycle cannot pause acquisition.

- `IngestResources` — every runtime bound, resolved once from `scheduler.yaml`,
  `runtime.ingest`, and `synoptic_rap.yaml`. There are no service-side
  operational constants; `runtime.ingest`'s documented inheritance from the
  MRMS/RAP catalogs is preserved. `reserved_slots` clamps so a reserved share
  can never consume the whole download window.
- `AcquisitionLedger` — bounded dedupe over queued / in-flight / settled
  identities. A repeated discovery is not an error, a saturated window raises
  `AcquisitionBacklogFull` so backpressure is explicit, and a *failed* identity
  is remembered separately so a successful cursor never advances past it.
- `AcquisitionJob` — one unit of source work keyed by a mirror-independent
  logical identity.
- `IngestService` — scheduling, dispatch, completion routing, maintenance.

### 2.2 Scheduling behaviour

- Dispatch runs on a monotonic deadline grid `t0 + n * poll`. Missed deadlines
  coalesce into exactly one refresh and increment a `coalesced_ticks` counter
  instead of replaying a burst.
- Listing and download work never runs on the timer path. The timer only
  submits to three owned executors (`ingest-listing`, `ingest-download`,
  `ingest-auxiliary`).
- One active listing per product, tracked in `_listing_active`, so a slow
  request cannot overlap itself; the product is retried on the next due
  refresh.
- Admission priority: check inputs are admitted first, but a reserved share of
  the window is always left for every other enabled product.
- A stalled network does not stop the cadence. The plan's requirement that
  "the 10-second requirement is the scheduling cadence, not a promise that a
  stalled network finishes in 10s" is implemented literally: the stalled job
  holds its worker, the cadence keeps firing, and other products keep
  publishing.

### 2.3 Durability

Every acquisition completion is put on one publisher queue, and a single
publisher thread performs *all* durable state transitions: `commit_input`,
`publish_render_ready`, and `publish_scan`. Keeping every durable mutation on one
thread means the shared input lease is never re-entered, so publication cannot
self-deadlock, and a completion is never delayed behind an unrelated tick. A
completion that lands after its originating poll has ended publishes
immediately through the same path.

Two bugs found and fixed during this work, both of which would have corrupted
backlog accounting in production:

1. Successful acquisitions were never marked settled, so `in_flight_jobs` grew
   without bound and the bounded window would eventually reject every new
   object.
2. Retry eligibility was scheduled on `time.monotonic()` while dispatch compared
   against the injected service clock, so retries could never become due.

### 2.4 Maintenance

Reconciliation of every crash window, inventory-aware retention, the RAP
analysis cap (yielding to live references via the new
`InputInventory.referenced_inputs()`), owned staging-directory collection, disk
pressure reported *before* anything is released, and incomplete-scan expiry
that publishes a terminal record listing the missing inputs.

### 2.5 Producer identity

`src/util/runtime/mrms_registry.py` now defines two descriptor contracts:

| Contract | File | Schema | Producer |
| --- | --- | --- | --- |
| legacy | `edgewarn-mrms-registry.json` | 1 | `edgewarn` (Core) |
| new | `ingest-mrms-registry.json` | 2 | `ingest` |

A separate destination file plus a separate schema version means a legacy Core
descriptor can never satisfy an ingest agreement check, and a Core restart
cannot be mistaken for a live ingestor. `require_ingest_agreement` checks the
descriptor *and* the live `ingest` heartbeat, and the descriptor carries the
resolved `IngestDependencies` fingerprint so an effective dependency-set change
fails visibly.

### 2.6 Service visibility

`ingest` joins `CANONICAL_SERVICE_NAMES` in both `src/util/runtime/services.py`
and `src/api/services/serviceRegistry.js` (they are documented mirrors). It
deliberately has **no** `ROUTE_SERVICE_REQUIREMENTS` entry, so an ingest outage
is visible in `/health/*` without gating already-published artifacts. The
`ServiceLock` therefore rejects a second ingestor automatically.

### 2.7 Entry point

**`src/run_ingest.py`** — full configuration preflight, resolved runtime root,
single-instance `ingest` service lock, signal handlers, queue-backed logging,
independent heartbeat thread, and clean shutdown. `handoff.enabled=false` is
rejected with an actionable diagnostic *before* any runtime directory is
created.

### 2.8 Phase 4 acceptance

> a blocked Core and a slow source do not stop scheduled polls or unrelated
> input publication; duplicate discoveries create no extra jobs

Verified by `tests/integration/processes/test_ingest_service_runtime.py`
(fixed-clock cadence, one-active-listing, coalescing, dedup, independent
notifications, per-check failure, scan expiry, shutdown, entry-point preflight)
and `tests/util/test_ingest_service.py` (resource resolution, ledger, descriptor
contract).

## 3. Phase 5 — Core as a local readiness consumer

### 3.1 What was removed

- `MRMSUpdateChecker` and the S3/HTTPS timestamp intersection from the realtime
  path (`run_edgewarn.py` no longer imports it).
- `run_staged_ingest_cycle`, `download_glm_for_scan`, the RAP download, and the
  whole `ingest_and_glm` coroutine from `run_primary_cycle_once`.
- The `PhaseRecordPublisher` `mrms-ready` / `rap-ready` publications and the
  `state/realtime/ingest-reports` snapshots from the realtime cycle.
- The Core-owned `publish_registry` producer descriptor.

Historical processing keeps its own explicit staged ingest entry point
(`EdgeWARN.pipeline.historical_pipeline` still calls `run_staged_ingest_cycle`)
and is unchanged.

### 3.2 What was added

- `LocalReadinessReader` in `primary_service.py` — selects candidate scans from
  durable `core-start-ready` records, oldest first, and persists explicit skips.
- `ReadinessWatcher` in `cycle.py` — polls the local durable namespace and
  installs each validated immutable snapshot into `shared_state` *before*
  releasing its barrier.
- `read_ready_phase` / `readiness_handoff` — dependency preflight, exact-selection
  matching, byte verification, and pinning in one place.
- `IngestHandoff.pin_phase` — verifies and pins one phase's exact selections
  under a single lease acquisition. Nesting the existing `pin()` would have
  tried to take the same advisory lock twice.

### 3.3 Retry accounting

A scan is scheduled only when a start record exists, so waiting for a
prerequisite costs no analysis retry: `record_attempt` is reached only once a
cycle actually starts. Selection returns the oldest pending scan; a start record
at or before the processing cursor is persisted as `status="skipped"` and never
rewinds tracking; backlog overflow is skipped explicitly with the cap in the
reason.

### 3.4 Lease change (important)

`run_primary_cycle_once` is **no longer** wrapped in `@protect_runtime_inputs`.
The coarse lease is not re-entrant (`flock` on a second descriptor in the same
process fails), so a watcher thread inside a lease-holding cycle could never
take it. The new design takes the lease briefly for each selection/pin and lets
retention honour those pins, which is exactly what the plan prescribes: a long
integration wait blocks neither maintenance nor another producer while the exact
bytes the cycle needs stay protected. This is a deliberate semantic change from
the acquisition-era cycle, and it is the reason `protect_runtime_inputs` remains
imported-but-unused in `cycle.py` (it is still used by the historical path).

### 3.5 Worker barrier contract

`edgewarn_cycle_worker` now requires the exact pinned manifest for each phase
and validates its alignment, instead of falling back to a rolling
`input_manifest`. A released barrier without its snapshot is a truthful
`failed` stage, not a silent fallback. The final optional-input snapshot is
validated too, so a malformed CTAM snapshot raises rather than being consumed.

### 3.6 Phase 5 acceptance

> Core makes zero source-acquisition calls, starts detection only with all local
> checks valid, and waits locally for later prerequisites

- An autouse fixture in `tests/integration/handoff/test_core_readiness_consumer.py`
  replaces every acquisition entry point with a raiser.
- An AST check pins the module graph of `util/runtime/cycle.py` and
  `util/runtime/primary_service.py` against a forbidden-import set, so a future
  import added outside the covered call cannot slip through.
- Eighteen tests cover each individual missing check, remote presence without a
  local commit, disabled handoff, delayed integration, terminal wake, worker
  restart, oldest-first selection, skip persistence, dependency disagreement,
  pin lifetime, and the absence of legacy render triggers.

### 3.7 Test-suite restructuring

`tests/integration/handoff/test_durable_handoff_wiring.py` was **deleted**. It
characterized the v1 realtime producer wiring that this phase retires. The v1
`handoff.py` primitives it also covered remain tested by
`tests/integration/handoff/test_runtime_handoff.py`, so the retained drain path
is still characterized. This is the only deleted test file in these three
phases; it is a phase-5 consequence, not incidental cleanup.

## 4. Phase 6 — EWMRS per-input, per-layer work

### 4.1 Consumer

`InputRenderConsumer` in `src/util/runtime/ewmrs_consumer.py` replaces
`EwmrsRecordConsumer` entirely.

- Reads the immutable `render-ready` outbox. No cycle checkpoint, no aggregate
  MRMS readiness predicate, and no Core record is ever consulted.
- Resolves each committed product to its enabled layer definitions
  (`get_mrms_file_list()` by product identity; all configured RAP uint16 layers
  for a RAP analysis; nothing for a GLM input, whose rendering belongs to the
  GOES ABI loop).
- Persists `render-plan` keyed by `(input-id, render-config-fingerprint)`, then
  per-layer `render-ack` keyed by `(input-id, layer-id, fingerprint)`. An input
  with no mapping is acknowledged `no-mapping` with a reason.
- `render_configuration_fingerprint()` covers the MRMS layer set, the RAP layer
  set with scales and short names, and the chunk format. A configuration change
  reopens work instead of reusing stale acknowledgments.

### 4.2 Non-blocking admission

`poll_once` collects finished renders first, then plans and admits new work, and
returns immediately. Renders run on a persistent bounded `RenderLayerPool`
(process pool) driven by a small orchestration thread pool, so a long render
never delays the next notification. `test_a_later_scan_arriving_during_a_render_is_not_blocked`
asserts exactly that: the T+2 input is admitted while the T render is still
gated.

### 4.3 Output validation

A layer is acknowledged only after its artifacts are published *and* validated:

- MRMS/GOES: the expected chunk set, the per-timestamp chunk index, and the
  product index must all be readable (`_current_render_paths`).
- RAP: `data.u16`, `metadata.json`, and an index that lists the timestamp.

An exception, a `None` render, a missing chunk, missing RAP metadata, or an
incomplete index are all failed jobs.

### 4.4 RAP partial success

`run_rap_uint16_pipeline` accepts an explicit `layers=[...]` subset and a
`cleanup=False` flag, so a consumer converts and acknowledges one layer while
the others are still retrying against the same pinned analysis. The same analysis
reused across scans produces the same `input_id`, so it emits one render event.

### 4.5 Index serialization

`_update_product_index` now takes a cross-process advisory lock
(`EWMRS/rap/.index-locks/<layer>.lock`) around its read-modify-write, because
per-layer jobs for the same analysis can finish in any order in separate
workers. Timestamps are stored descending and deduplicated, so a late older
completion cannot move a product's latest entry backward.

`rap_uint16.max_timestamps` remains the single retention window shared by the
published index and `cleanup_old_rap_uint16_layers`; an earlier attempt to
decouple them was reverted because
`tests/architecture/test_known_drift.py::test_rap_uint16_retention_is_observed_at_both_of_its_call_sites`
deliberately pins that they are one knob. `cleanup_old_rap_uint16_layers` gained
a `keep=` argument so retention never deletes a timestamp directory an active
job is still writing.

### 4.6 Retry, expiry, and references

Per-layer retry with bounded exponential backoff, then an explicit `expired`
terminal status with a reason. A pending job older than
`input_jobs.max_age_minutes` is expired with a reason. A saturated job window
defers new work with an explicit message. The input's render reference is
released only when every mapped layer has a durable terminal disposition, and
`acknowledge_input` re-validates that invariant under the shared lease.

### 4.7 Phase 6 acceptance

> each newly committed renderable input creates work without waiting for Core or
> poll completion; unrelated layers advance during a failure

Nineteen consumer tests plus 13 layer-pool tests plus 6 RAP conversion
regressions. A non-check arrival renders with zero Core phase records present
(`test_a_non_check_arrival_renders_before_core_readiness`), and
`test_unrelated_layers_advance_while_one_fails` shows a failing layer in `retry`
while every sibling is `success` and the input stays unacknowledged.

## 5. Verification record

Every command below was run in the `EdgeWARN` conda environment
(`~/miniconda3/envs/EdgeWARN/bin`).

| Command | Result |
| --- | --- |
| `python -m pytest tests/util/test_ingest_service.py -q` | 21 passed |
| `python -m pytest tests/integration/processes/test_ingest_service_runtime.py -q` | 14 passed |
| `python -m pytest tests/integration/handoff -q` | 110 passed |
| `python -m pytest tests/unit/rendering -q` | 216 passed (with `tests/architecture/test_known_drift.py`) |
| `python -m pytest -q` (full suite) | 2587 passed, 7 skipped, 3 failed |
| `npm test` | 12 suites, 140 tests passed |
| `npm run validate-config` | All 18 config files passed |
| `PYTHONPATH=src python -m common.config.validate` | All 18 config files passed |
| `git diff --check` | clean |

### 5.1 Pre-existing failures (not caused by these phases)

All three reproduce from an isolated archive of unchanged `HEAD`:

1. `tests/core/test_ingest_replay.py::test_previous_selection_uses_identity_and_encoded_time`
2. `tests/core/test_ingest_replay.py::test_previous_optional_remains_available_when_current_download_is_absent`

   The phase-3 verification note in the plan already records these two as
   existing fixture failures: the history-selection fixture supplies undecodable
   placeholder bytes, and the optional-history fixture supplies a registry
   without the required `discovery` fields.

3. `tests/integration/test_mrms_release_qualification.py::test_mrms_release_transport_qualification`

   Network-dependent release qualification. It shells out to
   `scripts/qualify_mrms_release.py` and asserts 18 outcomes; it fails in this
   sandbox because the child process cannot reach the upstream transport.

None of the three touches the ingest service, Core readiness, or EWMRS
rendering.

## 6. Test-support changes worth knowing about

- `tests/conftest.py`: `test_ingest_service_runtime.py` was added to
  `_PROCESS_TEST_FILES` so it carries the `process` and `slow` markers.
- `tests/architecture/test_boundary_audit.py`: `run_ingest.py`'s
  `HEARTBEAT_MIN_INTERVAL_SECONDS` was added to the explicit
  `OPERATIONAL_LITERAL_EXCEPTIONS` allowlist with the same rationale already
  recorded for `run_edgewarn.py`. This is a deliberate, enumerated exception, not
  a new source of hidden defaults.
- `tests/util/test_runtime_services.py`: the canonical-name assertion became
  four services and gained an explicit assertion that `ingest` owns no route
  family.
- `tests/api/test_service_registry.js`: the `/health/ready` services block now
  expects `ingest`.
- `tests/core/test_pipeline.py` and `tests/core/test_stormprob_fatal_supervision.py`:
  updated for the new consumer state shape and the local readiness reader.

## 7. Outstanding notes for the user

### 7.1 Blocking: the released topology is now inconsistent

This is the one item that needs a decision or a follow-up before anyone runs
the current build in production.

`edgewarn run`, `edgewarn run core`, and `run_edgewarn.py` all start Core with
no producer. Because phase 5 removed realtime acquisition from Core, Core will
log `No locally complete check set yet; waiting for the ingest service to
publish scan readiness` (or a producer-agreement diagnostic) forever. EWMRS will
log that its ingest descriptor is missing. The service is *correct* and *idle*,
not crashed, which is the designed degraded posture, but it is a silent
functional stop from an operator's point of view.

Phase 7 resolves this (`edgewarn run ingest`, container wiring, preflight that
rejects a topology without a producer). Until then, do not deploy this branch
as a runtime. It is safe as a library/test branch.

### 7.2 GLM render ownership

The plan says EWMRS "retains its existing GOES ABI, METAR, NWS, and WPC
loops" and that scan-time GLM is a Core integration input. The new consumer
therefore maps a `family="goes"` (GLM) input to **no** layer and acknowledges it
`no-mapping`; the existing `goes_render_loop` continues to render GOES ABI from
locally staged files as before. If the intent was for the GLM input to drive an
ABI render, that mapping does not exist yet and needs an explicit decision
(which ABI layer, if any, corresponds to a scan-time GLM arrival).

### 7.3 RAP jobs reuse one pinned analysis

A RAP input maps to *all* configured RAP uint16 layers (about 46 in the current
catalog). That is one per-layer job per RAP arrival, each pinned to the same raw
analysis. This is correct but it is the highest-volume producer in the queue, and
`input_jobs.pending_max_jobs` is the only bound on it. If RAP becomes the
bottleneck, the natural next step is to gate RAP mapping on the layers that are
actually enabled for the current API surface rather than every catalogued layer.

### 7.4 `decode_concurrency` semantics

`runtime.ingest.decode_concurrency` bounds concurrent blocking decode work for
the auxiliary (RAP GRIB / GLM NetCDF) paths through a semaphore. MRMS blocking
decode runs on the download pool, whose size is `download_concurrency`, and is
additionally bounded by the existing MRMS `WorkBudget` from
`ingest.mrms.downloads.max_concurrency`. The phase-2 acquisition primitive does
not expose a decode-stage callback, so a decode-only MRMS pool would have been
decorative; the current mapping enforces a real bound. This is documented in the
`IngestResources` docstring.

### 7.5 `state/realtime/ingest-reports` no longer written

The realtime cycle no longer writes per-phase ingest report snapshots
(`state/realtime/ingest-reports/<cycle>-<phase>.json`). Nothing in the current
tree reads them, and the durable `core-*` phase records plus `render-ready`
notifications replaced them. Historical processing still writes its own
snapshots. If any operator tooling or dashboard outside this repository reads
that directory, it needs updating.

### 7.6 Legacy v1 handoff retained but unwired

`src/util/runtime/handoff.py` (`PhaseRecordPublisher`,
`ConsumerCheckpointStore`, v1 phase records under `state/realtime/cycles/`) and
the legacy `edgewarn-mrms-registry.json` descriptor are still present and still
readable for the explicit drain step described in the plan's migration section.
Nothing in the realtime path writes them any more. Phase 8 should decide whether
they stay indefinitely or get a removal window.

### 7.7 Node API service enumeration

`ingest` is now in `CANONICAL_SERVICE_NAMES` on both sides, so `/health/ready`
and `/health/live` report it. `src/api/openapi/v3.yaml` and
`docs/api/unified_v3.md` were **not** updated — that is phase 7's "public
schema/docs" work. If the OpenAPI document currently enumerates the service
list, it is momentarily out of step with the runtime. Route ownership and gating
are unchanged (`ingest` owns no route family), so no existing route behavior
changed.

### 7.8 Metrics and correlation

`state/realtime/ingest/v1/poll-status.json` now carries
`polls`, `discovered_objects`, `committed_inputs`, `render_notifications`,
`duplicate_discoveries`, `listing_overruns`, `coalesced_ticks`, `expired_scans`,
`retired_inputs`, `tracked_scans`, plus queue/in-flight/failure counts and a
bounded reason list. Core logs `Local check set complete for <scan>` when it
schedules detection, and EWMRS logs the input/layer IDs it renders. That covers
the plan's "correlate logs by poll, product, source timestamp, input ID, and
Core cycle" requirement, but the plan's full metric set (readiness pickup
latency, outbox-to-render start, render completion) is not yet instrumented and
belongs to phase 8.

### 7.9 Sandboxing note

The `multiprocessing.Manager()` used by the Core cycle and by the new
cross-process index-serialization test needs local manager sockets. They work in
this environment. The phase-3 note records that they were blocked by a sandbox
elsewhere, so CI should confirm they run outside one.

## Follow-up verification — launcher and agreement fixes (2026-09-30)

- Package dispatch tests: 37 passed, including all five modes and ingest-scoped
  GLM option propagation.
- Launcher tests: 29 passed, including MRMS-only topology, preflight rejection,
  producer-only shutdown, and descendant cleanup.
- EWMRS registration/dependency tests: 6 passed; input consumer tests: 19 passed.
- Architecture suite: 243 passed outside the sandbox. The sandboxed broad run
  stalled in the architecture suite and was interrupted.
- Packaging suite passed its active tests; the opt-in container smoke test was
  skipped. The updated installed-wheel suite passed all 6 tests, including
  `run_ingest` module availability and `edgewarn run ingest --help`.
- API service registry/OpenAPI tests: 14 passed outside the sandbox, which
  otherwise prevents Supertest from binding local sockets.

GLM remains an explicit no-mapping input. RAP retains the configured layer
catalog and bounded job admission. No external operator tooling was available
for auditing retired realtime report readers. No runtime deployment or phase 8
release qualification was performed.
