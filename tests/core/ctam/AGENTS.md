# AGENTS Guide for `tests/core/ctam/`

## Purpose
Framework-level CTAM tests.

## Agent guidance
- Use this area for registry, engine, and interface behavior rather than module-specific math details.
- `baseline.py` is the Phase 0 snapshot harness for `plans/modular-ctam-internal-api-plan.md`.
  Committed snapshots live in `tests/ctam_baseline/`. Regenerate with
  `UPDATE_CTAM_BASELINE=1 python -m pytest tests/core/ctam`, and only when the
  source change was an intentional behavior change -- an unexplained diff is the
  regression these snapshots exist to catch.
- The `*_baseline.py` modules freeze today's StormProb output, alert payloads,
  stormcell field inventory, and cell-history semantics. Treat a failure there as
  a behavior change to justify, not a test to update.
- `contract/` covers the checked-in artifacts under `docs/ctam/` rather than any
  runtime code, so it is the one area here that fails on a documentation edit.
  See `contract/AGENTS.md` before changing a schema, the OpenAPI document, or a
  limit.
