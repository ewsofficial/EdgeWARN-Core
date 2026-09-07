# CTAM Internal API Limits

Default bounds for the `/internal/ctam/v1` internal API described in
`plans/modular-ctam-internal-api-plan.md`. This document is a Phase 0
deliverable: it fixes the numbers that Phase 1 manifest validation and Phase 2
and Phase 3 request handling are implemented and tested against.

Every value below is either derived from a value that already exists in this
repository, or is labelled as a proposed default with its reasoning. Citations
name the file and line that supplies the grounding value. Where a limit has no
precedent it says so.

Nothing here changes runtime behavior. No `src/` file is modified by this
document.

## Enforcement points

Limits are enforced in two different code paths and Phase 1 and Phase 3 own
different halves of the table.

| Enforcement point | Phase | What it checks |
| --- | --- | --- |
| Manifest validation at discovery time | 1 | Module count, declared `timeout_seconds`, declared `min_history_entries`, module ID length, write-pointer shape. A rejected manifest never launches a process. |
| Request handling at runtime | 2, 3 | Request body size, patch operation count, patch payload size, patch depth, patch field count, history read limit, streamed file size. |
| Runner supervision | 4 | Elapsed per-module runtime, elapsed CTAM stage time, captured stdout/stderr volume. |

A limit that is checkable from a manifest alone is checked at discovery, so the
failure is `invalid` with an actionable reason rather than a process that starts
and then fails. The plan's per-module state set is `discovered`, `invalid`,
`waiting`, `ready`, `running`, `committing`, `completed`, `skipped_disabled`,
`skipped_missing_requirements`, `timed_out`, and `failed`.

## Values

| Limit | Value | Unit | Enforced at | On excess |
| --- | --- | --- | --- | --- |
| Maximum external module count | 8 | modules | Discovery | Modules past the 8th in stable dependency-then-ID order are recorded `invalid` with an over-capacity reason. StormCast is not counted. |
| Minimum manifest `timeout_seconds` | 1 | seconds | Discovery | Manifest rejected, module `invalid`. |
| Maximum manifest `timeout_seconds` | 30 | seconds | Discovery | Manifest rejected, module `invalid`. |
| Default `timeout_seconds` when omitted | 10 | seconds | Discovery | Not applicable. |
| CTAM stage wall-clock ceiling | 30 | seconds | Runner | Remaining unstarted modules become `skipped_missing_requirements` with a deadline reason; a module already running is terminated as `timed_out`. |
| Terminate-to-kill escalation | 5 then 1 | seconds | Runner | Child is killed and marked `timed_out`. |
| Maximum request body size | 1048576 | bytes (1 MiB) | Runtime | Request-too-large error. Nothing is staged. |
| Maximum payload per patch operation value | 16384 | bytes (16 KiB) | Runtime | Invalid-patch error. Nothing is staged. |
| Maximum operations per PATCH request | 64 | operations | Runtime | Invalid-patch error. Nothing is staged. |
| Maximum staged operations per module transaction | 1000 | operations | Runtime | Invalid-patch error on the operation that would exceed it; already-staged operations survive until the module commits or abandons. |
| Maximum total staged payload per module transaction | 4194304 | bytes (4 MiB) | Runtime | Invalid-patch error, as above. |
| Maximum patch value depth below the operation path | 8 | levels | Runtime | Invalid-patch error. |
| Maximum leaf values per patch operation value | 256 | values | Runtime | Invalid-patch error. |
| Maximum module ID length | 128 | characters | Discovery | Manifest rejected, module `invalid`. |
| Maximum `properties` key and string value length | 256 | characters | Runtime | Invalid-patch error. |
| Default history read window | 5 | entries | Runtime | Not applicable. |
| Maximum history read window | 120 | entries | Runtime | The requested `limit` is clamped down to 120, not rejected. |
| Maximum streamed file size | 268435456 | bytes (256 MiB) | Runtime | Catalog descriptor reports `readiness` as unavailable with a size reason, and `GET /files/{file_id}/content` returns an unavailable-file error. |
| Range and stream chunk size | 1048576 | bytes (1 MiB) | Runtime | Not applicable. |
| Maximum captured stdout or stderr per module | 1048576 | bytes (1 MiB) per stream | Runner | Capture is truncated with a marker; the module is not failed for output volume alone. |

The plan's error-code set is authentication failure, unsupported version,
unavailable file, unmet requirement, forbidden path, stale revision, conflict,
invalid patch, request too large, and transaction already sealed. The
"On excess" column above uses only codes from that set.

## Reasoning and grounding

### Cycle cadence and the total time budget

