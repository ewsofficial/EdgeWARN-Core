# Configurable MRMS ingestion: architecture and implementation plan

Status: implemented through Phase 8, with offline release qualification recorded
in [migration and qualification](../docs/core/configurable-mrms-phase8.md).
Deployment-specific operational verification remains an operator responsibility.

## 1. Outcome and scope

An operator adds MRMS products to `config/ingest.yaml`, restarts the services,
and the ingestor automatically creates their raw-data directories and exposes
their paths. Core's critical input set is always included. External CTAM modules
declare their dependencies and startup fails with an actionable message when
their required product is not enabled. Enabled StormProb has a separate
all-inputs gate: missing dependencies produce a warning and terminate the whole
Core process with a nonzero exit. An explicit option disables StormProb and its
dependency gate. Every downstream reader checks ingestion eligibility before
looking for files.

The requested naming rule is exact:

```text
Configured product: MRMS_MergedAzShear_3-6kmAGL_00.50
Path name:          MRMS_MergedAzShear_3-6kmAGL
Directory:          <base-dir>/data/MRMS_MergedAzShear_3-6kmAGL
```

Design decisions:

- Reserve only inputs critical to the Core process: composite reflectivity,
  precipitation type, and ProbSevere. Enrichment and StormProb dependencies
  remain configurable; needing a product for StormProb does not reserve it.
- Operator products are additions to the protected set. An empty list means
  Core-only MRMS ingestion, not no MRMS ingestion. Running that configuration
  requires disabling StormProb and any external modules with unmet inputs.
- Preserve full upstream product identities, including elevation, in requests,
  manifests, dependency selectors, and download results. Strip elevation only
  for the path name.
- Support the existing CONUS GRIB2 product family and the existing special
  ProbSevere adapter. New standard CONUS products need no source-code entry.
  Other regions or encodings require an explicit future adapter.
- Keep RAP/GLM configuration and service ownership as they are. Evaluate
  StormProb dependencies separately from the reserved MRMS set, including
  non-MRMS sources, derived features, and model assets.
- Adding raw ingestion does not automatically create a renderer, colormap,
  integration statistic, or public API product.

## 2. Findings from the current implementation

These are source findings, not observations from a live weather cycle.

| Current owner | Finding and consequence |
| --- | --- |
| `config/ingest.yaml` | Contains 21 products, editable discovery checks, editable detection membership, and manually assigned `outdir` names. |
| `src/common/ingest/mrms/config.py` | Resolves each `outdir` with `getattr(util.file, ...)`; a new YAML entry cannot create a new path variable. |
| `src/util/file.py` | `_define_paths()` manually binds raw MRMS directories. `initialize_filesystem()` currently rebinds paths; it does not create every product directory. |
| `src/common/ingest/mrms/main.py` | Defines integration products as every enabled product outside detection. An unrelated optional download therefore participates in Core integration readiness. |
| `src/common/pipeline/coordinator.py` | Awaits detection, integration, RAP, and optional GOES results before emitting the current callbacks. Separating boolean gates alone will not eliminate the wait on additional MRMS products. |
| `src/common/ingest/mrms/downloader.py` | Provides structured batch results, S3-to-HTTPS fallback, and staged input records. ProbSevere has manifest identity `ProbSevere`, though the source modifier is `None`. |
| `src/EdgeWARN/process/integrate/config.py` and `pipeline.py` | Resolve path aliases and select pinned files by directory; default statistics consume 14 distinct MRMS products beyond the three detection inputs. |
| `src/EWMRS/render/config.py`, `src/EWMRS/pipeline.py`, `src/util/runtime/ewmrs_consumer.py` | Rendering scans each layer's local directory. `mrms-ready` is a best-effort cycle trigger, not an all-products barrier. Preserve this behavior despite older comments suggesting otherwise. |
| `src/EdgeWARN/ctam/manifest.py` | Already supports `[[requires]]` selectors. Product validation currently depends on the active catalog, excludes ProbSevere's null modifier, and can skip validation when catalog loading fails. |
| `src/EdgeWARN/ctam/discovery.py` and `run.py` | Discovery and execution errors can be converted to module statuses or caught. A fatal missing-ingestion prerequisite must be checked before those per-cycle isolation paths. |
| `src/common/config/loader.py`, `src/config/loader.js` | Both use a restricted schema walker. Do not introduce unsupported `$ref`, `oneOf`, or `format` keywords without implementing them in both runtimes. |

Relevant existing flow and constraints are documented in
[`docs/core/ingestion.md`](../docs/core/ingestion.md). Shared ingest belongs in
`common.ingest`; `EdgeWARN.ingest` remains a compatibility surface.

## 3. Protected Core contract

Create `src/common/ingest/mrms/core_contract.py` with immutable definitions,
an explicit contract version, and no dependency on operator configuration,
`util.file`, EWMRS, or CTAM. Use frozen dataclasses and tuples/frozensets.

The reserved (protected) set contains only the following **three products**
critical to detection and the Core cell-processing flow. A product is not
reserved merely because it supplies a default enrichment statistic or a
StormProb feature.

| Phase | Upstream product / manifest identity | Core consumer |
| --- | --- | --- |
| Detection | `MergedReflectivityQCComposite_00.50` | Reflectivity detection and previous-scan comparison |
| Detection | `PrecipFlag_00.00` | Precipitation classification |
| Detection | `ProbSevere` | Detection polygons and ProbSevere enrichment; special JSON adapter |

The following **14 enrichment products are configurable additions**, enabled
by default but removable by the operator. Missing inputs skip the affected
statistics and are evaluated separately by any dependent module. If enabled
StormProb requires them, its fatal policy in section 7 applies.

| Use | Upstream product / manifest identity | Enrichment consumer |
| --- | --- | --- |
| Integration | `Reflectivity_0C_00.50` | `Ref0` |
| Integration | `Reflectivity_-5C_00.50` | `Ref5` |
| Integration | `Reflectivity_-15C_00.50` | `Ref15` |
| Integration | `NLDN_CG_001min_AvgDensity_00.00` | `maxCGFlashDensity` |
| Integration | `EchoTop_18_00.50` | EchoTop18 statistics |
| Integration | `EchoTop_30_00.50` | EchoTop30 statistics |
| Integration | `EchoTop_50_00.50` | EchoTop50 statistics |
| Integration | `VIL_00.50` | VIL statistics |
| Integration | `VIL_Density_00.50` | VIL-density statistics |
| Integration | `MergedAzShear_0-2kmAGL_00.50` | Low-level AzShear statistics |
| Integration | `MergedAzShear_3-6kmAGL_00.50` | Mid-level AzShear statistics |
| Integration | `PrecipRate_00.00` | `maxPrecipRate` |
| Integration | `MergedReflectivityAtLowestAltitude_00.50` | `maxRALA` |
| Integration | `VII_00.50` | `maxVII` |

Use only the three reserved products as the scan-discovery readiness subset.
The existing ten-product discovery set is a baseline to migrate, not a new
requirement: retaining its enrichment products would let optional inputs block
Core before download scheduling even begins.

