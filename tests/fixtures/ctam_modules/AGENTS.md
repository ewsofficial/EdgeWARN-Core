# AGENTS Guide for `tests/fixtures/ctam_modules/`

## Purpose
Checked-in, inert CTAM module installations used as a discovery root by
`tests/core/ctam/test_discovery.py`. They stand in for what an operator would
drop into the production module root.

## Agent guidance
- **These are never launched.** Phase 1 discovery parses `module.toml` and does
  not import, execute, or subprocess anything. Each `main.py` is a no-op that
  exits 0 and says so; Phase 4 owns launching, and when it lands these fixtures
  should stay inert unless a test deliberately needs a runnable one.
- This directory is deliberately *outside* the production discovery root. The
  real root (`/ctam_modules/` at the repository root) is gitignored so an
  operator's installed modules are never committed, which also means a fixture
  placed there would not survive a clone. Tests point discovery here explicitly,
  or copy a folder into `tmp_path`.
- `cellstats/` is the fully populated valid manifest and mirrors the example in
  retired `plans/modular-ctam-internal-api-plan.md`, except that `timeout_seconds` is the
  10-second default rather than the plan's 30 — see the deviation recorded in
  `docs/ctam/internal-api-limits.md`.
- `brokenmanifest/` is invalid on purpose. Do not "fix" it: a test asserts that
  one bad manifest does not prevent the valid module beside it from being
  discovered.
- A module `id` must equal its directory name, so renaming a folder here means
  editing its manifest too.
