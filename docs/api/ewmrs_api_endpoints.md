# EWMRS API endpoints

EWMRS products are served by the unified v3 API. The authoritative contract is
`GET /api/v3/openapi.json`; see [the v3 guide](unified_v3.md) for binary formats,
headers, pagination, and errors.

All endpoints below use GET (with HEAD support).

| Resource | Endpoint |
| --- | --- |
| Render products | `/api/v3/render-products` |
| Product metadata | `/api/v3/render-products/:productId` |
| Render timestamps | `/api/v3/render-products/:productId/snapshots` |
| Chunk manifest | `/api/v3/render-products/:productId/snapshots/:timestamp/chunks` |
| Float16 chunk | `/api/v3/render-products/:productId/snapshots/:timestamp/chunks/:x/:y` |
| Radar sites | `/api/v3/radar-sites` |
| Radar availability | `/api/v3/radar-sites/:siteId/availability` |
| Radar field | `/api/v3/radar-sites/:siteId/scans/:timestamp/elevations/:elevation/products/:productId` |
| RAP layers | `/api/v3/models/rap/layers` |
| RAP timestamps | `/api/v3/models/rap/layers/:layerId/snapshots` |
| RAP metadata | `/api/v3/models/rap/layers/:layerId/snapshots/:timestamp/metadata` |
| RAP Uint16 data | `/api/v3/models/rap/layers/:layerId/snapshots/:timestamp/data` |
| WPC timestamps | `/api/v3/analyses/wpc/surface` |
| WPC GeoJSON | `/api/v3/analyses/wpc/surface/:timestamp` |

The old `/renders/*`, `/nexrad/*`, `/rap/*`, `/wpc/*`, `/colormaps`, and `/healthz`
endpoints are removed and return 404. The former PNG download/tile handlers no
longer return 410; clients must use float16 chunk resources. Health monitoring
uses `/health/live` and `/health/ready`.
