# EdgeWARN-Core 3.0.0 Release Readiness

**Assessment date:** 2026-09-14  
**Reviewed ref:** `version-test/3.0.0` (`e77678a7`)  
**Decision:** **No-go for a stable 3.0.0 release; suitable as a release candidate after blockers are closed.**

## Executive summary

The codebase has strong release-candidate maturity. The worktree is clean, the
Python and Node versions are synchronized at 3.0.0, the repository contains
substantial unit, integration, process, API-contract, packaging, and container
coverage, and the reported test suite, container smoke test, and API runtime
are healthy.

Those signals do not yet establish a shippable distribution. The most important
failure is packaging: the StormProb model assets are tracked in
`models/stormprob/`, but neither the Python wheel nor the Docker build copies
that directory. A container can therefore start successfully while StormProb
inference fails at runtime. The production Node dependency audit also fails,
and the render/colormap/compatibility contract is internally inconsistent.

## Release blockers

### 1. StormProb assets are not delivered in the installed artifact

`src/EdgeWARN/stormprob/assets.py` searches parent directories for
`models/stormprob/manifest.json`. The wheel configuration only includes package
data for `EWMRS`, while the Docker build copies `src` and `config`, not
`models` ([`pyproject.toml`](../pyproject.toml#L30),
[`Dockerfile`](../Dockerfile#L12)).

The built-in adapter catches model-loading exceptions and records failed
forecasts, allowing the supervisor and health checks to remain apparently
healthy ([`assets.py`](../src/EdgeWARN/stormprob/assets.py#L8),
[`__init__.py`](../src/EdgeWARN/ctam/builtins/stormprob/__init__.py#L180)).
This is a functional release blocker, not merely a test gap.

Required closure:

- package the manifest, ONNX graphs, calibrator, and normalization data;
- add an installed-wheel and container test that performs one real model load
  and inference;
- verify that missing model assets fail readiness or startup explicitly rather
  than degrading silently.

### 3. Production dependency audit fails

`npm audit --omit=dev --audit-level=high` returned five vulnerabilities on the
reviewed lockfile: one high-severity `js-yaml` issue and four moderate issues
in `morgan` and the `express`/`body-parser`/`qs` dependency chain. The audit
script exists in [`package.json`](../package.json#L14), but CI does not execute
it.

Required closure: update dependencies and lockfile deliberately, rerun the API
contract suite, and make the production audit a required CI gate.

### 4. Render delivery and client contract are inconsistent

The renderer now refuses to write flat PNG output and writes float16 chunks
instead ([`render.py`](../src/EWMRS/render/render.py#L138)). At the same time:

- the product catalog initially labels products `png-tiles`;
- PNG image/tile compatibility routes remain advertised;
- the documentation says clients use a published product colormap;
- the colormap catalog/API was removed.

Relevant evidence: [`product-catalog.json`](../src/api/config/product-catalog.json#L1)
and [`unified_v3.md`](../docs/api/unified_v3.md#L49).

Required closure: choose and document one authoritative client contract,
provide the colormap data required by raw chunks, and either make compatibility
routes work against fresh output or explicitly remove and sunset them.

### 5. Release branch integration is unresolved

The release branch is not a small change set relative to the release targets:
Git reports 1,311 commits unique to `origin/main` and 1,643 unique to the
release branch from their merge base. `origin/main` and `origin/dev` still
point to the 2.7 deployment line, while this branch has no `v3.0.0` tag.

Before tagging, reconcile the branch through the intended merge/review path
and verify that mainline fixes, security changes, and deployment changes have
not been omitted.

### 6. StormProb promotion evidence is incomplete

The project's own rollout document requires a 20-cycle runtime audit,
held-out scorecard comparison, and full cycle-latency measurement. It currently
records focused test-suite verification only ([`stormprob_phase5.md`](core/stormprob_phase5.md#L9)).
Complete those checks after fixing asset delivery.

## Important non-blocking cleanup

- Documentation alternates between `EdgeWARN-dev` and the actual `EdgeWARN`
  environment ([`README.md`](../README.md#L30),
  [`deployment.md`](api/deployment.md#L70)).
- The README calls `run.py` a deprecated alias, while the implementation and
  installation guide say it is retired and exits with status 2
  ([`README.md`](../README.md#L115), [`INSTALLATION.md`](../INSTALLATION.md#L189)).
- The changelog still describes colormap resources, sparse RGBA output, and a
  deprecated `run.py` alias ([`CHANGELOG.md`](../CHANGELOG.md#L3)).
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

- Clean working tree at the reviewed ref.
- Version `3.0.0` is aligned in `package.json`, `pyproject.toml`, the API
  OpenAPI document, and package-command CI.
- CI separates Node API contracts, offline Python correctness, connected
  integration, process/restart behavior, wheel installation, and configuration
  validation.
- Runtime architecture includes service heartbeats, single-instance locks,
  durable cross-service handoff, atomic publication, bounded teardown, and
  artifact-path containment.
- API implementation includes security headers, rate limits, request IDs,
  conditional caching, structured errors, and readiness checks.

## Recommended release sequence

1. Fix and test StormProb asset packaging and confirm model licensing.
2. Resolve the render/colormap/compatibility contract and update the OpenAPI
   and migration documentation.
3. Remediate the npm audit findings and add the audit to CI.
4. Reconcile the release branch with the intended mainline target.
5. Run the 20-cycle StormProb audit, held-out scorecard, latency check, and a
   real Docker smoke test from the exact release artifact.
6. Correct release documentation and metadata, then tag `v3.0.0`.

## Final status

**Release status: blocked.** The implementation is mature enough to continue as
a release candidate, but stable 3.0.0 publication should wait until the
packaged artifact is functionally complete, model redistribution is authorized,
production dependencies are cleared, and the public render contract is made
internally consistent.
