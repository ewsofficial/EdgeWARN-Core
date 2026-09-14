# Modular CTAM Internal API Plan

**Audit baseline:** commit `e52d17da18257b3acd8cab9993ad0f90dfc1f33c`
on `yuchen-wei3667/unified-ew-api`  
**Package version:** `2.7.0`  
**Status:** planning only; this document does not change CTAM runtime behavior

## Objective

Replace the current import-time CTAM registry and in-place Python object
interface with a versioned internal API that allows independently installed
modules to inspect cycle readiness, discover and read available files, declare
and check their required inputs, and add calculated information to the current
stormcell snapshot and persistent cell-history files.

The primary contract is:

- Optional CTAM modules are installed below the repository-root
  `ctam_modules/` directory.
- `/ctam_modules/` is gitignored. A fresh checkout contains no external module
  code, and installing or updating a module does not dirty the EdgeWARN-Core
  worktree.
- Every external module has a declarative manifest and an executable. The base
  package discovers modules from manifests; it never gains a new hard-coded
  import when a module is installed.
- External modules communicate with CTAM through a cycle-scoped internal API.
  Direct writes to `data/stormcells/`, `data/cells/`, indexes, or alerts are
  outside the supported contract.
- The CTAM host owns validation, conflict handling, atomic file replacement,
  history updates, index publication, failure isolation, and audit records.
- StormProb remains bundled in the base CTAM package because its motion output
  is consumed by tracking on later cycles. Its reserved module ID cannot be
  shadowed by an external installation.

"Modify files" in this plan means that a module submits a validated semantic
patch through the internal API and the CTAM host publishes the resulting
stormcell and cell-history files. It does not mean that multiple plugin
processes open and rewrite shared JSON paths. That distinction is required to
avoid lost updates, truncated files, stale indexes, and partial module output.

## Completion definition

The implementation is complete when all of the following are true:

- `/ctam_modules/` is anchored in `.gitignore`, is the only supported
  production discovery root by default, and may be absent or empty.
- Adding a valid module folder makes it discoverable on the next safe reload
  boundary without editing `src/EdgeWARN/ctam/`.
- Removing or disabling an external module cannot prevent StormProb from being
  available as the base module.
- No external module must subclass `AnalysisModule`, register itself at import
  time, or import EdgeWARN implementation modules to perform supported work.
- The versioned internal API reports the current cycle state, CTAM readiness,
  the complete file catalog, per-file availability/validation state, and a
  per-module evaluation of declared requirements.
- A module can read its admitted inputs and transactionally add output beneath
  its owned namespace in the current stormcell snapshot and selected entries
  in `data/cells/<cell-id>.json`.
- Module writes are confined by a positive allowlist to the `modules` and
  `properties` containers of a cell entry, scoped to the caller's own key within
  them. Core identity, geometry, timestamps, tracking fields, another module's
  namespace, and the containers themselves are unreachable by any patch, and no
  manifest or operator grant can widen a module past those two containers.
- Missing optional inputs produce an observable skip/degraded result rather
  than a crash or a false ready state. Missing required modules or inputs
  follow explicit manifest policy.
- A timeout, crash, invalid response, unauthorized patch, or partial commit in
  one external module does not corrupt files or prevent unrelated modules from
  running.
- Stormcell snapshots and cell histories use file-level atomic replacement;
  indexes are published after payloads; interrupted multi-file commits have a
  journaled, tested recovery path.
- Real-time and historical execution use the same API model and pinned
  per-cycle artifact catalog. A module cannot silently select a newer file by
  mtime while processing an older cycle.
- Existing StormProb forecasts, alerts, diagnostics, and downstream tracking
  consumption remain covered by regression tests.
- MorphoWind is deleted from the repository. No base package code imports it,
  registers it, or depends on its output namespace, and the StormProb threat
  field that previously read that namespace has an explicitly decided behavior.
- CTAM documentation contains the manifest schema, API schema, module SDK
  usage, install/update/remove workflow, compatibility policy, and a complete
  example module outside the ignored production directory.

## Current-state evidence

The current implementation is modular only at the Python class level:

- `src/EdgeWARN/ctam/interface.py` defines `AnalysisModule` and
  `GridAnalysisModule`. Implementations receive mutable dictionaries or locate
  raw data themselves.
- `src/EdgeWARN/ctam/modules/__init__.py` imports StormProb and MorphoWind by
  name and registers instantiated classes at import time. Every new module
  therefore requires a base-repository code change.
- `src/EdgeWARN/ctam/registry.py` stores process-global module instances, and
  duplicate names silently overwrite earlier registrations.
- `src/EdgeWARN/ctam/run.py` executes registered modules in-process. A module
  receives the full mutable cell, can import any EdgeWARN implementation, and
  has no declared input, write ownership, protocol version, timeout, or
  lifecycle contract.
- StormProb reads `data/cells/<id>.json` through shared history helpers and reads
  alerts directly. This makes filesystem access part of undocumented module
  behavior.
- `src/EdgeWARN/ctam/modules/StormProb/__init__.py` derives its published
  `tstm_wind` threat from `modules.MorphoWind.severity_index`, defaulting to
  `0.0` when absent. A built-in module therefore depends on another module's
  output namespace with no declared requirement, no ordering guarantee, and no
  distinction between "no wind risk" and "the producing module did not run".