The pipeline is event-driven, not interval-driven, but it is keyed to the MRMS
scan cadence. `src/EdgeWARN/schedule/scheduler.py:113` rounds every candidate
timestamp through `round_to_nearest_even_minute`
(`src/common/ingest/mrms/timestamp_utils.py:7-9`), so the scheduler can only
ever select an even minute and a cycle can arrive at most every 120 seconds.
`src/common/ingest/manifest.py:222-227` calls this "the normal two-minute scan
window" and enforces it with `min(120.0, self.mrms_tolerance_seconds)`.
`config/lineage.yaml:17` sets `scan_interval_seconds: 120.0` on the same basis,
and `config/detection.yaml:34` uses `fallback_dt_seconds: 120.0` as the assumed
inter-scan delta. 120 seconds is therefore the hard outer ceiling on everything
CTAM does.

CTAM does not get the whole cycle. It is stage 6 of 9 inside integration
(`docs/core/integration.md:52-62`), and integration as a whole is asserted to
finish in under 30 seconds at `tests/benchmarks/test_performance.py:182`. The
current CTAM stage is asserted at under 2 seconds
(`tests/benchmarks/test_performance.py:402`) and StormCast alone at under 1
second (`tests/benchmarks/test_performance.py:423`).

The CTAM stage ceiling is set to 30 seconds. CTAM runs inside integration, so it
cannot be permitted to exceed the budget of the stage that contains it, and 30
seconds still leaves 90 seconds of the cadence for the other eight integration
stages plus detection and ingest. With StormCast's existing 2 seconds reserved,
28 seconds remain for external modules.

Read the 2-second figure with care. The former in-package registry did not
provide a stable module API, so StormCast is now the only bundled built-in and
all optional modules use manifests. The stage-level benchmark reads
`data.get("cells", [])` at `tests/benchmarks/test_performance.py:381`, but the
snapshot envelope key is `features`
(`src/EdgeWARN/process/detect/tools/save.py:33`), so the fixture always returns
an empty list and the test always skips. The 2-second budget is recorded intent,
not an enforced measurement. Phase 7's benchmark task should replace these three
tests rather than treat them as a passing baseline.

### Maximum module count

**Proposed, by analogy.** The repository caps independently scheduled work units
at 3 (`config/detection.yaml:21`), 4 (`config/nexrad.yaml:41`), 8
(`config/ewmrs_pipeline.yaml:22`), 16 (`config/nws.yaml:70`), and 24
(`config/nexrad.yaml:39`). None of these is a plugin count, so none of them
derives a module count. 8 is taken from `config/ewmrs_pipeline.yaml:22` as the
nearest analogy: a bounded set of independent jobs inside one scheduled phase.

The stage deadline, not the count, is the binding constraint. At the 1-second
minimum timeout, 8 modules consume 8 seconds of the 28-second external budget,
so a full module set can always at least start and be individually timed out
inside the stage ceiling. At the 10-second default timeout only two modules fit,
which is intentional: the count cap bounds discovery and status work, and the
deadline bounds wall-clock cost.

For reference, the base package ships one reserved built-in, StormCast.

### Timeout bounds

The minimum of 1 second is taken from `config/schema/nexrad.schema.json:72`,
which constrains `worker_timeout_seconds` with `"minimum": 1`. That is the only
existing schema-level lower bound on a worker timeout in the repository.

The maximum of 30 seconds is the repository's standing outbound timeout: 30
seconds at `config/nws.yaml:68`, 30 seconds at `config/wpc.yaml:17`, and
`request_timeout_ms: 30000` at `config/api.yaml:10`. It also matches the plan's
own manifest example (`plans/modular-ctam-internal-api-plan.md:273`).

The default of 10 seconds when a manifest omits `timeout_seconds` comes from
`config/ingest.yaml:72`, `sync_timeout_seconds: 10`.

A manifest at the 30-second maximum consumes the entire external-module budget
on its own. That is permitted, because a single expensive module is a legitimate
deployment, but it is why the runner enforces a stage deadline as well as a
per-module timeout. Two modules at 30 seconds each are accepted by manifest
validation and the second is skipped at runtime with a deadline reason.

Termination follows the existing supervisor escalation exactly:
`process.terminate()`, join for `stop_join_timeout_seconds: 5`, then
`process.kill()` and join for `stop_kill_join_timeout_seconds: 1`
(`config/runtime.yaml:59-60`, implemented at
`src/util/runtime/processes.py:23-30`).

### Request body size

1 MiB is `decompress_chunk_size_bytes: 1048576` from `config/ingest.yaml:22`,
the repository's existing 1 MiB I/O unit. It is not a body-size precedent, and
there is no body-size precedent to cite: `config/api.yaml:14-19` records that
the live Node service mounts no JSON body parser at all, so `json_body_limit`
was deleted rather than moved. 1 MiB is 1000 times the largest measured module
payload in the Phase 0 fixtures, so it is generous rather than tight.

