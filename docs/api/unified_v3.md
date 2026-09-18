# Unified EdgeWARN API v3

The public service is started with `npm run api` and serves both EdgeWARN and
EWMRS products from one configured base directory. Its default port is `5000`.

`GET /api/v3/openapi.json` is the authoritative machine-readable contract.
Most v3 JSON collections use `{ "data": [], "meta": { "nextCursor" } }`;
single JSON resources use `{ "data": {}, "meta": {} }`. Native WPC GeoJSON
detail and the raw OpenAPI document are exceptions. Per-request correlation
is available through the `X-Request-Id` response header rather than the body,
so cacheable JSON responses keep a stable body and support conditional `GET`
(`ETag`/`If-None-Match` → `304`).
Standard v3 application errors use `application/problem+json`; rate-limit,
timeout, and legacy compatibility responses may use ordinary JSON envelopes.

## Runtime configuration

- Canonical base directory: `--base-dir <path>` or `EDGEWARN_BASE_DIR`
- Compatibility aliases: `--base_dir <path>` and `BASE_DIR`. Precedence is CLI,
  then `EDGEWARN_BASE_DIR`, then `BASE_DIR`, then `filesystem.yaml`.
- `PORT` sets the service port; `npm run debug:api` uses debug port `3001`
- `ALLOWED_ORIGINS` is a comma-separated browser-origin allowlist. The default
  is `*`, and requests without an `Origin` header proceed without CORS headers.
  Credentials are not enabled for this read-only API.
- `TRUST_PROXY_IPS` configures trusted reverse proxies. Production rejects the
  ambiguous `TRUST_PROXY=true` form. Only set this when a stripping reverse
  proxy removes client-supplied `X-Forwarded-For`/`X-Forwarded-Proto` headers
  before forwarding; on a directly exposed host, enabling trust lets clients
  spoof forwarded headers and bypass per-client rate limits.

## Primary resources

- Analysis: `/api/v3/cells`, `/storm-snapshots`, `/alert-snapshots`,
  `/alerts/{alertId}`, `/observations/metar`. `/alerts` is addressable by ID
  only; there is no `/alerts` collection. `/alert-snapshots` and `/alerts/{id}`
  additionally accept the `source` query parameter (`official` or `edgewarn`).
- Renders: `/api/v3/render-products`, and
  `/api/v3/render-products/{productId}/snapshots/{timestamp}/chunks`
  lists sparse float16 value chunks; `/chunks/{x}/{y}` returns the binary payload.
  Render products use the `binary_chunks` representation. V3 does not expose
  `/image` or `/tiles` resources.
- Radar: `/api/v3/radar-sites`
- RAP: `/api/v3/models/rap/layers`
- WPC: `/api/v3/analyses/wpc/surface`
- CTAM modules: `/api/v3/modules`, `/api/v3/modules/{moduleId}`, and
  `/api/v3/modules/{moduleId}/{routeId}`
- Infrastructure: `/health/live`, `/health/ready`

Canonical render IDs equal the render layer name / file prefix, such as `MRMS_MergedReflectivityQC`, `MRMS_QPE`,
and `GOES_ABI_C13_BrightnessTemp`. The product catalog maps IDs to runtime
folders; legacy prefixes are not separate lookup aliases. The id charset (`[A-Za-z0-9_.-]+`) is frozen so 3.1.0
dynamic ingest/render products only add catalog entries.

## EWMRS binary chunks

MRMS and GOES ABI renders publish one-channel float16 value chunks. They are
gzip-compressed `chunk_{x}_{y}.f16.gz` files under
`<BASE_DIR>/gui/<product>/<timestamp>/chunks/`. `NaN` is the no-data value;
gzip uses deterministic metadata and the API sends `Content-Encoding: gzip`.
The API publishes source values, not visualization styles. Clients choose and
version their own color scales for scalar values; there is no public colormap
catalog. GOES RGB composites are likewise derived client-side from the raw ABI
channel chunks. Chunks retain top-to-bottom row order and a bottom-left
chunk-grid origin.

Fetch the `/chunks` listing first. It provides the grid, format descriptor,
and the authoritative sparse coordinate list—missing coordinates are fully
transparent chunks, not a request to synthesize pixels. The payload endpoint
sets `X-EWMRS-Format-Version`, `X-Data-Type`, `X-Value-Kind`, `X-Channel-Count`,
`X-No-Data`, `X-Chunk-Width`, `X-Chunk-Height`, `X-Grid-Origin`, and
`X-Pixel-Row-Order`. Verify that the decoded byte length equals
`width * height * channels * 2` before creating a `Uint16Array` or
`Float16Array`; responses are immutable and support ETag conditional GET and
HEAD.

```js
const listing = await (await fetch(chunkListUrl)).json();
const response = await fetch(chunkUrl);
const bytes = new Uint16Array(await response.arrayBuffer());
if (bytes.byteLength !== 350 * 350 * 2) throw new Error('invalid float16 scalar chunk');
// Interpret as float16 (or upload as half-float); grid y=0 is the bottom row.
```

One float16 component is two bytes, so a `350 x 350` single-channel chunk is
`245000` bytes and `bytes.length` is `122500`. Compare `byteLength`, not
element count, against `width * height * channels * 2`.

These float16 value chunks are distinct from RAP `data.u16` scalar arrays and NEXRAD
`.bin.gz` products, which have their own metadata and decoders.

## Migration

The prior `/api/v2`, `/renders`, `/nexrad`, `/rap`, and `/wpc`,
`/health`, and `/healthz` paths are compatibility adapters on the same
process. They retain legacy bodies/representations and include `Deprecation:
true` plus a link to this API contract. New clients should use v3; no data
route redirects are issued. The obsolete PNG-producing `/renders/download`
and `/renders/tile` routes return `410 Gone` with the successor chunk resource.