- `properties.morphology` in `src/EdgeWARN/process/detect/tools/save.py` is
  produced by the detection-stage `MorphologyEngine`, not by the CTAM MorphoWind
  module, despite the comment there naming MorphoWind. It is a detection input
  and is unaffected by CTAM module changes.
- Grid results have a separate return convention and may be attached to the
  first storm cell under `_grid_outputs`, which is not an explicit artifact
  contract.
- `src/EdgeWARN/process/integrate/pipeline.py` runs CTAM after enrichment but
  before it saves the integrated stormcell snapshot, updates cell histories,
  and publishes API indexes. `run_ctam()` currently receives cells and a
  timestamp, but not the detection artifact path or `CycleInputManifest`.
- `CycleInputManifest` already records exact staged input paths, product IDs,
  roles, source families, analysis times, and validation state. CTAM should
  reuse it rather than rescan source directories.
- `.gitignore` does not currently ignore `ctam_modules/`.
- Tracking reads `modules.StormProb` from prior cell histories in
  `src/EdgeWARN/process/detect/track.py` and
  `src/EdgeWARN/process/detect/kalman/filter.py`. StormProb is therefore a base
  dependency, not merely an optional example plugin.

## Architectural decisions

### 1. External modules run out of process

Run every external module as a child process. Use a manifest-provided argument
vector and never invoke a shell. The child receives only cycle-scoped API
connection details and a scoped bearer token through environment variables.

This boundary provides:

- language-neutral modules;
- bounded runtime and deterministic termination;
- isolation from process-global registries and Python import state;
- a stable protocol that can evolve independently from internal Python types;
- a place to validate every shared-data mutation before it reaches disk.

This is failure isolation, not a hostile-code sandbox. A locally installed
module runs with the EdgeWARN service account and must still be trusted. The
documentation must state that installing a module grants code-execution
authority. Container, OS user, or seccomp isolation is a later hardening layer,
not a claim made by this interface.

### 2. The internal API is loopback-only and cycle-scoped

For each CTAM execution window, start an HTTP/JSON server on an OS-assigned
port bound only to `127.0.0.1`. Loopback TCP is selected over Unix sockets so
the same contract works on supported Windows installations. Do not add these
routes to either public Express service.

Each launched module receives:

```text
CTAM_API_URL=http://127.0.0.1:<ephemeral-port>/internal/ctam/v1
CTAM_API_TOKEN=<unguessable-cycle-and-module-scoped-token>
CTAM_CYCLE_ID=<canonical-UTC-cycle-id>
CTAM_MODULE_ID=<manifest-id>
```

The token authorizes one module ID, one cycle ID, content reads for declared
selectors, and declared write scopes. Catalog metadata remains visible so a
module can answer which known files do and do not exist; access to file content
is narrower. The token expires when the process exits or the cycle closes. API
logs redact it. Requests without the expected `Authorization: Bearer` header
fail closed.

### 3. The base host remains the sole publisher

API mutation calls stage RFC 6902-style JSON Patch operations in a private
per-module transaction. A successful module explicitly commits. The host then
validates a working copy and merges it into the cycle state. A nonzero exit,
timeout, disconnect, validation failure, or process exit without commit
discards all staged changes and staged alerts for that module.

Initial implementation executes external modules serially in a deterministic,
dependency-sorted order. This makes conflicts and dependencies auditable.
Parallel execution is deferred until write sets are proven disjoint and a
benchmark shows material value.

### 4. StormProb is a reserved built-in

Keep StormProb and its forecasting core in `src/EdgeWARN/ctam/`. Register it
explicitly as the only built-in analysis module; do not discover it through
`ctam_modules/`. Reserve the case-insensitive ID `stormprob` and output key
`StormProb`.

Adapt StormProb to the same host service methods used by the HTTP handlers, but
call those methods in-process to avoid serializing every storm cell and
history entry over loopback. Contract tests must prove the direct adapter and
HTTP API enforce identical read/write rules. StormProb runs before external
modules, so a module may declare `after = ["stormprob"]` and consume its
current-cycle output.

Retain `--disable-ctam` as the explicit compatibility switch that disables
both StormProb and external modules. Add `--disable-ctam-modules` if operators
need to suppress only external modules while preserving StormProb. Do not
silently change the existing flag's meaning.

## Target repository and runtime layout

```text
EdgeWARN-Core/
├── ctam_modules/                         # ignored, operator-installed modules
│   └── <module-id>/
│       ├── module.toml
│       ├── main.py
│       ├── requirements.lock             # module-owned, optional
│       └── ...
├── examples/
│   └── ctam_module/                      # tracked minimal example/template
├── src/EdgeWARN/ctam/
│   ├── api/
│   │   ├── server.py                     # loopback lifecycle/auth/routing
│   │   ├── models.py                     # request/response/schema types
│   │   └── service.py                    # transport-independent operations
│   ├── discovery.py                      # manifest-only external discovery
│   ├── manifest.py                       # TOML parsing and validation
│   ├── readiness.py                      # catalog and requirement evaluation
│   ├── runner.py                         # ordering, subprocesses, timeouts
│   ├── transaction.py                    # staged patches and conflict rules
│   ├── publication.py                    # snapshot/history commit and recovery
│   ├── sdk/                              # small optional Python HTTP client
│   ├── builtins/
│   │   └── stormprob/                    # adapter plus existing StormProb core
│   └── util/
└── tests/fixtures/ctam_modules/           # inert test-only module fixtures

<BASE_DIR>/
├── data/
│   ├── stormcells/
│   ├── cells/
│   └── ctam/
│       ├── cycles/<cycle-id>/status.json
│       ├── transactions/<transaction-id>.json
│       └── quarantine/
└── ...
```

