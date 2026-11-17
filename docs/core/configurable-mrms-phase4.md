# Configurable MRMS ingestion: phase 4

Phase 4 activates registry paths and acquisition for validated ingest v2 trees.
The shipped v1 catalog remains unchanged until the coordinated phase 8 release.
Core readiness separation, full startup dependency preflight, consumer eligibility,
and migration apply/resume remain owned by phases 5–8.

## Path binding and startup

`util.file.initialize_filesystem(base_dir, config_dir=...)` binds v2 paths without
creating directories. `bind_mrms_paths(registry)` exposes an immutable
`MRMS_PATHS` mapping and exact-name module attributes. Compatibility aliases
resolve through the current registry and disappear when their product is disabled.
Reinitializing with another base/config replaces the generation; returning to v1
restores its legacy paths. GUI product paths and representations are unchanged.

```python
path = fs.MRMS_PATHS['MRMS_MergedAzShear_3-6kmAGL']
assert path == getattr(fs, 'MRMS_MergedAzShear_3-6kmAGL')
assert path == fs.MRMS_AZSHEARMID_DIR
```

Hyphenated names require dictionary or `getattr` access. Legacy Python aliases
remain supported through 3.x, with earliest removal in 4.0 after deprecation.
Binding and creation reject symlink escapes from the resolved base/data tree.

`ensure_mrms_directories(registry)` is the explicit producer startup hook. Phase 6
must invoke it **after** complete CTAM/StormProb preflight. It is intentionally not
called by imports, validation, binding, migration planning, or consumer startup.
Acquisition creates a selected enabled product directory only when publishing.

Core cycle parents send the registry fingerprint and resolved base/config to
spawned workers. Workers rebuild and check the fingerprint before processing;
configuration changes between parent and child fail the worker. The public
`expected_mrms_fingerprint` initializer argument supports other spawned callers.
Configuration changes require a process restart.

## Acquisition ownership

The v2 sync/async phase and all-product entry points dispatch to
`common.ingest.mrms.acquisition`; historical processing uses those same entry
points. `EdgeWARN.ingest.mrms` remains a thin compatibility surface. Registry
specifications retain full elevation-bearing identities; only local directory
names omit elevation. Bucket names, S3 grammars and HTTPS directory derivation
come from the code-owned source adapter. ProbSevere uses its own source grammar
and JSON validation.

Detection/discovery always selects the three protected products. Configurable
non-detection downloads remain available through the integration compatibility
entry points. `get_enrichment_modifiers()` and `get_other_modifiers()` distinguish
configured statistics inputs from other additions; phase 5 changes which outputs
participate in readiness. Optional acquisition does not add API/render products.

Each requested product has a terminal `ProductResult`, with requested UTC time,
registry fingerprint, status, source, selected analysis time, path, failure reason,
elapsed time and SHA-256 for ready content. Enabled products outside an explicit
selection are `not_requested`. Empty optional selections open no network clients
and do not satisfy `DownloadBatchResult.successful`.

One process-wide budget per runtime base is shared across phase batches and sync
and async execution, including HTTPS fallback. Worker counts and queued worker
jobs are bounded by `downloads.max_concurrency`. Protected products are scheduled
first. Optional work reserves one slot when concurrency exceeds one; waiting
protected requests take precedence at concurrency one. Optional deadlines start
at batch entry, including time waiting for capacity. Network timeouts come from
`ncep_https.sync_timeout_seconds`; protected attempts also have a four-timeout
acquisition envelope. S3 retries are bounded to one retry.

Async cancellation joins the task group and closes transport resources. Gzip
expansion yields between chunks and never launches a detached thread. Sync
fallback uses bounded joined workers and checks deadlines between reads and
before publication. A blocking sync network call can drain beyond its optional
deadline until its bounded socket timeout/retry completes; it cannot publish
content after that deadline. Summary telemetry records peak active/queued work,
remaining work and batch elapsed time separately from per-product durations.

## Publication and recovery

Each attempt writes under a unique `<BASE_DIR>/state/mrms/staging/` directory,
outside consumer product globs. Complete response lengths and gzip CRC/EOF are
checked before validating the decoded content. GRIB2 validation checks every
message length, edition, section framing, required sections and terminator,
then uses ecCodes to decode the values in each message;
ProbSevere validates the GeoJSON collection/feature structure. These checks do
not replace downstream scientific interpretation or feature validation.

A validated file is atomically linked into its generated directory without
replacing existing content. Existing local files must pass validation before
reuse. Duplicate/out-of-order listings are selected deterministically by encoded
UTC time and full product identity. Concurrent conflicting content is rejected;
published or potentially pinned files are never overwritten. Invalid existing
files cause an explicit failure and are preserved for operator investigation.

Malformed payloads produce metadata and, when within the byte cap, payload copies
under `state/mrms/quarantine/`, bounded to 16 entries and 64 MiB. Completed,
failed and cancelled attempts remove their own staging directories. After a hard
process interruption, abandoned staging is ignored and never adopted as a ready
file; it may be removed by an operator while producers are stopped. No cleanup
sweeps another active attempt's staging directory.

## Verification

Offline fixtures cover async/sync S3 and HTTPS, new product directories,
malformed/truncated payloads, duplicate/out-of-order selection, valid and invalid
local reuse, conflicting publication, optional deadlines, cancellation, bounded
quarantine, restart with abandoned staging, and protected capacity. Path fixtures
cover no-I/O binding, rebinding, disabled-name removal, containment, explicit
creation, and a fresh Python process verifying/rejecting a parent fingerprint.
Existing MRMS, manifest, coordinator, handoff, filesystem and runtime suites
provide compatibility checks. No live upstream downloads are used.
