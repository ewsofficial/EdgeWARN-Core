# Configurable MRMS ingestion: Phase 1 contract and impact

Phase 1 originally characterized the v1 runtime. It does not enable v2, change
acquisition or readiness, move directories, or enforce new CTAM/StormProb gates.
The implementation sequence is in the
[implementation plan](../../plans/configurable-mrms-ingestion-plan.md#12-implementation-phases).

## Baseline and intended contract

The checked-in [contract fixture](../../tests/fixtures/config/mrms_ingestion_contract.json)
separates `v1_baseline` from `v2_target`. Its values are reviewed snapshots, not
an additional runtime catalog. Update baseline assertions deliberately in the
phase that changes behavior; do not regenerate them to hide a regression.

## Current consumers

The shipped catalog now uses ingest v2. Three protected inputs establish Core
readiness; optional MRMS products enrich statistics and CTAM snapshots.
ProbSevere retains its special JSON adapter and identity. Raw acquisition never
creates an API product. The historical v1 raw paths and v2 canonical identities
remain recorded in the contract fixture for migration verification.

Current ownership, readiness, and retained API products are documented in
[configuration](configuration.md), [ingestion](ingestion.md), and
[API v3](../api/unified_v3.md). See [migration](configurable-mrms-phase8.md) for
v1-to-v2 raw-path conversion, and the [changelog](../../CHANGELOG.md) for the
subsequent service retirement.
