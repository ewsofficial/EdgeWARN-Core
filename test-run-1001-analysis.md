# test-run-1001 log analysis

Source: `test-run-1001.txt` (6,839 lines, run started 2026-10-02T01:10 UTC).
Runtime tree inspected: `~/EdgeWARN_input`.

The earlier `NameError: get_check_modifiers` is gone, so Core now gets past
startup. It then crashes later, for a different reason. The NWS zone error is
excluded, as requested.

## Summary

| # | Issue | Severity | Status |
|---|-------|----------|--------|
| 1 | Core integration runs with almost no MRMS inputs → `StormProbDependencyError` | High | Cause of readiness decision not yet traced |
| 2 | Non-blocking `flock` used as a mutex → `BlockingIOError` across services | High | Cause identified in code |
| 3 | EWMRS `KeyError: 'render'` on MRMS layers | Medium | Source not located |
| 4 | GLM ingest: `NoneType.product_id`, HDF5 `.part` race, integer-dtype warnings | Medium | Partly identified |
| 5 | `RAP_MSLP_Surface` has no usable artifacts | Low | Not investigated |

## 1. Core crashes with `StormProbDependencyError`

Log line 6298, raised at `src/EdgeWARN/ctam/preflight.py:330` via
`process/integrate/pipeline.py:716` (`validate_stormprob_cycle`).

Integration ran while most MRMS inputs were absent from disk.

Empty directories under `~/EdgeWARN_input/data/`:

- `MRMS_Reflectivity_0C`, `MRMS_Reflectivity_-5C`, `MRMS_Reflectivity_-15C`
- `MRMS_VIL`, `MRMS_VIL_Density`, `MRMS_VII`
- `MRMS_EchoTop_18`, `MRMS_EchoTop_30`, `MRMS_EchoTop_50`
- `MRMS_PrecipRate`, `MRMS_MESH`, `MRMS_MergedReflectivityAtLowestAltitude`

Populated directories:

| Directory | Files |
|-----------|-------|
| `MRMS_MergedReflectivityQCComposite` | 17 |
| `MRMS_PrecipFlag` | 22 |
| `MRMS_ProbSevere` | 17 |
| `MRMS_MergedAzShear_0-2kmAGL` / `3-6kmAGL` | 1 each |

Supporting observations:

- Ingest was still working: `scan ... still waiting for ['MergedReflectivityQCComposite', 'PrecipFlag', 'ProbSevere']` and many `newly queued` lines.
- `state/realtime/` has no `cycles/` directory, so Core never wrote a cycle record.
- EWMRS logged rendering QC composites stamped 23:10–23:14, but those files are not on disk. The file with the newest modification time is named `..._213242`. Cause not determined (cleanup, or a mismatch between readiness records and retained files).
- `CellIntegration` warnings (`No files found for VIL/VII/Ref...`) are a symptom of this issue.

Open question: why integration was allowed to start before its inputs
existed. The log shows no check that the readiness record's files exist, and
the readiness code has not been traced yet.

### 1a. Likely upstream cause: ingest downloads the whole window, oldest first

Hypothesis from the user, checked against the code. Mostly confirmed.

What the code does:

- Every enabled product is listed over the full lookback window
  (`config/scheduler.yaml:10`, `s3_lookback_hours: 2`;
  `src/util/runtime/ingest_service.py:444`, `_observation_window`). Every object
  found is offered for download (`ingest_service.py:471`). This is why the log
  shows about 110 objects per product, all `newly queued`. Nothing anchors the
  window start to a common timestamp.
- `AcquisitionLedger.due()` (`ingest_service.py`) sorts jobs by
  `(not protected, target, identity)`: check products first, then the oldest
  observation first. After a cold start, the freshest scan is downloaded last.
- The ledger is bounded (`AcquisitionBacklogFull`), so the window can saturate.

Evidence from the run:

- The run stopped at 01:11 UTC. The newest QC composite on disk is
  `20261001-232241`, so ingest was still about 1h50m behind real time and working
  forward from old data. This fits oldest-first ordering.
- The oldest files on disk are stamped 21:30 UTC, which is before the 2h window
  should start. They may be leftovers from an earlier run or adopted at startup;
  not determined.

Refinements to the hypothesis:

