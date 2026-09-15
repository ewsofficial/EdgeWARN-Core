# Source Configuration Extraction Plan

**Audit baseline:** commit `3afb88f` on `version-test/3.0.0`
(the commit that added `config/`)  
**Package version:** `2.7.0`  
**Status:** the 18 `config/*.yaml` files exist but are **not consumed by code**,
except three sections of `config/kalman.yaml`. This document governs wiring them
in; it does not itself move or change runtime configuration.

`plans/yaml-configurations.md` is the companion map of which file owns which
keys, verified against the tree at `e12d407`. Where the two documents overlap,
that one describes the file layout and this one describes the migration.

## Objective

Move deployable settings, product catalogs, scientific tunables, source
endpoints, retention policy, concurrency limits, and API policy out of Python
and JavaScript source files and into the validated configuration tree under
`config/`.

The extraction must include, at minimum, the complete MRMS ingest catalog and
its readiness/detection/integration/render memberships, the integration
dataset/statistic catalog, GOES ABI products, RAP integration and render
layers, NEXRAD products/VCP policy, base-directory resolution, runtime
scheduling, and the API service.

Artifact **path layout** is explicitly not a deliverable. `config/` owns
base-directory resolution; the ~113 derived artifact directory names stay in
`src/util/file.py`. See the `filesystem.yaml` scope note below.

This is not a request to turn every literal into configuration. Binary format
constants, mathematical and physical invariants, public wire-format
requirements, validation rules, and control-flow safety invariants remain in
code. The boundary is made explicit below so the implementation does not
create an unmaintainable “everything is YAML” system.

## Completion definition

The implementation is complete when:

- Every item in the source inventory below is either read from `config/`,
  derived from another configured value, or recorded in the intentional
  code-constant allowlist.
- The parallel catalogs that exist on disk are **cross-checked by test** rather
  than derived from one another. `mrms_goes.yaml` carries `mrms.products` plus a
  separate `mrms.check_products` readiness subset; `ewmrs_render.yaml` carries
  `mrms_layers` and `goes_layers`; `integration.yaml` carries `stats_datasets`.
  These stay separate lists — a test asserts every readiness product is also an
  ingest product, every render layer names an ingested product, and every
  integration dataset names an available source.
- No config key coexists with an `argparse` default or a keyword-argument
  default holding the same value. Exactly one base default per setting, and it
  lives in YAML.
- Python and Node load the same shared files, validate them before starting
  work, and report actionable key paths for invalid values.
- Existing CLI and environment overrides retain compatibility and have a
  documented precedence order.
- No production module silently substitutes a second hard-coded default when
  a config file or key is missing.
- Tests compare the new configuration against a checked-in current-behavior
  snapshot before any values are intentionally tuned.
- Documentation names the authoritative file for every operator-facing
  setting.
- A repository audit test prevents new configurable endpoints, product lists,
  timers, retention values, worker limits, and scientific thresholds from
  being added directly to production source.

## Configuration boundary

### Move to `config/`

Move values when at least one of these is true:

- Operators may need to change the value by deployment, domain, resource
  budget, data-source availability, or policy.
- The value selects a product, layer, statistic, event type, pressure level,
  source URL/bucket, output directory, colormap, or processing phase.
- The value is an empirically chosen scientific threshold, score weight,
  confidence cutoff, smoothing parameter, search window, or fallback policy.
- The value controls polling, retry, retention, cleanup, cache, timeout,
  concurrency, memory, logging, or server behavior.
- The same conceptual value or mapping appears in more than one source file.

### Keep in code

Keep these categories in code and cover them with named constants and tests:

- File-format and protocol invariants, such as NEXRAD record/header sizes,
  archive magic bytes, message status codes, binary block masks, Uint16
  no-data value/byte order, timestamp grammars, and safe-filename rules.
- Mathematical and physical invariants, such as metres per kilometre, Earth
  radius, Web Mercator radius, unit conversions, covariance equations,
  standard-atmosphere equations, and transform implementations.
- Public schema requirements and route contracts, such as required alert
  fields, HTTP status semantics, and route parameter validation.
- Derived values, such as an affine transform derived from configured bounds
  and shape, grid rows derived from height/tile size, and a worker count
  bounded by available CPUs and configured limits.
- Safety and orchestration invariants, including staged readiness order,
  atomic publication, path-containment checks, non-daemonic NEXRAD parser
  ownership, signal handling, and “do not advance on failed cycle” rules.
- Algorithms and registries that execute trusted behavior. Config may select
  a named transform or statistic, but may not contain executable expressions,
  imports, callbacks, or arbitrary formula strings.
- Package metadata. `package.json` remains the single version source;
  configuration and user-agent strings interpolate that version instead of
  copying `2.7.0`.
- **Algorithms transcribed into the current files as pseudo-expressions.**
  `ewmrs_rap_uint16.yaml` `scale_rule`, and `ewmrs_render.yaml`
  `goes_transform.radian_detection` and `crs_strategy`, are prose descriptions
  of hardcoded code paths, not settings. They are deleted, not wired. See the
  corrections table.
- **`util.file` attribute indirection.** Catalog entries name an output
  directory by *attribute name* (`MRMS_VIL_DIR`), and the loader resolves it
  with `getattr` against the `util.file` module. The directory names themselves
  stay derived in `src/util/file.py`, which is what the "derived values" rule
  above already permits.
- **Name-matching helpers** in `src/EWMRS/rap/config.py:17-66`
  (`_wind_colormap_key`, `_temperature_colormap_key`, `_with_colormap_key`) —
  prefix/suffix logic that cannot be expressed as flat YAML. The per-layer
  `colormap_key` values they produce are configured; the matching stays code.

## Current-state findings

All 18 `config/*.yaml` files exist and every one except `kalman.yaml` opens with
the header line *"Not yet consumed by code"*. They were produced by transcribing
literals out of the source, so they are a **starting inventory, not a working
configuration** — the corrections table near the end of this document lists the
places where the transcription is wrong, incomplete, or describes an algorithm
rather than a setting. Those corrections gate any implementation.

`config/kalman.yaml` is the sole partial exception: its `kalman_filter`,
`tracking` and `assignment` sections are read by three `from_yaml` classmethods
in `src/EdgeWARN/process/detect/kalman/config.py`. Its other four sections are
inert. That file is also the reference pattern for the loader — frozen dataclass,
one named section read with `.get()` — except that its inline literal fallbacks
must go away, per the precedence rule below.

The configuration is otherwise split between:

- Python functions and module constants named `config.py`, many of which are
  still source code rather than external configuration.
- `src/EWMRS/colormaps.json` and `src/EWMRS/mappings.json`, which are already
  data files but live below `src/` and are resolved independently by Python
  and Node.
- CLI defaults, environment fallbacks, function defaults, constructor
  defaults, and inline literals spread across the pipeline.
- Separately maintained Python and Node product/directory mappings.

The most important drift risks found in the baseline are:

- The 28-entry MRMS ingest catalog, 12-entry readiness list, three detection
  modifiers, derived integration membership, and EWMRS render membership are
  defined through separate functions.
- MRMS integration output/statistic definitions are separate from the ingest
  products they require.
- EWMRS product identity is repeated in Python render definitions, Python
  filesystem paths, Node `PRODUCT_MAPPING`, and Node `GUI_SUBDIRS`.
- GOES channel identity is repeated in ingest specs, render layers, paths, and
  API mappings.
- RAP integration products, RAP Uint16 render layers, Python colormap mapping,
  and `mappings.json` do not share an authority.
- `src/EWMRS/mappings.json` exposes `RAP_BestLiftedIndex_180_0mbAGL`, but the
  Python RAP render catalog produces `RAP_LiftedIndex_Surface_500_1000mb`
  (`src/EWMRS/rap/config.py:250`). Re-verified at this baseline; still drifted.
- `TrackingConfig.max_prediction_time_minutes` is `6.0` in the dataclass and
  YAML (`kalman/config.py:63`) but uses `10.0` as the inline `.get()` fallback
  (`:92`). Re-verified at this baseline.
- `nws/zone_sync.py` `pause_seconds` is `0.0` in argparse but `0.05` in the
  constructor (`zone_sync.py:159`). `config/nws.yaml` recorded `0.0`, so
  transcription silently picked one side of a live disagreement.
- Two different user-agent strings existed: `zone_sync.py`
  `"(EdgeWARN/1.0, contact@edgewarn.com)"` versus `nexrad/config.py`
  `"(EdgeWARN/2.7.0, ewsbackend@gmail.com)"` — different versions *and*
  different contacts. **Done.** Both are gone; `runtime.yaml identity` is the one
  owner and `format_user_agent()` interpolates the `package.json` version, so
  neither line can be cited any more.
- `config/nexrad.yaml` `cli.sites: []` misreads the source sentinel.
  `nexrad/pipeline/__init__.py:74` distinguishes `None` from `[]`, so `[]` is
  not "all sites".
- `KalmanConfig.from_yaml()` exists, but the production tracker does not
  clearly inject the loaded Kalman filter parameters, allowing code defaults
  to remain effective.
- Detection threshold defaults (`37.5`, `0.001`, `10.0`) are copied through
  the CLI, EdgeWARN pipeline, detection entrypoints, and gate mapper.
- Lineage overlap is `0.15` in the detector but `0.10` in `CellTracker`.
  These may be intentionally distinct policies; configuration must give them
  distinct names rather than accidentally unifying them.
- Base-directory logic and directory names are independently implemented by
  Python, the EdgeWARN API, and the EWMRS API.
- EdgeWARN API version strings and the NEXRAD weather API user agent copy the
  package version instead of reading `package.json`.
