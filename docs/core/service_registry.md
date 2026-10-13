# Service registry and heartbeat contract

Canonical names are `ingest`, `edgewarn`, and `nexrad`. Each service owns
`state/realtime/services/<name>.json` and an adjacent single-instance lock.
Ingest acquires MRMS/RAP/GLM; Core consumes readiness and publishes analysis;
NEXRAD owns Level-II acquisition and compressed polar products. Unknown or stale
retired service filenames are outside discovery.

Heartbeats are atomically published schema-version-1 records. Required fields:
`schema_version`, `service`, positive integer `pid`, `run_id`, `updated_at`.
Additional fields include phase, version, last_successful_activity, and
`degraded_children`. A fresh heartbeat does not prove work is progressing.

The API reports active, stale, stalled, disabled, degraded, or unsupported-schema.
Staleness and progress bounds come from api.yaml. Readiness checks the services
listed in `api.server.enabled_services`; set that list to the deployed topology.
Published Core artifacts remain accessible during producer outages. Radar routes
require NEXRAD; ingest is diagnostic and has no route-family gate. Python and
Node canonical names, schema version, and route maps must agree.

Correctness uses committed readiness, registry/dependency agreement, and exact
input validation. NEXRAD can optionally yield to Core's bounded activity lease.
Signals and shutdown remain bounded, and child degradation is visible in the
owning heartbeat.