The 14 enrichment products plus `RadarQualityIndex_00.00`, `MESH_00.50`,
`RadarOnly_QPE_01H_00.00`, and `MergedRhoHV_00.50` form **18 shipped optional
additions**. The default configuration still downloads the existing 21 product
identities. `mrms.products: []` downloads only the three reserved products.
“Optional” here means not reserved for the base Core process. Enabled StormProb
and external modules may impose additional deployment requirements without
automatically enabling or reserving products. Missing StormProb requirements
are fatal to Core unless StormProb is explicitly disabled.

Protection must cover more than list membership:

1. Code owns protected product identity, source region, adapter, phase,
   discovery membership, path derivation, and requiredness.
2. Move MRMS bucket names, filename/key templates, HTTPS endpoint definitions,
   and exceptional upstream directory mappings into a code-owned source
   adapter. Editable shared source templates must not redirect or break
   protected downloads. This intentionally changes their current ownership.
3. Reject attempts to supply `outdir`, `enabled: false`, phase overrides,
   discovery overrides, or protected source overrides. Do not silently ignore
   obsolete configuration keys.
4. Keep validated operational settings such as retention, chunk sizes, and
   network limits configurable. They cannot remove a protected download or
   invalidate the minimum history needed by active consumers.
5. Keep the protected set enabled under `--disable-ctam`,
   `--disable-ctam-modules`, historical execution, and `mrms-core-only`.
   The last flag retains its existing RAP/GLM meaning.

If a future change adds or removes a genuinely critical Core input, update
this contract, its consumer mapping, and its regression tests together. Changes
to optional statistics or model features update their consumer dependencies.
Editing `integration.yaml` alone must never redefine the protected set.

## 4. Operator configuration and validation

Change only the ingest document's schema version to 2; other catalog documents
retain their own versions. Proposed MRMS portion of `config/ingest.yaml`:

```yaml
schema_version: 2
mrms:
  # Additional products. Core's three reserved products are always included.
  products:
    - MRMS_Reflectivity_0C_00.50
    - MRMS_Reflectivity_-5C_00.50
    - MRMS_Reflectivity_-15C_00.50
    - MRMS_NLDN_CG_001min_AvgDensity_00.00
    - MRMS_EchoTop_18_00.50
    - MRMS_EchoTop_30_00.50
    - MRMS_EchoTop_50_00.50
    - MRMS_VIL_00.50
    - MRMS_VIL_Density_00.50
    - MRMS_MergedAzShear_0-2kmAGL_00.50
    - MRMS_MergedAzShear_3-6kmAGL_00.50
    - MRMS_PrecipRate_00.00
    - MRMS_MergedReflectivityAtLowestAltitude_00.50
    - MRMS_VII_00.50
    - MRMS_RadarQualityIndex_00.00
    - MRMS_MESH_00.50
    - MRMS_RadarOnly_QPE_01H_00.00
    - MRMS_MergedRhoHV_00.50
  remove_old_files: true
  cleanup_max_age_minutes: 60
  decompress_chunk_size_bytes: 1048576
  downloads:
    max_concurrency: 8
    optional_timeout_seconds: 30
  ncep_https:
    sync_timeout_seconds: 10
    match_window_seconds: 120
    download_chunk_size_bytes: 8192

# Existing goes section follows unchanged.
```

The concurrency and optional timeout values above are proposed initial defaults;
validate them with the latency regression workload before release. There is one
aggregate MRMS concurrency budget, with scheduling priority for protected inputs.

An operator can include
`MRMS_MergedReflectivityQCComposite_00.50` in `products`; it deduplicates
against the reserved definition and is reported as reserved. Removing that
entry does not disable it. Removing `MRMS_MergedAzShear_3-6kmAGL_00.50`
does disable that optional input and affects its enrichment/StormProb consumers.
Exact duplicates within the operator list are errors.

Validation rules:

- Standard configured IDs have exactly one leading `MRMS_`, preserve case, and
  end in an underscore followed by a two-digit/two-decimal elevation token.
  Permit letters, numbers, internal underscores, and hyphens in the base name.
  Bound total ID length to fit existing CTAM product limits after normalization.
- Internally remove the leading `MRMS_` once to obtain the existing upstream
  modifier/manifest product ID. Do not strip the elevation there.
- Handle `MRMS_ProbSevere` explicitly: manifest ID `ProbSevere`, region
  `ProbSevere`, source modifier `None`, JSON decoding, no elevation suffix.
- `products: []` is valid. Removed `check_products`, `membership_lists`,
  `outdir`, source-template, and endpoint keys produce migration instructions.
- Reject unsafe path segments, control characters, slashes, backslashes,
  traversal, malformed elevation suffixes, repeated prefixes, and unknown
  configuration fields.
- Reject collisions in the generated path names across the full effective
  set, including case-folded collisions for Windows. Two elevations of the
  same base product cannot share the requested directory. Report both IDs;
  do not silently merge them or invent an elevation subdirectory.
- New syntactically valid CONUS products are admitted without maintaining a
  static allowlist. Remote absence is a per-cycle availability result, not proof
  that configuration syntax is invalid. Startup validation requires no network.

Implement structural and pure semantic validation in both configuration
loaders, including the configuration editor's in-memory validation path.
Use identical valid/invalid fixtures for Python and Node. Put the Python pure
normalization rules in `common.config.mrms_products`, not in a module that
imports `util.file`. The JavaScript counterpart only needs normalization and
collision checks; it does not download products or create directories.

Cross-check collisions with protected IDs using a release-owned contract
fixture generated from `core_contract.py` and tested for parity. Package that
fixture for Node rather than copying an independently maintained second Core
list. It is application data, not part of the operator configuration tree.

## 5. Registry, generated paths, and dependency direction

Introduce these responsibilities:

| Module | Responsibility |
| --- | --- |
| `common.config.mrms_products` (new) | Pure product-ID parsing and path-name derivation; no filesystem mutation or domain imports |
| `common.ingest.mrms.core_contract` (new) | Protected definitions, phases, discovery set, legacy aliases |
| `common.ingest.mrms.registry` (new) | Merge protected and configured definitions into one immutable effective registry |
| `common.ingest.mrms.source` (new) | Code-owned S3/HTTPS source grammars, endpoints, adapter exceptions |
| `util.file` | Bind registry paths and legacy path aliases; explicitly create enabled directories at startup |
| `common.ingest.mrms.config` | Compatibility accessors backed by the registry |
| `common.ingest.mrms.main` / `downloader` | Execute explicit product subsets using existing acquisition implementations |
| `common.pipeline.coordinator` | Validate per-phase completion and publish immutable input snapshots |
| `EdgeWARN.ctam.preflight` (new) | Compare active module declarations with ingestion eligibility |

Data model sketch; names are proposed interfaces:

