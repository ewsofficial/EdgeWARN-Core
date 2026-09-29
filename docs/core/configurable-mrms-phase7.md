# Configurable MRMS ingestion: phase 7

Implemented downstream eligibility and producer agreement for ingest v2.
The shipped v1 catalog remains unchanged until the phase 8 coordinated release.

Integration and EWMRS configuration accessors resolve product identities and
filter disabled entries before path resolution. Both expose an optional full
diagnostic view. Integration warns once per disabled statistic and registry
generation. Legacy aliases remain supported for migration, and GUI output
identities remain unchanged.

Core manifest-aware MRMS reads select validated current records by family and
product. Detection also selects explicit previous roles, without promoting a
previous-only record to current data. No manifest-aware MRMS lookup falls back
to arbitrary latest files. Direct detection verifies reserved membership before
scanning. AzShear support checks eligibility before resolving either input.

Core atomically publishes its versioned effective registry after preflight and
service-lock acquisition, with its heartbeat run ID. EWMRS requires matching
identity membership, contract version, fingerprint, and a fresh heartbeat from
that same run. Paused MRMS drains preserve checkpoints; RAP drains continue.
Direct MRMS rendering also enforces agreement. A repaired matching producer
resumes consumption automatically. The descriptor never supplies raw paths.

## Verification

- Focused configuration, manifest, catalog, enrichment, EWMRS, and handoff tests
  passed; additional regressions cover inactive diagnostics, identity/role
  selection, producer restart/staleness/mismatch recovery, and RAP progress
  while MRMS is paused.
- Both configuration validators passed all 18 documents.
- Node API: 11 suites, 121 tests passed (excluding nested `.kilo` worktree
  copies). Public catalogs and removed legacy-route behavior remain covered.
- Full Python run: 2,360 passed, 7 skipped, 24 failed. All 24 failure identities
  reproduced on unchanged HEAD in an isolated temporary checkout. Existing
  failures include historical/preflight fixtures, CLI baselines, the connected
  weather-spine fixture, and optional-enrichment mocks. Thus full replay/release
  qualification is still constrained by those existing failures.

Multiprocessing and HTTP tests required execution with local socket access;
the initial sandboxed runs could not start their local servers/process managers.