- ~~WPC cleanup searches `surface_analysis_*.geojson` while generated artifacts
  are named `wpc_sfc_*`.~~ **Refuted in Phase 0**: `surface_analysis` is only a
  directory name, and the sweep and the writer have always agreed on the
  `wpc_sfc_` prefix. The glob and the output template are now both keys in
  `wpc.yaml`, coupled by a round-trip test that also pins that the glob does not
  match `latest_filename`.

## Target configuration tree

The tree is the flat per-subsystem files that already exist, plus one JSON
Schema per file. Use YAML for operator-edited settings and product catalogs,
JSON for JSON Schema files. Add a maintained YAML parser to Node rather than
introducing parallel JSON-only copies of shared settings.

```text
config/
├── api.yaml                 ├── kalman.yaml
├── api_index.yaml           ├── lineage.yaml
├── detection.yaml           ├── metar.yaml
├── ewmrs_pipeline.yaml      ├── mrms_goes.yaml
├── ewmrs_rap_uint16.yaml    ├── nexrad.yaml
├── ewmrs_render.yaml        ├── nws.yaml
├── filesystem.yaml          ├── runtime.yaml
├── historical.yaml          ├── scheduler.yaml
├── integration.yaml         ├── synoptic_rap.yaml
└── schema/                  └── wpc.yaml
    └── <name>.schema.json   (one per file above, 19 total)
```

`alerts.yaml` is gone. It was never one subsystem: it held CTAM alert emission,
the MRMS polling loop, the API index and a pipeline bootstrap flag, grouped only
because the audit listed them together. The scheduler and index halves became
`scheduler.yaml` and `api_index.yaml`; the CTAM half was dropped from the
catalogs entirely, leaving `schema.py` and `manager.py` as the sole owners of
those literals until CTAM is extracted on its own terms. Whatever file finally
owns NWS alert *parsing* is `nws.yaml`, which already does.

Otherwise no file is renamed. In particular there is no `config/paths.yaml`, no
`config/products/`, no `config/ingest/`, no `config/render.yaml` and no
`config/colormaps.json` — earlier drafts of this plan named those, and they do
not exist. CTAM configuration and the `colormaps.json` relocation are out of
scope here and become a separate follow-up.

Do not create generated copies of these files under `src/`. Python and Node
must resolve the repository configuration root through one loader contract.
Packaged/deployed installations must explicitly copy the `config/` tree.

### `filesystem.yaml` scope

`filesystem.yaml` owns **base-directory resolution, cleanup defaults, the
directory-scan skip-extension list and the colormap search path — not the ~113
artifact directory names.** Those names are derived values, which the "Keep in
code" rule above already permits, and keeping them in `src/util/file.py` is what
makes the attribute-name indirection in the catalogs coherent: a catalog entry
says `outdir: GUI_VIL_DIR`, and the loader resolves that name against the
`util.file` module.

## Catalog shape as written

The catalogs exist on disk in the following shapes. This section records what
they actually contain, so an implementation validates against reality rather
than an idealized model.

| Catalog | Entry shape | Count |
| --- | --- | --- |
| `mrms_goes.yaml` `mrms.products` | `{region, product, outdir}` | 28 |
| `mrms_goes.yaml` `mrms.check_products` | flat list of product strings | 12 |
| `mrms_goes.yaml` `goes.abi_channels` | `{id, name}` | 16 |
| `ewmrs_render.yaml` `mrms_layers` | `{name, colormap_key}`, plus `filepath` and `outdir` to be added | 15 |
| `ewmrs_render.yaml` `goes_layers` | `common` / `reflectance` / `brightness_temp` groups | 16 |
| `integration.yaml` `stats_datasets` | `{name, source, key, method, percentile?}` | 25 |
| `ewmrs_rap_uint16.yaml` | `{schema_version, max_timestamps, timestamp_format, force}` runtime policy | 4 keys; the 43-layer Uint16 display catalog remains code-owned in `src/EWMRS/rap/config.py` by the Phase 5 decision not to ship inert catalog keys |

There are no stable lowercase product IDs and no `roles:` membership map. The
readiness subset is a separate list of product strings, not a role flag, and
`get_check_modifiers()` reads that list rather than querying a catalog. Do not
introduce an ID/role model as part of the wiring work; cross-check the parallel
lists by test instead. Introducing one is a defensible later refactor, but it is
a schema change, not a value-preserving extraction, and mixing the two is how
this plan became unexecutable the first time.

**Loader contract for catalogs.** `outdir`, `filepath` and `source` values are
`util.file` attribute *names*, resolved with `getattr` against that module. An
unknown name is a load-time error, not a `None`. Note the deliberate asymmetry
`MRMS_DVIL_DIR` → `GUI_VILD_DIR`; it is correct and must survive transcription.

## Loader, validation, and precedence

### Shared behavior

Add:

- **`src/common/config/loader.py`** for typed Python loading, caching, schema
  validation, and domain accessors, with frozen dataclasses per file following
  the `from_yaml` pattern in `src/EdgeWARN/process/detect/kalman/config.py`.
  This is one module, not a `src/util/config/` loader plus a separate
  `src/common/config/` model package. It must import only the standard library
  and `yaml` — no `util.file`, no domain modules — so it can be imported before
  the filesystem is initialized.
- **Schema validation is a hand-rolled walker, not `jsonschema`.** Earlier
  drafts of this plan required `jsonschema` here and in Phase 1; that is
  **corrected**. `src/config/loader.js` implements the same walker over the same
  keyword set, and cross-language parity is an acceptance requirement — a full
  validator on the Python side alone would accept `$ref`/`oneOf`/`format`
  schemas that Node still rejects. The walker's unknown-keyword guard is also a
  property `jsonschema` would remove rather than add, since it ignores keywords
  it does not recognize and would let a misspelled `requred` enforce nothing.
  And `environment.yml` is the only dependency manifest in the tree; it does not
  list `jsonschema`. Revisit only if a schema needs composition keywords, at
  which point both loaders take the dependency together.
- `src/config/loader.js` for Node loading and the same schema validation.
- **`schema_version: 1` in all 19 files.** None has one today. Note the
  collision: `ewmrs_render.yaml:12` already uses `schema_version: 2` for the
  chunk *wire* format; rename that key to `chunk_format.wire_version` before
  adding the config-level one.

Load and validate configuration once in each root process before starting
threads, process pools, child workers, or HTTP listeners. Pass immutable
settings or a config-root argument to children. Do not repeatedly parse YAML
inside hot paths.

### Ordering hazards

Three import-time behaviors will silently defeat a naive loader:

- **`src/run.py:35-79` sits outside any `__main__` guard.** Under Windows
  `spawn`, that module scope re-executes in every child — 7 accessory processes
  plus per-cycle workers — so `get_args()` and any config load run once per
  child. The loader must therefore be memoized and idempotent, and a YAML error
  must surface once at startup rather than once per child. Prefer moving the
  module-scope work into `main()`.
- **`src/util/file.py:194-200` calls `_define_paths()` at import time**, before
  argparse runs, so paths are already bound by the time `--base_dir` is parsed.
  Resolve `base_dir` in two phases: use the already-present-but-unused
  `IOManager.get_base_dir_arg()` (`src/util/io.py:68-73`, which uses
  `parse_known_args`) to peek the flag, combine it with the environment variable
  and `filesystem.yaml`, then call `initialize_filesystem()` exactly once at
  `run.py:44`.
- **`src/EWMRS/render/config.py:221` snapshots `file_list = get_file_list()` at
  import time**, capturing pre-`--base-dir` paths. Delete it or make it lazy.

**Done, with one bullet deliberately unfinished.**

`util/file.py` no longer binds a stale answer: `IOManager.get_base_dir_arg()` peeks
`--base_dir` off `sys.argv` before the module-scope `_define_paths()` call, so the
113 path globals are already correct at import. A sibling `get_config_dir_arg()`
does the same for `--config-dir`, needed because that bind now reads
`filesystem.yaml` for the colormap search path — earlier than `export_config_root`
publishes `EDGEWARN_CONFIG_DIR`. `initialize_filesystem()` remains as phase two,
for a spawned child (which has no argv) and for a programmatic caller.

`render/config.py`'s `file_list` snapshot is deleted; `get_file_list()` is called
per use, and `test_known_drift.py` asserts the attribute's absence on that module.
`tests/integration/test_ewmrs_pipeline.py` had a `raising=False` setattr for
`file_list` on `EWMRS.pipeline`, which was always a no-op — the snapshot was never
there, and `pipeline.py` imports the accessors by name — so it is gone rather than
retargeted, to avoid implying cleanup once read it.

`src/run.py:41` still calls `get_args()` at module scope, outside the `__main__`
guard. The hard requirement is met — the loader is memoized on
`(resolved_root, name)` and idempotent, so `spawn` re-execution is cheap and a YAML
error surfaces once per process — but the preference to move the work into `main()`
is not. The module scope binds a large set of globals that the loop functions close
over, so relocating it is a refactor of `run.py`'s structure rather than a
configuration change, and it is out of scope here. The accessor-per-use pattern at
`run.py:259` is the pattern to follow for any value added later.

### Token expansion

Expand only an explicit allowlist of tokens in configured path values:
`<base_dir>`, `<gui_dir>`, and a new `<src_dir>` needed by
`filesystem.yaml`'s colormap search path, because `src/util/file.py` resolves
`src/EWMRS/colormaps.json` relative to `__file__`, not the working directory.

**Done.** `PATH_TOKENS` and `expand_path` in `src/common/config/loader.py`, mirrored
by `PATH_TOKENS`/`expandPath` in `src/config/loader.js`. The token is mandatory and
must lead, so a bare relative value is an error rather than something that quietly
resolves against the working directory. `roots` carries only the tokens meaningful
to the caller, which is what makes `<base_dir>` an error in a context that has no
base directory instead of an empty expansion. `config/schema/filesystem.schema.json`
repeats the allowlist as a `pattern` so a misspelled token is caught during
validation, in both languages, before any expander runs; a test pins the two lists
to each other.

