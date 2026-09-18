# CTAM Module Manifest Reference

An external CTAM module is a directory of code plus one declarative `module.toml`
manifest. The manifest is the only thing Phase 1 reads from an installed module:
it is parsed and validated at discovery time, before any module code is imported
or launched, so a broken or hostile module cannot influence its own admission.
Discovery records an invalid manifest as `invalid` with an actionable reason;
it never guesses.

This reference describes what `src/EdgeWARN/ctam/manifest.py` accepts today. The
the schema artifacts are checked in and mirrored by the current runtime contract;
`api_version`, and the identifiers match the OpenAPI document under
`docs/ctam/openapi/`.

## The module root

External modules live below one configurable root. Relative roots resolve
against the parent of the selected config directory; absolute roots are used as
provided. The directory is operator-owned and gitignored; installing or updating
a module never modifies the EdgeWARN-Core worktree.

Where the root comes from, highest precedence first:

1. `--ctam-module-dir` (`src/util/io.py:126`)
2. `EDGEWARN_CTAM_MODULE_DIR` (`src/util/ctam_config.py:30`)
3. `run.ctam_module_dir` in `config/runtime.yaml` (`config/runtime.yaml:109`,
   default `ctam_modules`)

A relative value resolves against the selected installation/config root rather
than the working directory. A root that is a regular file is a
misconfiguration and raises; a missing root is an empty external module set,
which is a supported StormProb-only installation.

## Discovery model

`discover_modules()` (`src/EdgeWARN/ctam/discovery.py`) scans the root one level
deep. A directory without a `module.toml` is not a module and is not reported.
Every directory that *claims* to be a module appears in the result, sorted into
stable dependency-then-ID order:

| State | Meaning |
| --- | --- |
| `discovered` | Manifest parsed, dependencies resolve, inside capacity. Runnable in order. |
| `invalid` | Manifest unparseable, id/name/version illegal, a dependency is missing/disabled/invalid, part of a dependency cycle, or past the module count. |
| `skipped_disabled` | Manifest is valid but sets `enabled = false`. |

`stormprob` is the active reserved built-in id; an external manifest may depend
on it but cannot claim it. Two module
ids differing only by case invalidate **both** — the
old registry silently picked a winner, which is precisely the failure this model
exists to remove.

## Field reference

| Field | Type | Default | Rules |
| --- | --- | --- | --- |
| `schema_version` | int | — required | Must be `1` (`src/EdgeWARN/ctam/limits.py:33`). |
| `id` | string | — required | Lowercase letter/digit start; `[a-z0-9_-]` thereafter, 1–128 chars (`manifest.py:41`). Must equal the containing directory name. Reserved ids rejected. |
| `name` | string | — required | Display name, becomes `modules.<name>`. Letter/digit start; `[A-Za-z0-9_-]`, 1–64 chars. Case-insensitively reserved names (`StormProb`, `_grid_outputs`) rejected (`limits.py:74`). |
| `version` | string | — required | Three-part `X.Y.Z` (`manifest.py:43`). |
| `api_version` | string | — required | Digits only; must be `"1"` (`manifest.py:45`). |
| `enabled` | bool | `true` | `false` means `skipped_disabled` at discovery. |
| `required` | bool | `false` | `true` marks a failure in the CTAM status record; the current integration caller isolates the exception and continues publication. |
| `scope` | string | `"stormcells"` | `stormcells` or `cycle`. |
| `entrypoint` | string array | — required | Argument vector, never a shell string. Only `{python}` is expanded. Absolute paths, shell metacharacters, and paths escaping the module directory are rejected at discovery (`manifest.py:494`). |
| `timeout_seconds` | int | `10` | Bounded 1–30 seconds (`limits.py:46`). |
| `after` | string array | `[]` | Module ids that must run first; `stormprob` is always legal and precedes external modules. Self-dependency rejected here; cycles are detected by discovery. |
| `[[requires]]` | table list | `[]` | Declared inputs; see below. |
| `[[writes]]` | table list | — one or more required | Declared output locations; see below. |
| `[[public_routes]]` | table list | `[]` | Optional public JSON routes. Each requires a unique `id` and `description`; at most 16 are allowed. |

## Public routes

A module may declare host-served, read-only JSON resources without supplying
HTTP code or filesystem paths:

```toml
[[public_routes]]
id = "forecast-summary"
description = "Latest module forecast summary"
```

The host derives `/api/v3/modules/<module-id>/<route-id>`. Route ids are 1–128
characters, start with a letter or digit, and use only letters, digits, `.`,
`_`, and `-`. Descriptions are required plain text, at most 256 characters, and
may not contain control characters. Duplicate ids or more than 16 declarations
make only that module invalid. Modules stage finite JSON through
`CTAMClient.register_route()` and the representation becomes eligible for
publication only when the module transaction commits.

## Requirement selectors

Each `[[requires]]` block names one selector and the conditions that make it
usable. `required` defaults to `true`, so an author who forgets to mark an input
optional gets a blocked module rather than a silent skip.

| Selector | Kind | Notes |
| --- | --- | --- |
| `stormcells.current` | stormcells | The current cycle's parsed in-memory cells. |
| `cells.history` | cell history | Existing `data/cells/<id>.json` records. `min_history_entries` (1–120, `limits.py:60`) applies. |
| `input:<FAMILY>:<PRODUCT>:<role>` | input | Family `mrms`, `goes`, or `rap` (case-insensitive); role `current` or `previous`. `max_age_seconds` (positive) applies. |

Unknown families, roles, products, and selectors are rejected at discovery —
a typo never becomes a requirement that can never be satisfied. Product names
are checked against the host's own ingest catalogs (`manifest.py:223`). The
`alerts.current` kind is deliberately not an admitted Phase 1 input.

## Write pointers and ownership

A `[[writes]]` block declares `resource` (`stormcells.current` or
`cells.history`) and a document-relative JSON Pointer that uses one `*` for the
entry:

```toml
[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/modules/CellStats"
```

The pointer must resolve, in decoded segments, to the `modules` or `properties`
container of a cell entry **and to the caller's own key inside it**. This is
enforced segment-wise on the decoded RFC 6901 segments (`manifest.py:712`), never
by string prefix and never through a filesystem path normalizer:

- Under `modules`, the leaf key must equal the manifest `name`.
- Under `properties`, the leaf key must equal the `id` or start with
  `id + "_"` — a module may not claim a shared detection/enrichment value.
- Pointers to the container itself, reserved keys, or any other part of the
  document (identity, geometry, timestamps, tracking state) fail validation
  before the module is ever launched.

Some pointers accepted by discovery (in the sense that they stay inside the two
containers) may still be refused at runtime if a `properties` key the module does
not own already exists in the frozen cell entry. Ownership is the contract;
mandating that a module carry no shared-state blast radius is what lets
predecessor modules run first and be read safely.

## Example

The tracked fixture at `tests/fixtures/ctam_modules/cellstats/module.toml` is the
minimal reference:

```toml
schema_version = 1
id = "cellstats"
name = "CellStats"
version = "1.0.0"
api_version = "1"
enabled = true
required = false
scope = "stormcells"
entrypoint = ["{python}", "main.py"]
timeout_seconds = 10
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

[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/properties/cellstats_severity"
```

## Diagnosing an installation without running it

`--list-ctam-modules` prints every discovered manifest and exits before any
pipeline setup runs. `--check-ctam-modules` is the gate: it exits nonzero if any
manifest is invalid. Neither executes module code. Readiness — whether a cycle's
inputs satisfy a manifest's requirements — is decided per cycle against that
cycle's input catalog and cannot be checked outside a running cycle.
