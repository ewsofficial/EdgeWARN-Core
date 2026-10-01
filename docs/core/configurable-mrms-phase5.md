# Configurable MRMS phase 5: readiness and replay

> Realtime migration: the independent ingest path no longer writes the realtime
> report snapshots described below. Use `state/realtime/ingest/v1/` readiness
> records and `poll-status.json`. Historical snapshots remain supported.

Detection and base Core integration no longer wait for the optional MRMS
batch. Detection checks every expected protected identity, current role,
validation result, and analysis timestamp. EWMRS receives one best-effort
`mrms-ready` trigger immediately afterward. RAP remains a base-integration
requirement, and realtime scan-time GLM remains an integration requirement.
Unavailable mandatory phases release worker events with a false readiness
state; optional failures do not change those gates.

The coordinator emits separate immutable detection, integration, and final
CTAM manifests. Callback state objects are separate, so retaining an earlier
callback does not expose later readiness changes. The worker receives serialized
snapshots before each event is released. Its first enrichment pass can run while
optional downloads continue. Once optional acquisition is terminal, it enriches
only newly selected current inputs, then collects StormProb observations, runs
CTAM, and publishes once. This final enrichment pass also runs with CTAM disabled.
StormProb's fatal dependency policy and external CTAM declaration checks remain
phase 6 work.

V2 acquisition owns the configured optional deadline, aggregate work budget,
and per-product transport fallback. The coordinator allows one configured
network-timeout interval for acquisition teardown before its outer cancellation
bound. It does not retry the entire optional batch synchronously. The v1
compatibility path uses the configured HTTPS timeout as its optional bound.
Every owned asyncio task is cancelled and joined on failure or cancellation.

## Durable records

The strict EWMRS handoff wire format is unchanged. Optional outcomes and the
registry fingerprint live in separate schema-version-1 JSON records:

```text
<BASE_DIR>/state/realtime/ingest-reports/<cycle-id>-detection.json
<BASE_DIR>/state/realtime/ingest-reports/<cycle-id>-integration.json
<BASE_DIR>/state/realtime/ingest-reports/<cycle-id>.json
```

Phase snapshots are committed before releasing successful worker phases. The
final report contains all three snapshots and optional per-product outcomes.
It is committed only when mandatory inputs are valid, so a failed acquisition
attempt does not prevent recovery when its missing protected source arrives.
Writes publish a fully flushed temporary file using an exclusive hard link;
identical retries preserve the existing bytes. A retry with a different
fingerprint or selected snapshot fails instead of rewriting a committed record.
Reports are independent of whether EWMRS handoff publication is enabled.
Late optional arrivals are considered by subsequent EWMRS scans.

## Input retention and history

History selection uses encoded UTC timestamps, matching product identities and
file suffixes, with a deterministic path tie-break. Modification time does not
select observations. Previous-role records never satisfy current-input lookups
or readiness gates. Previous observations are selected for optional products as
well as protected inputs; phase 6 will validate declarations and retention.

A cycle holds an OS lock at `<BASE_DIR>/state/input-pins.lock` through ingest,
processing, and worker shutdown. This deliberately conservative implementation
protects the entire raw cache while a cycle is active. Other processes using the
shared cleanup helpers defer deletion; the producer drains deferred cleanup
after releasing its lease. RAP cleanup participates in the same protocol. A
crashed process releases its OS lock automatically. Concurrent Core cycles in
the same runtime root are refused. This avoids a race between selecting an
input and publishing a per-file pin.

## Historical runs

`process_historical.py` uses `<resolved-base-dir>/historical` as its isolated
runtime root, including raw inputs, stormcells, databases, and indexes. Its
reports are written under `state/historical/ingest-reports` inside that root;
it never emits realtime handoff records. Programmatic `historical_pipeline`
callers must likewise initialize a separate runtime root. Historical runs use
the same coordinator deadlines and consume its final immutable manifest.

## Validation

Focused tests cover mandatory callbacks while optional work is held open,
optional failure/deadline, cancellation and task joining, each missing protected
identity, previous/current separation, encoded-time history, cross-process
cleanup protection, idempotent/conflicting reports, historical report isolation,
final enrichment before observation/CTAM, single publication, and existing
EWMRS handoff readers.