Traversal is rejected twice: textually (`..` segment, leading `/`, backslash
separators) before any path is built, and by containment after resolving, which on
the Python side is what catches a symlink inside the root pointing out of it.

Two further rejections were added after a review pass found both loaders sharing the
same two holes. A falsy or relative `roots` entry made `Path.resolve()` /
`path.resolve` fall back to the working directory — the defect the mandatory leading
token exists to prevent, arriving through the other argument, and silent because the
result is still a plausible path. And a NUL byte was accepted, which on Windows
renders as a space and so resolves to a real but different file. Neither is reachable
from a schema-valid catalog today; both are two lines to reject, so they are rejected
rather than documented as acceptable. The
Node half of that check is the one `src/api/config/index.js` already had — that copy
is gone, and `resolveRuntimeDirectory` now delegates to the shared expander, so the
`<base_dir>` prefix rule has one implementation rather than two.

`src/EWMRS/pipeline.py` assigned `fs.GUI_COLORMAP_JSON` from its own
`__file__`-relative literal at module scope, which would have silently outranked the
catalog for every EWMRS run. It resolved to the same file, so it was deleted rather
than reconciled.

Leave `<SITE>`, `<scan_timestamp>` and `<volume_id>` alone — none is a load-time
token, and no rewrite is owed because none ever reached a catalog in either
spelling. `SITE` and `scan_timestamp` are f-string parameters in
`src/EWMRS/render/nexrad.py:70-77`, built per scan. `volume_id` is not a render
field at all: it was a `cli.volume_id` key, deleted outright as an argument to one
invocation rather than a deployment setting, and recorded as such at
`config/nexrad.yaml:19-21`.

