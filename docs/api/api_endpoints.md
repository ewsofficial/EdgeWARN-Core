# EdgeWARN API endpoints

The supported data API is `/api/v3`. See [the v3 guide](unified_v3.md)
and `GET /api/v3/openapi.json` for request parameters and response contracts.

| Resource | Endpoint family |
| --- | --- |
| Discovery and OpenAPI | `/api/v3`, `/api/v3/openapi.json` |
| Cells | `/api/v3/cells`, `/api/v3/cells/:cellId` |
| Storm snapshots | `/api/v3/storm-snapshots[/:timestamp]` |
| Alert snapshots | `/api/v3/alert-snapshots[/:timestamp]?source=official` (or `edgewarn`) |
| Individual alerts | `/api/v3/alerts/:alertId?source=official` (or `edgewarn`) |
| CTAM modules | `/api/v3/modules[/:moduleId[/:routeId]]` |
| METAR | `/api/v3/observations/metar[/:timestamp]` |
| Render products | `/api/v3/render-products` |
| NEXRAD | `/api/v3/radar-sites` |
| RAP | `/api/v3/models/rap/layers` |
| WPC surface analyses | `/api/v3/analyses/wpc/surface[/:timestamp]` |

Brackets indicate optional path segments, not literal URL characters. Render,
radar, and model resources have detail and binary endpoints documented in the
[EWMRS endpoint guide](ewmrs_api_endpoints.md).

Analysis and CTAM routes require the EdgeWARN service; rendering, RAP, and WPC
require EWMRS; radar routes require NEXRAD. Inactive owners return 503 with
`SERVICE_NOT_ENABLED` in an `application/problem+json` body. METAR is ungated.

Operational endpoints remain `/health/live` and `/health/ready`. Root discovery
(`/`) and `/robots.txt` also remain available. Readiness checks the configured
data, GUI, and WPC directories and reports service diagnostics.

## Removed endpoints

All v1/v2 and unversioned data adapters have been removed: `/api/v1`, `/api/v2`,
`/features`, `/data`, `/renders/*`, `/nexrad`, `/nexrad/*`, `/rap/*`, `/wpc/*`,
`/colormaps`, `/health`, and `/healthz`. Requests now receive 404, including paths
that previously returned 410. There are no redirects or compatibility bodies.
Clients must migrate URLs and response parsing to the v3 contract.
