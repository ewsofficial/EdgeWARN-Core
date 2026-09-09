# Phase 0 findings — modular CTAM internal API

Phase 0 of `plans/modular-ctam-internal-api-plan.md` is complete. Phase 0 was
value-preserving by design: nothing in `src/` was modified. The deliverable is a
characterization suite that freezes today's CTAM behavior, the contract artifacts
the later phases are written against, and the corrections below, which the plan
needs before Phase 1 begins.

Audited against `74cc623` on `yuchen-wei3667/modular-ctam`. Package version
`2.7.0`. The plan's own audit baseline is commit `e52d17d`.

## What was added

| Path | Contents |
| --- | --- |
| `tests/core/ctam/baseline.py` | Snapshot harness, sibling of `tests/architecture/baseline.py`. Adds `datetime`, non-finite float, and float-rounding handling. Regenerate with `UPDATE_CTAM_BASELINE=1`. |
| `tests/core/ctam/test_stormcast_baseline.py` | 29 tests freezing StormCast's success, skipped, error, and alert output, including the `tstm_wind` mapping. |
| `tests/core/ctam/test_cell_history_baseline.py` | 11 tests over history file format, append/replace semantics, and every skip path. |
| `tests/ctam_baseline/*.json` | 15 committed snapshots. |
| `docs/ctam/schema/*.schema.json` | 7 schemas: response envelope, file descriptor, cycle state, requirements evaluation, patch request, transaction, status record. |
| `docs/ctam/schema/README.md` | What each schema covers, the two-step validation, and what these schemas cannot enforce. |
| `docs/ctam/openapi/ctam-internal-v1.json` | OpenAPI 3.1.0 for all 17 operations in the plan's resource table, every response carrying a validated example. |
| `docs/ctam/internal-api-limits.md` | 17 limits, each with a value, an enforcement point, and the behavior on excess. |
| `tests/core/ctam/contract/test_schema_contract.py` | 28 tests holding the schemas inside the validator's keyword set. |
| `tests/core/ctam/contract/test_openapi_contract.py` | 126 tests: the route surface, every example against its declared schema, and the pattern/anchor tie to the schema directory. |
| `tests/core/ctam/contract/test_pointer_allowlist.py` | The plan's table-driven allowlist: 42 pointers tagged by which layer must reject them, over 46 tests. |
| `tests/core/ctam/contract/test_limits_contract.py` | 9 tests tying the limits table to the constraints that restate it. |
| `tests/core/ctam/contract/test_doc_citations.py` | 6 tests requiring every source citation in the CTAM docs to resolve to a real file and line. |

`python -m pytest tests/core/ctam` gives **377 passed, 4 skipped** — the 114
pre-existing tests plus 267 new ones. The 4 skips are the request-body cases in
the parametrized envelope test, which is shared with the response examples.

Two design choices in the contract layer are worth knowing before editing it.

**Validation uses this repository's own walker, not `jsonschema`.** The package is
deliberately absent (argued at `src/common/config/loader.py:18-31`) so that the
Python and JavaScript validators stay in step, which restricts authors to the
keyword set at `_KNOWN_SCHEMA_KEYWORDS`: no `$ref`, `oneOf`, `format`,
`minLength`, or `patternProperties`. The contract tests import
`_check_supported_keywords` and `_walk` directly, so a schema that would fail at
config-load time fails in CI instead of at startup.

**The payload pairing is declared, not inferred.** With no `$ref`, the envelope
schema cannot describe the payload it wraps, so each response names its payload
schema in `x-edgewarn-data-schema` (or `x-edgewarn-item-schema` for a
collection). A response that names neither must appear in a recorded-exception
list with a reason, so a new route cannot quietly opt out of validation.

Three design choices are worth knowing before touching these files.

**Floats are rounded to 9 decimal places.** StormCast computes motion through a
flat-earth approximation using `math.cos`/`math.radians`, and libm differs in the
last bits across platforms. Baselines are generated on Windows and verified on
Linux in CI, so full-precision snapshots would fail against the runner rather
than the code. 1e-9 is far below the significance of any value present: motion is
in m/s, and forecast coordinates are already rounded to 3 decimals by
`StormCastEngine._meters_to_latlon`.

