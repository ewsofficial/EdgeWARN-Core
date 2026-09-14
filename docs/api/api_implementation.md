# EdgeWARN Unified API Technical Implementation

The live Express implementation is `src/api/`, started with `npm run api` (or
`npm run debug:api`). It serves `/api/v3` as its primary contract and retains
the `/api/v2`, `/renders`, `/nexrad`, `/rap`, `/wpc`, `/health`,
and `/healthz` paths as compatibility adapters in the same process.

There is no `src/EdgeWARN/api` or `src/EWMRS/api` tree; both were removed in
commit `a3d6cbb`. Route-level contracts live in `docs/api/api_endpoints.md` and
`docs/api/ewmrs_api_endpoints.md`.

## File Structure

```text
src/api/
├── server.js                   # listen, port selection, effective-config report
├── app.js                      # composition root: config, repository, services, routes
├── config/
│   ├── index.js                # catalog load + CLI/env overlay resolution
│   ├── productCatalog.js       # catalog invariants and lookup maps
│   └── product-catalog.json    # 32 render products
├── openapi/v3.yaml             # JSON despite the extension; served verbatim at /api/v3/openapi.json
├── middleware/
│   ├── requestId.js
│   ├── logging.js
│   ├── security.js             # helmet, compression, request timeout
│   ├── cors.js
│   ├── rateLimit.js
│   └── errors.js               # notFound + problem+json handler
├── repositories/
│   └── artifactRepository.js   # rooted, symlink-refusing file reads
├── services/
│   ├── analysis.js             # cells, storm snapshots, alerts, METAR
│   ├── renders.js              # products, snapshots, PNG tiles, float16 chunks
│   ├── ancillary.js            # NEXRAD, RAP, and WPC
│   └── validation.js           # identifier validators and pagination
└── routes/
    ├── v3/index.js
    └── compatibility/index.js
```

## Startup

`server.js` calls `createApp()` and then `app.listen()`.

- Bind address is `api.yaml` `server.host` (`0.0.0.0`)
- Port is `api.yaml` `server.debug_port` (`3001`) when `--debug-server` is
  passed, otherwise `PORT` if set, otherwise `api.yaml` `server.port` (`5000`)
- `--compat=edgewarn` and `--compat=ewmrs` are accepted, print a deprecation
  warning, and start the same unified service

On successful listen the process logs the effective configuration: the config
root, each loaded catalog with its schema version, the list of active override
*layers*, the enabled product counts, and the port and base directory. It
reports which layer won for each override rather than the value, matching
`report_effective_config` in `src/run.py`, so a diagnostic never discloses a
configured secret. Configuration is read once; changes require a restart.

There is no `dotenv` load, no `cluster` fork, and no JSON body parser — the
service answers `GET` and `HEAD` only. Runtime directories are not created at
startup; `/health/ready` reports missing ones instead.

## Request Lifecycle

`app.js` mounts, in order:

1. `requestId` — echoes an inbound `X-Request-Id` matching
   `^[A-Za-z0-9_-]{8,128}$`, otherwise generates a UUID, and always sets the
   response header
2. the access log, only when `api.yaml` `logging.access_log_enabled` is true
3. `helmet` and `compression`
4. `cors`
5. the per-second and per-minute rate limiters
6. `requestTimeout`

then the routes:

- `GET /` — service banner with links to `/api/v3` and the OpenAPI document
- `GET /robots.txt`
- `GET /health/live` — always `200`, includes the config diagnostics block
- `GET /health/ready` — `200` or `503` after stat-ing the `data`, `gui`, and
  `wpc` roots
- the `/api/v3` router
- the compatibility router
- `notFound`, then the error handler

### Access log

One JSON line per finished response, at event `api_access`, carrying
`requestId`, `method`, `route`, `status`, `bytes`, and `durationMs`. `route` is
the matching OpenAPI **path template** rather than the concrete URL, so
high-cardinality identifiers do not enter log aggregation; an unrecognized path
logs `unmatched`.

## Configuration (`config/index.js`)

`validateAllConfigs()` runs before anything else, so an invalid catalog tree
fails startup rather than a later request. `api.yaml`, `filesystem.yaml`, and
`wpc.yaml` are then loaded through the shared `src/config/loader.js`, the same
loader the Python side uses.

Config-tree selection: `--config-dir`, then `EDGEWARN_CONFIG_DIR`, then
discovery from the installed source tree.

Base-directory resolution:

1. `--base-dir` (or the compatibility alias `--base_dir`)
2. `EDGEWARN_BASE_DIR`
3. `BASE_DIR`
4. `filesystem.yaml` `base_dir.windows` or `base_dir.posix` by platform

A leading `~` is expanded, and the result is resolved to an absolute path.
Supplying the same flag twice with different values throws rather than silently
picking one. Derived roots are `<BASE_DIR>/data`, `<BASE_DIR>/gui`, and
`<BASE_DIR>/wpc`; the `static` root is `src/EWMRS`, which is where

