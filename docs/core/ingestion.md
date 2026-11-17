# Weather acquisition and Core readiness

`run_ingest.py` independently acquires MRMS, RAP, and scan-time GLM. Core never
performs realtime remote discovery: it consumes exact validated readiness
records. NEXRAD owns separate Level-II acquisition and rendering children.
Historical processing uses the staged Core coordinator.

## MRMS

The schema-v2 registry combines three protected detection/check inputs
(composite reflectivity, PrecipFlag, ProbSevere) with configurable optional
products. Product identities include elevation; each registry entry owns its
remote locator, local directory, adapter, retention, and dependency roles.
ProbSevere retains its special source and JSON handling.

Discovery lists a bounded observation window across day boundaries and every
eligible S3 page. Object/page/queue overflow applies backpressure without
advancing an incomplete cursor. Acquisition retains per-product S3/HTTPS
fallback, exact-time selection, bounded concurrency, payload validation,
quarantine, and atomic publication. A committed observation cannot be replaced
with conflicting bytes. Mirrors share one input identity based on product,
encoded source time, and validated digest.

## RAP and GLM

RAP selection uses analysis-hour timestamps, freshness limits, and bounded
backward search. Reuse of one analysis preserves its committed identity.
Scan-time GLM validates NetCDF flash coordinates and energy, shape agreement,
and cycle alignment. It stages merge work before immutable publication and
never replaces inventory-owned observations. `--disable-glm` disables lightning;
`--mrms-core-only` disables RAP and GLM. Both choices must agree in ingest/Core.
ABI imagery acquisition is removed.

## NWS alerts

Ingest runs a background thread (`util/runtime/nws_loop.py`) that downloads the
active-alert feed from `nws.active_alerts_url` every
`background_intervals.nws_seconds` (default 60). It runs beside, never inside,
the MRMS/RAP/GLM polling path, so a slow or failing weather.gov request cannot
delay acquisition or readiness. Blocklisted events are dropped, UGC zones are
mapped to geometry, and a deduplicated registry is reconciled against the active
ID set. Registry and snapshots are under `<BASE_DIR>/data/Alerts/official/`.
`nws.registry_ttl_hours` defaults to 6 hours for timestamp snapshots and alerts
not seen recently. Expired alerts and alerts absent from a successful active-feed
response are still removed earlier.
`nws.edgewarn_alert_retention_minutes` (default 360) bounds EdgeWARN-generated
alert files and timestamp snapshots under `<BASE_DIR>/data/Alerts/EdgeWARN/`.

Zone assets are an operator prerequisite: run `edgewarn sync-nws-zones --apply`
first. Without them ingest refuses to start (exit 1) unless NWS fetching is disabled.
`--disable-nws` (`run.disable_nws`) turns the loop off, and
`--mrms-core-only` disables it too. Core matching reads the published registry.

## Durable handoff and retention

`InputInventory.commit_input()` persists validated identity, exact source path,
source time, provenance, digest, dependency fingerprint, and producer run ID
under `state/realtime/ingest/v1/inputs/`. Reconciliation validates existing
observations, adopts explicitly enumerated source files through a payload
validator, and completes interrupted retention. There is no render outbox.

Core readiness is published in order: `core-start-ready`,
`core-integration-ready`, `core-final-ready`; `terminal` records explain skipped
or expired scans. Detection/history selections stay immutable across phases.
Mandatory integration includes enabled StormProb requirements, RAP, and GLM.
Optional MRMS acquisition terminates at its deadline; the final CTAM snapshot
contains successful aligned optional inputs and history and cannot be revised
by late arrivals. Consumers check fingerprints, membership, exact paths, and
bytes before use. Required missing sources fail closed.

Publication, selection, worker pins, and retention share a bounded cross-process
input lease. Retention preserves live Core phases, explicit worker pins, and
previous-history selections. Only unreferenced observations outside the
rediscovery window can retire; identity tombstones precede deletion and support
restart reconciliation. Pins left by a previous Core run are swept safely.
Cache validation uses file versions while source time comes from encoded data.

## NEXRAD and runtime storage

Level-II discovery/download/parse work uses bounded scans and per-site timeouts,
worker heartbeats, stale-worker recovery, staged writes, and retention.
Source artifacts live in `data/NEXRAD_Level2`; compressed polar fields and
manifests live in `gui/NEXRAD/<SITE>/<ELEVATION>`. `nexrad.gui` owns render
cadence and retention. The v3 radar routes serve those bytes unchanged.

All paths honor the configured base directory and atomic publication. Use a
shared umask that lets the independent Node deployment read artifacts. Core
continues matching published official alerts if an external producer supplies
them; this runtime contains no official-alert acquisition service.

See [configuration](configuration.md), [integration](integration.md),
[service contracts](service_registry.md), and [migration](../../CHANGELOG.md).