**The history tests observe the decision, not the file.** They substitute a
recorder for `atomic_write_json` and assert append-versus-replace directly. This
is both closer to the semantic Phase 3 preserves and the only way these tests run
on Windows — see the blocking defect in §1. The skip paths still assert real mtime
preservation, because the skip happens before any file access.

**The cell field inventory is read from source text with `ast`.** Constructing a
real cell requires radar arrays and the full detection stack. The declaration is
also closer to the actual question, which is which keys detection promises to
emit; there are three separate cell-construction literals in `save.py` and a test
asserts they agree, because an allowlist derived from one would be wrong for the
others.

## 1. Blocking defect: `atomic_write_json` fails on Windows

This is the most important finding and it is not in the plan.

`util.atomic.atomic_output_path` calls `os.fsync` on a descriptor opened
read-only:

- `src/util/atomic.py:36-37` — `with temporary.open("rb") as handle: os.fsync(handle.fileno())`

Linux permits `fsync` on an `O_RDONLY` descriptor. Windows returns `EBADF`. Every
`atomic_write_bytes` / `atomic_write_text` / `atomic_write_json` call therefore
fails on Windows, which means cell-history writes, index publication, and alert
publishing all silently fail there — `CellHistoryManager.update_cell_histories`
catches the exception and logs it (`history.py:98-99`), and in
`tests/unit/enrichment/test_history.py` the injected `mock_io_manager` is a
`MagicMock`, so even the log message disappears.

Verified fix, one character: open the temporary file `"r+b"` instead of `"rb"`.
Confirmed by probe on this machine — `rb` fails with errno 9, `r+b` succeeds.
**Not applied**, because Phase 0 does not modify `src/` and a filesystem commit
primitive deserves its own reviewed change.

Why it matters to this plan specifically: Phase 3 is entirely about atomic
replacement, multi-file journaling, and interrupted-commit recovery, and the test
matrix requires "Windows and Linux loopback launch, token propagation,
termination, and path handling". Building a journaled publisher on top of a commit
primitive that does not work on one of the two supported platforms would produce
a recovery path that cannot be tested where it is most likely to be needed. This
should be fixed before Phase 3, ideally before Phase 1.

## 2. Cell entries have no `geometry` field — strike it from the allowlist

The plan lists `/geometry` among the immutable paths a patch must not reach
(plan lines 470, 324). No code in `src/EdgeWARN/process` ever assigns it. All
three cell-construction literals in `src/EdgeWARN/process/detect/tools/save.py`
(lines 326-339, and the two duplicates) emit exactly:

```text
id, num_gates, centroid, bbox, hail_core, max_refl,
event_type, parent_ids, split_from, properties
```

Geometry is carried implicitly by `centroid`, `bbox`, and `hail_core`. Because the
plan's allowlist is *positive* — only `modules` and `properties` are reachable —
the completeness of the immutable enumeration is not load-bearing for safety. But
listing a field that does not exist invites a reader to infer that cells are
GeoJSON features with a `geometry` member, and they are not. Frozen by
`test_geometry_is_not_a_cell_field`.

## 3. The snapshot is not a `FeatureCollection`

`CellDataSaver.create_json_structure` (`save.py:28-34`) returns a flat dict with
exactly `source`, `product`, `version`, `latest_timestamp`, `features`. There is
no `"type"` key and no per-cell GeoJSON `geometry`/`properties` wrapper — a cell's
`properties` is an ordinary member, not a GeoJSON container. Any documentation or
SDK helper that describes the snapshot as GeoJSON will mislead module authors.

## 4. `bbox` is a polygon ring, not a bounding box

`bbox` is a list of `[lat, lon]` points forming a ring, produced by
`__round_polygon_points`, and `StormIntegrationUtils.create_cell_polygon` consumes
it as a ring. A module author reading "bbox" in the catalog documentation will
expect four scalars. Worth naming explicitly in the module-development guide a
later phase writes (`docs/ctam/module-development.md`, which does not exist yet).

## 5. The `properties.p95VIL` integration test premise is wrong

Plan lines 917-919 propose an integration test where "a fixture attempting to
write `properties.p95VIL` is rejected, and the value StormCast reads is
unchanged". StormCast never reads `p95VIL`. It reads `x`, `y`, `p100EchoTop30`,
`EchoTop50`, and `wind_field.u{level}`/`v{level}`
(`ctam/modules/StormCast/__init__.py:64-76`, `109-114`).