Anchor the ignore rule as `/ctam_modules/`; do not use a broad pattern that
would hide `src/EdgeWARN/ctam/`, test fixtures, or the tracked example. The
runtime state remains below the configured base directory, never the ignored
source module directory.

The module root is resolved from the repository root by default. Add one
explicit override (`EDGEWARN_CTAM_MODULE_DIR` and a matching CLI option) for
packaged deployments, with precedence documented alongside other base-path
settings. Reject a discovery root that is a regular file. Treat a missing root
as an empty external module set, not a startup failure.

## Module manifest contract

Use TOML so Python 3.13 can parse manifests with `tomllib` and module authors
do not need executable registration code. The first schema should support:

```toml
schema_version = 1
id = "cellstats"
name = "CellStats"
version = "1.0.0"
api_version = "1"
enabled = true
required = false
scope = "stormcells"                    # stormcells or cycle
entrypoint = ["{python}", "main.py"]
timeout_seconds = 30
after = ["stormprob"]

[[requires]]
selector = "stormcells.current"
required = true

[[requires]]
selector = "cells.history"
required = true
min_history_entries = 2

[[requires]]
selector = "input:MRMS:MergedReflectivityQCComposite_00.50:current"
required = false
max_age_seconds = 180

[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/modules/CellStats"

[[writes]]
resource = "cells.history"
json_pointer = "/*/modules/CellStats"

# Optional flat scalars for API/index consumers. Must live under `properties`
# and must be prefixed with the module id.
[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/properties/cellstats_severity"
```

Manifest validation rules:

- `id` is stable, lowercase, filename-safe, and unique case-insensitively.
- `schema_version` and `api_version` must be supported before launch.
- `entrypoint` is an argument array. Expand only documented placeholders such
  as `{python}`; allow a configured interpreter or a module-owned virtual
  environment executable, but reject shell operators, payload paths outside
  the module folder, and symlink escapes.
- Dependencies form an acyclic graph and refer to installed modules or the
  reserved `stormprob` ID. Ordering is stable by dependency then ID.
- Requirement selectors are drawn from a documented registry. Unknown product,
  family, role, or resource selectors fail manifest validation rather than
  becoming perpetually unavailable.
- Every `[[writes]]` pointer must resolve inside the `modules` or `properties`
  container of a cell entry, and must name the module's own key within it. A
  pointer that targets any other part of the document fails manifest validation
  at discovery time, before the module is ever launched.
- No write grant, at any level, can widen beyond those two containers. Operator
  base configuration can only broaden ownership *within* `modules`/`properties`
  (for example, permitting one module to update a second declared key). It
  cannot unlock `/id`, `/geometry`, `/centroid`, `/bbox`, `/max_refl`,
  `/event_type`, `/parent_ids`, `/split_from`, timestamps, tracking or Kalman
  state, the `features` array itself, indexes, or internal CTAM metadata.
- Timeouts have bounded minimum and maximum values. Output and request body
  sizes are bounded.
- `required = true` means failure affects the CTAM stage outcome; it does not
  permit partial publication. Optional modules are skipped or failed in an
  observable way while the cycle continues.

Discovery records invalid manifests as disabled with an actionable path and
reason. One invalid optional module must not prevent valid modules or
StormProb from running. Changes are loaded between cycles, never halfway
through a cycle.

## File catalog and readiness model

### Canonical catalog

Build one immutable `CTAMCycleCatalog` from:

- the cycle ID and requested analysis time;
- the pinned `CycleInputManifest` passed into integration;
- the current detection/stormcell artifact path and parsed in-memory cells;
- existing `data/cells/<id>.json` histories for active cells;
- other explicitly admitted runtime products, such as current alerts, when a
  module declares them;
- validation, analysis-time, and readiness information already produced by
  the source coordinator.

Do not populate the catalog through a generic recursive scan. Every item has a
known artifact kind, owner, analysis time, role, and validation rule. This
prevents temp files, stale cache entries, indexes, and unrelated runtime files
from being advertised as usable inputs.

Each file descriptor contains at least:

```json
{
  "file_id": "opaque-stable-within-cycle-id",
  "kind": "input",
  "family": "mrms",
  "product": "MergedReflectivityQCComposite_00.50",
  "role": "current",
  "analysis_time": "2026-08-05T12:00:00+00:00",
  "available": true,
  "validated": true,
  "readiness": "ready",
  "reason": null,
  "size_bytes": 12345,
  "media_type": "application/x-grib2"
}
```

Physical paths are not part of the portable module contract. Modules fetch
JSON or stream/range-read binary content through the API. The optional Python
SDK may materialize a file into a module-private temporary cache for libraries
that require a path. It must not return the shared writable source path.

### State machine

Use explicit cycle states:

```text
catalog_building
      |
      v
requirements_evaluated
      |
      +--> not_ready / failed
      |
      v
stormprob_running -> external_modules_running -> committing
                                                  |
                                      +-----------+-----------+
                                      v                       v
                                  completed                 failed
```

Per-module states are `discovered`, `invalid`, `waiting`, `ready`, `running`,
`committing`, `completed`, `skipped_disabled`,
`skipped_missing_requirements`, `timed_out`, and `failed`.