```python
@dataclass(frozen=True)
class MrmsProductSpec:
    configured_id: str      # MRMS_MergedAzShear_3-6kmAGL_00.50
    product_id: str         # MergedAzShear_3-6kmAGL_00.50; ProbSevere special
    source_modifier: str | None
    region: str
    adapter: str            # conus_grib2 or probsevere_json
    path_name: str          # MRMS_MergedAzShear_3-6kmAGL
    directory: Path
    protected: bool
    core_phase: str | None  # detection for reserved products; otherwise None

class MrmsRegistry:
    def is_enabled(self, product_id: str) -> bool: ...
    def require(self, product_id: str) -> MrmsProductSpec: ...
    def path_for(self, product_id: str) -> Path: ...
    def for_phase(self, phase: str) -> tuple[MrmsProductSpec, ...]: ...
    def paths_by_name(self) -> Mapping[str, Path]: ...
```

Derivation algorithm:

```python
# Validate the complete ID first, then strip only the final elevation token.
base_product, elevation = source_modifier.rsplit("_", 1)
path_name = f"MRMS_{base_product}"
directory = resolved_base_dir / "data" / path_name
```

For example, preserve `EchoTop_18`, `RadarOnly_QPE_01H`, and
`MergedAzShear_3-6kmAGL` in full. Splitting at the first underscore or at the
current HTTPS `_00.` token is not a safe general naming algorithm. The HTTP
adapter may use the same parsed base product as its default remote directory,
with explicit exceptions; it must not derive remote identity from local paths.

Expose exact path names through an immutable mapping and module attributes:

```python
import util.file as fs

path = fs.MRMS_PATHS["MRMS_MergedAzShear_3-6kmAGL"]
same_path = getattr(fs, "MRMS_MergedAzShear_3-6kmAGL")
assert path == same_path
```

The requested hyphenated name is not a legal Python assignment/import
identifier. Exact-name dictionary keys and `getattr` preserve the requested
contract; do not advertise `fs.MRMS_MergedAzShear_3-6kmAGL` as valid access.
Names containing only identifier characters may also use normal attribute
syntax. Do not generate Python source files or use `eval`/`exec`.

Prefer module `__getattr__` backed by the current path mapping, including
legacy aliases. This avoids stale dynamically assigned globals when tests or
programmatic callers reinitialize with a different base directory or product
set. A disabled product must disappear from the active mapping. Convert new
consumers to registry lookup; retain old `MRMS_*_DIR` aliases for a documented
compatibility period, resolving to the new canonical location when enabled.

Build the registry only after resolving the effective config root and base
directory. Its builder takes frozen configuration and a base path explicitly;
it must not import `util.file`, CTAM, or EWMRS. Keep catalog loading independent
of path binding to avoid the existing `util`/`EdgeWARN` import-cycle hazards.

Separate `bind_mrms_paths(registry)` from `ensure_mrms_directories(registry)`.
After all preflight checks pass, create each enabled directory with
`mkdir(parents=True, exist_ok=True)`. Fail startup clearly if creation fails.
Check resolved containment beneath `<base-dir>/data`, including symlink
escapes. No directory creation occurs during import, config validation, module
listing, or dry runs. Consumer processes bind paths but do not need to create
raw download directories.

Freeze a registry generation/fingerprint over product definitions, Core
contract version, and normalized config. Pass serializable specs to spawned
children, or rebuild from the resolved configuration and verify the fingerprint.
Configuration changes require restart; no implicit mid-cycle reload.

## 6. Startup, acquisition, and cycle architecture

```text
resolve config root, base directory, CLI/profile/CTAM flags
                    |
validate catalog + normalize configured products
                    |
protected Core contract + operator additions -> immutable registry
                    |
read active CTAM manifests + validate downstream dependency declarations
                    |
fatal errors? -> diagnostic with missing IDs and YAML fix; exit nonzero
                    |
bind paths -> create enabled raw directories -> start services/workers
                    |
scan discovery from protected discovery subset
                    |
requested UTC cycle T + frozen registry
        +-----------+-----------------+-------------------+
        |           |                 |                   |
   detection    optional enrichment   other additions   RAP / GLM
        |           |                 |              existing owners
        +-----------+-----------------+-------------------+
                    |
validated immutable snapshots and phase-specific results
        +-----------+-----------------+-------------------+
        |           |                 |                   |
   detection    EWMRS trigger     Core integration     CTAM input snapshot
    worker      per-layer scan    exact pinned inputs  declared dependencies
                    |
existing output publication, indexes, service gates, API v3
```

### Acquisition and results

1. Replace complement-based mandatory integration membership with the three
   explicit reserved detection specs. Download configured enrichment and other
   additions as optional-to-Core work; there is no reserved integration batch.
2. Resolve paths once through the registry. Migrate sync, async, direct/all-files,
   historical, and HTTPS fallback entry points together. Existing modifier
   triple accessors can adapt the registry during migration.
3. Preserve S3 lookup, timestamp selection, HTTPS fallback after listing/fetch/
   processing failures, decompression, semantic validation, and atomic rename.
   Every requested ID must appear in the result even if no source file exists.
4. Keep `DownloadBatchResult.successful` strict for nonempty mandatory batches.
   Represent an intentionally empty optional batch as `not_requested`; never
   let an empty attempted set prove required-phase readiness.
5. Maintain a product result map with at least `not_requested`, `pending`,
   `unavailable`, `failed`, and `ready`; consumers add `consumed`/`published`
   outcomes at their own boundaries. Include requested UTC time, source,
   selected analysis time, path, failure reason, and registry fingerprint.
6. Bound queued work and total concurrency as product counts grow. Reserve
   service for protected inputs; optional batches cannot consume every slot
   while protected work waits. Bound sync fallback too, including underlying
   network timeouts. Cancelling `asyncio.to_thread()` alone does not stop its
   worker or prevent a late filesystem write.
7. Deduplicate by product identity and selected observation/source identity.
   Validate an existing local file before reuse. Never overwrite a file pinned
   by an active cycle with different bytes. Reject and log conflicting content,
   or stage it under a distinct versioned identity before future adoption.
8. Quarantine malformed payloads with product/source/time/reason metadata in a
   bounded runtime quarantine directory. Keep quarantine and temporary files
   outside consumer globs. Fail the product explicitly; do not publish invalid
   data as a successful download.

### Readiness and timing

Readiness is computed against expected product IDs from the frozen contract,
not against whichever records happened to arrive:

```text
detection_ready = all protected detection IDs have valid current records
base_ready = detection_ready AND existing RAP requirement
integration_ready = base_ready AND existing scan-time GLM requirement
```

There is no all-enrichment-products MRMS barrier. An optional failure changes
that product's status, not any of these equations. Enabled StormProb evaluates
its own fatal dependency gate after bounded acquisition/enrichment completes;
these Core readiness equations do not authorize bypassing that gate.
Alignment errors must be scoped to the phase's expected IDs; optional errors
must not contaminate a family-wide Core error check.

Refactor the coordinator's existing up-front `gather` so the detection callback
can receive a frozen detection snapshot when that batch completes. Publish the
EWMRS trigger after the detection phase notification, then release Core
integration when its own prerequisites are terminal and valid. Preserve callback
order and explicit failure releases so worker events cannot hang.

Optional downloads run concurrently under the same bounded acquisition manager.
Give the optional batch a deadline measured from its start, not an unbounded
wait after Core finishes. Do not await that batch before detection or Core
integration callbacks. The coordinator drains/cancels and joins owned tasks
before returning; the next scan may incur at most that bounded optional work,
not indefinite optional retry. Track this separately from Core readiness latency.

