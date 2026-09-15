# EdgeWARN-Core 3.0.0 Release Readiness

**Assessment date:** 2026-09-15
**Reviewed state:** `version-test/3.0.0` at `72e7ca19`, plus the render-contract
remediation in the current working tree
**Decision:** **Go for 3.0.0 within the reviewed scope.**

## Executive summary

The codebase has strong release-candidate maturity. The Python and Node
versions are synchronized at 3.0.0, the repository contains substantial unit,
integration, process, API-contract, packaging, and container coverage, and the
reported test suite, container smoke test, and API runtime are healthy.

The previously identified StormProb delivery and npm dependency issues have
since been addressed in the reviewed branch: the wheel/Docker build now carry
the model assets, packaging tests exercise installed-artifact inference, and
the lockfile contains the dependency remediation commit. The render contract
is now consistently chunk-only, with client-owned styling and explicit
retirement behavior for obsolete PNG routes.

## Resolved release blockers

### Resolved: StormProb assets are delivered in the installed artifact

The reviewed branch now declares `models/stormprob/*` as wheel data-files and
copies `models` into the Docker build ([`pyproject.toml`](../pyproject.toml#L54),
[`Dockerfile`](../Dockerfile#L12)).

Packaging tests now assert delivery and probe installed-wheel model loading and
inference ([`test_package_delivery.py`](../tests/packaging/test_package_delivery.py#L81),
[`test_installed_command.py`](../tests/packaging/test_installed_command.py#L147)).
Treat this item as closed, subject to the user-reported container smoke test.

### Resolved: Production dependency remediation

The lockfile includes the remediation commit `e2f292d3`, updating the affected
production packages (`js-yaml`, `morgan`, `express`, `body-parser`, and `qs`).
The audit command remains available as `npm run audit:prod` in
[`package.json`](../package.json#L14). A networked CI run should still verify
the registry-backed audit result before tagging, but the prior vulnerable
lockfile is no longer the reviewed state.

### Resolved: Render delivery and client contract

The renderer, static product catalog, v3 routes, OpenAPI document, and client
documentation now agree on `binary_chunks` as the sole current render
representation ([`render.py`](../src/EWMRS/render/render.py#L138),
[`product-catalog.json`](../src/api/config/product-catalog.json#L1)).

V3 no longer advertises PNG image or tile resources. The obsolete legacy PNG
download/tile routes return `410 Gone` with a successor link. Scalar values are
explicitly client-styled; the API does not promise a public colormap catalog
([`unified_v3.md`](api/unified_v3.md#L49)). Contract tests cover all three
decisions.

This blocker is closed.

## Important non-blocking cleanup

- Documentation alternates between `EdgeWARN-dev` and the actual `EdgeWARN`
  environment ([`README.md`](../README.md#L30),
  [`deployment.md`](api/deployment.md#L70)).
- The README calls `run.py` a deprecated alias, while the implementation and
  installation guide say it is retired and exits with status 2
  ([`README.md`](../README.md#L115), [`INSTALLATION.md`](../INSTALLATION.md#L189)).
- The changelog still describes `run.py` as a deprecated alias even though the
  command is retired ([`CHANGELOG.md`](../CHANGELOG.md#L3)).
- The real Docker build/run test is opt-in and skipped by default in CI
  ([`test_container_smoke.py`](../tests/packaging/test_container_smoke.py#L10)).
- `pyproject.toml` labels the package Beta and OS Independent, declares no
  Python dependencies, and relies on a Conda environment as the dependency
  authority. That is acceptable for an explicitly Conda/container-only
  distribution, but should be made intentional in release metadata.
- The supplied Docker/Compose topology runs the Python services; the Node API
  remains a separately installed service. If 3.0.0 promises one complete
  backend image, the image and Compose file need an API service as well.

## Evidence of maturity

- The render-contract implementation and documentation changes are present in
  the current working tree; an unrelated untracked planning document was left
  untouched.
- Version `3.0.0` is aligned in `package.json`, `pyproject.toml`, the API
  OpenAPI document, and package-command CI.
- CI separates Node API contracts, offline Python correctness, connected
  integration, process/restart behavior, wheel installation, and configuration
  validation.
- The render-contract remediation passes all 82 Jest API tests and 125 targeted
  Python catalog, API-surface, and renderer tests.
- Runtime architecture includes service heartbeats, single-instance locks,
  durable cross-service handoff, atomic publication, bounded teardown, and
  artifact-path containment.
- API implementation includes security headers, rate limits, request IDs,
  conditional caching, structured errors, and readiness checks.

## Recommended release sequence

1. Verify `npm run audit:prod` in networked CI and make it a required gate.
2. Run a real Docker smoke test from the exact release artifact.
3. Correct the remaining non-blocking release documentation and metadata, then
   tag `v3.0.0`.

## Final status

**Release status: ready within the reviewed scope.** Ignoring the requested
items 2, 5, and 6, the implementation is mature enough for stable 3.0.0. The
previous packaging, dependency-lock, and render-contract blockers are
addressed. The remaining sequence items are pre-tag verification and
non-blocking cleanup rather than known functional release blockers.
