# AGENTS Guide for `src/EdgeWARN/ctam/`

## Purpose
CTAM module framework: manifest discovery, readiness evaluation, the loopback
internal API, transactional mutation, publication, and external process
execution. StormProb is the reserved built-in module.

## Agent guidance
- External modules are discovered from manifests below `ctam_modules/`; never
  add hard-coded imports or import-time registration for optional modules.
- Keep per-module failure isolation intact: a timeout, crash, or invalid commit
  in one module must not corrupt data or block unrelated modules.
- All shared-data mutations flow through the host-owned transaction boundary;
  patches are confined to the `modules` and `properties` containers of a cell.
- Changes may alter downstream analytics, alerting, tracking inputs, and
  history usage.
