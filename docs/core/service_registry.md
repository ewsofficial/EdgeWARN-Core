# Realtime service-name registry and heartbeat contract

This document records the Phase 0 contracts from
`plans/realtime-runner-decomposition-plan.md`: the canonical service names, the
heartbeat schema each service publishes, and the route-family-to-service
dependency map mirrored by the unified Node API. Route enforcement is currently
wired explicitly at each route and must stay synchronized with the maps.

## Canonical service names

Exactly three canonical service names exist. The filenames beneath
`<BASE_DIR>/state/realtime/services/` are the registry:

| Name | Producer | Heartbeat file |
| --- | --- | --- |
| `edgewarn` | Primary EdgeWARN service (MRMS selection/ingest, detection, integration, tracking, CTAM, alerts, API indexes) | `services/edgewarn.json` |
| `ewmrs` | EWMRS/accessory service (MRMS/GOES/RAP rendering, GOES ABI, METAR, NWS, WPC) | `services/ewmrs.json` |
| `nexrad` | NEXRAD service (Level-II ingest and rendering) | `services/nexrad.json` |

The same names are used for single-instance locks, heartbeats, and API
discovery. The active lease is a single `leases/primary-active.json` record
owned by a run ID. Accessory loops (METAR, NWS, WPC, GOES ABI) are not top-level
services; their status appears as child entries inside the EWMRS heartbeat.

## Heartbeat schema

Each heartbeat is a single JSON object constructed by `ServiceHeartbeat` and
written atomically (sibling temporary file, `os.replace`; the final filename is
the only commit point). Schema version 1:

```json
{
  "schema_version": 1,
  "service": "ewmrs",
  "pid": 12345,
  "run_id": "<uuid>",
  "updated_at": "2026-08-23T12:00:00+00:00",
  "phase": "mrms-render",
  "version": "3.0.1",
  "last_successful_activity": "2026-08-23T11:59:40+00:00",
  "degraded_children": []
}
```

Required fields: `schema_version`, `service`, `pid`, `run_id`, `updated_at`.
`service` must be one of the canonical names. The Node API also requires it to
match the filename; the Python diagnostic reader currently validates the name
but does not enforce that filename match.
`degraded_children` lists accessory children that are crash-looped or disabled;
a service that is active but degraded still serves requests.

Heartbeats are diagnostic. Correctness uses committed phase records and
checkpoints; Python services never read heartbeats for correctness.

## Heartbeat states

Derived from the heartbeat file, the current clock, and
`config/api.yaml:server.service_stale_after_seconds`:

- `active`: file exists, parses against schema version 1, and `updated_at` is
  within the staleness threshold.
- `stale`: file exists but `updated_at` is older than the threshold or is too
  far in the future — crashed, hung, killed without cleanup, or clock skew.
- `disabled`: no heartbeat file — never started or intentionally omitted.
- `unsupported-schema`: file exists but fails validation against the supported
  schema version.
- `degraded`: active with non-empty `degraded_children`. Degraded services still
  serve requests; degradation is surfaced, never fabricated as health.

The API staleness threshold is intentionally independent of supervisor tuning
and comes from `config/api.yaml:server.service_stale_after_seconds`.

## Route-family dependencies

Gated public route families declare a required service. Requests whose required
service is neither active nor degraded fail with HTTP 503 and the structured
`SERVICE_NOT_ENABLED` error envelope rather than serving stale artifacts
silently.

| Route family | Required service |
| --- | --- |
| `/api/v3/cells*`, `/api/v3/storm-snapshots*`, `/api/v3/alert-snapshots*`, `/api/v3/alerts*`, `/api/v3/modules*`, `/api/v2/features/*` | `edgewarn` |
| `/api/v3/render-products*`, `/api/v3/models/rap/*`, `/api/v3/analyses/wpc/*` | `ewmrs` |
| `/api/v3/radar-sites*` | `nexrad` |
| Legacy adapters (`/renders/*`, `/wpc/*`, `/rap/*`, `/nexrad/*`) | same service as the v3 family they adapt, except retired PNG routes |

METAR observation routes, discovery, health, and OpenAPI routes are intentionally
ungated.

## Implementation

- Registry, schema, writer, and state classification: `src/util/runtime/services.py`
- Explicit route gates and registry classification: `src/api/middleware/serviceGate.js`,
  `src/api/routes/v3/index.js`, and `src/api/routes/compatibility/index.js`
- Jest coverage: `tests/api/test_service_registry.js` and related route tests
