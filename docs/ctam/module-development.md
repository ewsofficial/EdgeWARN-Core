# Developing a CTAM module

Start from [`examples/ctam_module`](../../examples/ctam_module). Copy it to an
operator-owned directory outside this repository, rename the directory and the
manifest `id`, then declare every required input and output in `module.toml`.
The SDK client is `EdgeWARN.ctam.sdk.CTAMClient`; it uses only Python's standard
library and the documented HTTP API.

Modules are trusted executable code. Installing one grants it the EdgeWARN
service account's OS permissions; the loopback API restricts the supported data
contract but is not a container or sandbox.

Use a module-owned virtual environment when dependencies differ from EdgeWARN:
set the manifest entrypoint to that environment's Python executable followed by
the module program. The entrypoint is an argument vector, never a shell command.
Keep lockfiles and dependencies beside the module, not in EdgeWARN-Core.

At runtime a module receives `CTAM_API_URL`, `CTAM_API_TOKEN`, `CTAM_CYCLE_ID`,
and `CTAM_MODULE_ID`. Authenticate every request with the bearer token, read
only declared selectors, stage owned JSON patches, and call
`/transaction/commit` only after all intended changes are staged. Do not write
stormcells, histories, indexes, or alerts directly.

For every `[[public_routes]]` declaration, stage or replace its JSON value with
`client.register_route(route_id, payload)` before committing. Registration is a
transactional `PUT`: repeating it replaces that route's staged value, and a
failed, abandoned, timed-out, or uncommitted run publishes none of it.

See [module-manifest.md](module-manifest.md) for declarations and
[internal-api.md](internal-api.md) plus the [OpenAPI contract](openapi/ctam-internal-v1.json)
for the wire format.