- "The check" is only three products. `get_check_modifiers()` returned 3
  (`MRMS readiness=3`): the products with `discovery` set (QCComposite,
  PrecipFlag, ProbSevere). The other 18 MRMS products are listed and downloaded
  over the same window.
- Check products are prioritized but not exclusive. `reserved_slots` keeps some
  download slots for other products (`dispatch_pending`).
- No "latest common timestamp where the check products agree" logic was found in
  `ingest_service.py`. Not read: `_reconcile` (startup), the full `evaluate_scan`,
  and scan selection in `src/EdgeWARN/schedule/`. A startup anchor could exist
  there.

Relation to issue 1: the empty VIL/VII/EchoTop/Ref directories are not check
products, so they are never prioritized, and oldest-first ordering means the
latest scan's files arrive last. The `StormProbDependencyError` is therefore
probably a downstream effect of this ordering, plus the readiness question above.

Possible fix: seed the listing window start at the latest scan time present in
every check product, and download newest-first from there.

## 2. Non-blocking lock contention

`input_lock()` (`src/common/ingest/replay.py:19`) reuses `_AdvisoryFileLock`
(`src/util/runtime/handoff.py:126`), which calls
`flock(LOCK_EX | LOCK_NB)` with no retry. It is used as a short mutex shared
by the ingest, Core and EWMRS processes, so any overlap raises
`BlockingIOError: [Errno 11] Resource temporarily unavailable`.

| Log message | Count |
|-------------|-------|
| `EWMRS input consumer pass failed` | 48 |
| `[Ingest] publisher completion failed` | 24 |
| `[Readiness] readiness watcher failed` | 16 |
| `release_pin` traceback (log line 6409) | 1 |

Why Core exits: in the traceback at line 6409, `release_pin`
(`src/util/runtime/ingest_handoff.py:436`), called from
`src/util/runtime/cycle.py:642`, raises `BlockingIOError`. That turns a
worker-level failure into a crash of the primary service (`rc=1`), and the
launcher then stops every other service.

Suggested fix: make `input_lock` a blocking or bounded-retry acquire (it is a
mutex, not an ownership lock), and make `release_pin` unable to mask the
original error.

## 3. EWMRS `KeyError: 'render'`

- MRMS layers (`MRMS_MergedReflectivityQC`, `MRMS_MergedAzShear_*`) fail 3 attempts and then expire: 18 failures and 5 expirations.
- RAP and GOES layers render fine, so this is specific to the MRMS render path.
- The log contains only the exception text, with no traceback, and the failing lookup was not found in source. Cause unknown.

Next step: log the traceback on the retry path in the EWMRS consumer, then
rerun.

## 4. GLM ingest

- `AttributeError: 'NoneType' object has no attribute 'product_id'` appears 6 times on attempt 1 of the GLM scan-time ingest; later attempts succeed and merged files are saved. The call site has not been located.
- 97 HDF5 diagnostics: `unable to open file ... .OR_GLM-L2-LCFA_merged_...nc.part ... errno = 2`. The merge appears to probe a temp `.part` file after it has been renamed or removed. This looks like a race and is mostly noise.
- `SerializationWarning` at `src/common/ingest/mrms/downloader.py:608`: `event_lat`, `event_lon`, `event_time_offset`, `group_time_offset`, `flash_time_offset_of_first_event` and `flash_time_offset_of_last_event` are saved as float data in an integer dtype without a `_FillValue`. NaNs become integers in the merged GLM files, which is a possible data-quality problem.

## 5. Minor

- `RAP_MSLP_Surface`: `no usable artifacts published`, and `RAPUint16` reports it as a missing layer (3 occurrences). The layer is absent from the 23z f00 file or its name does not match.
- RAP 404s for the 01z and 00z cycles on S3 and NOMADS: expected late publishing; the fallback to 23z worked.

## Earlier fixes in this session (uncommitted)

- `src/util/runtime/primary_service.py:182`: import `get_check_modifiers` (fixes the `NameError`).
- `src/util/runtime/logging.py`: `drain_log_queue` skips the `None` shutdown sentinel (fixes the stray `None` log line).

## Suggested order of work

1. Lock acquisition (item 2).
2. Why integration starts before inputs exist (item 1), including the
   startup download window and ordering (item 1a).
3. Log the traceback for `KeyError: 'render'` (item 3).
4. GLM dtype/`_FillValue` handling (item 4).