The only in-`src` reader of `p95VIL`, `p95EchoTop18`, and `p95AzShearLow` is
MorphoWind (`morphowind.py:48-50`, `98-99`, `118-119`) — which Phase 6 deletes.
After Phase 6 no production code reads those keys at all, so a test asserting
"the value StormCast reads is unchanged" would be vacuous.

Retarget the test at `p100EchoTop30`, `EchoTop50`, or a `wind_field` key. The
underlying concern is sound and worth keeping: those *are* detection/integration
enrichment values that StormCast consumes as if measured, so a module overwriting
one is exactly the failure the `properties` ownership rule prevents.

## 6. `tstm_wind` is a string, gated on a strict threshold, and untested

Confirming and sharpening the plan's §"The StormCast `tstm_wind` coupling must be
decided, not dropped":

- `ctam/modules/StormCast/__init__.py:490-492`. The published values are the
  **strings** `"true"` and `"false"`, not booleans. The threshold is a strict
  `> 0.6` against a `0.0` default.
- It is the **only** key in `threats`. Removing the field empties the dict
  entirely, which is a visible alert-schema change, not an internal one.
- **Nothing currently asserts it.** `tests/core/ctam/modules/stormcast/test_module.py`
  injects `{"severity_index": 0.7}` at lines 94 and 123, but its assertions only
  cover `cell_id`, `alert_outcome`, and `next_alert_eligible_minutes`. The
  injected value is load-bearing for realism and covered by nothing.

`test_tstm_wind_mapping` now covers absent-namespace, `0.0`, the exclusive
boundary at `0.6`, just above it, `0.7`, and `1.0`, and
`stormcast_alert_with_morphowind_serialized.json` versus
`stormcast_alert_without_morphowind_serialized.json` differ in exactly one line.
The Phase 6 decision can now be made against measured output.

## 7. A grid-only registry does not raise `KeyError`

Worth recording because it is a plausible-sounding claim that is false. The
attachment at `ctam/run.py:201-206` indexes `cells[0]["modules"]` directly, and
detection never creates that container (§2). It is nonetheless always present:
the per-cell loop calls `initialize_modules(cell, module_names)` unconditionally
at `run.py:82-84`, and `initialize_modules` runs `setdefault("modules", {})` even
when `module_names` is empty. Frozen by
`test_modules_container_is_created_even_with_a_grid_only_registry`.

The genuinely awkward case is an empty cell list: `run.py:204-206` *replaces* the
list with a single synthetic entry carrying only `modules._grid_outputs`, with no
`id`, `timestamp`, or `properties`. It is then skipped by history (`history.py:27`)
and by the API index (`pipeline.py:416`). Phase 6's grid migration must either
preserve or deliberately drop that shape; it is frozen by
`test_grid_output_with_no_cells_creates_synthetic_entry`.

## 8. History replacement compares only the last entry

`history.py:79-86` reads `history[-1]` and replaces it when the timestamp matches.
A cell re-submitted with a timestamp equal to an *earlier* entry appends, producing
a file with a repeated timestamp out of order. This is real current behavior, not
a hypothetical — StormCast already defends against duplicate history timestamps
when building its track (`__init__.py:306-322`). Phase 3's single publication
coordinator must keep this or change it deliberately. Frozen by
`test_only_the_last_entry_is_considered_for_replacement`.

## 9. `$`-anchored patterns admit a trailing newline, and `config/schema/` has 5

The walker matches `pattern` with `re.search` (`src/common/config/loader.py:410`),
not `re.fullmatch`, so anchoring is the schema author's job. Python's `$` then
matches at the end of the string *or* immediately before a trailing newline, so a
`$`-anchored pattern accepts a value with a newline glued to it.

For the CTAM schemas this is a safety property rather than a cosmetic one: the
patch-pointer allowlist is a pattern, and `/modules/Foo\n` matching it would be an
allowlist bypass. Probed on this machine:

