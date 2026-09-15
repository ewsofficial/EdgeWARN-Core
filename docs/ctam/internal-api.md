# CTAM internal API v1

This is a private HTTP API for an external CTAM module during one
pipeline cycle. It is not part of EdgeWARN's public API: the host binds it to
`127.0.0.1` on an ephemeral port, starts it only while CTAM is active, and
shuts it down with the cycle.

The host gives a launched module `CTAM_API_URL`, `CTAM_API_TOKEN`,
`CTAM_CYCLE_ID`, and `CTAM_MODULE_ID`. Send the token only as
`Authorization: Bearer <token>`. Tokens are scoped to one module and one
cycle, expire when the server closes, and are never logged. Request logs contain
only cycle ID, module ID, request ID, method, and status; they deliberately
exclude headers, query values, payloads, host paths, and exception text.

The checked-in [OpenAPI v1 document](openapi/ctam-internal-v1.json) is the
wire contract. Phase 2 implements the read endpoints: `/health`, `/cycle`,
`/files`, `/files/{file_id}`, `/files/{file_id}/content`, `/requirements`,
`/requirements/check`, `/stormcells`, `/stormcells/{cell_id}`, and
`/cells/{cell_id}`. Phase 3 also enables cycle-local mutation:
`PATCH /stormcells/{cell_id}` and
`PATCH /cells/{cell_id}/entries/{timestamp}` stage only manifest-owned
`modules`/`properties` paths; `POST /alerts` stages caller-owned alerts; and
`PUT /routes/{routeId}` stages inert JSON only for a route declared by the
authenticated module;
the `/transaction` endpoints validate, seal idempotently, or abandon the
module's private transaction. Staged work is never visible through the API or
filesystem until the host validates and publishes the completed cycle.

`GET /files` deliberately exposes metadata for every frozen catalog entry,
including unavailable files and their reason. File bytes are narrower: a
module may fetch content only for a file selected by its own manifest
requirements. Content is read through the pinned descriptor, never found by a
fresh directory scan or newest-mtime selection. Responses support a single
`Range: bytes=start-end` request and reject artifacts above the documented
stream limit.

The optional Python client is `EdgeWARN.ctam.sdk.CTAMClient`. It uses only the
standard library and imports no private EdgeWARN processing modules. Its
`materialize()` helper writes downloaded bytes to a module-private temporary
location; it never reveals a shared host path.
