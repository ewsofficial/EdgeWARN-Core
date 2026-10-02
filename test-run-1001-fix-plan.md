# test-run-1001 fix plan

Companion to `test-run-1001-analysis.md`. Every issue below has a root cause
confirmed in code. One new issue (1b) was found while tracing. No code has been
changed yet.

## Root causes

| # | Issue | Cause | Where |
|---|-------|-------|-------|
| 1 | Integration starts with no StormProb inputs | No `CORE_PRODUCTS` entry has `core_phase="integration"`, so `mandatory_integration` is always `()`. The integration gate waits only for RAP and GLM; the 13 StormProb MRMS sources are classed *optional*. That is why `integration_released status=ready` fired for 23:12 with only AzShear present. | `src/common/ingest/mrms/core_contract.py:31`, `:131`; `src/EdgeWARN/ctam/preflight.py:44` |
| 1a | Ingest backfills oldest-first | `AcquisitionLedger.due()` sorts by ascending `target`; only the three check products are prioritized. | `src/util/runtime/ingest_service.py:218` |
| 1b | **New:** downloaded files are deleted immediately | Listing covers `s3_lookback_hours: 2`, but retention is `cleanup_max_age_minutes: 60`. Anything older than 60 min is retired as soon as it is rendered, which explains the missing 23:xx files and `retired 20 unreferenced input(s)`. | `config/scheduler.yaml:10` vs `config/ingest.yaml:7` |
| 2 | `BlockingIOError` across services | `input_lock` uses a non-blocking `flock` with no retry, and it is held almost continuously: `publish_scan` runs once per committed file and, under the lock, re-reads and parses every input record (~2,300 during backfill). | `src/common/ingest/replay.py:19`, `src/common/ingest/inventory.py:68`, `src/util/runtime/ingest_service.py:701` |
| 2b | Core crash on `release_pin` | `release_pin` is called unguarded in both the normal and the `except` path. | `src/util/runtime/cycle.py:625`, `:642` |
| 3 | EWMRS `KeyError: 'render'` | `_ensure_pool` calls `util.runtime.config.section("render")`, which reads `runtime.yaml`; `render` is a section of `ewmrs_pipeline.yaml`. Only MRMS hits it because RAP/GOES never build that pool. | `src/util/runtime/ewmrs_consumer.py:408` |
| 4a | GLM `'NoneType' object has no attribute 'product_id'` | Auxiliary GLM/RAP jobs are offered to the shared ledger, but `_acquire_auxiliary` never calls `ledger.take()`. `dispatch_pending` therefore also runs them through `_acquire_mrms` with `discovered=None`, so every GLM/RAP job runs twice. | `src/util/runtime/ingest_service.py:604`, `:527`; `src/common/ingest/mrms/acquisition.py:678` |
| 4b | GLM integer dtype without `_FillValue` | `merge_glm_files` concatenates without preserving packing encoding (`_FillValue`, `scale_factor`, `_Unsigned`), so NaNs are written as integers. | `src/common/ingest/mrms/utils.py:62` |
| 4c | HDF5 `.part` diagnostics | Likely the netCDF-C probe on a not-yet-existing path, amplified by 4a's duplicate runs. Unproven. | `src/common/ingest/mrms/downloader.py:41` |
| 5 | `RAP_MSLP_Surface` has no artifacts | Config requests `prmsl @ surface`; the RAP awp130 file has `mslma @ meanSea` (verified with `grib_ls` on the 23z file). | `config/ewmrs_pipeline.yaml:77` |

## Fix plan (in order)

### Step 1 — Lock (#2, #2b). Unblocks everything else.

1. Add `acquire(timeout=None)` to `_AdvisoryFileLock`: poll the non-blocking
   `flock` with short backoff until the deadline, then raise a typed
   `InputLockTimeout`. `ServiceLock` and the primary lease keep fail-fast
   ownership semantics.
2. `input_lock()` uses the bounded acquire, with the timeout from a new
   `runtime.yaml` key `handoff.input_lock_timeout_seconds` (schema, docs and
   baseline tests updated together). `cleanup_permission` deliberately stays
   try-once so cleanup keeps deferring.
3. Make `input_lock` re-entrant per thread (depth counter), so nested use
   cannot deadlock under blocking semantics.
4. Shrink critical sections:
   - Coalesce scan evaluation: the publisher drains its queue and evaluates
     each affected scan once per batch, not once per committed file.
   - Cache parsed records in `handoff.records()` keyed by `(path, mtime_ns)`.
   - Log a warning when the lock is held longer than a threshold.
5. Guard `release_pin` in `cycle.py`: in the `except` path, log and suppress so
   the original error propagates; in the normal path, log and leave the pin for
   a startup sweep. Confirm (or add) a sweep of pins whose owner run is dead.

### Step 2 — Readiness contract (#1, #1a, #1b)

1. When CTAM and StormProb are enabled, `get_ingest_dependencies()` adds the
   enabled `STORMPROB_MRMS_SOURCES` to `mandatory_integration`; startup
   preflight fails if any is disabled. This changes the dependency fingerprint
   (expected).
