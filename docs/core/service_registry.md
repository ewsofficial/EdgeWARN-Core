# Realtime service-name registry and heartbeat contract

This document records the Phase 0 contracts from
`plans/realtime-runner-decomposition-plan.md`: the canonical service names, the
heartbeat schema each service publishes, and the route-family-to-service
dependency map the unified Node API enforces.

## Canonical service names

Exactly three canonical service names exist. The filenames beneath
`<BASE_DIR>/state/realtime/services/` are the registry:

| Name | Producer | Heartbeat file |
| --- | --- | --- |
| `edgewarn` | Primary EdgeWARN service (MRMS selection/ingest, detection, integration, tracking, CTAM, alerts, API indexes) | `services/edgewarn.json` |
| `ewmrs` | EWMRS/accessory service (MRMS/GOES/RAP rendering, GOES ABI, METAR, NWS, WPC) | `services/ewmrs.json` |
| `nexrad` | NEXRAD service (Level-II ingest and rendering) | `services/nexrad.json` |

The same names are used for single-instance locks, leases, heartbeats, and API
discovery. Accessory loops (METAR, NWS, WPC, GOES ABI) are not top-level
services; their status appears as child entries inside the EWMRS heartbeat.

## Heartbeat schema

Each heartbeat is a single JSON object written atomically (sibling temporary
file, validated payload, `os.replace`; the final filename is the only commit
point). Schema version 1:

```json
{
  "schema_version": 1,
  "service": "ewmrs",
  "pid": 12345,
  "run_id": "<uuid>",
  "updated_at": "2026-08-23T12:00:00+00:00",
  "phase": "mrms-render",
  "version": "3.0.0",
  "last_successful_activity": "2026-08-23T11:59:40+00:00",
  "degraded_children": []
}
```

Required fields: `schema_version`, `service`, `pid`, `run_id`, `updated_at`.
`service` must be one of the canonical names and must match the filename.
`degraded_children` lists accessory children that are crash-looped or disabled;
a service that is active but degraded still serves requests.

Heartbeats are diagnostic. Correctness uses committed phase records and
checkpoints; Python services never read heartbeats for correctness.

## Heartbeat states

Derived by the API from the heartbeat file alone:

- `active`: file exists, parses against schema version 1, and `updated_at` is
  within the staleness threshold.
- `stale`: file exists but `updated_at` exceeds the threshold — crashed, hung,
  or killed without cleanup.
- `disabled`: no heartbeat file — never started or intentionally omitted.
- `unsupported-schema`: file exists but fails validation against the supported
  schema version.
- `degraded`: active with non-empty `degraded_children`. Degraded services still
  serve requests; degradation is surfaced, never fabricated as health.

The staleness threshold reuses the existing supervisor settings from
`config/runtime.yaml` rather than introducing a second tuning surface.

## Route-family dependencies

Every public route family declares exactly one required service. Requests whose
required service is not active fail with HTTP 503 and the structured
`SERVICE_NOT_ENABLED` error envelope rather than serving stale artifacts
silently.

| Route family | Required service |
| --- | --- |
| `/api/v3/cells*`, `/api/v3/storm-snapshots*`, `/api/v3/alert-snapshots*`, `/api/v3/alerts*` | `edgewarn` |
| `/api/v3/render-products*`, `/api/v3/models/rap/*`, `/api/v3/analyses/wpc/*`, `/api/v3/styles/colormaps` | `ewmrs` |
| `/api/v3/radar-sites*` | `nexrad` |
| Legacy adapters (`/renders/*`, `/wpc/*`, `/colormaps`, `/rap/*`, `/nexrad/*`) | same service as the v3 family they adapt |

## Implementation

- Registry, schema, writer, and state classification: `src/util/runtime/services.py`
- Gating behavior in the unified Node API is introduced in later phases of the
  decomposition plan together with Jest coverage under `tests/api/`.