`ctam_ready` becomes true only after the catalog is frozen, the current
stormcells are available in memory, and every base requirement for StormProb
has been evaluated. It does not imply that every optional source exists.
Every module receives its own `requirements_satisfied` boolean and detailed
list; a missing optional file never masquerades as a ready file.

Persist the final state for observability at
`data/ctam/cycles/<cycle-id>/status.json` with atomic replacement. Status data
contains module versions, durations, requirement outcomes, patch counts,
commit IDs, and redacted errors, but no bearer tokens.

## Internal API v1

Publish and validate an OpenAPI document for
`/internal/ctam/v1`. The minimum resource surface is:

| Method and route | Purpose |
| --- | --- |
| `GET /health` | Protocol version and cycle-scoped liveness; not public service health. |
| `GET /cycle` | Cycle ID, analysis time, CTAM state, readiness, and allowed operations. |
| `GET /files` | Complete cycle catalog metadata, including unavailable items and reasons. |
| `GET /files/{file_id}` | One descriptor with availability and validation reason. |
| `GET /files/{file_id}/content` | Authenticated streaming/range read of an admitted artifact. |
| `GET /requirements` | The caller's declared requirements and current evaluation. |
| `POST /requirements/check` | Re-evaluate only dynamic conditions before work starts. |
| `GET /stormcells` | Current working snapshot after committed predecessor modules. |
| `GET /stormcells/{cell_id}` | One current cell by stable ID. |
| `PATCH /stormcells/{cell_id}` | Stage operations confined to the cell's `modules`/`properties` containers. |
| `GET /cells/{cell_id}` | Read admitted history with timestamp/limit query controls. |
| `PATCH /cells/{cell_id}/entries/{timestamp}` | Stage history-entry operations confined to the same two containers. |
| `POST /alerts` | Stage schema-valid alert payloads owned by the caller. |
| `GET /transaction` | Staged operation count, validation state, and conflict details. |
| `POST /transaction/validate` | Validate without publication. |
| `POST /transaction/commit` | Seal the caller transaction; idempotent by request key. |
| `DELETE /transaction` | Explicitly abandon staged work. |

Responses use a common envelope with `api_version`, `cycle_id`, `module_id`,
`request_id`, `data`, and structured `errors`. Error codes distinguish at
least authentication failure, unsupported version, unavailable file, unmet
requirement, forbidden path, stale revision, conflict, invalid patch, request
too large, and transaction already sealed.

Every mutable resource has a cycle-local revision. A patch includes the
revision observed by the module. If a predecessor changed that resource after
the read, return a stale-revision error; never apply a patch to an unexpected
base. A retry with the same idempotency key returns the original result.

### Patch and ownership rules

A patch never addresses the document root. Both PATCH endpoints accept
operations against a single resolved cell entry, and every operation path is
validated against a positive allowlist. There is no grant, flag, or
configuration that exposes the full JSON structure.

**Two containers, and nothing else.** An operation path must begin with
`/modules/` or `/properties/`. Anything else is rejected with a forbidden-path
error before evaluation:

| Path form | Result |
| --- | --- |
| `/modules/<OwnKey>` and below | Allowed |
| `/properties/<own-prefixed-key>` | Allowed |
| `/modules` or `/properties` (the container itself) | Rejected — cannot replace or clear a container |
| `/modules/<OtherModule>`, `/modules/_grid_outputs` | Rejected — not the caller's key |
| `/id`, `/centroid`, `/bbox`, `/hail_core`, `/max_refl`, `/num_gates`, `/event_type`, `/parent_ids`, `/split_from`, `/geometry`, timestamps, tracking and Kalman fields | Rejected — core identity, geometry, and tracking state |
| `/features/...`, `/`, any array index, any pointer escaping the entry | Rejected — not addressable by a module |

**Key ownership inside the containers.** Container membership is necessary but
not sufficient; the leaf key must also belong to the caller.

- Under `modules`, the caller owns exactly `modules.<display-name>` from its
  manifest. Reserved keys, including `StormProb` for non-StormProb callers and
  the legacy `_grid_outputs`, are never grantable.
- Under `properties`, the caller owns only keys declared in its manifest and
  prefixed with its module id. This matters because `properties` is *shared,
  pre-populated* state: detection and integration write `morphology`, `p95VIL`,
  `p95EchoTop18`, `p95AzShearLow`, and similar enrichment values there, and
  modules and downstream physics read them. Reject any `properties` write whose
  key already exists in the frozen entry and was not written by the same module,
  so a module cannot overwrite a detection input that another module then
  consumes as if it were measured data.
- Prefer `modules` for structured output. `properties` exists for flat scalars
  that API and index consumers need, and its narrower rules reflect the higher
  blast radius of writing into a shared namespace.

Remaining rules:

- Support `add`, `replace`, and `test` initially. Reject `remove`, `move`, and
  `copy` until their provenance and ownership semantics are defined.
- Resolve `cell_id` through an internal index; never interpolate it into a path
  before validating its canonical form and containment.
- Validate the pointer by parsing it into RFC 6901 segments, unescaping `~0`
  and `~1` once, and comparing the decoded segments against the allowlist. Do
  not validate by string prefix and do not pass the pointer through any path
  normalizer: JSON Pointer has no traversal semantics, so a segment such as `..`
  is a literal key name, and treating it as a filesystem path is what turns
  `/modules/Foo/../id` into a write to `/id`. Reject malformed pointers, double
  unescaping, and pointers with a trailing empty segment.
