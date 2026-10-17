# API implementation

`src/api/app.js` constructs the Express app and shared middleware.
`routes/v3/index.js` defines v3 routes. `services/analysis.js` reads Core
artifacts; `services/ancillary.js` reads NEXRAD artifacts; `services/modules.js`
uses the published CTAM allowlist. `repositories/artifactRepository.js` owns
path containment, bounded JSON caching, listing, and open-file validation.
`streamArtifact.js` preserves range/HEAD/stream handling and closes handles.

`config/index.js` validates catalogs before listener startup, resolves CLI/env
settings, and publishes redacted diagnostics. `serviceRegistry.js` discovers
only ingest, edgewarn, and nexrad heartbeats. Analysis routes retain published
artifact access during producer outages; radar routes require NEXRAD availability.

Middleware preserves request IDs, RFC 9457 errors, Helmet/security, CORS,
rate limits, and request timeouts. OpenAPI is served from `openapi/v3.yaml` and
must agree with route behavior and tests. Retired product routes and readers
are removed. See [API v3](unified_v3.md).