### Patch size, count, depth, and field count

These are derived from the size ceiling that the public API already applies to
the files CTAM publishes. `src/api/services/analysis.js:17,24` read
`data/cells/<cell-id>.json` and `data/stormcells/stormcells_<ts>.json` through
`readJson`, which opens them with `kind: 'json'`
(`src/api/repositories/artifactRepository.js:115`) and rejects them with
`INVALID_ARTIFACT` when `stat.size` exceeds the limit for that kind
(`src/api/repositories/artifactRepository.js:79`). That limit is
`size_limits_bytes.json: 8388608` at `config/api.yaml:50`. A published
stormcell snapshot larger than 8 MiB is unreadable by the API that serves it, so
8 MiB is a real, already-enforced ceiling on what module patches may grow the
snapshot to.

The largest storm-cell count configured anywhere in the repository is 200, in
the "Large MRMS-like grid" benchmark case at
`tests/benchmarks/benchmark_grid_index.py:186`; the other cases are 50 and 100,
and `tests/benchmarks/benchmark_lazy_loading.py:183` defaults to 100. Taking 200
as the worst-case snapshot width, 8388608 / 200 gives 41943 bytes per feature for
the entire entry, including everything detection and integration already wrote.

The per-operation payload limit is therefore set to 16384 bytes, roughly 40
percent of that per-entry allowance, which leaves room for the existing
detection and enrichment content plus a second module. 16 KiB is also 16 times
the largest measured module payload: the golden StormCast success output at
`tests/ctam_baseline/stormcast_success_with_history.json` is 1004 bytes when
serialized compactly with the baseline harness's `@tuple` wrappers removed.

The remaining three numbers follow arithmetically:

- 64 operations per request, because 1048576 / 16384 = 64 exactly, so the body
  limit and the per-operation limit cannot contradict each other.
- 4 MiB per transaction, half the 8 MiB snapshot ceiling, so two modules at full
  budget still cannot produce an unreadable snapshot. The 200-cell worst case at
  the per-operation limit is 200 * 16384 = 3276800 bytes, which fits.
- 1000 staged operations per transaction, from `list_limit: 1000` at
  `config/api.yaml:52` and `max_limit: 1000` at `config/api.yaml:58`, the
  repository's standing collection-size ceiling. The worst case is one
  stormcell operation plus one history operation per cell, 2 * 200 = 400, plus
  staged alerts.

Depth is set to 8. The measured depth of the StormCast payload below its
namespace root is 4 (`modules.StormCast` to `forecast_cones` to an element to
`center` to a coordinate), computed from
`tests/ctam_baseline/stormcast_success_with_history.json`. Existing enrichment
reaches `properties.wind_field.u1000`, two levels below `properties`, built by
`_set_nested` at `src/EdgeWARN/process/integrate/integrate_rap.py:169-180`. 8 is
twice the deepest real producer.

Field count is set to 256 leaf values per operation value. The StormCast payload
has 9 top-level keys and 51 leaf values. The floor the limit must clear is the
existing `properties` container, which `config/integration.yaml` already fills
with 25 `stats_datasets` keys (`config/integration.yaml:35-61`), 40
`probsevere_field_map` keys (`:67-112`), 74 `wind_field` leaves from the 37
isobaric levels crossed with u and v (`:118-136`), 5 single-level RAP keys
(`:137-143`), and 2 derived keys (`:148-150`), for roughly 150 leaves before any
module writes. 256 is the next power of two above that floor.

Module ID length of 128 characters matches `LAYER_ID` at
`src/api/services/validation.js:4`, the repository's existing bound on a
filename-safe public identifier. The 256-character key and string bound is
`query.max_value_length: 256` at `config/api.yaml:67`.

### History window

The default of 5 entries is grounded twice, independently:
`get_cell_history(cell_id, limit=5)` at
`src/EdgeWARN/ctam/util/history.py:5`, and `_HISTORY_WINDOW = 5` at
`src/EdgeWARN/process/integrate/azshear/constants.py:8`, applied as
`payload[-azshear_history_window():]` at
`src/EdgeWARN/process/integrate/azshear/integration.py:74`. The comment at
`config/integration.yaml:15` records the same value as the only real cap on
history reading.

Over-limit requests are clamped rather than rejected, following
`Math.min(limit, maxLimit)` at `src/api/services/validation.js:19`.