- The common case writes one object below `modules.<display-name>`. Require
  JSON-serializable finite values and enforce depth, field-count, and payload
  size limits.
- A module may update its own previously written namespace in a history entry,
  but cannot rewrite a different module's historical result.
- The host creates a missing `modules` or `properties` container itself when
  first needed. A module cannot create one by patching the container path, so
  container creation can never be used to supply sibling keys.
- Unknown cells and timestamps fail explicitly. Creating a new storm cell,
  inventing a history timestamp, or deleting history is not a v1 operation.
- Grid/cycle modules remain possible through a cycle-scoped output namespace.
  Preserve legacy `_grid_outputs` only through a documented compatibility
  adapter; new modules should publish a named cycle artifact or explicitly
  attach a namespaced summary to stormcells.
- Alerts are not written until the module transaction commits. Alert IDs and
  `source_module` are validated and ownership-scoped.

## Execution and publication flow

Change integration to pass the current artifact and immutable input selection
into CTAM:

```python
run_ctam(
    cells,
    timestamp=timestamp,
    stormcell_path=json_path,
    input_manifest=input_manifest,
)
```

The target flow is:

1. Integration finishes all normal enrichments using the pinned manifest.
2. CTAM freezes a cycle catalog and loads only the active cell histories needed
   by discovered manifests and StormProb.
3. StormProb runs as the reserved base module and commits its namespaced cell
   patches and alerts to the in-memory working set.
4. The runner discovers valid external manifests, evaluates requirements, and
   sorts dependencies.
5. For each runnable module, the host creates a transaction and token, launches
   its argument vector, serves scoped requests, enforces timeout/output limits,
   and accepts or discards the sealed transaction.
6. The host validates the complete working snapshot and pending history edits.
7. Publication writes a transaction journal, writes sibling temporary files,
   validates serialized JSON, atomically replaces each stormcell/history
   target, publishes alerts, and updates indexes last.
8. The journal is marked committed and final cycle/module status is published.

There is no portable filesystem primitive that atomically replaces one
stormcell snapshot and many history files as a group. Do not claim otherwise.
Use a write-ahead journal containing target paths, pre/post hashes, temporary
paths, and commit progress. On startup, recovery validates and rolls forward a
prepared transaction or quarantines it and preserves the last known valid
files. API indexes remain the final discovery commit point. Recovery behavior
must be covered with process-death tests at every replacement boundary.

Refactor the existing integration save/history/index sequence so CTAM output
is published once. Do not let `run_ctam()` write files and then let
`CellHistoryManager` independently append a second version. The publication
coordinator becomes the single owner of current snapshot, current history
entry, module-requested historical patches, alerts, and index ordering.

## MorphoWind removal

MorphoWind is deleted rather than migrated. It is the only optional built-in
module, and keeping it would require either a second bundled production
analytics module inside the base package or an external distribution channel
this project does not have. The base package ships StormProb plus a synthetic
example, and nothing else.

Delete, in one reviewed change:

- `src/EdgeWARN/ctam/modules/MorphoWind/` (`__init__.py`, `morphowind.py`,
  `config.py`, `AGENTS.md`)
- its import, instantiation, registration, and `__all__` entry in
  `src/EdgeWARN/ctam/modules/__init__.py`
- `docs/ctam/modules/MorphoWind/README.md` and its references from
  `docs/ctam/README.md`, `docs/core/integration.md`, `AGENTS.md`, and `GEMINI.md`
- `tests/core/ctam/test_morphowind_physics.py`
- `test_morphowind_module` in `tests/benchmarks/test_performance.py`
- the `"MorphoWind" in cell_module_names` assertion in
  `tests/core/ctam/test_registry.py`

Do not delete `properties.morphology` or the detection-stage `MorphologyEngine`.
Those are unrelated detection outputs; only the misleading MorphoWind comment in
`src/EdgeWARN/process/detect/tools/save.py` should be corrected.

### The StormProb `tstm_wind` coupling must be decided, not dropped

StormProb currently sets `tstm_wind` from `modules.MorphoWind.severity_index`
with a `0.0` default. Deleting MorphoWind makes that threat unconditionally
`"false"`, which is a silent alert-content regression rather than a refactor.
Choose one option explicitly and record it in a release note:

- **Remove the threat field.** StormProb stops publishing `tstm_wind` because it
  has no wind-risk input of its own. Alert consumers and the alert schema are
  updated accordingly.
- **Keep it as a declared optional input.** StormProb reads a documented
  `severity_index` from a named optional module namespace through the host
  service, publishes `tstm_wind` only when that namespace is present, and
  distinguishes absent from `"false"`.

Do not leave the current code shape in place after deletion, where a permanently
missing namespace is indistinguishable from a measured absence of wind risk. The
StormProb tests that inject a `MorphoWind` namespace
(`tests/core/ctam/modules/stormprob/test_module.py`) must be rewritten to match
whichever option is chosen.

## Legacy framework compatibility

During one deprecation window, keep import shims for documented StormProb core
imports. Do not preserve import-time external registration as a hidden fallback.
Emit a targeted error for legacy third-party subclasses explaining how to add
a manifest and use API v1. Remove `interface.py`, `registry.py`, and the generic
`engine.py` after repository callers and tests no longer depend on them.

