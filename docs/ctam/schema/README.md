# CTAM internal API v1 schemas

Contract schemas for `/internal/ctam/v1`, checked in by Phase 0 of
`plans/modular-ctam-internal-api-plan.md`. Phase 0 does not change runtime
behavior: nothing here is loaded by `src/` yet. These files exist so the later
phases are implemented against a fixed contract instead of inventing one as they
go, and so the OpenAPI examples in `docs/ctam/openapi/` have something to
validate against.

## Files

| File | Covers | Where it appears |
| --- | --- | --- |
| `response-envelope.schema.json` | The common wrapper and the structured error list | Every response |
| `file-descriptor.schema.json` | Catalog descriptors | `GET /files`, `GET /files/{file_id}` |
| `cycle-state.schema.json` | Cycle state and host readiness | `GET /cycle` |
| `requirements-evaluation.schema.json` | Per-module requirement outcomes | `GET /requirements`, `POST /requirements/check` |
| `patch-request.schema.json` | Staged patch operations | `PATCH /stormcells/{cell_id}`, `PATCH /cells/{cell_id}/entries/{timestamp}` |
| `transaction.schema.json` | Staged counts, conflicts, commit identity | `GET /transaction`, `POST /transaction/validate`, `POST /transaction/commit` |
| `status-record.schema.json` | The persisted cycle record | `data/ctam/cycles/<cycle-id>/status.json` |

## Validation is two-step

`response-envelope.schema.json` types `data` as only `object`, `array`, or
`null`. It deliberately does not describe the payload, because the payload shape
depends on the endpoint and the supported keyword set has no `$ref` and no
`oneOf` to select between shapes.

So a response is validated twice: once against the envelope, then the `data`
member against the resource schema for that route. `tests/core/ctam/contract/`
does exactly this, and the OpenAPI document records which resource schema pairs
with which route.

## The supported keyword set is restricted, on purpose

These schemas are validated by the repository's own walker in
`src/common/config/loader.py`, not by the `jsonschema` package. That choice is
argued at `src/common/config/loader.py:18-31`: config must validate identically
from Python and from `src/config/loader.js`, so the repo implements one small
walker in each language rather than depending on a validator that exists in only
one of them. `_check_supported_keywords` (line 298) turns any keyword the walker
does not implement into a startup error, so an author reaching for `oneOf` gets a
failure instead of a constraint that silently enforces nothing.

The permitted keywords are exactly `$schema`, `title`, `description`, `type`,
`properties`, `required`, `additionalProperties`, `items`, `minItems`,
`maxItems`, `uniqueItems`, `minimum`, `maximum`, `exclusiveMinimum`,
`exclusiveMaximum`, `const`, `enum`, and `pattern`.

Consequences worth knowing before editing these files:

- **No `$ref` or `$defs`.** Shared shapes are written out in full. The timestamp
  pattern and the module-id pattern each appear in several files. Changing one
  means changing all of them, which is why
  `tests/core/ctam/contract/test_schema_contract.py` asserts the repeated
  patterns are identical across files rather than trusting an author to keep them
  in sync.
- **No `oneOf`, `anyOf`, or `if`/`then`.** Conditional requirements cannot be
  expressed. `patch-request.schema.json` cannot say "`value` is required when
  `op` is `add` or `replace`"; that rule is enforced in host code.
- **No `format`.** Timestamps and identifiers are constrained with `pattern`
  instead. The timestamp pattern accepts an explicit offset or `Z` and rejects a
  naive timestamp, which matters because StormProb currently falls back to a
  naive `datetime.now()`.
- **No `minLength` or `maxLength`.** String length cannot be bounded. Where a
  string must merely be non-empty, the schema uses `"pattern": "\\S"`. Where a
  real length bound is needed it is folded into the pattern as a `{m,n}`
  quantifier, as in the module-id and idempotency-key patterns.
- **No `patternProperties`.** Open maps are expressed by giving
  `additionalProperties` a schema, which the walker applies to every key not
  named in `properties`. `status-record.schema.json` keys modules this way.

`type: "number"` additionally requires `math.isfinite`
(`src/common/config/loader.py:335-340`), so a `number`-typed field rejects `NaN`
and infinity for free. This does not extend into a patch `value`, because the
walker only recurses where a subschema is given, and a patch value is arbitrary
JSON by design.

## Patterns are anchored with `\Z`, not `$`

The walker matches with `re.search` (`src/common/config/loader.py:410`), not
`re.fullmatch`, so a pattern is only as anchored as its author made it. Python's
`$` also matches immediately before a trailing newline. A pattern ending in `$`
therefore accepts a value with a trailing newline: `re.search` of
`^/(modules|properties)(/[^/]+)+$` against `"/modules/Foo\n"` succeeds.

Every pattern in this directory ends in `\Z` for that reason. This differs from
the config schemas in `config/schema/`, which use `$` and have the same gap;
that is recorded in `plans/modular-ctam-phase0-findings.md` rather than changed
here, because Phase 0 does not modify existing behavior.

## What these schemas cannot enforce

Schema validation is the first gate, not the security boundary. These rules are
named in the plan and must be implemented in host code, because the keyword set
cannot express them:

- **Key ownership.** `patch-request.schema.json` enforces that an operation path
  begins with `/modules/` or `/properties/` and names at least one segment
  below it. It cannot tell whether the leaf key belongs to the caller, so it
  accepts `/modules/SomeOtherModule` and `/modules/_grid_outputs`. Ownership is
  decided by parsing the pointer into RFC 6901 segments, unescaping `~0` and
  `~1` once, and comparing decoded segments against the allowlist.
- **Traversal segments.** `/modules/Foo/../id` matches the pattern, and that is
  correct: in a JSON Pointer, `..` is a literal key name with no traversal
  meaning. It becomes a write to `/id` only if the host passes the pointer
  through a filesystem path normalizer, which it must not do.
- **Size, depth, and field count.** No keyword bounds a document's byte size or
  nesting depth. See `docs/ctam/internal-api-limits.md` for the values and
  enforce them while reading the request body, before parsing.
- **Non-standard JSON literals.** Python's `json.loads` accepts `NaN`,
  `Infinity`, and `-Infinity` unless `parse_constant` rejects them. A patch value
  containing one would pass these schemas.
- **Revision correctness.** The schema requires a `revision` integer. Whether it
  matches the current cycle-local revision is a runtime comparison.