```text
re.search("^/(modules|properties)(/[^/\x00-\x1f]+)+$",  "/modules/Foo\n")  -> match
re.search("^/(modules|properties)(/[^/\x00-\x1f]+)+\Z", "/modules/Foo\n")  -> None
```

Every pattern in `docs/ctam/schema/` therefore ends in `\Z`, which is a deliberate
deviation from the `config/schema/` house style, enforced by
`test_every_pattern_compiles_and_is_anchored` and explained by
`test_dollar_anchor_admits_a_trailing_newline`.

The pre-existing gap: `config/schema/` holds 38 patterns, 27 of them `$`-anchored
(the other 11 are intentional prefix checks such as `^https://`). Five admit a
trailing newline against a trivially valid value —
`api.schema.json` (the CIDR/alias pattern), `metar.schema.json` (`^[^/\\]+$`),
`nws.schema.json` (`^[^a-z]+$`), `runtime.schema.json` (the `module/attr`
pattern), and `wpc.schema.json` (`^[A-Za-z0-9_]+$`). The practical exposure is
low, because these validate checked-in YAML rather than untrusted input, and
nothing was changed: it is outside Phase 0's scope and touches config validation
for the whole repository. It is recorded because the CTAM schemas now differ from
their neighbours on purpose, and a future author "fixing" the inconsistency in the
wrong direction would reopen the allowlist hole.

Note that the OpenAPI document uses `$` in its inline parameter patterns, because
that document is also read by OpenAPI tooling whose regex dialect has no `\Z`.
That makes two spellings of the same constraint;
`test_parameter_patterns_are_the_schema_patterns_with_an_ecma_anchor` requires
each inline pattern to be a checked-in schema pattern with only the anchor
swapped, so the pair cannot drift and a pattern cannot be copied into a schema
file without being re-anchored.

## 10. What the restricted keyword set cannot enforce

Recorded because each of these looks enforced when it is not, and Phase 2 and 3
must implement them in host code.

- **String lengths.** `minLength`/`maxLength` are not in the keyword set at all,
  so bounds are folded into patterns as quantifiers. During authoring,
  `"minItems": 1` was used on a string field to require non-emptiness; the walker
  applies `minItems` only to lists, so it was a silent no-op. Non-emptiness is now
  `"pattern": "\\S"`, the one sanctioned unanchored pattern.
- **Patch value contents.** A patch `value` is arbitrary JSON, so there is no
  subschema for the walker to recurse with. Depth, field count, serialized size,
  and the finiteness of nested numbers are all unexpressible. `json.loads` accepts
  `NaN` and `Infinity` unless `parse_constant` rejects them, so a non-finite value
  nested in a patch reaches the host untouched. `type: "number"` does check
  `math.isfinite` (`loader.py:335-340`), but only where a subschema applies.
  `test_patch_value_finiteness_is_not_enforced_by_schema` asserts the gap rather
  than the wish, so it cannot be assumed covered.
- **Key ownership.** The pointer pattern decides *shape* — which container a path
  starts with, and that a key is named below it. It cannot decide *ownership*,
  which needs the caller's manifest. 12 rows of the allowlist table are tagged
  `HOST` for exactly this reason, including `/modules/StormCast`,
  `/properties/undeclared_key`, and `/modules/cellstats` (the module id rather
  than the manifest display name). Phase 3's validator should import `TABLE` and
  assert those rows are rejected there.
- **Traversal segments.** `/modules/CellStats/../id` is deliberately *admitted* by
  the pattern. `..` is a legal JSON Pointer key name; it becomes a write to `/id`
  only if the host resolves pointers positionally, which it must not.
  `test_traversal_segment_is_a_literal_key` pins the reasoning so it is not
  "fixed" into a filesystem-path check.

## 11. An 8 MiB ceiling already applies to the files CTAM publishes

Not in the plan, and it constrains Phase 3.

`src/api/services/analysis.js:17,24` read `data/cells/<cell-id>.json` and
`data/stormcells/stormcells_<ts>.json` through `readJson`, which opens them with
`kind: 'json'` (`src/api/repositories/artifactRepository.js:115`) and rejects them
when `stat.size` exceeds the configured limit
(`src/api/repositories/artifactRepository.js:79`). That limit is
`size_limits_bytes.json: 8388608` at `config/api.yaml:50`.