For CTAM, add a distinct optional-input-completion signal/snapshot in
`util.runtime.cycle` and `EdgeWARN.pipeline.edgewarn_cycle_worker`. Core
integration starts immediately after its existing event. Optional enrichment
uses ready records, then completes a bounded final enrichment pass from the
terminal optional snapshot before dependent CTAM/StormProb execution. Merge
only successful property patches; never reselect the reserved inputs or repeat
Core publication. CTAM reads the final enriched snapshot only after the bounded
optional batch is terminal. External module requirements determine which modules
can execute; an enabled StormProb dependency failure exits Core as specified
in section 7. When CTAM is disabled, retain bounded completion for configured
enrichment, but omit the CTAM execution wait/event. Historical callers use the final returned
snapshot and the same deadline rules.

Do not repeatedly mutate a manifest already given to a worker. Store separate
serialized detection, integration, and CTAM snapshots before their respective
events. Merge successful optional records into the CTAM snapshot without
reselecting Core inputs. Publish durable `mrms-ready` only once per cycle;
late optional arrivals are eligible for the next EWMRS per-layer scan.

Preserve the current handoff wire format and RAP exact-path behavior unless an
explicit versioned extension is necessary. Persist optional statuses and the
registry fingerprint in a separate versioned cycle ingest report, rather than
silently adding fields to strict handoff readers. Reports are atomic and
idempotent; incompatible retries do not rewrite committed snapshots.

Pin current and previous inputs by encoded UTC analysis time, never mtime.
Expand previous-input selection beyond detection when CTAM declares a
`previous` requirement. Protect active pinned files from cleanup across child
processes; use cycle-owned references or an explicit lease/pin set. Previous
requirements need sufficient count/age retention, validated during preflight.
At cold start, missing previous data is a runtime readiness condition, not a
missing-product configuration error.

## 7. CTAM registration and fatal prerequisite validation

Reuse `[[requires]]`; do not add a second MRMS registration list that can drift.
Example registration in an external module's `module.toml`:

```toml
[[requires]]
selector = "input:MRMS:MESH_00.50:current"
required = true
max_age_seconds = 180
```

The selector uses the existing unprefixed, elevation-bearing manifest ID.
Its configuration counterpart is `MRMS_MESH_00.50`.

For external CTAM modules, implement two distinct checks:

1. **Startup eligibility:** a declared required product must be enabled in the
   effective registry. Missing eligibility is a fatal configuration error.
2. **Per-cycle availability:** an enabled required product must have a usable
   pinned record satisfying role and age constraints. A source outage blocks
   that module for that cycle under existing module failure policy; it does
   not terminate the ingestor or disable unrelated Core work.

Change `parse_selector()` so syntax parsing is independent of enabled-product
membership. Move eligibility checks into `ctam.preflight`; otherwise a missing
input can make discovery discard the module before the fatal check sees it.
Do not swallow configuration-loader errors. Include the special `ProbSevere`
identity in registry resolution.

Make `requires` explicitly present for all enabled external modules: authors
use `requires = []` when no host inputs are needed, or declare table entries.
Existing manifests with declarations remain compatible. A missing declaration
gets a migration error, rather than an implicit empty list. A syntactically
valid but misspelled product gets a missing-product diagnostic with nearby
enabled IDs; do not claim offline validation proves an upstream product exists.

Fatal behavior applies to required inputs of **every enabled external module**, even
when the module's top-level `required` flag is false. That flag controls
execution failure policy; it does not excuse an invalid deployment dependency.
`required = false` on an individual input allows it to be disabled; report that
state and avoid resolving or probing its path.

Preflight must inspect enabled declarations before discovery drops invalid
candidates. An enabled external module missing a declaration or containing an invalid
MRMS declaration causes a nonzero startup exit. Disabled modules and globally
disabled external CTAM are excluded. Preserve diagnostics for malformed
manifests and existing ordering/capacity rules.

Example fatal diagnostic:

```text
Cannot start Core: CTAM module hail-check requires MESH_00.50,
but MRMS_MESH_00.50 is not enabled for ingestion.
Manifest: /resolved/modules/hail-check/module.toml
Config: /resolved/config/ingest.yaml
Add MRMS_MESH_00.50 to mrms.products, or disable hail-check, then restart.
No workers or downloads were started.
```

Integrate preflight before runtime initialization, indexes, service locks,
heartbeats, network requests, and worker launch in `run_edgewarn.py` and
`process_historical.py`. Invoke the same check from the package command using
the resolved Core worker flags before starting any topology children. Direct
entry points must also enforce it. EWMRS-only/NEXRAD-only processes do not
validate unrelated CTAM installations. Propagate nonzero exits through
`run_all.py`/package supervision without a restart loop for configuration errors.

Reuse the frozen discovery result during cycle execution; module changes take
effect on restart. Keep module listing read-only and make
`--check-ctam-modules` include the dependency audit and nonzero failure status.

### StormProb dependency and exit policy

StormProb runs only when **all of its required inputs are enabled and usable**.
It consumes host-created cell/database features, so checking that a database
row exists or that any MRMS record exists is insufficient. Maintain a versioned,
host-owned dependency declaration tracing each required model feature to its
source product, enrichment step, role, freshness limit, and derived-data input.
Validate it against `stormprob/features.py`, `stormprob/records.py`, the model
manifest, and integration configuration. None of these dependencies expands
the three-product reserved Core set.

The declaration must cover the actual MRMS statistics, ProbSevere fields,
RAP environment/wind fields, detection geometry/morphology, valid history and
masks, and compatible model/normalization assets. Map dependencies by identity,
not source family alone. For example, StormProb's `MESH` feature currently
comes from ProbSevere, not the optional raw `MESH_00.50` product; GLM and NLDN
must not become requirements merely because the integration pipeline supports
them. Preserve the trained history-mask contract: unused padded history slots
are not failed upstream inputs.

1. **Operator omitted/disabled a dependency:** preflight emits a consolidated
   warning naming missing products/features, the relevant configuration file,
   and how to enable them or disable StormProb. Exit the whole Core process
   nonzero before runtime initialization, network activity, or worker launch.
   Do not silently disable StormProb or restore omitted products.
2. **Enabled input is unavailable or invalid:** after bounded acquisition and
   enrichment, evaluate exact pinned records and the completed feature inputs.
   Missing, stale, corrupt, failed-to-decode, or failed-to-integrate required
   inputs cause an actionable warning and a fatal dependency result. Terminate
   the whole Core process nonzero; do not merely skip a cycle or affected cell.
   Validate all candidate cells before batch inference/publication so an invalid
   required input cannot leave a partially successful StormProb cycle. A cycle
   with no candidate cells requires no fabricated feature/history rows.
3. **All inputs work:** run inference only after dependency, feature, history,
   schema, and asset checks pass. Do not use zeroes, stale values, median
   imputation, or missing-value sentinels to bypass this eligibility gate.
   Keep feature ordering, tensor shapes, and trained normalization unchanged.