The runtime fields that *are* in the catalogs already use `{}`
(`mrms_goes.yaml:42-56`, `metar.yaml:13`, `integration.yaml:149-150`), so that class
stays visually distinct from the `<token>/` the expander claims. Do not implement the allowlist by scanning for bare `<`/`>`: six
comment lines and one expression value (`synoptic_rap.yaml`'s named capture groups)
would false-positive.

Both loaders must:

- Resolve `config/` relative to the repository/install root, not the current
  working directory.
- Accept an explicit `--config-dir` and `EDGEWARN_CONFIG_DIR`.
- Reject unknown keys by default so misspellings do not silently do nothing.
- Reject missing files/keys, duplicate product IDs, invalid ranges, invalid
  role combinations, unresolved path keys, unknown colormaps, and unknown
  transform/statistic names.
- Include filename plus dotted key/index in validation errors.
- Return immutable/deep-frozen data to prevent a worker from mutating global
  configuration.
- Expose sanitized effective configuration and provenance for diagnostics,
  excluding secrets.

### Precedence

Use this order, highest first:

1. Explicit CLI option.
2. Supported environment variable.
3. Value in the selected `config/` tree.

An explicitly-passed CLI flag beats an environment variable, which beats the
YAML value. **YAML is the base layer.** There is no fourth production fallback:
the repository default configuration contains the current values, so a missing
file or key is an early startup error rather than an invitation to use a hidden
literal.

This supersedes the "env > CLI > YAML > code fallback" ordering that appeared in
earlier notes and in `plans/yaml-configurations.md`; that document has been
corrected to match.

### Argparse must move to `None` sentinels

For CLI-beats-YAML to be implementable, "the operator passed this flag" must be
distinguishable from "the flag was absent". It currently is not:

- `src/util/io.py:88-90` defaults `--refl-threshold` to `37.5`,
  `--min-seed-percentage` to `0.001` and `--drop-offset` to `10.0`. A parsed
  value of `37.5` is indistinguishable from an unspecified flag, so YAML could
  never win.
- Every `store_true` flag defaults to `False`, so a YAML `disable.ctam: true`
  could never take effect — the absent flag would overwrite it every time.

Convert value flags to `default=None`, and boolean flags to
`argparse.BooleanOptionalAction` (or `default=None` with an explicit tri-state
check). The overlay then applies only keys whose parsed value `is not None`.
This affects the 16 flags in `src/util/io.py:83-109`, the historical flags at
`:125-130`, `src/common/ingest/nexrad/pipeline/__init__.py:390-397`,
`src/common/ingest/nexrad/main.py:172-179`, and
`src/common/ingest/nws/zone_sync.py:367-420`.

Help strings that read "(default: 37.5)" must stop naming a literal, since the
default now comes from YAML.

An environment override should keep the shape
`src/common/ingest/synoptic/config.py`'s `get_rap_max_age_minutes` established: a
named env-var constant, an unset test, and a `ValueError` that re-quotes the raw
value. **That describes the semantics, not the location.** Earlier drafts read it
as an endorsement of the bespoke `os.environ` read inside that function, which
contradicted Phase 1 step 6 ("Implement CLI/environment overlay adapters without
putting environment parsing inside domain modules") and the Phase 6 audit that
flags new `os.environ` reads outside the overlay loaders. **Resolved in favour of
the location rules.** The three properties now live in
`common.config.overlay.resolve`: `value_type` for a key whose YAML value is
`null` and therefore carries no type to infer, `minimum` for a bound that was
previously enforced by hand, and an error message naming the variable instead of
the bare `invalid literal for int()` that `int(raw)` produced. Domain modules
pass `env_names=` and retain only the decision of whether a bad value is fatal —
`EWMRS/render/render.py`'s `_resolve_tile_workers` still swallows one, because it
alone has a working fallback in the CPU cap.

Preserve existing aliases for one deprecation window:

- `--base_dir` and `--base-dir`.
- `EDGEWARN_BASE_DIR` for the EdgeWARN API and Python.
- `BASE_DIR` for the EWMRS API.
- Existing rate-limit, RAP-age, render-memory, tile-thread, GOES cleanup, and
  NEXRAD worker environment variables.

Document each alias and map it to one dotted key. Warn when deprecated names
are used, but do not change their precedence.

## Exhaustive source inventory and destination

The inventory below covers production Python and JavaScript at the audit
baseline. “Extract” includes values currently expressed as function defaults,
constructor defaults, inline conditions, and duplicated lists, not only
uppercase constants.

### Filesystem and artifact layout

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `src/util/file.py` | Default base directories (Windows `C:\EdgeWARN_input`, POSIX `~/EdgeWARN_input`, workspace fallback), the cleanup age/count defaults, the `.idx`/`.gz` scan skip rules, and the colormap search path | `filesystem.yaml` |
| `src/util/file.py` | The ~113 MRMS/GOES/RAP/METAR/NWS/stormcell/cell/alert/NEXRAD directory and manifest names | **Stays in code.** Derived from base dir; referenced by catalogs via attribute name |
| `src/api/config/index.js` | Base-dir fallback and all API data/index directory mappings and required-directory list | `filesystem.yaml` plus `api.yaml` |
| `src/EdgeWARN/api/`, `src/EWMRS/api/` | Legacy API services | Resolved: server source was deleted in `a3d6cbb`; only `src/api/` is active |
| `src/EWMRS/api/routes/colormaps.js`, `src/EWMRS/api/routes/rap.js`, `src/EWMRS/pipeline.py` | Source-relative `colormaps.json` and `mappings.json` paths | Out of scope for this plan; see the follow-up note below |
| `src/EWMRS/colormaps.json`, `src/EWMRS/mappings.json` | Colormap payload; RAP layer-to-colormap mapping | **Out of scope.** These stay where they are for now |

`filesystem.yaml` stores base-directory candidates and the colormap search path.
The loader joins derived names to the selected runtime base directory via
`util.file`. It must reject `..` traversal in repository defaults.

Relocating `colormaps.json`, deleting `mappings.json` as an independent
authority, and reconciling the `RAP_BestLiftedIndex_180_0mbAGL` drift noted above
are a **separate follow-up**, not part of wiring the 19 files. They require a
Node-side path change and an API response change, which is a different blast
radius from reading YAML.

### Product and data-source catalogs

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `src/common/ingest/mrms/config.py` | NOAA MRMS and GOES bucket names; all 28 MRMS region/modifier/output entries; 12 readiness entries; GLM and 16 ABI channel ingest specs; ABI source product; per-channel names/path keys/max-files | `mrms_goes.yaml` |
| `src/common/ingest/mrms/main.py` | Three detection modifiers and derived integration/EWMRS memberships | `mrms_goes.yaml` as named lists, cross-checked against `mrms.products` by test |
| `src/EdgeWARN/process/integrate/config.py` | All MRMS output/statistic definitions and all RAP integration product definitions | `integration.yaml` (`stats_datasets` and `rap_products`) |
| `src/EdgeWARN/process/integrate/core/integrator.py` | Complete ProbSevere source-field to output-field map | `integration.yaml` |
| `src/EdgeWARN/process/integrate/integrate_rap.py` | Names chosen from the trusted transform registry; derived-field formula strings | `integration.yaml`; implementations stay in code. See the derived-formula note below |
| `src/EWMRS/render/config.py` | Fifteen MRMS render layers and 16 GOES ABI layers, including source variables, fallbacks, ranges, colormaps, transforms, prefixes, and `fs.*` path attributes | `ewmrs_render.yaml` |
| `src/EWMRS/rap/config.py` | 43-layer Uint16 display catalog, pressure levels, filters, scales, descriptions, output names, and colormap selection | Deliberately stays in this typed code registry. `ewmrs_rap_uint16.yaml` owns only retention/timestamp/force policy; extracting the display catalog would be a separate schema migration |
| `src/common/ingest/nexrad/config.py`, `grouping.py` | Source buckets/API, allowed VCPs, elevation dedup/range policy, canonical elevation bins/readiness IDs, and supported waveform policy | `nexrad.yaml` |
| `src/EWMRS/render/nexrad.py` | VCP-to-elevation labels and NEXRAD variable-to-colormap mapping | `nexrad.yaml` (selection policy) and `ewmrs_render.yaml` (colormaps) |
| `src/api/routes/` NEXRAD validation | Duplicated allowed NEXRAD product set | Derived from `nexrad.yaml`; regexes remain code |
| `src/api/config/product-catalog.json`, `src/api/config/productCatalog.js` | 31-entry product catalog and route mappings | Cross-checked against `mrms_goes.yaml` and `ewmrs_render.yaml` by test |
| `src/EdgeWARN/process/detect/tools/alert_matcher.py` | Convective/flood event allowlist used for cell matching | `integration.yaml` |
| `src/common/ingest/nws/main.py` | NWS dropped-event blocklist | `nws.yaml` |
| `src/common/ingest/nws/zone_sync.py` | Zone-type catalog used by the maintenance sync | `nws.yaml` |
| ~~`src/common/ingest/wpc/config.py`~~ | **Done.** WPC feature types and display styles (`FEATURE_TYPES`, 7 front/pressure types with colors — undocumented in the audit). Snapshotted, because the converter's fallback makes a dropped entry render unnamed and black rather than fail | `wpc.yaml` |
| ~~`src/common/ingest/wpc/converter.py`~~ | **Done.** The two duplicated feature-type lookups collapse into one `_style_for` helper. The output metadata labels (`source: WPC`, `product: Surface Analysis`) stayed in code: they identify the product itself, not a setting | `wpc.yaml` |

There are no GOES RGB rows in this table. `src/EWMRS/render/goes_rgb.py`,
`GOES_RGB_RECIPES`, the terminator angles, the solar cache, the gamma values and
the green-band blend **no longer exist in the tree** (confirmed absent at this
baseline); `src/EWMRS/render/config.py` documents derived color products as a
client-side concern. Earlier drafts of this plan listed them.

The current MRMS catalog must be transcribed losslessly, including
EchoTop 18/30/50; all FLASH products; RQI; MESH; NLDN; precipitation/QPE;
low/mid AzShear; VIL/VIL density/VII; ProbSevere; RhoHV; PrecipFlag; RALA;
composite reflectivity; and reflectivity at 0, -5, and -15 °C.

The MRMS integration portion must preserve every current output and statistic:
reflectivity at 0/-5/-15 °C, NLDN, EchoTop 18/30/50, VIL, VIL density,
low/mid AzShear, precipitation rate, RALA, and VII, including every
max/percentile output name and percentile value. The migration test must also
make the currently implicit default statistic on the -15 °C reflectivity
entry explicit.

The RAP integration catalog (`get_rap_products()`, undocumented in the audit)
must preserve its 37 isobaric levels × u/v, 10 m winds, 2 m temperature/dewpoint,
freezing level, and 2 derived values. The EWMRS RAP catalog must preserve the
separate display-layer set and scale ranges; it is **43 layers, not the 29 the
audit reports**, and it is not assumed to be identical to integration needs.
`Dewpoint_2m`, `CIN`/`MLCIN`/`MUCIN`, `SnowWaterEquivalent`, `SnowDepth`,
`WetBulbZeroHeight`, `FreezingLevelHeight` and `LiftedIndex` are present in the
tree but absent from the audit.

### Remote ingest and selection policy

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `src/common/ingest/mrms/https_client.py` | MRMS 2D base URL and HTTP timeout; the 16-entry S3-modifier→NCEP-directory map (`:73-90`) with its `split("_00.")` fallback (`:96-100`); the hardcoded ProbSevere URL at `:107`, which does **not** use `NCEP_BASE_URL` | `mrms_goes.yaml`; timeout to `runtime.yaml` |
| `src/common/ingest/mrms/parse.py`, `downloader.py` | S3 key templates: `{region}/{modifier}/{YYYYMMDD}/` and the modifier-`None` variant (`parse.py:4-27`); the `MRMS_{modifier}_{YYYYMMDD}-{HH}` prefix (`downloader.py:274-275`); the ProbSevere `StartAfter` `MRMS_PROBSEVERE_{YYYYMMDD}_{HH}` (underscore, not hyphen, `:305-306`); GOES `{product}/{YYYY}/{DDD}/{HH}/` with Julian day (`parse.py:30-55`) | `mrms_goes.yaml` as five named templates. The currently recorded `path_patterns` are fabricated — see corrections |
| `src/common/ingest/mrms/downloader.py`, `s3_common.py`, `s3_sync.py`, `s3_async.py` | Search-entry limits, GOES hour lookback, GLM one-minute window, per-product cleanup age/count, download concurrency policy, and decompression chunk size | `mrms_goes.yaml` product overrides; shared policy in `runtime.yaml` |
| `src/common/ingest/mrms/timestamp_utils.py` | MRMS nominal two-minute cadence and midpoint rounding/selection policy | `mrms_goes.yaml` |
| `src/common/pipeline/coordinator.py`, `goes_readiness.py` | Max entries, GOES lookback, candidate count, and 20-minute source offset | `runtime.yaml` |
| `src/common/ingest/synoptic/config.py`, `downloader.py` | RAP bucket/path patterns, age/file limits, hourly lookback behavior, and environment alias | `synoptic_rap.yaml` |
| ~~`src/common/ingest/metar.py`~~ | **Done.** Station database URL (`https://aviationweather.gov/data/cache/stations.cache.json`, not the audit's `api.aviationweather.gov/v1/stations/`) and cycle URLs; the two request timeouts; station cache filename; CONUS bounds (lon `-125.0..-66.0`, not `-125..-67`); lookback; rounding; retention. The user agent is not a `metar.yaml` key — it resolves from `runtime.yaml identity` | `metar.yaml` |
| `src/common/ingest/nws/main.py`, `registry.py`, `geomapper.py` | Alerts URL; user agent/contact; request/chunk settings; two-hour registry TTL; property drop list; geometry rounding/simplification | `nws.yaml` |
| `src/common/ingest/nws/zone_sync.py` | API URL templates, timeout, retries/backoff, worker count, pause, geometry precision, output path policy, and the `:160` user agent | `nws.yaml` `zone_sync` section |
| ~~`src/common/ingest/wpc/config.py`, `downloader.py`, `main.py`~~ | **Done.** Source URL, valid hours, source cadence, timeout, backfill lookback, file templates, cleanup glob/age, and the `FEATURE_TYPES` styling table | `wpc.yaml` |
| `src/common/ingest/nexrad/config.py`, `s3_chunks.py`, `s3_async.py`, `main.py` | Buckets; station catalog URL (`https://api.weather.gov/radar/stations`, not the audit's `/api/stations`); user agent; timeout; cache TTL (`30`, not `0`); minimum volume chunks; chunk download semaphore (`max_chunk_downloads = 64`, not `8`); volume candidate count and volumes/site; heartbeat stale `240.0` and startup grace `60.0` (now reached through the accessors at `:139-151`) | `nexrad.yaml`; shared concurrency in `runtime.yaml` |

**Done.** TLS verification policy disabled in METAR code is now `metar.verify_tls`,
still `false`, routed through `metar_config.ssl_context` and
`metar_config.aiohttp_ssl` so the five independent downgrades became one decision
that warns on every use while it is off. Flipping the key to `true` is verified to
actually verify, so the remaining work is a run against the live hosts, not code.

**WPC was wrongly named here**: both of its download paths already set
`check_hostname` and `CERT_REQUIRED`, so `wpc.verify_tls` pins that behavior
rather than offering to relax it — the schema fixes it to `true` and the
downloader raises if it ever reads false. METAR was the only subsystem that
needed the warn-when-false treatment.

Contact/user-agent values must not
contain a copied package version; the loader formats a configured template
using `package.json`.

### Detection, tracking, lineage, and alerts

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `src/util/io.py`, `src/EdgeWARN/pipeline.py`, `process/detect/detect.py`, `process/detect/main.py`, `tools/gatemapper.py` | Reflectivity threshold `37.5`, seed ratio `0.001`, drop offset `10`, processing bounds, and duplicated function/CLI defaults — **8 sites each**, enumerated below | `detection.yaml`, with CLI overrides |
| `tools/gatemapper.py` | Baseline floor/cap, adaptive 37.5/40/45/52 dBZ rules, crop padding, minimum retained gates, contour sampling steps/thresholds, coordinate precision, and `all_touched` policy. **These are inline literals at `:101` and `:174`, not parameters** — see corrections | `detection.yaml` |
| `process/detect/morphology.py` | Minimum full-analysis pixels and contour-defect points | `detection.yaml` |
| `process/detect/tools/save.py` | Polygon/centroid precision, hail-core contour sampling step, and serialization choices that affect output geometry | `detection.yaml` |
| `process/detect/detect.py` | Detection executor worker count | `runtime.yaml` |
| `process/detect/track.py` | Tracker overlap, fallback scan interval, decay threshold/factor/floor, and diagnostic sample limits | `detection.yaml` and `runtime.yaml` |
| `process/detect/lineage/detector.py`, `lineage/spatial.py`, `lineage/buffer.py` | Lineage overlap, confirmations, pending limit, prune scans, scan interval, and buffer filename | `lineage.yaml`; buffer filename derived in `util.file` |
| `process/detect/kalman/config.py`, `config/kalman.yaml` | Process/measurement noise, tracking confidence/prediction/reacquisition, assignment gate/weights/method/covariance and assignment motion cutoffs | `kalman.yaml`, with the inline `.get()` fallbacks deleted |
| `process/detect/kalman/confidence.py` | Confidence time penalty, motion-variance scale/floor, position-uncertainty threshold/scale/floor, and confidence-status display bands | `kalman.yaml` |
| `process/detect/kalman/filter.py` | Initial position uncertainty, reference origin if operational, innovation regularization, and direct gate defaults still copied from assignment configuration | `kalman.yaml`; matrix equations and unit conversions stay in code |
| `process/detect/kalman/assignment.py`, `state.py` | Near-stationary/implied-motion cutoffs, fallback interval, and reference origin if still operationally selectable | `kalman.yaml`; physical conversion constants stay in code |
| `process/detect/main.py` | Stormcell cleanup age and tracking fallback interval | `runtime.yaml` |
| `alerts/manager.py`, `src/EdgeWARN/alerts/schema.py`, alert payload modules | Alert cleanup age, default severity, geometry precision | Deferred with the rest of CTAM; these literals stay their own sole owners for now |
| `api_integration/index_manager.py`, `pipeline.py`, `process_historical.py` | Resync update count, inactive-cell retention, and the realtime/historical splits for cell expiry and index bootstrap | `api_index.yaml` |

Do not flatten different overlap concepts into one setting. Use names such as
`tracking.lineage_overlap_ratio`, `lineage.event_overlap_ratio`, and
`lineage.spatial_query_overlap_ratio`, with descriptions and valid ranges. The
`0.15`/`0.10` split noted above may be an intentional policy distinction; give
the two values distinct names rather than accidentally unifying them.

### Integration and scientific policy

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `process/integrate/core/stats.py` | Output decimal precision and default statistic/percentile policy | `integration.yaml` |
| `process/integrate/core/integrator.py`, `geometry/cell_polygon.py`, and integration helpers | ProbSevere field map; coordinate-key precision; minimum fallback polygon size; duplicate-overlap and distance tolerance policies; chosen percentiles and buffers | `integration.yaml` |
| `process/integrate/azshear/constants.py`, `azshear/integration.py`, `azshear/metrics.py` | Buffer, low/mid thresholds, minimum gate count, maximum pair separation, five-entry history window, coordinate/output precision, p95 statistic, overlap/dedup tolerances, spacing multiplier/floor, and pairing/alignment policy | `integration.yaml` |
| `process/integrate/integrate_glm.py` | GLM spatial bin size and related matching/search policy | `integration.yaml` |
| `process/integrate/io/rap_files.py` | GRIB variable aliases and target pressure-level expectations currently repeated in fallback dataset scoring | Derive from `integration.yaml` `rap_products`; scoring mechanics stay code |
| `process/integrate/pipeline.py` | Enrichment concurrency cap/policy | `runtime.yaml` |
| `process/integrate/history.py` | No current configurable limits found; retain serialization mechanics in code | Intentional no-op |

### Derived formulas and transforms

Statistic implementations remain trusted registries in Python. Configuration
selects `max`, `percentile`, `kelvin_to_celsius`, or another registered name.

Earlier drafts of this plan asserted that "existing derived RAP formula strings
must be replaced by named, tested derived-field implementations". **That is
wrong and is hereby corrected.** `src/EdgeWARN/process/integrate/integrate_rap.py`
already implements a bounded safe-AST evaluator at `:142-200`: 7 binary operators,
2 unary operators, `ast.Name` resolved against cell properties, numeric
`ast.Constant`, and `raise ValueError` on anything else. It compiles each formula
once via `ast.parse(..., mode="eval")` and never calls `eval`. Moving
`derived[].formula` into `integration.yaml` is therefore lift-and-shift, not a
rewrite. The loader's obligation is to confirm each formula string parses under
that grammar at startup rather than failing per-cell at runtime.

`products[].transform` is a different case and **does** need validation. It is a
registry key looked up in `TRANSFORMS` (`integrate_rap.py:11-14`), but `:87` uses
`TRANSFORMS.get(name, lambda x: x)` — a **silent identity fallback**. A misspelled
transform today produces unconverted Kelvin values with no error. The loader must
validate every `transform` against `TRANSFORMS.keys()` and fail on an unknown
name.

### CTAM: out of scope

CTAM MorphoWind and StormProb tunables are **not part of this plan**. No CTAM
config file exists under `config/`, and creating `config/ctam/morphowind.yaml`
and `config/ctam/stormprob.yaml` would add a subdirectory to an otherwise flat
tree and a new nesting convention, while the 18 existing files are still
unconsumed.

CTAM extraction is a separate follow-up, to be planned once the loader is proven
against the flat files. The inventory of what it would cover — QLCS/microburst/
collapse thresholds, blend weights, smoothing parameters, Bunkers parameters,
lead times, and the module registry order — is preserved in the git history of
this document. Standard-atmosphere and covariance equations would stay in Python
regardless.

### Rendering and presentation

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `src/EWMRS/pipeline.py` | GUI cleanup ages; cleanup minimum interval; source freshness; render/process worker budgets; reserve memory; NEXRAD render poll/workers/retention; tile-index cache size | `ewmrs_pipeline.yaml` |
| `src/EWMRS/pipeline.py` | `WEB_MERCATOR_BOUNDS`, `WEB_MERCATOR_SHAPE`, `WEB_MERCATOR_TRANSFORM` | **Stays in code** as projection invariants |
| `src/EWMRS/render/config.py` | Chunk size/grid and layer definitions | `ewmrs_render.yaml` |
| `src/EWMRS/render/render.py` | Tile thread default/override and colormap cache size | `ewmrs_pipeline.yaml` |
| `src/EWMRS/render/tools.py` | Deprecated duplicate fixed render bounds | Remove and derive; CRS IDs and timestamp/file-format parsing remain code |
| `src/EWMRS/render/goes_transform.py` | Resampling method | `ewmrs_render.yaml` `goes_transform.resampling` only. The radian-detection and CRS-strategy pseudo-expressions are deleted — see corrections |
| `src/EWMRS/rap/uint16_pipeline.py` | Number of retained RAP timestamps and force behavior default | `ewmrs_rap_uint16.yaml` |
| `src/EWMRS/render/nexrad.py` | Product colormaps and VCP sweep labels | `nexrad.yaml` and `ewmrs_render.yaml` |

The render path emits **float16 value chunks, not PNG tiles**: the current
artifacts are `chunk_{x}_{y}.f16.gz`, and the audit's `tile_{x}_{y}.png` and
compress-level `1` are stale. The `CHUNK_*` constants are undocumented in the
audit. Chunk grid rows and columns should be derived and validated from configured
shape and chunk size, with no separately editable copies.

The EWMRS chunk wire format is recorded in `ewmrs_render.yaml` for visibility
because API clients depend on it, and is marked **non-tunable**. Its
`schema_version: 2` key is a wire version and must be renamed to
`chunk_format.wire_version` to avoid colliding with the config `schema_version`.

### Runtime scheduling, retention, and resources

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `src/run.py`, `src/util/runtime/cycle.py`, `goes.py` | GOES poll/wait intervals, optional ingest pause, cycle polling cadence, cycle retry attempts/backoff, render-wait poll granularity and interval floor, and the render-task entry cap. DONE — now read at `run.py:48-49,167,187-191,227,416`, `goes.py:54,57,71` (and the second waiter at `:87,90,107`), and `cycle.py:609` | `runtime.yaml` |
| `src/util/runtime/background.py` | METAR 5-minute boundary, NWS 120-second poll, WPC 15-minute boundary, GOES poll, NEXRAD restart backoff, and interruptible-sleep granularity where operational. DONE — each loop reads its own section at `:77,204,255,270,284` | `runtime.yaml` |
| `src/util/runtime/processes.py` | Graceful join and forced-stop timeouts and the **supervisor restart policy**. DONE — `stop_process` takes `join_timeout=None` and falls through to `supervisor.stop_join_timeout_seconds` / `stop_kill_join_timeout_seconds` (`:12,17-18,25,30`), and the four restart-policy fields are `field(default_factory=...)` reads at `:78-81` | `runtime.yaml` |
| `src/process_historical.py` | One-minute step, one-second throttle, historical bounds/output defaults | `historical.yaml` plus CLI overrides |
| `src/EdgeWARN/schedule/scheduler.py` | MRMS search entries/lookback and slow-operation logging threshold | `scheduler.yaml`. The two bare `ThreadPoolExecutor()` calls are **excluded**: they have no cap today, so adding one is a retune, not an extraction |
| `src/EdgeWARN/pipeline.py` | Historical ingest directories retained per family | `runtime.yaml`, with directory names derived |
| `src/util/file.py` | Generic cleanup age and count defaults | `filesystem.yaml` |
| `src/util/performance.py` | Performance tracker enablement and future thresholds | `runtime.yaml` with existing env alias |
| `src/common/ingest/nexrad/pipeline/__init__.py` | Scan/completion intervals and volume candidate defaults. Both intervals are re-clamped `max(1.0, ...)` at `:101-102`, and `coordinator.py:100` clamps `max(1, ...)` — schema needs `minimum: 1` | `nexrad.yaml` with CLI overrides |
| `src/common/ingest/nexrad/service.py`, `worker_pool.py` | Site/chunk concurrency, parse checkpoint, prefetch, pool size, recycle interval, timeout, and memory behavior | `nexrad.yaml` with existing env aliases |
| `src/common/ingest/nexrad/writer.py` | Scan/elevation directories retained and stale-manifest age | `nexrad.yaml` |
| `src/common/pipeline/coordinator.py` | The ingest entry cap. DONE — `max_entries` defaults to `None` (`:126`) and resolves to `cycle.ingest_max_entries` at `:145-146`, so the keyword no longer carries a second copy of the number | `runtime.yaml` |

Hardware-derived defaults may use a named strategy such as
`render.workers.strategy: adaptive_memory`. Its tunable caps, reserves, and
minimums belong in config; the CPU/memory calculation remains code.

### Explicitly classified source constants

The mechanical audit also finds the following files. They do not create
additional configuration authorities after the migrations above:

| Source | Classification |
| --- | --- |
| `src/common/ingest/mrms/parse.py`, `utils.py` | GOES/MRMS bucket and timestamp grammar, day-of-year conversion, NetCDF merge mechanics, and function parameters supplied by configured callers are protocol/implementation code. |
| `src/common/ingest/nexrad/parser.py`, `stream.py` | Record sizes, header lengths, archive magic, message/block/status codes, and stream overlap are Level-II format invariants. Elevation/product policy moves to config. |
| `src/common/ingest/nexrad/weather_api.py` | Consumes configured URL, timeout, user agent, and cache TTL; parsing and cache mechanics remain code. |
| `src/common/ingest/nexrad/worker.py` | Process memory cleanup and partial-volume parsing mechanics remain code; its pool/recycle/timeout settings move from the parent modules. |
| `src/common/ingest/wpc/parser.py` | Coded surface bulletin keywords and coordinate grammar are protocol vocabulary; feature enablement/styles move to config. |
| `src/EdgeWARN/process/detect/lineage/spatial.py` | Antimeridian handling, polygon minimum-point rules, floating-point degeneracy epsilon, and deterministic parent/child tie-breaking are geometry/safety mechanics. The caller's overlap policies move to config. |
| `src/EdgeWARN/process/integrate/grid_index.py` | Regular-grid floating-point tolerance and index arithmetic are numerical safety mechanics; processing domain and scientific matching tolerances move to config. |
| `src/EWMRS/render/tools.py` | CRS definitions, file timestamp regexes, and transformation mechanics remain code; duplicate domain bounds are removed as noted above. |
| `src/EWMRS/render/goes_transform.py` | Radian-unit detection (`:241-244`) and CRS-strategy selection are algorithm branches, not settings. Only the resampling method is configurable. |
| `src/EWMRS/rap/config.py:17-66` | `_wind_colormap_key`, `_temperature_colormap_key`, `_with_colormap_key` are prefix/suffix matching helpers; the `colormap_key` values they produce are configured, the matching is not. |
| `src/EWMRS/rap/uint16_pipeline.py:135-151` | The `np.rint` uint16 quantization is a hardcoded algorithm. `UINT16_VALID_MAX`/`UINT16_NODATA` (`rap/config.py:7-8`) are format constants. Per-layer `scale.min`/`scale.max` are configured. |
| `src/util/handler.py` | GRIB time-coordinate decoding is a format rule. |
| `src/util/release.py` | Package-version discovery stays code and reads `package.json`. |
| `src/util/runtime/timing.py` | Interruptible-sleep arithmetic stays code; operator-facing cadences and retry intervals come from `runtime.yaml`. |

Small numerical epsilons used solely to prevent division by zero, repair
invalid polygons, regularize a singular matrix, or clamp a probability are
algorithm safety constants unless they materially alter a configurable
scientific decision boundary. Named scientific regularization values already
present in `kalman.yaml` remain configurable; local machine-epsilon guards do
not.

### Node API service

`src/api/` is the current service and the only one this plan wires. The legacy
server source under `src/EdgeWARN/api/` and `src/EWMRS/api/` was retired in
`a3d6cbb`; do not recreate legacy adapters solely to give them configuration
ownership.

| Current source | Values to extract | Destination |
| --- | --- | --- |
| `src/api/config/index.js` | Ports/host, rate windows/maxima, CORS origins/methods/headers/credentials, trust-proxy policy, HSTS/CSP policy, JSON body limit, health-check limiter bypass, compression policy, cluster worker cap, and logging mode | `api.yaml` |
| `src/api/middleware/` | Security and rate-limit policy values | `api.yaml` |
| `src/api/repositories/artifactRepository.js` | Cache entries, default/index TTLs, per-worker byte budget | `api.yaml` |
| `src/api/services/validation.js`, `services/renders.js` | Validation limits and render list limits | `api.yaml` |
| `src/api/routes/v3/` | Route-specific response cache-control TTLs | `api.yaml` |
| `src/api/config/product-catalog.json`, `config/productCatalog.js` | 31-entry product catalog and route mappings | Kept as the Node-side catalog; cross-checked against the Python catalogs by test |
| `src/api/server.js`, `app.js` | Copied package version | Read `package.json`; production redaction mode in `api.yaml` |

Route tables, deprecation headers, `ArtifactError` status mapping, the ETag
format, HTTP status meanings, path-validation regexes and the error schema remain
API contract code — they are protocol behavior, not tunables. Security settings
must be validated conservatively: production cannot silently broaden CORS or
proxy trust because a config key is missing.

## Corrections required before the files can be loaded

The 19 files were produced by transcription, and the transcription has errors.
**This table gates implementation** — the files cannot be wired as written. Each
row was verified against the source at this baseline.

| File / key | Recorded | Source says | Action |
| --- | --- | --- | --- |
| `mrms_goes.yaml` `path_patterns.{raw,decoded}` | `'regional/.../{YYYYMMDD}'` | Fabricated. No raw/decoded split exists, and `"regional"` appears nowhere in `src/common/ingest/`. `parse.py:4-27` → `{region}/{modifier}/{YYYYMMDD}/`, or `{region}/{YYYYMMDD}/` when modifier is `None`. `downloader.py:274-275` appends `MRMS_{modifier}_{YYYYMMDD}-{HH}`. `downloader.py:305-306` ProbSevere uses `StartAfter` `MRMS_PROBSEVERE_{YYYYMMDD}_{HH}` (underscore, not hyphen). `parse.py:30-55` GOES → `{product}/{YYYY}/{DDD}/{HH}/`, Julian day | Replace with five named templates |
| `mrms_goes.yaml` | omits | `https_client.py:73-90` holds a 16-entry S3-modifier→NCEP-directory map with a `split("_00.")` fallback at `:96-100`; `:107` hardcodes `https://mrms.ncep.noaa.gov/data/MRMS_ProbSevere`, which does **not** use `NCEP_BASE_URL` | Add both keys |
| `ewmrs_render.yaml` `mrms_layers` | `{name, colormap_key}` | `render/config.py:44-137` — each dict also carries `filepath`/`outdir` as `fs.*` attributes. Note the asymmetry `MRMS_DVIL_DIR` → `GUI_VILD_DIR` | Add the attribute names |
| `ewmrs_render.yaml` `goes_layers` `mask_min`/`mask_max` | scalars | `render/config.py:178-185` — dicts keyed by `channel_id` plus a `"default"` key. C10 is `185.0..320.0`, not the audit's uniform floor of `180` | Preserve the dict shape |
| `ewmrs_render.yaml` `goes_layers` | omits | source dicts also carry `channel_id`, `display_name` (an f-string), `value_transform` and `source_type` | Record or derive |
| `ewmrs_rap_uint16.yaml` `scale_rule` | `'rint((v - scale.min) / ... * 65534)'` | `uint16_pipeline.py:135-151` is hardcoded `np.rint`; `65534`/`65535` are `UINT16_VALID_MAX`/`UINT16_NODATA` at `rap/config.py:7-8` | **Delete the key** — algorithm, not setting. Keep per-layer `scale.min`/`scale.max` |
| `ewmrs_render.yaml` `goes_transform.radian_detection` | `'units == "rad" or max(abs(coord)) <= 2.0'` | `goes_transform.py:241-244` — a substring test `"rad" in units`, `np.nanmax` not `max`, evaluated independently per x and y axis. The paraphrase is wrong on all three counts | **Delete**; only `resampling: bilinear` is a real knob. `crs_strategy` likewise |
| `ewmrs_render.yaml:12` `schema_version: 2` | chunk wire format | Collides with the config-level `schema_version` this plan requires | Rename to `chunk_format.wire_version` |
| `integration.yaml` `stats_datasets` Ref15 `method: max` | explicit | `integrate/config.py:18-22` omits `"method"` entirely | Confirm `stats.py`'s implicit default really is `max` before calling this value-preserving |
| `integration.yaml` `output.decimals: 2` | config | `integrate_rap.py:135` and `:156` hardcode `round(..., 2)` | Must be parameterized or the key is inert |
| `integration.yaml` `rap_products[].transform` | `kelvin_to_celsius` | Registry key with a silent identity fallback (`integrate_rap.py:87`) | Loader validates against `TRANSFORMS.keys()` |
| `nexrad.yaml` `cli.sites: []` | `[]` = all sites | `pipeline/__init__.py:74` distinguishes `None` from `[]`; `[]` is not "all" | Use `null` |
| `nexrad.yaml` `cli.scan_interval_seconds` / `completion_interval_seconds` | `20` / `10` | Re-clamped `max(1.0, ...)` at `pipeline/__init__.py:101-102`; `coordinator.py:100` clamps `max(1, ...)` | Schema `minimum: 1`. Both keys also moved out of `cli` into `realtime`, since production reaches the pipeline through `background.py` and never through argparse |
| `nexrad.yaml` `cli` | omitted it | `nexrad/main.py:183` also exposes `--max-candidate-volumes-per-site` | **Done.** Added as `realtime.max_candidate_volumes_per_site`, not under `cli`: the one-shot coordinator needs it too, so it is a pipeline setting a flag may override |
| `nws.yaml` `zone_sync.pause_seconds` | `0.0` | argparse `0.0` versus constructor `0.05` — real drift | **Done.** Both defaults are now `None` (`zone_sync.py:169`); the value resolves from YAML at `:189` for the constructor and `:477` for argparse, so there is one owner and nothing to disagree with |
| `nws.yaml` `zone_sync.assets_dir` | `assets/nws_zones` | `_resolve_assets_dir()` probed the filesystem | **Done.** Literal wins and the probe is gone. `zone_sync.py:471` joins the key onto `repo_root(args.config_dir)`, so the path follows `--config-dir` instead of whichever candidate happened to exist; the constructor takes it as a required `Path` (`:164`) with no fallback |
| `nws.yaml` | omitted it | `zone_sync.py` `user_agent`, which differed from `nexrad.yaml` `stations.user_agent` in both version and contact | **Done.** Neither file owns a user agent now. The constructor parameter defaults to `None` (`zone_sync.py:170`) and `weather_api_headers` builds the header at `:194` from `runtime.yaml identity`, interpolating the `package.json` version |
| `detection.yaml` `gatemapper.baseline_refl_floor`, `dynamic_min_threshold` | recorded as settings | `gatemapper.py:101` `min(37.5, self.refl_threshold)` is a **hard floor**, so raising `refl_threshold` above 37.5 has no effect on the baseline mask; `gatemapper.py:174` `np.where(valid_max_refl < 45.0, 37.5, 40.0)` is fully inline | Must be parameterized or these keys are inert and misleading |
| `filesystem.yaml` colormap search path | `src/EWMRS/colormaps.json` | `file.py` resolved it relative to `__file__`, not cwd | **Done.** `<src_dir>/EWMRS/colormaps.json` plus a `<gui_dir>` fallback; the `Path.cwd()` candidate that led the list is gone |
| `runtime.yaml` | omitted them | supervisor restart policy; `stop_process` join timeouts; background loop cadences; render-wait poll granularity and interval floor; the two `max_entries=10` literals; NEXRAD heartbeat stale/startup-grace | **Done.** `runtime.yaml` now owns all of them, read at `processes.py:17-18,30,78-81`, `background.py:77,204,255,270,284`, `goes.py:54,57,71`, `cycle.py:609`, and `coordinator.py:145-146`. The NEXRAD pair stayed in `nexrad.yaml` (`timeouts`) rather than moving here, since it is a subsystem timeout and not a shared runtime one — reached via `nexrad/config.py:139-151` |
| all 19 files | no `schema_version` | this plan requires one | Add `schema_version: 1` |

Rows marked "must be parameterized or the key is inert" are the dangerous class:
the YAML *looks* authoritative, so an operator can change the value and observe no
effect. Either thread the value through to the call site or delete the key. Do not
ship a config key that silently does nothing.

## Duplicate defaults to remove

For YAML to ever win, each setting needs exactly one base default. These values
currently have several, and there is no module-level constant to point at.

- **`refl_threshold` / `min_seed_percentage` / `drop_offset`** — 8 sites each:
  argparse at `src/util/io.py:88-90`, then keyword defaults at
  `EdgeWARN/pipeline.py:148-150`, `:216-218`, `:378-380`,
  `process/detect/detect.py:24-26`, `process/detect/main.py:29-31`, `:100-102`,
  and `tools/gatemapper.py:12`. Plus the two inline literals at
  `gatemapper.py:101` and `:174` noted above.
  `TandemCycleConfig` (`util/runtime/cycle.py:323`, fields at `:330-332`) declares
  these with **no** defaults and is the correct seam — leave it alone.
- **`max_candidate_volumes_per_site`** — was 3 copies. **Done.** All three now
  default to `None` and resolve through `nexrad_config` (`pipeline/__init__.py:57`,
  `coordinator.py:27` resolving at `:29-30`, `pipeline/volume_discovery.py:67`
  resolving at `:69-70`), so `realtime.max_candidate_volumes_per_site` is the sole
  owner.
- **`max_volumes_per_site`** — **no longer a catalog key.** The `nexrad.yaml` entry
  was deleted rather than wired: the two functions that honour the parameter have
  no callers outside `nexrad/__init__.py`'s re-export and tests, so threading it
  through would have made a dead path configurable. Its four `=1` signature
  defaults stay at `nexrad/main.py:110`, `:154` and `nexrad/service.py:1286`,
  `:1314` — with no key claiming ownership they are ordinary library defaults, not
  a second copy. A test pins them.
- **`zone_sync`** — 6 keys were duplicated across argparse and
  `NWSZoneSync.__init__`, one of which disagreed with its flag. **Done.** Every
  optional parameter in that signature is now `None` (`zone_sync.py:162-171`) and
  the docstring at `:173` records that unsupplied settings come from
  `nws.yaml zone_sync`.
- **`kalman.yaml`** — every `.get()` call in
  `process/detect/kalman/config.py` passes an inline literal fallback, and the
  dataclass field defaults repeat them a third time.

## Migration phases

### Phase 0: Freeze and characterize current behavior

1. Add characterization tests that serialize the effective values returned by
   current MRMS/GOES/integration/render/RAP/NEXRAD config functions.
2. Snapshot current path keys, API mappings, colormap keys, CLI defaults,
   environment aliases, timers, retention, and scientific parameters.
3. Add explicit tests for the known drift points before deciding whether each
   is a bug or an intentional distinction.
4. Record config-sensitive benchmark baselines for ingest selection,
   detection, integration, EWMRS rendering, and NEXRAD parsing.

This phase is value-preserving. Do not combine extraction with scientific
retuning.

### Phase 1: Build config loading and validation

1. Add Python and Node dependencies for YAML. JSON Schema validation is
   hand-rolled in both loaders over a shared keyword set — see the loader
   contract above for why no validator dependency is taken.
2. Implement `src/common/config/loader.py` with config-root discovery, caching,
   memoization, immutability, provenance, schema versioning, and dotted-key
   errors. Stdlib + `yaml` imports only.
3. Write the 18 `config/schema/<name>.schema.json` files and add
   `schema_version: 1` to all 19 config files, renaming the
   `ewmrs_render.yaml` wire version first.
4. Convert argparse to `None` sentinels across the five entrypoints listed
   above, so CLI-beats-YAML is expressible.
5. Add `--config-dir` and `EDGEWARN_CONFIG_DIR` to Python and the Node
   entrypoint.
6. Implement CLI/environment overlay adapters without putting environment
   parsing inside domain modules.
7. Add a `validate-config` command usable in CI and deployment checks.

### Phase 2: Base-directory resolution and ingest sources

1. Implement two-phase `base_dir` resolution using
   `IOManager.get_base_dir_arg()`, and make `util/file.py` stop binding paths at
   import time.
2. Make the Node API consume the same base-directory resolution.
3. Move endpoints, buckets, file templates, user agents, timeouts, and TLS
   policy into the per-subsystem ingest files.
4. Keep legacy base-directory names working through the overlay layer.
5. Test the same config against POSIX and Windows path construction.

Artifact directory names are **not** migrated in this phase or any other; they
stay derived in `util/file.py`.

### Phase 3: Correct and complete the existing catalogs

Driven by the corrections table above, not by transcription from scratch — the
transcription already happened.

1. Fix MRMS first: replace the fabricated `path_patterns` with the five real
   templates, add the NCEP directory map and the ProbSevere URL.
2. Wire `get_mrms_modifiers()` and `get_check_modifiers()` to read
   `mrms.products` and `mrms.check_products`, and delete the source lists.
3. Add the missing `filepath`/`outdir` attribute names to the render layers and
   implement `getattr` resolution with a load-time error on unknown names.
4. Restore the per-channel dict shape for GOES `mask_min`/`mask_max`.
5. Delete `scale_rule`, `radian_detection` and `crs_strategy`.
6. Keep the RAP integration catalog complete in `integration.yaml` and validate
   `transform` names against `TRANSFORMS`. The 43-layer Uint16 *display* catalog
   deliberately remains in `EWMRS/rap/config.py`; Phase 5 rejected unused YAML
   catalog keys rather than adding a second owner.
7. Fix the NEXRAD `sites` sentinel and add the missing CLI key.
8. Add uniqueness and coverage tests for upstream modifier, output name,
   directory, API route key, and list membership.

MRMS is the gate for this phase: no other family should be wired until MRMS
proves the loader can drive ingest, readiness, integration, rendering, and API
behavior from the flat files without circular imports.

### Phase 4: Extract scientific settings

1. Migrate detection and gate-mapping thresholds and thread one typed
   `DetectionConfig` through the pipeline, removing all 8 duplicate defaults per
   value and parameterizing the two inline `gatemapper.py` literals.
2. Make `kalman.yaml` fully authoritative; remove the dataclass and `.get()`
   fallback duplicates and inject the loaded filter configuration. Resolve the
   `max_prediction_time_minutes` `6.0`/`10.0` disagreement explicitly.
3. Migrate integration maps/statistics/AzShear/GLM policy, and parameterize the
   hardcoded `round(..., 2)` calls so `output.decimals` is live.
4. Move `derived[].formula` strings into `integration.yaml` unchanged and have
   the loader parse-check them against the existing safe-AST grammar.
5. Run output-equivalence tests against fixed meteorological fixtures at each
   step.

### Phase 5: Extract runtime and API settings

1. Migrate polling, readiness waits, search windows, retries, retention,
   cleanup, caches, worker limits, and memory budgets — including the supervisor
   restart policy and background cadences currently missing from `runtime.yaml`.
2. Remove duplicated function/constructor defaults; callers pass typed config
   or access one injected application settings object. Resolve the `zone_sync`
   `pause_seconds` disagreement and unify the two user-agent strings.
3. Migrate `src/api/` to `api.yaml`, retaining environment and CLI
   compatibility. Retire the legacy `src/EdgeWARN/api/` and `src/EWMRS/api/`
   services rather than wiring them.
4. Derive version strings from `package.json`.
5. Add effective-config summaries to startup logs and health diagnostics.

**Phase 5 completion note.** Runtime and API settings now use the 19 validated
catalogs. HSTS, proxy parsing, source-root paths, diagnostics provenance,
product counts, restart semantics, boundary-wait granularity, and access-log
mode are all owned by configuration and covered by tests. The health-check
limiter bypass remains deliberately absent: restoring it would retune the live
service, not extract a setting.

Enrichment concurrency is deliberately excluded, but not because a key would be
inert — that reasoning was too strong. `integrate/pipeline.py:353` sizes the pool
as `max_workers=len(future_order)`, and `future_order` holds exactly the tasks
being scheduled: `["stats", "support"]`, plus `"azshear"` only when
`_AZSHEAR_SUPPORT_ENABLED` is on, which it is not (`:20`). So the pool is two
threads, and a cap key could reach only two values with any effect: `>= 2` is a
no-op, and `1` serializes. Serializing two threads is a performance decision about
a fixed two-item workload, not a deployment setting, so the key is excluded as
out of scope rather than as inert. If the azshear path is ever enabled the task
count becomes variable and this is worth revisiting.

Two other Phase 5 candidates were resolved by deletion rather than wiring, on the
same "do not ship a key that does nothing" rule as the discrepancy table above:
`api_index.resync_every_updates` (a per-update counter cannot survive
`cycle.py`'s fresh `multiprocessing.Process` per cycle, so no interval could ever
be reached) and `nexrad.cli.max_volumes_per_site` (resolved onto `args` and then
never read). NEXRAD `trim_buffer` stayed a code parameter for a different reason:
it authorizes rewriting the caller's partial volume in place, which is a
file-ownership invariant rather than a value.

### Phase 6: Remove fallbacks and enforce the boundary

1. Delete obsolete source `config.py` catalogs or reduce them to typed adapter
   modules with no embedded values.
2. Search production source for every migrated literal/list and remove
   duplicates.
3. Add a CI audit that flags:
   - Production `http://` or `https://` literals outside the allowlist.
   - New `os.environ`/`process.env` reads outside overlay loaders.
   - Product/event/pressure-level catalogs outside typed registries.
   - Unapproved polling, timeout, retention, cache, or worker numeric
     literals.
   - Direct source-relative access to files in `config/`.
4. Maintain `config/code_constants_allowlist.yaml` only if the audit needs a
   machine-readable list — it would be a 19th file, exempt from the
   `schema_version` and catalog rules above. Each entry must name the source
   symbol, category, and reason; it must not become a second runtime
   configuration source.

## Validation plan

### Schema and loader tests

- Valid repository defaults load identically in Python and Node.
- Missing files, unknown keys, wrong types, invalid enum values, duplicate
  entries, non-finite numbers, inverted ranges, negative intervals, and unsafe
  paths fail before service startup.
- All 19 config files have `schema_version: 1`, and no file uses that key for
  anything else.
- Precedence is tested per override in all four combinations: YAML only, env
  over YAML, CLI over YAML, and CLI over env over YAML. A `store_true` flag that
  is absent must not overwrite a YAML `true`.
- Every `outdir`/`filepath`/`source` attribute name resolves against
  `util.file`; an unknown name raises at load time.
- Every `transform` name resolves in `TRANSFORMS`; every `derived[].formula`
  parses under the safe-AST grammar.
- The loader is memoized: importing it twice, or re-executing `run.py` module
  scope under `spawn`, parses the YAML once per process and reports an invalid
  file once rather than once per child.
- Config caches are process-local, immutable, and resettable in tests.
- Error messages include the file and exact dotted key.

### Catalog consistency tests

- Every product in `mrms.check_products` also appears in `mrms.products`.
- Every detection modifier appears in `mrms.products`.
- Every render layer names an ingested product and a colormap key that exists
  in `colormaps.json`.
- Every integration dataset in `stats_datasets` names an available source.
- Every NEXRAD API product corresponds to a renderer/writer product.
- The Node `product-catalog.json` and the Python catalogs contain the same
  product names and directory names.
- Catalog lengths are asserted explicitly: 28 and 12 MRMS, 16 ABI, 15 MRMS
  render, 16 GOES render, 25 stats, 43 code-owned RAP Uint16 display layers,
  and 31 Node product catalog. A silent drop during transcription is the most
  likely regression.
- List order is explicit where it affects readiness, output, or tests; no loader
  relies on unordered map iteration.

### Duplicate-default audit

- No literal `37.5`, `0.001` or `10.0` survives as an argparse or keyword
  default at any of the sites enumerated in "Duplicate defaults to remove".
- `TandemCycleConfig` still declares its detection fields without defaults.
- No `.get()` call in `kalman/config.py` passes a fallback value.
- Grepping production source for each migrated literal finds it in exactly one
  place: the YAML.

### Behavioral regression tests

- MRMS staged readiness order and required groups are unchanged.
- Detection masks/cells, tracking assignments/lineage, integration statistics,
  CTAM outputs, alerts, and API indexes match the baseline fixtures.
- MRMS, GOES, RAP, WPC, METAR, NWS, and NEXRAD selection/retention behavior
  matches the baseline at time boundaries. S3 key generation is asserted
  byte-for-byte, since the recorded path templates were fabricated.
- EWMRS float16 chunk payloads and metadata, colormap output, RAP Uint16
  payloads, and API route results match the baseline.
- Node security middleware and rate-limit behavior retain existing defaults.
- Historical and real-time CLI defaults retain existing behavior.

### Operational tests

- Services start from a working directory outside the repository when given a
  config root and runtime base directory.
- A worker child receives the same effective configuration as its parent.
- Invalid config never leaves partial directory creation, listeners, or worker
  pools.
- Windows and POSIX default/override paths remain supported.
- Config reload is explicitly unsupported initially; changing files requires
  a restart and is reported as such.
- Startup diagnostics show config version, config root, source provenance,
  enabled product counts, and active overrides without exposing secrets.

## Documentation updates

Update:

- `INSTALLATION.md` with config discovery, `--config-dir`, environment
  precedence, validation, and deployment copying.
- `README.md` and `docs/core/README.md` with the 18-file config tree.
- Ingest/detection/integration architecture docs with the authoritative
  product and scientific config files.
- `CONFIGURATION_AUDIT.md`, which disagrees with the working tree in the places
  called out in this document: the NEXRAD station URL and cache TTL, the chunk
  download semaphore, the METAR station URL and CONUS bounds, the integration
  dataset count, the undocumented `get_rap_products()`, the 43-layer RAP catalog
  and its SRH range, the removed `goes_rgb.py`, the PNG-versus-float16 render
  output, the GOES C10 mask floor, and the undocumented WPC `FEATURE_TYPES`.
  Stale paths in the audit: `cycle.py` → `src/util/runtime/cycle.py`;
  `nexrad/pipeline/worker_pool.py` → `src/common/ingest/nexrad/worker_pool.py`;
  `metar/metar.py` → `src/common/ingest/metar.py`;
  `product-catalog.json` → `src/api/config/product-catalog.json`.
- `docs/api/api_endpoints.md` and
  `docs/api/ewmrs_api_endpoints.md` with configured product/mapping behavior
  and current endpoint paths.
- Environment variable tables, including aliases and deprecation notices.
- A new `docs/core/configuration.md` containing key descriptions, units,
  valid ranges, restart requirements, examples, and ownership.

Generated reference documentation may be built from JSON Schema descriptions,
but the schemas and checked-in config remain authoritative.

## Rollout and compatibility

- Ship repository defaults with values matching the audit baseline.
- Require an explicit opt-in only for custom config roots, not for the
  repository defaults.
- Keep public Python accessor function names temporarily as adapters where
  external imports may exist, but have them query typed loaded catalogs.
- Keep current CLI/environment names for one deprecation window.
- Do not support mixed old/new catalog authorities. Migrate one domain
  atomically and delete its source literals in the same change.
- Treat scientific output changes, source additions/removals, and retention
  changes as separate reviewed commits after extraction.
- Add `schema_version` migrations before changing config structure in a later
  release.

## Acceptance checklist

- [ ] Every row of the corrections table is resolved, and no config key remains
  that an operator can change with no observable effect.
- [ ] All 19 `config/*.yaml` files carry `schema_version: 1`, have a sibling
  schema in `config/schema/`, and pass both loaders.
- [ ] No config file still carries the "Not yet consumed by code" header.
- [ ] Argparse uses `None` sentinels, and precedence is demonstrably
  CLI > env > YAML — including for boolean flags.
- [ ] MRMS ingest, readiness and detection lists are read from
  `mrms_goes.yaml`, and the source lists are deleted.
- [ ] GOES ingest and render views read from `mrms_goes.yaml` and
  `ewmrs_render.yaml`, cross-checked by test.
- [ ] RAP integration reads `integration.yaml` with `get_rap_products()` parity;
  RAP Uint16 retention/timestamp/force policy reads `ewmrs_rap_uint16.yaml`.
  Its 43-layer display catalog remains code-owned by recorded decision and is
  pinned by `tests/config_baseline/rap_uint16_layers.json`.
- [ ] NEXRAD VCP/elevation/product/render/API policy reads `nexrad.yaml`.
- [ ] Python and Node share base-directory and API settings without duplicated
  defaults; the legacy API server source is retired.
- [ ] `kalman.yaml` is actually injected and has no source fallback copies.
- [ ] All duplicate defaults listed above are reduced to one.
- [ ] All detection and integration empirical parameters in this inventory are
  externalized.
- [ ] All endpoints, timers, retries, retention, caches, workers, memory
  budgets, and server policy in this inventory are externalized.
- [ ] Package version is read from `package.json`, and the two divergent
  user-agent strings are unified.
- [ ] Intentional code constants are documented and covered by the audit.
- [ ] Characterization, schema, cross-language, catalog-consistency,
  duplicate-default, behavioral, API, and operational tests pass.
- [ ] Documentation is synchronized, including the `CONFIGURATION_AUDIT.md`
  corrections.
- [ ] A final literal/catalog audit finds no unclassified configuration in
  production source.

## Out of scope, tracked separately

- CTAM MorphoWind and StormProb configuration.
- Relocating `src/EWMRS/colormaps.json` into `config/`.
- Deleting `src/EWMRS/mappings.json` as an independent authority and fixing the
  `RAP_BestLiftedIndex_180_0mbAGL` drift.
- Introducing stable product IDs and a `roles:` membership model in place of the
  parallel catalogs. This is a schema change, not a value-preserving extraction.
