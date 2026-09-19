# CTAM public module routes plan

## Goal

Add a public, read-only `/api/v3/modules` resource family that lets an admitted
external CTAM module publish JSON at a stable namespaced route without loading
module code into the Node.js API process.

The first version should support:

- `GET /api/v3/modules` — list modules that declare public routes.
- `GET /api/v3/modules/{moduleId}` — describe one module and its routes.
- `GET /api/v3/modules/{moduleId}/{routeId}` — return the latest committed JSON
  payload for one route.
- `CTAMClient.register_route(route_id, payload)` — stage or replace the calling
  module's payload for a route declared in its manifest.

Only `GET` and implicit `HEAD` are public. A module does not supply Express
middleware, status codes, headers, redirects, filesystem paths, or executable
handlers. "Route registration" means registering inert JSON with the CTAM host;
the unified API serves that JSON through a fixed host-owned handler.

## Current boundaries to preserve

- External modules are discovered declaratively from `module.toml` by
  `src/EdgeWARN/ctam/discovery.py`; module code is not imported during
  discovery.
- A module runs as a short-lived subprocess in
  `src/EdgeWARN/ctam/runner.py`. It receives only the cycle-scoped loopback API
  URL, its bearer token, the cycle ID, and its module ID.
- All module mutations currently pass through
  `src/EdgeWARN/ctam/transaction.py` and become visible only after the module
  commits.
- The Python pipeline and Node API share the configured runtime base directory,
  but they are separate processes. The handoff must therefore be a durable
  runtime artifact, not an in-memory callback registry.
- `src/api/routes/v3/index.js` owns the public v3 routing policy, including
  query validation, service gates, response envelopes, cache headers, and 405
  handling. The new routes must stay inside those policies.
- The existing `CTAMPublicationCoordinator` publishes storm snapshots, cell
  histories, alerts, and indexes as one recoverable operation. Public module
  payloads must join that publication boundary so a failed cycle cannot expose
  half-committed module data.

## Contract

### Manifest declaration

Extend manifest schema version 1 with an optional repeated table:

```toml
[[public_routes]]
id = "forecast-summary"
description = "Latest module forecast summary"
```

The public path is derived by the host as
`/api/v3/modules/<manifest.id>/<public_routes.id>`; modules do not provide an
arbitrary path. Keep the initial declaration deliberately small:

- `id` is required, unique within the manifest, and uses the existing safe
  segment alphabet (`[A-Za-z0-9_.-]`) with a documented length limit.
- `description` is required, plain text, length-bounded, and rejects control
  characters.
- The representation is JSON only and the HTTP method is always `GET`/`HEAD`.
- Set a per-module route-count limit in `docs/ctam/internal-api-limits.md` and
  mirror it in `src/EdgeWARN/ctam/limits.py`.
- An absent `public_routes` list means the module has no public API surface.

Add a frozen `PublicRoute` model and a `public_routes` tuple to
`ModuleManifest`. Update every direct `ModuleManifest(...)` construction in
tests to use keywords or a shared factory before adding the field, avoiding a
fragile positional-argument migration.

### Module registration method

Add this stdlib-only SDK method in `src/EdgeWARN/ctam/sdk/client.py`:

```python
client.register_route("forecast-summary", {"risk": "elevated"})
```

It issues `PUT /internal/ctam/v1/routes/{routeId}` to the existing loopback
server. The host authenticates the bearer token to a single module and then:

1. validates the route ID as one safe decoded path segment;
2. verifies that the caller declared that route ID in `module.toml`;
3. validates finite JSON, nesting depth, per-route bytes, and aggregate
   per-module route bytes;
4. stages the payload in that module's existing transaction; and
5. returns the staged route ID and byte count in the normal internal envelope.

Calling the method again before commit replaces the staged payload for that
route, matching `PUT` semantics. A transaction commit seals cell patches,
history patches, alerts, and route payloads together. Abandon, timeout, process
failure, missing commit, or failed validation discards every staged route.

Keep this as an additive internal API v1 operation: update the internal OpenAPI
document, schemas, SDK documentation, and `cycle.allowed_operations`. Do not
expose a public write endpoint and do not pass the public API URL or credentials
to module subprocesses.

### Durable artifact layout

Publish only beneath the configured runtime data root:

```text
<BASE_DIR>/data/ctam/public/
├── registry.json
└── modules/
    └── <module-id>/
        └── <route-id>.json
```