2. Tiered ledger priority: check products → mandatory-integration products →
   the rest, **newest first** within each tier:
   `(tier, -target.timestamp(), identity)`.
3. Clamp the effective listing window to
   `min(s3_lookback_hours * 60, retention_minutes)` and add a config validation
   error when lookback exceeds retention.
4. Verify how Core selects the next pending scan. If it takes the oldest ready
   scan, realtime mode should take the newest and mark older ready scans
   terminal as `superseded`. Historical mode is unchanged.

### Step 3 — EWMRS (#3)

1. Use `EWMRS.pipeline_config.render_phase_name()` in `_ensure_pool`.
2. Log `traceback.format_exc()` on the layer-retry path so future unknown
   errors carry a stack trace.

### Step 4 — GLM (#4)

1. Separate auxiliary jobs from MRMS dispatch: `dispatch_pending` only admits
   `kind == "mrms"`, and `_acquire_auxiliary` calls `ledger.take()` (both, for
   defence in depth). Alternatively keep auxiliary jobs out of `_queued`
   entirely and use the ledger only for dedupe/settle.
2. In `merge_glm_files`, copy each variable's packing encoding from the first
   source dataset, set an explicit `_FillValue`, and drop encodings that would
   write float data as integer without a fill value.
3. Rerun and check whether the HDF5 `.part` noise disappears once jobs stop
   running twice. If not, use a temp name without a leading dot or silence
   HDF5 auto-printing around the write.

### Step 5 — RAP MSLP (#5)

Change the layer to `short_names: [mslma, prmsl]`,
`filter: {typeOfLevel: meanSea, level: 0}`. Update the RAP Uint16 catalog,
documentation and fixtures together, per `AGENTS.md` binary-contract rules.

## Regression tests

| Issue | Test | File |
|-------|------|------|
| 2 | Two processes contend for `input_lock`: the second blocks then succeeds; past the deadline it raises `InputLockTimeout`. | `tests/core/test_ingest_replay.py` |
| 2 | Same-thread nested `input_lock` does not deadlock; `cleanup_permission` still yields `False` immediately while held. | `tests/core/test_ingest_replay.py` |
| 2 | `ServiceLock` still fails fast while held (ownership semantics unchanged). | `tests/integration/processes/` |
| 2 | A burst of N completions for one scan causes ≤1 `publish_scan` per scan per batch (spy on inventory). | `tests/util/test_ingest_service.py` |
| 2b | `release_pin` raising inside `run_primary_cycle_once` does not mask the original exception and does not turn a clean cycle into rc=1. | `tests/core/test_tandem_coordinator.py` |
| 1 | `evaluate_scan` publishes no `core-integration-ready` while any StormProb source is missing, and publishes once all are present; with StormProb disabled the old gate still applies. | `tests/integration/handoff/test_ingest_inventory.py` |
| 1 | Contract: with StormProb enabled, every `STORMPROB_MRMS_SOURCES` product is in `get_ingest_dependencies().mandatory_integration` (catches drift between the lists). | `tests/architecture/test_mrms_ingestion_contract.py` |
| 1a | `ledger.due()` orders check → mandatory → optional, newest first within each tier; a cold-start backlog fetches the newest common scan first. | `tests/util/test_ingest_service.py` |
| 1b | Config invariant: listing lookback ≤ ingest retention, for the shipped catalog plus a violating overlay. | `tests/architecture/test_catalog_invariants.py` |
| 3 | `EwmrsRecordConsumer._ensure_pool()` builds a pool against the real config catalog (no mocks). | `tests/integration/handoff/test_ewmrs_consumer.py` |
| 3 | Static audit: `util.runtime.config.section(name)` is only called with top-level keys of `runtime.yaml` (AST scan of call sites). | `tests/architecture/test_boundary_audit.py` |
| 4a | A GLM/RAP auxiliary job is never passed to `_acquire_mrms`: queue one, call `dispatch_pending`, assert the MRMS acquirer is not called. | `tests/util/test_ingest_service.py` |
| 4b | Merge two GLM fixtures containing NaNs, round-trip to disk, assert NaNs survive; run with `-W error::xarray.SerializationWarning`. | `tests/unit/enrichment/test_glm_merge.py` (new) |
| 5 | Every RAP layer filter in `ewmrs_pipeline.yaml` matches at least one message in a checked-in RAP awp130 inventory (`shortName/typeOfLevel/level` list, not a real GRIB). | `tests/unit/rendering/test_ewmrs_rap_uint16.py` |

### Smoke test

A short end-to-end cold start on a temporary base directory with fake listers
and acquirers. Assert that:

- no `BlockingIOError` is logged;
- the first scan Core processes is the newest available;
- the integration manifest contains every StormProb source.

## Rollout note

Step 2.1 changes the dependency fingerprint, so existing `state/realtime`
ingest records will be rejected as a mismatch on the first run after upgrade.
Start from a fresh base directory or run the migration.

## Verification

```bash
python -m pytest tests/core tests/util tests/architecture
python -m pytest tests/integration/handoff tests/integration/processes
python -m pytest tests/unit/enrichment tests/unit/rendering
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```