Here, **exit means terminate the entire Core process with a nonzero status**.
Propagate a typed fatal dependency result through the built-in adapter, CTAM
runner, integration worker, and Core supervisor; existing per-cell/module
exception handlers must not downgrade it to a skipped/error module status.
Cancel and join owned work, close runtime resources, update service state, and
propagate the failure through package/process supervision. Configuration errors
must not enter a restart loop. Apply the same failure policy to historical runs.

Do not publish successful forecasts or new StormProb alerts for the failed
cycle, including reuse of a previous forecast as a current success. Preserve
already committed records and mark the failed cycle explicitly; never rewrite
old durable snapshots during shutdown. Existing historical forecasts/alerts
follow their existing retention/expiration rules. The warning must list missing
inputs and distinguish disabled, unavailable, and invalid dependencies.

The existing `records.py` readiness policy accepts some missing weather fields
with a sentinel. Tighten StormProb eligibility explicitly rather than assuming
that existing `inference_ready` already satisfies this all-inputs requirement.
Update record/adapter tests and document the policy change without changing
model tensors or the database schema unless a versioned extension is necessary.
A deliberately disabled StormProb/CTAM path does not emit dependency warnings.

Example diagnostic:

```text
Cannot continue Core: StormProb requires MRMS_Reflectivity_0C_00.50,
which is not enabled in /resolved/config/ingest.yaml; Ref0 cannot be produced.
Enable this product, or set runtime.run.disable_stormprob=true / use
--disable-stormprob, then restart. Core is exiting with a nonzero status.
```

### Explicit StormProb disable option

Add `runtime.run.disable_stormprob: false` to `config/runtime.yaml` and its
schema, with Python/Node validation, configuration-editor support, and catalog
baselines. Add `--disable-stormprob` / `--no-disable-stormprob` using
`argparse.BooleanOptionalAction` and `default=None` so explicit CLI values
override the catalog. Propagate the resolved value through package worker
arguments, direct Core and historical entry points, spawned workers,
integration, CTAM execution, and StormProb asset preflight.

Proposed operator commands:

```bash
edgewarn configure runtime.run.disable_stormprob true
edgewarn run core --args core '["--disable-stormprob"]'
# Direct entry point from src/:
python run_edgewarn.py --disable-stormprob
```

Flag interactions are explicit:

- `--disable-stormprob` disables only StormProb: skip its dependency checks,
  model/normalization asset validation and loading, inference, and new
  StormProb forecast/alert publication. External CTAM modules remain enabled
  subject to their own declarations. No missing-StormProb-input warning or
  fatal exit occurs while it is disabled.
- `--disable-ctam` continues to disable both built-in StormProb and external
  modules. It takes precedence over `--no-disable-stormprob`.
- `--disable-ctam-modules` continues to disable external modules only; enabled
  StormProb still enforces its fatal dependency gate.
- `--no-disable-stormprob` overrides a configured disable value but never
  enables missing ingestion products. With StormProb enabled, incompatible
  settings such as `mrms-core-only` disabling required RAP inputs fail preflight.

Disabling StormProb does not remove reserved inputs, change configured MRMS
additions, or delete stored forecasts/history. Preserve shared cell/history
storage used by other consumers; skip only work owned solely by StormProb.
Document restart requirements and test both positive and negative CLI forms.


Enforce declaration at the internal API boundary as well: bind each module's
token to its declared input selectors, filter raw-input descriptors accordingly,
and reject undeclared input metadata/content requests with a typed error.
The runner records a contract violation with instructions to update
`[[requires]]`. Module execution remains isolated. This catches undeclared use
through the supported SDK/API; arbitrary operator-supplied code reading the
filesystem directly is outside that API enforcement boundary and cannot be
inferred automatically at startup.

## 8. Downstream consumption contract

Use two questions in order: **is this product enabled?** and **is an eligible
file available for this cycle/use?** A path existing on disk answers neither.

| Consumer | Disabled product | Enabled but unavailable |
| --- | --- | --- |
| Reserved Core detection | Internal contract failure; fail preflight | Fail the required phase; preserve retry behavior |
| Configurable enrichment statistic (including shipped defaults) | Warn once and skip before path lookup | Warn with cycle/product reason; skip that statistic |
| EWMRS layer | Mark inactive and skip before scanning its directory | Preserve per-layer best effort and retry on later cycle |
| External CTAM required input | Fatal startup error naming module and config fix | Per-cycle module requirements not satisfied |
| External CTAM optional input | Expose disabled/absent status; no filesystem lookup | Expose unavailable; module handles absence |
| Enabled StormProb required input | Warn with missing dependencies; exit whole Core nonzero before startup side effects | Warn and exit whole Core nonzero after bounded validation; no inference/new forecasts |
| Explicitly disabled StormProb | Skip StormProb dependency checks and execution | No StormProb dependency gate; other consumers retain their own policies |
| Cleanup | No implicit sweep of old disabled directories | Clean enabled data under retention/pin policy |

Implementation instructions:

1. Add `product` identity to MRMS-backed `integration.yaml` statistics and
   `ewmrs_render.yaml` layer definitions; replace their raw `filepath` aliases
   with upstream product IDs. Keep GUI output names/outdirs and colormaps
   unchanged. Resolve eligibility before calling `path_for()`.
2. Centralize this conversion in `get_datasets_config()` and
   `get_mrms_file_list()` and keep a full diagnostic view alongside the active
   execution list. Update schemas and the configuration editor.
3. For detection and integration, select records by `(family, product, role)`
   instead of directory equality. Require current records explicitly; an old
   `previous` record must not satisfy a current-input gate.
4. Update direct/legacy code in `EdgeWARN.pipeline`, detection entry points,
   scripts, and compatibility imports. Before any `latest_files()` fallback,
   verify the product is enabled. Manifest-aware production paths never fall
   back to arbitrary latest files when the required pinned file is missing.
5. Preserve EWMRS's intentional independent per-layer latest-file selection.
   Validate complete inputs and retain each rendered source timestamp in
   output metadata. Do not convert MRMS rendering to RAP's exact-path model.
6. EWMRS and Core must agree on active ingestion. Publish an atomic,
   versioned effective-registry descriptor under
   `state/realtime/services/edgewarn-mrms-registry.json`, containing product
   IDs, Core contract version, and fingerprint. Exclude secrets and avoid
   treating descriptor-supplied arbitrary paths as authoritative.
7. EWMRS intersects its configured layers with producer-enabled products and
   its own matching registry. Missing descriptor or a mismatch pauses MRMS
   scanning with a clear readiness diagnostic; unrelated GOES/accessory work
   continues. Recover automatically after a matching Core startup. Consult
   the existing Core heartbeat for liveness rather than trusting an old
   descriptor indefinitely. No silent fallback to scanning stale directories.
8. Existing rendered history can remain available through the API's current
   retention/index rules. Disabled ingestion must not create fresh render
   timestamps, false success records, or synthetic API product entries.

Update CTAM SDK/docs and internal OpenAPI/contract tests if descriptor visibility
or error responses change. Public v3 product names and binary formats remain
unchanged; if implementation changes public responses, update v3 OpenAPI,
documentation, product catalogs, and Jest contracts together.

