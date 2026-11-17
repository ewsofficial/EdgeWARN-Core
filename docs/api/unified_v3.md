# Unified API v3

Start with `npm run api` (port 5000). The read-only service exposes published
Core and NEXRAD artifacts. `/api/v3/openapi.json` is the authoritative contract.

## Resources

- `/api/v3/cells` and `/cells/{cellId}`
- `/api/v3/storm-snapshots` and `/storm-snapshots/{timestamp}`
- `/api/v3/alert-snapshots` and `/alert-snapshots/{timestamp}`
- `/api/v3/alerts/{alertId}`
- `/api/v3/modules`, `/modules/{moduleId}`, `/modules/{moduleId}/{routeId}`
- `/api/v3/radar-sites`, `/radar-sites/{siteId}/availability`, and
  `/radar-sites/{siteId}/scans/{timestamp}/elevations/{elevation}/products/{productId}`
- `/health/live` and `/health/ready`

Alert routes require a `source` query (`edgewarn` or `official`). The API can
read published official alert files; their acquisition is outside this runtime.
There is no alerts collection. Collection envelopes use
`{data: [], meta: {nextCursor}}`; resources use `{data: {}, meta: {}}`.
The raw OpenAPI document and radar bytes are exceptions.

Requests carry `X-Request-Id`. Application errors use RFC 9457
`application/problem+json`. JSON resources support ETags and conditional GET.
Pagination rejects pruned cursors; restart without a cursor to obtain the
current collection. NEXRAD `.bin.gz` artifacts retain their binary layout,
stream/range handling, HEAD behavior, and immutable caching.

## Configuration and health

Base-directory precedence is `--base-dir`, `EDGEWARN_BASE_DIR`, legacy
`--base_dir`/`BASE_DIR`, then `filesystem.yaml`. Set `PORT`, `ALLOWED_ORIGINS`,
and `TRUST_PROXY_IPS` for deployment. Production rejects blanket
`TRUST_PROXY=true`. CORS does not enable credentials.

Readiness reports only `ingest`, `edgewarn`, and `nexrad` with state, phase,
lastSeen, and degradedChildren. States are active, stale, stalled, disabled,
degraded, or unsupported-schema. Set `api.server.enabled_services` to the
selected topology. Readiness returns 503 for missing runtime roots or unhealthy
enabled services. Published analysis artifacts remain accessible through Core
outages; radar routes retain their NEXRAD service gate.

## Removed resources

Raster render-products, RAP display arrays, WPC surface analysis, and METAR
observations have no producer or reader and return 404. All legacy v1/v2,
`/features`, `/data`, `/renders/*`, `/rap/*`, `/wpc/*`, `/nexrad/*`, `/colormaps`,
`/health`, and `/healthz` paths also return 404 without redirects. Existing
retired artifacts on disk cannot restore these routes. See
[breaking changes and operator migration](../../CHANGELOG.md).