`registry.json` is host-generated from successfully parsed, enabled manifests;
module code never writes it. It records schema version, module ID/name/version,
route ID/description/href, and whether a committed representation exists.

Each route artifact is also host-generated and contains:

```json
{
  "schema_version": 1,
  "module_id": "cellstats",
  "module_version": "1.0.0",
  "route_id": "forecast-summary",
  "cycle_id": "20260915-120000",
  "published_at": "2026-09-15T12:00:05Z",
  "data": {"risk": "elevated"}
}
```

The wrapper prevents a module from spoofing ownership or freshness metadata.
Use the configured data directory rather than the module installation directory
so the API never needs access to executable module packages.

Preserve the last successfully committed representation when a later cycle
skips or fails a module; consumers can judge freshness from `cycle_id` and
`published_at`. If a module or declaration is removed, omit it from the next
registry so any orphaned payload is no longer addressable. Physical cleanup can
be a later retention feature and should not be coupled to this first change.

## Implementation phases

### 1. Freeze the public and manifest contracts

- Add the three public paths and schemas to `src/api/openapi/v3.yaml`.
- Define collection, module descriptor, route descriptor, and route response
  shapes. Use the standard `{data, meta}` public envelope; return the module's
  payload in `data` and host metadata (`moduleId`, `moduleVersion`, `routeId`,
  `cycleId`, `publishedAt`) in `meta`.
- Define errors explicitly:
  - `400 INVALID_PATH` for malformed identifiers;
  - `404` for an undeclared module or route;
  - `503 MODULE_ROUTE_UNAVAILABLE` for a declared route with no committed
    representation;
  - `503 SERVICE_NOT_ENABLED` when the EdgeWARN producer is disabled or stale.
- Add `public_routes` to `docs/ctam/module-manifest.md` and add its limits and
  excess behavior to `docs/ctam/internal-api-limits.md` before copying constants
  into Python.

### 2. Parse and validate route declarations

- Extend `src/EdgeWARN/ctam/manifest.py` with `PublicRoute`, strict route ID and
  description validation, duplicate rejection, and count limits.
- Keep discovery failure isolation unchanged: an invalid route declaration
  makes only that module invalid and publishes an actionable reason.
- Include declarations in discovery/list/check output so operators can verify
  the resulting public URL without running a cycle.
- Extend manifest and discovery tests for valid declarations, absent optional
  declarations, duplicates, traversal-like IDs, encoded slash/control
  characters, excessive descriptions, and route-count overflow.

### 3. Stage route payloads through the CTAM transaction

- Add a `staged_routes` mapping to `ModuleTransaction` and include its count and
  bytes in transaction snapshots/status records.
- Add `stage_route(module_id, route_id, payload)` and
  `committed_routes()` to `CTAMTransactionService`.
- Apply the same lock, open/sealed checks, authentication-derived ownership,
  finite-JSON rules, and abandon/commit semantics used by existing mutations.
- Add a distinct `route_not_declared`/forbidden error instead of silently
  creating a public surface that was absent from the manifest.
- Add transport support in `src/EdgeWARN/ctam/api/service.py` and
  `server.py`, including `do_PUT`, strict URL decoding, and rejection of extra
  path segments.
- Add `CTAMClient.register_route()` and update the example CTAM module to show
  registration before `commit_transaction()`.

### 4. Carry committed routes to cycle publication

- Introduce an internal `CTAMRunResult` containing updated cells, discovered
  manifests, module run results, and committed route payloads. Keep the public
  `run_ctam(...) -> list[cells]` compatibility wrapper if callers outside the
  integration pipeline rely on the current return type.
- Update the integration path to retain the richer result and pass its route
  payloads into `_publish_cycle`.
- Build the registry and wrapped route artifacts in host code, never in module
  code.
- Add those files to the same `CTAMPublicationCoordinator.publish(...)` payload
  map as the storm snapshot and histories. Route data must not become visible
  before the outer cycle publication succeeds, and crash recovery must roll all
  prepared targets forward together.
- Merge prior route availability metadata only for declarations still present,
  allowing last-known-good payloads while preventing removed declarations from
  remaining reachable.

### 5. Add a Node module-route service

- Create `src/api/services/modules.js` using `ArtifactRepository`; inject it from
  `src/api/app.js` into `createV3Router`.
- Read `data/ctam/public/registry.json`, validate its schema defensively, and use
  it as the allowlist before resolving a payload path. Never construct a file
  lookup solely from request parameters.