## 9. Existing directory and configuration migration

The naming rule changes several raw directories. An alias change alone does
not move existing files. Provide a separate offline migration command with
dry-run as default and explicit `--apply`; normal startup must not move data.

| Existing raw directory basename | New raw directory basename |
| --- | --- |
| `MRMS_EchoTop18` | `MRMS_EchoTop_18` |
| `MRMS_EchoTop30` | `MRMS_EchoTop_30` |
| `MRMS_EchoTop50` | `MRMS_EchoTop_50` |
| `MRMS_QPE` | `MRMS_RadarOnly_QPE_01H` |
| `MRMS_VILDensity` | `MRMS_VIL_Density` |
| `MRMS_MergedReflectivityQC` | `MRMS_MergedReflectivityQCComposite` |
| `MRMS_ReflectivityAtLowestAltitude` | `MRMS_MergedReflectivityAtLowestAltitude` |
| `MRMS_ReflectivityAt0C` | `MRMS_Reflectivity_0C` |
| `MRMS_ReflectivityAtM5C` | `MRMS_Reflectivity_-5C` |
| `MRMS_ReflectivityAtM15C` | `MRMS_Reflectivity_-15C` |

Other active MRMS raw directory basenames already follow the proposed rule.
Do not rename `<base-dir>/gui` products, NWS directories whose Python aliases
happen to begin `MRMS_`, or unused FLASH definitions by prefix matching.

Proposed commands to implement:

```bash
edgewarn migrate-mrms --config-path /etc/edgewarn/config --base-dir /runtime
edgewarn migrate-mrms --config-path /etc/edgewarn/config --base-dir /runtime --apply
```

The migration report lists config changes, protected products restored despite
their absence in old config, path renames, unchanged paths, collisions, and
backlog/pinned-record issues. Convert only nonprotected v1 catalog products into
the v2 additions list. Preserve operational settings, validate all converted
documents in both runtimes, and write them atomically with backups. For old
custom directory overrides, require an explicit reviewed source-to-target
mapping; do not guess or merge directories.

Stop Core/EWMRS and drain their durable backlog before applying directory
renames. Abort if there are unconsumed records or active references requiring
old paths. Do not rewrite committed records to point at different files.
Default to same-filesystem directory rename; fail clearly on target conflicts,
escaping symlinks, or cross-device moves. No automatic deletion or recursive
merge. Journal each applied step so interruption is resumable and rollback is
explicit. Already-identical source/target paths are no-ops.

Operators who do not need cached inputs can start with newly created canonical
directories after safely archiving/draining the old runtime state. Old data is
not automatically consumed or deleted. Test rollback to the previous release
using restored configuration and the inverse journal before release.

## 10. Implementation sequence and reviewable checkpoints

Each checkpoint should be independently reviewable; do not ship the schema
switch until its runtime and migration dependencies are ready.

| Step | Files/work | Completion criterion |
| --- | --- | --- |
| 1. Characterize contracts | Add focused tests beside `tests/core/ingest/mrms/test_config.py`, architecture catalog tests, and tandem tests | Pin three reserved products, three target discovery IDs, 18 default additions, old aliases, and current EWMRS trigger behavior |
| 2. Add pure definitions and registry | New normalization, Core contract, registry, and source modules | Empty additions still resolves Core; new syntactically valid product resolves without code edits; collisions fail before I/O |
| 3. Add configuration v2 and migration tooling | `config/ingest.yaml`, ingest schema, Python/JS loaders, CLI editor, new migration command | Shared parity fixtures pass; v1 gets actionable migration diagnostics; dry run writes nothing |
| 4. Generate filesystem paths | `src/util/file.py`, runtime initialization and spawned-child setup | Exact names/paths exposed; only enabled product directories created; aliases rebind safely across base/config changes |
| 5. Convert acquisition | MRMS config/main/downloader/pipeline/parse/HTTPS and compatibility exports | Sync and async share the same registry/subsets; source fallback and atomic staging remain correct |
| 6. Separate phase readiness | Coordinator, `util/runtime/cycle.py`, `EdgeWARN/pipeline.py` worker, historical runner, handoff tests | Optional outage cannot fail or delay mandatory callbacks; per-phase snapshots are immutable; all owned work terminates |
| 7. Enforce CTAM prerequisites | Manifest/discovery/preflight/run/runner/readiness/API/SDK, package and direct startup | External CTAM prerequisites and enabled StormProb prerequisites fail preflight; StormProb runtime missing inputs exit Core; explicit disable bypasses only StormProb |
| 8. Convert downstream readers | Detection/integration, EWMRS config/pipeline/consumer, runtime descriptor, relevant scripts | Disabled inputs cause zero file probes; stale files do not reactivate disabled products; rendered/API names remain stable |
| 9. Complete migration and docs | Migration CLI tests, config/catalog/path baselines, installation and developer docs | Existing runtime dry-run/apply/resume/rollback verified in temporary directories; operational instructions are complete |

Read each subtree's applicable `AGENTS.md` before implementation. Do not add
runtime Python dependencies to `pyproject.toml`; this design can use existing
dependencies and standard-library dataclasses/path handling.

## 11. Verification and release acceptance

Use deterministic local fixtures and temporary base directories. No default
test should contact NOAA or an operational runtime tree.

| Test group | Required scenarios |
| --- | --- |
| Naming/registry | Requested AzShear example; internal underscores; negative-temperature products; nonzero elevations; ProbSevere; duplicate IDs; case/elevation collisions; malformed/traversal IDs; import safety |
| Core protection | Empty additions; attempts to override Core source/path/phase; all CTAM flags; modified integration config; discovery exclusions; exactly three reserved identities; enrichment omission cannot block discovery when StormProb and incompatible external modules are disabled |
| Filesystem | Fresh creation; idempotent creation; unwritable destination; symlink escape; base rebind; config rebind; disabled-name removal; Windows-safe collisions; spawned process registry parity |
| Downloads | New optional product; S3 success; listing/fetch/decode failure with HTTPS fallback; both sources absent; corrupt/truncated data; duplicate delivery; out-of-order arrival; retry after restart; bounded concurrency and optional deadline |
| Cycle readiness | Every missing protected ID fails its phase; optional failure leaves base Core gates ready; enabled StormProb dependencies separately fail Core; empty optional batch valid; empty mandatory batch invalid; incorrect timestamps fail; previous cannot satisfy current; ordered callbacks; GLM/RAP behavior unchanged |
| Cleanup/replay | Pinned files survive cleanup; restart mid-download exposes no partial file; late optional file does not mutate a committed manifest; historical run does not overwrite realtime cycle records; deterministic repeated selection |
| CTAM preflight | Required input disabled; optional input disabled; missing explicit declaration; global/module disable flags; top-level optional module with required input; malformed enabled declaration; ProbSevere selector; no-raw-input module; retention conflicts |
| StormProb | Omitted MRMS dependency; removed enrichment mapping; disabled RAP; per-product missing/stale/corrupt source; incomplete derived features; incompatible assets; warning and nonzero whole-Core exit without inference/new forecasts/alerts; fatal propagation and bounded shutdown; restart with complete inputs; disable/config/CLI precedence and asset-check bypass; valid history padding; no raw-MESH requirement inferred from the ProbSevere MESH field |
| CTAM runtime | Enabled but missing/stale input; previous-input cold start; undeclared API access; blocked module with unaffected unrelated modules; frozen manifests despite edits on disk |
| Downstream | Disabled layer/statistic performs no `latest_files`, glob, stat, or open call; stale directory remains ignored; unknown references produce one actionable diagnostic; enabled unavailable products remain distinct |
| Handoff/API | Duplicate trigger is idempotent; retry/checkpoint semantics preserved; producer/consumer registry mismatch handled; RAP exact input unchanged; existing GUI identities, binary payloads, and v3 routes unchanged |
| Migration/parity | Python/JS accept/reject identical fixtures; config editor parity; v1 conversion; all ten directory renames; conflicts; pending backlog refusal; apply interruption/resume; rollback |