## Implementation phases

### Phase 0 — Freeze behavior and schemas

- [ ] Add golden fixtures for StormProb current-cell outputs, history reads,
  alerts, skipped states, and error states.
- [ ] Record the current StormProb alert payload with and without a populated
  `modules.MorphoWind.severity_index`, so the `tstm_wind` decision is made
  against measured output rather than assumption.
- [ ] Record the current stormcell and cell-history JSON shapes, including
  inactive cells and duplicate-timestamp replacement behavior.
- [ ] Define JSON Schemas for catalog descriptors, readiness, patch requests,
  transactions, status records, and errors.
- [ ] Check in the OpenAPI v1 document and validate examples against it.
- [ ] Decide and document maximum module count, runtime, request size, patch
  size, history window, and streamed file size defaults.

Acceptance:

- Tests can distinguish a behavior-preserving StormProb migration from a
  changed forecast/alert schema.
- Every API example validates against the checked-in schema.

### Phase 1 — Add manifest discovery and readiness without execution

Files:

- `.gitignore`
- `src/EdgeWARN/ctam/manifest.py`
- `src/EdgeWARN/ctam/discovery.py`
- `src/EdgeWARN/ctam/readiness.py`
- `src/EdgeWARN/process/integrate/pipeline.py`
- `tests/core/ctam/`

Tasks:

- [ ] Ignore only `/ctam_modules/` and add the configurable discovery root.
- [ ] Implement strict TOML manifest parsing, duplicate/reserved ID checks,
  path containment, dependency validation, and stable ordering.
- [ ] Pass `json_path` and `CycleInputManifest` into CTAM.
- [ ] Build the immutable catalog without recursive filesystem discovery.
- [ ] Evaluate required/optional selectors and persist status for dry-run
  inspection.
- [ ] Add `--list-ctam-modules` and `--check-ctam-modules` diagnostics that do
  not execute module code.

Acceptance:

- Dropping a valid fixture folder into a temporary module root discovers it
  without changing base code.
- Missing, invalid, duplicate, cyclic, shadowing, traversal, and symlink-escape
  manifests have deterministic results.
- Readiness accurately changes for present, missing, invalid, stale, wrong-role,
  and wrong-cycle files.

### Phase 2 — Implement the read-only internal API and SDK

Files:

- `src/EdgeWARN/ctam/api/`
- `src/EdgeWARN/ctam/sdk/`
- `docs/ctam/internal-api.md`
- `tests/core/ctam/api/`

Tasks:

- [ ] Implement loopback lifecycle, module/cycle-scoped tokens, version checks,
  request IDs, bounds, redacted structured logging, and graceful shutdown.
- [ ] Implement cycle, catalog, requirement, stormcell, and history reads.
- [ ] Stream admitted content and support HTTP byte ranges.
- [ ] Add an optional dependency-light Python client and temporary
  materialization helper.
- [ ] Generate or validate the OpenAPI document in CI.

Acceptance:

- The server is unreachable through non-loopback interfaces and is absent when
  CTAM is not running.
- A token cannot read a different cycle or fetch content for an undeclared
  file, while catalog metadata still reports that file's availability.
- Binary and JSON reads return the catalogued bytes, not a latest-file rescan.
- The SDK works without importing private EdgeWARN implementation packages.

### Phase 3 — Add transactional mutation and publication

Files:

- `src/EdgeWARN/ctam/transaction.py`
- `src/EdgeWARN/ctam/publication.py`
- `src/EdgeWARN/process/integrate/pipeline.py`
- `src/EdgeWARN/process/integrate/history.py`
- `src/EdgeWARN/api_integration/index_manager.py`
- `src/EdgeWARN/alerts/manager.py`

Tasks:

- [ ] Implement revisioned staging, validation, ownership enforcement,
  idempotent commit, and conflict errors.
- [ ] Implement the `modules`/`properties` pointer allowlist as segment-wise
  validation in one place, shared by the HTTP handlers and the StormProb
  in-process adapter, so neither path can drift into broader access.
- [ ] Stage alerts in the same semantic module transaction.
- [ ] Consolidate stormcell, current history, historical patch, alert, and
  index publication beneath one coordinator.
- [ ] Use atomic replacement for every file and an explicit multi-file journal.
- [ ] Recover or quarantine interrupted transactions before accepting a new
  cycle that touches the same targets.
- [ ] Preserve current history semantics: inactive cells are not refreshed and
  the same timestamp replaces the last entry rather than duplicating it.

Acceptance:

- Unauthorized paths and invalid JSON values never alter the working set.
- A patch targeting anything outside `modules`/`properties` is rejected, and a
  cell's core identity, geometry, and tracking fields are byte-identical before
  and after a cycle in which every module wrote successfully.
- A module cannot reach a sibling key by patching a container path, by traversal
  segments, or by encoded separators.
- A module crash before commit leaves all payloads unchanged.
- Repeating a commit request is idempotent.
- Fault injection before and after every file replacement produces a tested,
  recoverable state; indexes never advertise an unvalidated payload.

### Phase 4 — Add external process execution

Files:

- `src/EdgeWARN/ctam/runner.py`
- `src/EdgeWARN/ctam/run.py`
- `examples/ctam_module/`
- `tests/fixtures/ctam_modules/`

Tasks:

- [ ] Launch manifest entrypoints without a shell using a module-private working
  directory and minimal documented environment additions.