**The maximum of 120 entries is proposed, not derived, and it needs care.**
There is no retention limit on cell history in this repository.
`src/EdgeWARN/process/integrate/history.py:92` appends without a cap, and the
comment at `config/integration.yaml:12-15` states that a `max_payloads` key was
removed because it was fabricated. Cleanup deletes whole files rather than
trimming entries: `cleanup_inactive_cells` unlinks
`data/cells/<cell-id>.json` once a cell has been *inactive* for
`inactive_cell_max_age_minutes: 120` (`config/api_index.yaml:16`, applied at
`src/EdgeWARN/api_integration/index_manager.py:177,186-189`). A cell that stays
active is never expired and never trimmed, so its history file can hold far more
than 120 minutes of entries.

120 entries is one 120-minute retention budget at the 2-minute cadence, doubled,
which covers a cell tracked continuously for four hours. It is a read-side
convenience bound for external modules, not a claim about file contents.

**StormCast must be exempt from this cap.** StormCast builds its motion track
from the entire history file: it calls `history_cache.get(cell_id)` with no
limit at `src/EdgeWARN/ctam/modules/StormCast/__init__.py:242`, and
`CellHistoryCache.get` returns the full list when `limit` is `None`
(`src/EdgeWARN/ctam/util/history_cache.py:11,36-39`). Applying a 120-entry cap
to the built-in adapter would silently shorten the track for a long-lived cell
and change forecast output, which is exactly the regression the Phase 0 golden
fixtures exist to catch. Phase 5 must either exempt the built-in adapter or
raise this bound; it must not quietly clamp StormCast.

### Streamed file size

256 MiB is derived from the only documented statement of real MRMS field size in
the repository: `tests/benchmarks/benchmark_lazy_loading.py:114-117` records
MRMS grids as roughly 3500 by 7000 points with a "File size: ~100-200MB per
uncompressed field", and MRMS files are stored decompressed locally
(`config/ingest.yaml:18-22` sizes the local gzip expansion). 256 MiB covers the
200 MB upper end with headroom. `tests/benchmarks/test_performance.py:128`
records a larger figure, roughly 784 MB, but that is the decoded float64 array
in memory rather than the file on disk, so it does not bound a streamed read.

The internal limit deliberately does **not** reuse the public API's binary
artifact limit of `134217728` bytes, 128 MiB, at `config/api.yaml:50`. That
value sits below the documented 200 MB upper bound for a single uncompressed
MRMS field, so the public API cannot serve the largest raw input even today. The
internal API streams raw ingest artifacts that the public API never exposes, so
the two limits are independent and the internal one has to be larger.

The 1 MiB chunk size is `decompress_chunk_size_bytes: 1048576` from
`config/ingest.yaml:22`, which is the local-file read unit. The smaller 8192
chunk at `config/ingest.yaml:78` sizes network reads and does not apply to a
loopback stream of a local file.

### Captured output volume

**Proposed, no precedent.** Nothing in the repository currently caps captured
subprocess output; the Python tree spawns `multiprocessing.Process` children
whose output goes to inherited handles or an explicit log queue rather than to a
captured pipe. 1 MiB per stream reuses the 1 MiB unit from
`config/ingest.yaml:22`. Truncating rather than failing is chosen so that a
noisy but correct module still commits, which matches the Phase 4 acceptance
requirement that a noisy fixture cannot block later modules.

## Deviations from the plan text

Three of the plan's stated values or assumptions do not survive contact with the
repository. Each needs a decision before the phase that depends on it.

1. **`timeout_seconds = 30` in the manifest example**
   (`plans/modular-ctam-internal-api-plan.md:273`) is 15 times the current CTAM
   stage budget of 2 seconds and equal to the whole-integration budget of 30
   seconds. It is accepted here as the maximum, but it cannot be the example
   value in the manifest reference Phase 1 writes
   (`docs/ctam/module-manifest.md`, which does not exist yet) without also
   documenting that one such module consumes the entire external budget. The
   example should use the 10-second default.

2. **`min_history_entries = 2`** (`plans/modular-ctam-internal-api-plan.md:285`)
   is a requirement floor, and it is fine. The problem is the paired notion of a
   bounded history window: StormCast reads unbounded history today and nothing
   trims an active cell's history, so a history cap is a StormCast behavior
   change unless the built-in adapter is exempted. See the history section above.

3. **The 8 MiB public JSON artifact ceiling is not mentioned anywhere in the
   plan.** `config/api.yaml:50` already bounds the published stormcell snapshot
   and cell-history files at 8388608 bytes, enforced at
   `src/api/repositories/artifactRepository.js:79`. Every patch-size limit in
   this document is derived from it. Phase 3's publication coordinator should
   validate the serialized snapshot against that ceiling before atomic
   replacement, otherwise a valid transaction can publish a file the API refuses
   to read.