Extend existing suites rather than creating a parallel testing framework:

```bash
conda activate EdgeWARN
python -m pytest tests/core/ingest/mrms tests/core/ctam
# Include the existing StormProb feature, record, and built-in adapter suites.
python -m pytest tests/core/test_input_manifest.py tests/core/test_tandem_coordinator.py
python -m pytest tests/integration/test_tandem_coordinator.py tests/integration/handoff
python -m pytest tests/architecture tests/packaging tests/unit/config tests/util
npm run validate-config
PYTHONPATH=src python -m common.config.validate
npm test
```

Apply any closer test guide's environment instruction when running its subtree.
Once focused checks pass, run the full Python suite for the coordinated release
because startup, config loading, and filesystem aliases have broad reach.
Update assertions that currently require every catalog to have schema version
1; do not weaken unrelated schema or product-catalog invariants.

Run a fixture-based timing comparison with protected inputs ready and optional
inputs slow/missing. Mandatory callback timing must remain independent of the
optional deadline. Verify actual task/thread/process shutdown, not just a
timeout return value. Report peak queued jobs, active downloads, and cycle lag.

Release acceptance walkthrough:

1. With the default config, download the same 21 product identities as today,
   with paths following the new rule and all 18 optional additions excluded
   from Core readiness.
2. With `mrms.products: []`, only the three reserved inputs remain enabled.
   Optional statistics/layers are inactive and do not scan old directories.
   With StormProb enabled, warn and exit Core for its missing dependencies.
   With `--disable-stormprob` and no unmet external module requirements, Core
   starts with those three reserved inputs.
3. Add a new supported product ID only in `ingest.yaml`; its exact path name,
   directory creation, acquisition, and per-product status require no code edit.
4. Require MESH in a CTAM manifest, remove MESH from additions, and confirm a
   nonzero startup exit naming the module, file, and YAML fix before any worker
   starts. Restore MESH and confirm startup succeeds.
5. Make enabled MESH unavailable upstream; Core proceeds, and its dependent
   CTAM module reports unsatisfied cycle input requirements.
6. Remove a declared StormProb enrichment product such as
   `MRMS_Reflectivity_0C_00.50`: warn with the missing product and `Ref0`
   feature and exit the whole Core process nonzero. Restore it, then simulate
   stale/corrupt data and failed enrichment: warn and terminate Core without
   inference or new StormProb forecasts/alerts for that cycle.
   Repeat with `--disable-stormprob`: Core continues, external CTAM remains
   available, and StormProb performs no dependency/asset checks or inference.
7. Restart producer/consumer processes and replay an existing cycle: no partial
   artifacts, double publication, stale-directory activation, or path drift.

Update `docs/core/ingestion.md`, `docs/core/configuration.md`,
`docs/core/integration.md`, CTAM manifest/development/operations/compatibility
and internal API docs, `INSTALLATION.md`, and README configuration examples.
Document the exact-name Python access limitation, protected-product table,
restart requirement, collision policy, and migration/rollback commands.

## 12. Implementation phases

These phases expand section 10 into deliverable work packages. Complete each
phase's exit gate before its dependents. Review commits independently, but
release the schema, runtime, consumer, and migration changes together. Until
that coordinated release is ready, exercise v2 through fixtures and development
configuration; do not ship a partially converted default catalog.

### Phase 1 — Characterize the current contracts and impact

Depends on: none. Covers checkpoint 1.

- Pin the existing 21 identities and ten discovery inputs as the baseline;
  assert the target three reserved/discovery inputs and 18 default additions.
  Cover source templates, raw aliases,
  render identities, and phase notification behavior in focused fixtures.
- Build a product impact table linking identity, remote adapter, raw path,
  retention, readiness phase, consumer, manifest/index, API product, and
  compatibility imports. Classify each dependency as required, optional,
  disabled, derived, compatibility-only, or obsolete. Include tracking,
  lineage, StormProb, CTAM, alerts, history, health, and historical processing.
- Record the old-to-new contract: v1 catalog entries become v2 additions plus
  the immutable Core contract; raw paths follow section 5; GUI identities and
  payloads retain their current contracts. Give ProbSevere its explicit JSON
  adapter and manifest identity. Set and document the legacy Python alias
  support period before implementation begins.

Exit gate: characterization tests pass against the current implementation,
and every affected producer/consumer has an identified implementation phase.
Baseline fixtures distinguish present behavior from intended new assertions.

### Phase 2 — Implement pure product definitions and registry

Depends on: phase 1. Covers checkpoint 2.

- Add `common.config.mrms_products`, the immutable `core_contract.py`, source
  definitions, and registry builder described in sections 3–5. Keep imports
  free of configuration loading, filesystem mutation, and network access.
- Implement exact normalization, elevation-preserving identities, generated
  path names, ProbSevere resolution, effective membership, and fingerprints.
  Reject unsafe IDs and collisions across protected and configured products.
- Generate the release-owned Core contract fixture used by Node validation,
  and verify that packaging includes it. Define compatibility accessors from
  the registry rather than maintaining a second product catalog.

Exit gate: empty additions produce exactly the protected set; a new supported
CONUS ID resolves without a source edit; the requested AzShear name, negative
temperatures, nonzero elevations, duplicates, and Windows collisions pass
their acceptance/rejection fixtures without I/O.

### Phase 3 — Add configuration validation and migration planning

Depends on: phase 2. Starts checkpoint 3; apply/resume is completed in phase 8.

- Implement ingest v2 validation in Python, Node, and the configuration editor
  using shared parity fixtures. Prepare `runtime.run.disable_stormprob` and
  CLI/editor parity fixtures for phase 6. Validate operational bounds and reject removed
  source, membership, and path overrides with migration diagnostics.
- Prepare the `product` fields and schemas for integration statistics and
  EWMRS MRMS layers together with their conversion rules. Preserve the document
  version policy in section 4 and leave GOES configuration intact.
- Add the offline `edgewarn migrate-mrms` entry point and pure conversion/dry-run
  planner. It must read a v1 tree even though normal v2 startup rejects it,
  preserve operator settings, identify custom mappings and conflicts, and
  describe all ten raw-directory renames before making changes.