So a transaction can be individually valid — every operation inside the
per-operation and per-transaction limits — and still publish a snapshot the public
API refuses to serve. The publication coordinator must size-check the serialized
result *before* atomic replacement, not just bound the patches going in.
Taking 200 cells as the worst-case snapshot width (the largest count configured
anywhere, at `benchmarks/benchmark_grid_index.py:186`), 8388608 / 200 leaves
41943 bytes per feature for everything detection, integration, and modules
together write.

## 12. A bounded history window is a StormCast behavior change

The plan's `min_history_entries` requirement pairs with a bounded history read, and
that pairing is not behavior-preserving for the built-in module.

- `src/EdgeWARN/ctam/util/history_cache.py:11` returns `full_history[:limit]` when
  a limit is given and `full_history` otherwise, and StormCast calls
  `history_cache.get(cell_id)` with no limit. It reads the whole file today.
- The separate helper `src/EdgeWARN/ctam/util/history.py:5` does default
  `limit=5`, which is where the documented default window comes from — but
  StormCast does not go through it.
- Nothing trims an active cell's history file. `history.py` appends or replaces the
  last entry and writes the whole list back with no cap. The only removal is
  whole-file deletion after a cell has been inactive for
  `inactive_cell_max_age_minutes: 120` (`config/api_index.yaml:16`, applied at
  `src/EdgeWARN/api_integration/index_manager.py:177`).

Phase 5 must therefore exempt the in-process StormCast adapter from the default
window, or accept a measurable forecast change and re-baseline. The limits document
resolves the request side by *clamping* an over-large `limit` down to the maximum
rather than rejecting it, so the OpenAPI parameter deliberately declares no
`maximum`; `test_history_window_maximum_is_deliberately_not_declared` pins that.

## 13. All three CTAM benchmarks are dead

They pass by skipping, so they have been reporting success while measuring nothing.

- The shared fixture at `benchmarks/test_performance.py:381` does
  `data.get("cells", [])`, but the snapshot envelope's key is `features`
  (`src/EdgeWARN/process/detect/tools/save.py:28-34`, and §3). It always yields an
  empty list.
- The old benchmark imported an obsolete in-package module path and called a
  retired in-process method. Phase 6 removes that benchmark with the legacy
  framework; Phase 7 replaces it with a cycle-scoped measurement.

The 2-second CTAM stage assertion at `:402` is consequently unproven, which matters
because the limits document uses that budget to justify the 30-second per-module
ceiling. On this machine the skip is also masked by absent `data/stormcells/`, so
the key-name defect is established from `save.py` and the frozen envelope baseline
rather than from the skip itself. Repairing these is a `src/`-adjacent test fix and
was left for the phase that needs the measurement.

## Limits chosen

`docs/ctam/internal-api-limits.md` is the decision record; the values are not
repeated here. Three have no precedent in the repository and are labelled as
proposals in that document: the maximum external module count (8), the maximum
history window (120 entries), and the captured stdout/stderr cap (1 MiB per
stream). Everything else is derived from an existing constant, cited to file and
line, and every citation is checked by `test_doc_citations.py`.

Two reconciliations came out of cross-checking the document against the schemas.
The module-id bound widened from 63 to 128 characters to match the existing
`LAYER_ID` regex at `src/api/services/validation.js:4`, and the
`request_too_large` example limit changed from 262144 to 1048576 to match the
documented body size. `test_limits_contract.py` now ties both directions, so the
next such divergence fails instead of being noticed by hand.

## Environment baseline

Recorded so later phases can diff failures by test identity rather than count.

- Three modules cannot be collected on Windows at all:
  `tests/core/ingest/test_nexrad_{parser,worker,worker_pool}.py`, because
  `src/common/ingest/nexrad/worker.py:18` imports the Unix-only `resource`
  module.
- Excluding those, the suite is **53 failed, 1209 passed, 16 skipped**. None are
  in `tests/core/ctam`. The large majority trace to the `atomic_write_json`
  defect in §1 — 41 failures mention `EBADF` directly, and the 5 in
  `tests/unit/enrichment/test_history.py` have the same cause with the
  message swallowed by a `MagicMock`.
- On Linux CI these numbers should be substantially lower. Do not treat this as a
  target; treat it as the set to diff against.