Integer environment overrides are validated, not coerced: `PORT`,
`REQUEST_TIMEOUT_MS`, `RATE_LIMIT_MAX_SEC`, and `RATE_LIMIT_MAX_MIN` must be
non-negative integer strings, and a malformed value throws at startup.

## Artifact Repository

Every file read goes through `ArtifactRepository`, which owns the path safety
rules rather than leaving them to individual routes:

- path segments must match `^[A-Za-z0-9_.-]+$` and cannot be `.` or `..`
- each root is `realpath`-resolved once and must be a real directory
- every intermediate segment is `lstat`-checked; symbolic links and
  non-directory components are refused
- files are opened with `O_NOFOLLOW` and must be regular files
- size is capped per kind from `api.yaml` `artifacts.size_limits_bytes`
  (`json`, `binary`, `image`); the constructor rejects an incomplete limit map
  rather than defaulting, since a missing entry would make the size guard
  vacuously false
- directory listings drop symlinked entries and are truncated to
  `artifacts.list_limit`
- ETags are weak and derived from size, mtime, and inode

Parsed JSON is memoized in an LRU cache bounded by both `max_entries` and
`max_size_bytes`, keyed by root and path, and only reused when the stored ETag
still matches — so a rewritten artifact is never served from cache.

## Validation and Pagination (`services/validation.js`)

- timestamps: `^\d{8}-\d{6}$` **and** a real UTC calendar instant, returned as
  an ISO string
- cell IDs: `^[1-9][0-9]*$`
- alert IDs: `^[A-Za-z0-9_.:-]{1,200}$`, excluding `__proto__`, `constructor`,
  and `prototype`
- RAP layer IDs: `^[A-Za-z0-9_.-]{1,128}$` and must not contain `..`

Collections are cursor-paginated. The cursor is the `id` of the last item in the
previous page, `limit` defaults to `api.yaml` `pagination.default_limit` (100)
and is clamped to `max_limit` (1000), and `meta.nextCursor` is `null` on the
final page.

## v3 Router Behavior

- Query parameters are allowlisted per path. Only the collection paths accept
  `cursor` and `limit`; the alert paths additionally accept `source`; every
  other v3 path accepts none. Repeated parameters, non-string values, and values
  over `query.max_value_length` (256) are rejected.
- Cache lifetimes come from `api.yaml` `cache_control_max_age`: `5` seconds for
  collections, `60` for single resources and GeoJSON, and one year plus
  `immutable` for binary assets.
- Binary and image responses carry an `ETag`, honor `If-None-Match` with a `304`,
  and support `HEAD`.
- Any path that matches an OpenAPI template but arrives with another method gets
  `405` and an `Allow: GET, HEAD` header. The route table is derived from the
  spec, so it cannot drift from it.

## Error Handling

One handler answers `application/problem+json` with `Cache-Control: no-store`
and a `requestId` member, and logs an `api_error` JSON line. Status comes from
the `ArtifactError` code — `NOT_FOUND` is `404`, `INVALID_ARTIFACT` and
`IN_PROGRESS` are `503`, and everything else is `400`. Detail text for `5xx`
responses is replaced with a fixed string so internal paths and parser messages
are not disclosed.

Two responses deliberately do not use problem+json: the rate limiter's
`{ "error": "Too many requests, please try again later" }` and the request
timeout's `{ "error": "Request timed out" }` at `503`.

## Environment Variables

- `EDGEWARN_CONFIG_DIR`
- `EDGEWARN_BASE_DIR`, and the compatibility alias `BASE_DIR`
- `PORT`
- `REQUEST_TIMEOUT_MS`
- `RATE_LIMIT_MAX_SEC`, `RATE_LIMIT_MAX_MIN` — `0` disables that window
- `ALLOWED_ORIGINS` — comma-separated exact origins; CORS is deny-all when unset
- `TRUST_PROXY_IPS` — comma-separated allowlist, or a hop count of `0` to `8`
- `TRUST_PROXY` — accepted, but the bare `true` form throws under
  `NODE_ENV=production` and counts as one hop otherwise
- `NODE_ENV` — `production` withholds the package version behind
  `api.yaml` `server.production_version_label`

Rate-limit *windows* have no environment override; they are YAML-only.

Only set proxy trust when a stripping reverse proxy removes client-supplied
`X-Forwarded-For` and `X-Forwarded-Proto` headers before forwarding. On a
directly exposed host, enabling trust lets clients spoof those headers and
bypass per-client rate limiting.

## Runtime Modes

- `npm run api` — default port `5000`
- `npm run debug:api` — `--debug-server`, port `3001`
- EdgeWARN and EWMRS compatibility routes are served by that same process

See also:

- `docs/api/unified_v3.md` (v3 contract and the binary chunk format)
- `docs/api/api_endpoints.md` (v2 compatibility routes)
- `docs/api/ewmrs_api_endpoints.md` (EWMRS compatibility routes)
- `docs/core/configuration.md` (which catalog owns which setting)