Exit gate: Python and Node agree on valid and invalid fixtures; editor
validation uses the same rules; migration dry runs leave config and runtime
trees byte-for-byte unchanged. Converted fixture trees validate in both runtimes.

### Phase 4 — Bind paths and convert acquisition

Depends on: phases 2–3. Covers checkpoints 4–5.

- Separate path binding from directory creation in `util.file`; expose exact
  names and compatibility aliases from the active registry. Verify containment,
  base/config rebinding, disabled-name removal, and spawned-child fingerprints.
  Wire directory creation after the complete preflight added in phase 6.
- Convert MRMS sync, async, all-files, historical, and HTTPS fallback paths to
  explicit reserved detection and configurable enrichment/other selections.
  Update `EdgeWARN.ingest` compatibility exports with the shared implementation.
- Preserve acquisition, decoding, validation, and atomic publication boundaries.
  Add complete per-product results, provenance, validated local reuse,
  deduplication, conflicting-content handling, and bounded quarantine.
- Apply one bounded MRMS work budget across primary downloads and fallbacks,
  with protected-input priority, optional deadlines, and bounded network calls.
  Expose queue depth, active work, failure reasons, and elapsed time.

Exit gate: fixture downloads cover both sources, malformed/truncated payloads,
duplicate and out-of-order delivery, and restart during staging. No partial
file is visible; every requested product has a result; a new optional product
downloads into its generated directory through both supported execution paths.

### Phase 5 — Separate readiness and preserve replay state

Depends on: phase 4. Covers checkpoint 6.

- Refactor `common.pipeline.coordinator`, `util.runtime.cycle`, and
  `EdgeWARN.pipeline` so detection, EWMRS notification, and Core integration
  follow section 6 without awaiting optional completion. Keep RAP/GLM gates
  and failure-event releases explicit.
- Introduce immutable detection, integration, and final CTAM snapshots, plus
  the bounded optional-completion signal. Complete configured enrichment from
  that final input set before testing StormProb eligibility; do not publish
  twice or infer from incomplete property patches. Record optional outcomes in a
  separate versioned ingest report; preserve strict handoff readers and publish
  `mrms-ready` once. Late optional arrivals are considered by later render scans.
- Select current/previous inputs by encoded UTC time and product role, protect
  active pins across processes during cleanup, and make retries idempotent.
  Reuse the same selection/deadline rules for historical processing while
  keeping its reports and outputs isolated from realtime publication.

Exit gate: deterministic timing tests show optional delay/failure does not
delay mandatory callbacks. Missing protected inputs fail their own phase,
previous records cannot satisfy current gates, committed snapshots do not
change, pinned files survive cleanup, and all owned tasks/threads/processes
terminate within bounds after success, failure, or cancellation.

### Phase 6 — Enforce CTAM declarations and startup preflight

Depends on: phases 3–5. Covers checkpoint 7.

- Add `EdgeWARN.ctam.preflight`; separate selector syntax from enabled-product
  validation and audit enabled declarations before discovery can discard them.
  Require explicit `requires`, include ProbSevere, validate previous-input
  retention, and register StormProb's feature-to-source dependencies separately.
  Implement its all-inputs gate, warning, and fatal whole-Core exit policy in
  observation readiness, the built-in adapter, and supervisor propagation;
  never reserve its additions. Add the dedicated config/CLI disable option,
  including asset-check bypass and interactions with existing CTAM flags.
- Apply the same preflight to package startup, direct Core startup, historical
  startup, and `--check-ctam-modules`, using resolved flags and frozen discovery.
  Propagate configuration failures through supervisors before filesystem
  initialization, locks, heartbeats, network activity, or child launch.
- Enforce declared selectors in CTAM token permissions, descriptors, SDK/API
  access, and typed errors. Evaluate cycle availability against the final
  snapshot while preserving external-module isolation. Propagate enabled
  StormProb dependency failures across that boundary to terminate Core.

Exit gate: a disabled required input fails startup even for a top-level optional
module, with no startup side effects. Optional disabled inputs cause no file
lookup; enabled but absent/stale inputs for external modules block only their
execution. StormProb follows the fatal policy below.
Global/module disable flags, missing declarations, undeclared API access,
previous-input cold start, and frozen discovery have regression coverage.
StormProb missing-input tests prove the entire Core process exits nonzero and
owned workers terminate without new forecasts/alerts. Explicit disable permits
Core operation without StormProb dependencies and preserves external CTAM;
complete inputs permit enabled StormProb execution.

### Phase 7 — Convert downstream readers and producer agreement

Depends on: phases 3–6. Covers checkpoint 8.

- Convert detection, integration, direct callers, and scripts to check product
  eligibility before resolving paths. Use identity/role selection for pinned
  inputs and preserve explicit missing-data behavior without stale fallback.
- Activate the integration/EWMRS configuration conversions through
  `get_datasets_config()` and `get_mrms_file_list()`. Keep inactive entries in
  diagnostics and active entries in execution lists.
- Publish the atomic effective-registry descriptor after successful Core
  preflight. Make EWMRS verify producer/local agreement and Core liveness before
  MRMS scanning, recover when agreement returns, and continue unrelated work.
- Preserve independent per-layer render selection, source timestamps, retained
  history, GUI paths, binary payloads, indexes, API catalogs, and service gates.
  Verify that adding raw ingestion creates no implicit public API product.

Exit gate: disabled products cause zero file probes despite stale directories;
missing/mismatched producer state pauses only MRMS scanning. Handoff retries,
RAP exact-path behavior, render reuse, public v3 contracts, and legacy-route
404 responses remain covered. A fixed scan sequence preserves Core detection/tracking and, with full inputs,
enrichment, StormProb, CTAM, alerts, and history. Omitting enrichment products
produces explicit consumer outcomes: enabled StormProb exits Core if affected;
with StormProb disabled and no unmet external requirements, Core continues.

### Phase 8 — Complete migration and qualify the coordinated release

Depends on: phases 1–7. Completes checkpoints 3 and 9.

- Finish migration apply, config backups, atomic writes, the per-step journal,
  resume, and explicit rollback. Require stopped producers/consumers and a
  drained backlog; refuse active pins, conflicting targets, escaping symlinks,
  cross-device moves, and ambiguous custom directory mappings.
- Exercise the complete v1-to-v2 upgrade and inverse-journal rollback in
  temporary trees, including interruption between config and directory steps.
  Keep services stopped until the whole converted catalog and runtime pass
  validation. Preserve old committed records and unrelated data directories.
- Switch the shipped catalog only with all runtime/consumer changes present.
  Update baselines, packaged contract assets, examples, architecture/CTAM and
  StormProb readiness docs, installation instructions, alias support period,
  and migration/rollback guide.
- Run section 11's focused suites, both configuration validators, Node API
  tests, full Python suite, and fixture latency/restart/replay checks. Record
  callback latency separately from optional drain time and total cycle lag;
  use the results to confirm concurrency and deadline defaults.

Exit gate: every section 11 acceptance walkthrough passes, upgrade and rollback
are reproducible, and release evidence includes validation results and bounded
resource/shutdown measurements. No unresolved product dependency, schema
mismatch, migration conflict, or public-contract regression remains.