- [ ] Capture bounded stdout/stderr with module-prefixed logs; prevent pipe
  backpressure from hanging the service.
- [ ] Enforce timeout, terminate, bounded wait, and force-kill only the known
  child when needed. Never kill by name or broad process match.
- [ ] Run modules in dependency order and expose predecessor revisions.
- [ ] Distinguish skip, failure, timeout, invalid commit, and success in cycle
  outcome and metrics.
- [ ] Add the tracked synthetic example and installation documentation.

Acceptance:

- Installing/removing a fixture changes only the discovered external set.
- Hanging, crashing, noisy, malformed, oversized, and non-committing fixtures
  cannot corrupt data or block later optional modules indefinitely.
- A required-module failure marks CTAM degraded/failed according to policy;
  optional failures remain isolated and visible.

### Phase 5 — Move StormProb onto the host service boundary

Files:

- `src/EdgeWARN/ctam/modules/StormProb/` (move/adapt)
- `src/EdgeWARN/ctam/builtins/stormprob/`
- `src/EdgeWARN/ctam/run.py`
- `src/EdgeWARN/process/detect/track.py`
- `src/EdgeWARN/process/detect/kalman/filter.py`
- `tests/core/ctam/modules/stormprob/`
- `tests/integration/`

Tasks:

- [ ] Preserve StormProb core public imports through deprecation shims while
  moving its adapter to the built-in runner.
- [ ] Replace direct history/alert file access with host service calls.
- [ ] Reserve its ID and execute it before external modules.
- [ ] Preserve forecast, diagnostic, status, alert, and summary logging shapes.
- [ ] Verify that prior-cycle StormProb velocity remains available to tracking
  and Kalman prediction after history publication.
- [ ] Add `--disable-ctam-modules` while retaining `--disable-ctam` behavior.

Acceptance:

- Golden StormProb tests match the frozen baseline or document an intentional
  correction separately.
- An empty/missing external module root still produces StormProb output.
- An external `stormprob` manifest is rejected and cannot shadow the built-in.
- A two-cycle integration test proves cycle N StormProb output is consumed by
  tracking in cycle N+1.

### Phase 6 — Delete MorphoWind and remove the legacy framework

Files:

- `src/EdgeWARN/ctam/modules/MorphoWind/` (delete)
- `src/EdgeWARN/ctam/modules/__init__.py`
- `src/EdgeWARN/ctam/modules/StormProb/__init__.py`
- `src/EdgeWARN/ctam/interface.py`
- `src/EdgeWARN/ctam/registry.py`
- `src/EdgeWARN/ctam/engine.py`
- `src/EdgeWARN/process/detect/tools/save.py` (comment correction only)
- `docs/ctam/`
- affected tests

Tasks:

- [ ] Decide the `tstm_wind` question, implement it in StormProb, and write the
  release note before deleting MorphoWind.
- [ ] Delete the MorphoWind package, its import-time registration, its docs, and
  its tests as listed in the MorphoWind removal section.
- [ ] Correct the MorphoWind comment in `save.py` without changing
  `properties.morphology` behavior.
- [ ] Migrate grid-module behavior to the cycle-scoped API or explicitly
  deprecate unsupported legacy behavior with a release note.
- [ ] Replace repository tests that assert registry internals with discovery,
  readiness, API, transaction, and process-contract tests.
- [ ] Remove legacy framework files after their supported deprecation window;
  do not leave two production execution paths.

Acceptance:

- `rg -i morphowind` finds no production code, test, or documentation reference
  except an intentional release note or changelog entry.
- `rg` finds no production auto-registration or hard-coded optional module
  import.
- A clean checkout runs StormProb normally with an absent or empty
  `ctam_modules/`, and detection-stage `properties.morphology` is byte-identical
  to the Phase 0 baseline.
- StormProb alert payloads match the decided `tstm_wind` behavior, and no test
  fixture injects a `MorphoWind` namespace to obtain a passing assertion.
- No optional production analytics module source lives inside the base CTAM
  package.

### Phase 7 — Documentation, operations, and performance gate

- [ ] Rewrite `docs/ctam/README.md` around discovery and the internal API.
- [ ] Document trust, authentication scope, version negotiation, timeouts,
  file selectors, readiness states, write ownership, conflict handling,
  transactions, recovery, and troubleshooting.
- [ ] Document module virtual-environment choices and dependency isolation.
- [ ] Add operator commands to validate a module before enabling it and inspect
  the last cycle status without executing code.
- [ ] Add protocol compatibility tests for the current and immediately previous
  supported API version.
- [ ] Benchmark no-external-module, StormProb-only, and representative external
  module cycles for latency, memory, serialization, and file I/O.
- [ ] Set a measured regression budget before making the new runner default.

Acceptance:

- A module author can build the example using only public CTAM documentation
  and the SDK/OpenAPI contract.
- Operators can explain why a module did not run from one status record.
- The default path does not start an API server or child process when CTAM is
  disabled, and an empty external root adds negligible overhead beyond the
  StormProb adapter.

## Test matrix

### Unit and schema tests

- Manifest versions, IDs, entrypoints, dependencies, selectors, write scopes,
  timeout bounds, duplicates, reserved names, traversal, and symlink escapes.
- Catalog descriptors and exact `CycleInputManifest` correspondence.
- Requirement evaluation for required/optional, file existence, validation,
  role, timestamp, age, and history-count constraints.