- Validate `moduleId` and `routeId` as safe single segments even after Express
  decoding. Reject `.`, `..`, slashes, backslashes, percent-encoded separators,
  NUL/control characters, repeated decoding tricks, and identifiers beyond the
  contract limits.
- Confirm the payload wrapper matches the registry and requested IDs before
  returning its `data`; treat mismatches or malformed JSON as
  `INVALID_ARTIFACT`, not module-controlled HTTP output.
- Return an empty `/modules` collection when the registry has never been
  published, consistent with existing index-backed collection behavior. Detail
  routes still return 404/503 as specified above.

### 6. Mount the public routes within v3 policy

- Add `/modules` to collection query validation and mount all three handlers in
  `src/api/routes/v3/index.js` behind `requireService('edgewarn')`.
- Use existing `collection()` and `resource()` helpers so pagination, envelopes,
  cache control, CORS, rate limiting, request IDs, and error handling remain
  uniform.
- Add the modules link to `GET /api/v3` discovery.
- Because the wildcard route is declared statically as
  `/api/v3/modules/{moduleId}/{routeId}` in OpenAPI, the existing 405 generator
  can recognize it. Verify `POST`, `PUT`, `PATCH`, and `DELETE` return the
  read-only `405` response and `Allow: GET, HEAD`.
- Ensure access-log route templating records the template only and never route
  payloads or query values.

### 7. Test end to end

Python tests:

- Manifest parsing and discovery coverage for all declaration constraints.
- Transaction tests for declared/undeclared routes, cross-module attempts,
  replacement semantics, JSON/size/depth limits, commit, abandon, timeout, and
  unsealed process exit.
- Loopback API and SDK tests for authenticated `PUT`, malformed encoded IDs,
  unsupported methods, response envelopes, and `allowed_operations`.
- Integration publication tests proving route files and registry appear only
  with the storm snapshot/index commit, recover after an interrupted replace,
  retain last-known-good data after a later module failure, and stop exposing a
  removed declaration.

Node/Jest tests:

- Empty collection before first publication, paginated module discovery,
  module details, successful payload retrieval, standard envelopes/cache
  headers, and implicit `HEAD`.
- Unknown versus declared-but-unavailable behavior and EdgeWARN service gating.
- Registry/payload mismatch, oversized or malformed artifacts, symlinks, path
  traversal/encoding cases, invalid queries, and non-GET 405 responses.
- OpenAPI path/schema coverage and access-log template redaction.

Run targeted suites first:

```bash
conda run -n EdgeWARN-dev python -m pytest tests/core/ctam tests/integration
npm test -- --runInBand tests/api/test_unified_app.js
```

Then run the full Python and Node suites before merging.

### 8. Synchronize documentation and operations

- Update `docs/api/api_endpoints.md`, `docs/api/unified_v3.md`, and
  `docs/api/api_implementation.md` with the resource family and response/error
  contract.
- Update `docs/ctam/module-development.md`, `module-manifest.md`,
  `internal-api.md`, the internal OpenAPI document, and schema README with the
  declaration/register/commit lifecycle.
- Update `docs/ctam/module-operations.md` with registry inspection and stale
  payload guidance.
- Document that route changes require an EdgeWARN cycle to publish and do not
  require a Node API restart.

## Acceptance criteria

- An enabled, valid module can declare a route, call
  `CTAMClient.register_route()`, commit, and have its JSON returned at the
  deterministic `/api/v3/modules/{moduleId}/{routeId}` URL after the containing
  cycle publishes.
- A module cannot register an undeclared route, another module's route, an
  arbitrary public path, a non-JSON response, or any HTTP behavior.
- Failed, abandoned, timed-out, or uncommitted module work never reaches the
  public endpoint.
- Public route artifacts are confined to the configured runtime base directory,
  are allowlisted by the host registry, and participate in recoverable atomic
  cycle publication.
- Existing `/api/v3` security, pagination, service gating, caching, method, and
  error contracts apply to the new resource family.
- OpenAPI, CTAM schemas/limits, module author documentation, operator
  documentation, Python tests, and Jest tests all describe and verify the same
  behavior.

## Deliberate first-version exclusions

- Arbitrary Express handlers or loading CTAM packages in Node.
- Public write methods, authentication delegated to a module, module-controlled
  response headers/status codes, redirects, streaming, HTML, or binary payloads.
- Free-form nested public paths or query-parameter schemas.
- Per-route historical snapshot APIs and automatic deletion of orphaned route
  files; both can be added later without changing the initial namespaced route
  contract.