- Authentication, token scope/expiry, version negotiation, range reads, size
  bounds, error envelopes, redaction, and server shutdown.
- Patch operations, namespace ownership, immutable paths, revisions,
  conflicts, idempotency, finite JSON values, and payload limits.
- Pointer allowlist enforcement as a dedicated table-driven test: accepted
  `/modules/<OwnKey>` and `/properties/<own-prefixed-key>` paths; rejected
  container paths, other modules' keys, reserved keys, every core identity and
  geometry field, `/features` and root paths, array indices, traversal segments,
  encoded separators, and malformed pointers. The same table runs against both
  the HTTP handlers and the StormProb in-process adapter.
- Rejection of a `properties` write whose key already exists in the frozen entry
  and belongs to detection or another module.
- Journal transitions, hashes, recovery, quarantine, and index-last ordering.

### Process-contract tests

- Successful read/patch/commit module.
- Disabled and missing-requirement module.
- Nonzero exit, timeout, signal death, excessive output, malformed request,
  invalid patch, forbidden patch, stale revision, double commit, and exit
  without commit.
- Dependency ordering, missing dependency, dependency failure, and cycle.
- Hot install/remove between cycles with no mid-cycle reload.

### Integration tests

- Real-time pipeline with no external directory, empty directory, one module,
  multiple dependent modules, and one failing optional module.
- Historical processing where file availability is evaluated against the
  requested historical cycle rather than current filesystem recency.
- Current stormcell namespace and current/history cell entries contain the
  same committed module output after publication.
- Inactive-cell history mtime and contents remain unchanged.
- API indexes expose only committed artifacts.
- Two modules cannot overwrite each other or a core field.
- A module cannot corrupt a detection enrichment value that later physics reads:
  a fixture attempting to write `properties.p95VIL` is rejected, and the value
  StormProb reads is unchanged.
- StormProb output parity, alert parity, and next-cycle tracking use.
- `--disable-ctam` and `--disable-ctam-modules` behavior.
- Windows and Linux loopback launch, token propagation, termination, and path
  handling.

### Fault injection and recovery tests

- Service death after journal prepare, after each temporary file write, after
  each replace, before alert publication, before index publication, and before
  journal completion.
- Disk full, permission error, corrupt prior history, corrupt journal, and
  module output exceeding limits.
- Restart proves either the last valid generation remains readable or the
  prepared transaction is safely rolled forward; no index points at missing or
  invalid JSON.

## Documentation changes

Update:

- `docs/ctam/README.md`
- `docs/core/integration.md`
- `docs/core/README.md`
- `INSTALLATION.md`
- `AGENTS.md` and `GEMINI.md` module inventories
- CLI help generated from `src/util/io.py`

Remove:

- `docs/ctam/modules/MorphoWind/README.md`

Add:

- `docs/ctam/internal-api.md`
- `docs/ctam/module-manifest.md`
- `docs/ctam/module-development.md`
- `docs/ctam/module-operations.md`
- the versioned OpenAPI and JSON Schema artifacts

Documentation must clearly separate the private loopback API from the public
EdgeWARN and EWMRS REST APIs. It must also state that gitignore is not a module
installer, module code is trusted executable code, and the runtime base
directory remains the source of truth for generated artifacts.

## Rollout and rollback

1. Land schemas, catalog, and dry-run discovery behind a disabled feature flag.
2. Land the read-only API and synthetic example; run it in CI only.
3. Land transactions/publication and execute a shadow module whose patches are
   validated but discarded; compare proposed output with current output.
4. Move StormProb to the host service boundary and verify two-cycle tracking.
5. Ship the decided StormProb `tstm_wind` behavior and its release note, then
   delete MorphoWind in a separate reviewed change.
6. Enable external modules by default when present; an absent folder remains a
   valid StormProb-only installation.
7. Remove the legacy registry after one release of warnings and passing
   compatibility/performance gates.

Rollback disables external discovery and returns to the built-in StormProb
adapter. It must not restore hard-coded optional-module imports. Keep the
transaction recovery reader for at least one additional release so prepared
work from the new publisher can be resolved safely after a downgrade.

## Final verification checklist

- [ ] `/ctam_modules/` is ignored and tested as the default external root.
- [ ] External discovery requires no base import or registry edit.
- [ ] StormProb is present, reserved, regression-tested, and used by later
  tracking.
- [ ] API v1 exposes file inventory, file availability, CTAM readiness, and
  per-module requirement readiness.
- [ ] External modules read admitted artifacts only through the supported API.
- [ ] Stormcell and cell-history modifications are namespaced, revisioned,
  validated, transactional, and host-published.
- [ ] Patches can only reach the `modules` and `properties` containers of a cell
  entry and only the caller's own key inside them. No path, grant, or
  configuration exposes the wider JSON structure.
- [ ] Missing inputs, module failures, timeouts, and commit failures are
  isolated and observable.
- [ ] Exact cycle inputs are pinned in real-time and historical processing.
- [ ] File publication is atomic per target, journal-recoverable across
  targets, and indexed last.
- [ ] MorphoWind is fully deleted from code, tests, and docs, and the StormProb
  `tstm_wind` behavior change is deliberate and released.
- [ ] Legacy registry execution is gone rather than retained as a competing
  production path.
- [ ] Docs, schemas, fixtures, tests, and performance evidence cover the full
  contract.
