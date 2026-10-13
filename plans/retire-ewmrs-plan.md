# Retire EWMRS and remove all mentions from code

Status: implementation in progress.

## 1. Outcome and scope

Delete the EWMRS runtime surface and erase every `EWMRS` / `ewmrs` mention
from executable code, configuration, schemas, CLI help, API contracts, docs,
tests, fixtures, benchmarks, and packaging so that a case-insensitive search
for `ewmrs` returns zero hits on tracked source (excluding historical release
notes if explicitly retained — see §3).

Concretely this means:

- Remove the standalone service `src/run_ewmrs.py` and the whole
  `src/EWMRS/` package (`pipeline.py`, `pipeline_config.py`, `rap/`,
  `render/`, and their `AGENTS.md` guides).
- Remove EWMRS runtime support: `src/util/runtime/ewmrs_service.py`,
  `src/util/runtime/ewmrs_consumer.py`, EWMRS branches in
  `src/util/runtime/background.py`, `src/util/runtime/goes.py`,
  `src/util/runtime/ingest_handoff.py` (the `ewmrs-inputs-v1` consumer
  namespace), `src/util/runtime/handoff.py` (EWMRS checkpoints), and
  `src/util/runtime/services.py` (`ewmrs` canonical name + route gates).
- Remove EWMRS configuration: `config/ewmrs_pipeline.yaml`,
  `config/ewmrs_render.yaml`, their schemas
  `config/schema/ewmrs_pipeline.schema.json` /
  `config/schema/ewmrs_render.schema.json`, and every `ewmrs_*` /
  `disable_ewmrs` / `disable_metar|nws|wpc|goes`-as-EWMRS key in
  `config/runtime.yaml`, `config/api.yaml`, `config/ingest.yaml` overlays,
  plus `src/config/mrms-products.js` and `src/config/loader.js` mirrors.
- Remove EWMRS from launchers and packaging: `src/run_all.py`
  (`SERVICE_SCRIPTS`, `_ROUTING`, `resolve_services`,
  `preflight_topology`, `build_service_commands`), `src/edgewarn_cli/run.py`
  (`TOPOLOGIES`, `WORKERS`, `_WORKER_TO_SERVICE`, wrapper-owned flags),
  `src/util/cli.py` (`build_service_parser("ewmrs")`, `--render-consumer`
  help text, `--disable-ewmrs`), `src/util/io.py`, `src/run_ingest.py`
  (`--render-consumer`), `pyproject.toml` (`run_ewmrs` module, `EWMRS*`
  packages, `EWMRS` package-data), `Dockerfile` / `compose.yaml` /
  `docker/edgewarn-entrypoint.sh` service entries.
- Remove or re-gate EWMRS-owned API surface in `src/api/`: `serviceRegistry.js`,
  `routes/v3/index.js`, `openapi/v3.yaml`, `server.js` compat warning.
  Default recommendation is deletion of
  `/api/v3/render-products`, `/api/v3/models/rap`, `/api/v3/analyses/wpc`
  families unless §3 decides to rehome them (see open question Q1).
- Remove EWMRS docs, tests, fixtures, benchmarks, and scripts (full inventory
  in §2), and update `AGENTS.md`, `src/AGENTS.md`, `README.md`,
  `INSTALLATION.md`, `CHANGELOG.md`, `docs/core/*`, `docs/api/*`,
  `docs/ctam/*`, `CONTRIBUTING.md` prefix examples if they name EWMRS.

Non-goals: no new rendering engine, no behavioral change to Core
(detection/integration/tracking/CTAM/StormProb/alerts) or NEXRAD except
deleting the EWMRS coupling points. No data migration of already-published
`gui/` float16 / RAP Uint16 artifacts; operators retain old files on disk but
no code reads or writes them after retirement.

## 2. Findings in the current tree

Inventory generated 2026-10-13 by case-insensitive `ewmrs` search over tracked
files (`__pycache__`, `*.pyc`, `.pytest_cache`, `build/` excluded from the
deletion set — `build/` is generated output, do not hand-edit). 203 total hits
including build artifacts; ~150 tracked-source hits remain.

### 2.1 Runtime ownership that disappears

| Location | What it does today | Retirement action |
| --- | --- | --- |
| `src/EWMRS/pipeline.py` (`_render_layer`, `run_render_pipeline`, `ewmrs_goes_worker`) | MRMS float16 rendering, tiling, GOES render worker | Delete package |
| `src/EWMRS/render/{render,tiler,goes_transform,tools,config}.py` | Raster reprojection, chunk writing, colormaps, layer lists | Delete package |
| `src/EWMRS/rap/{config,uint16_pipeline}.py` | RAP GRIB → Uint16 conversion, index maintenance, retention | Delete package |
| `src/EWMRS/pipeline_config.py` | Reads `config/ewmrs_pipeline.yaml` | Delete package |
| `src/run_ewmrs.py` (`SERVICE_NAME="ewmrs"`) | Lock, heartbeat, `register_ewmrs_accessories`, GOES/METAR/NWS/WPC children | Delete file |
| `src/util/runtime/ewmrs_service.py` (`register_ewmrs_accessories`) | Registers `GOES Render`, `EWMRS Consumer` + METAR/NWS/WPC loops | Delete file |
| `src/util/runtime/ewmrs_consumer.py` (`CONSUMER_NAME="ewmrs-inputs-v1"`, `ewmrs_consumer_loop`) | Per-input/layer job queue, bounded executor, ack store | Delete file |
| `src/util/runtime/background.py` (imports `EWMRS.pipeline.ewmrs_goes_worker`) | GOES background render path | Delete EWMRS branch; decide GOES fate per §3 Q2 |
| `src/util/runtime/goes.py` (`get_ewmrs_goes_render_specs`) | GOES spec adapter over EWMRS render config | Delete file or shrink to ingest-only helper if GOES ingest is rehomed |
| `src/util/runtime/ingest_handoff.py` (`consumers/ewmrs-inputs-v1`, `render-ready/`) | Per-input render outbox + acks | Delete render outbox; keep Core `core-ingest-v1` path |
| `src/util/runtime/handoff.py` (`ewmrs-{phase}` checkpoints) | Legacy cycle checkpoints consumed by EWMRS | Delete EWMRS checkpoint helpers |
| `src/util/runtime/services.py` (`CANONICAL_SERVICE_NAMES`, `ROUTE_SERVICE_REQUIREMENTS`) | `("ingest","edgewarn","ewmrs","nexrad")`; render/RAP/WPC routes → `ewmrs` | Reduce to `("ingest","edgewarn","nexrad")`; delete/regate render routes |
| `src/util/runtime/primary_service.py`, `cycle.py` | Core publishes EWMRS records (legacy) / skips when `disable_ewmrs` | Delete publish + flag branches |
| `src/common/pipeline/coordinator.py` (`include_ewmrs`, `ewmrs_*_ready`, `on_ewmrs_*`) | Staged ingest callbacks for EWMRS | Delete params/callbacks; keep historical batch API shape minus EWMRS |
| `src/common/pipeline/goes_readiness.py`, `src/common/ingest/mrms/main.py` (`get_ewmrs_modifiers`, `download_ewmrs_files_async`) | EWMRS product selectors | Delete or rename to neutral names if still needed by ingest |
| `src/NEXRAD/{config,gui_pipeline,render}.py` (reads `ewmrs_pipeline` for shared budgets) | Reuses EWMRS pipeline config section | Point at `runtime`/`nexrad` config or duplicate owned keys; drop import |
| `src/EdgeWARN/pipeline.py` (`include_ewmrs=False`) | Skips EWMRS trigger in historical/Core path | Delete parameter |
| `src/edgewarn_cli/migrate_mrms.py` (`ewmrs_render.mrms_layers`, `ewmrs-{phase}` checkpoint) | MRMS migration reads EWMRS catalog + checkpoints | Delete EWMRS half of migration |

### 2.2 Launchers, CLI, config

| Location | EWMRS mention | Action |
| --- | --- | --- |
| `src/run_all.py` | `SERVICE_SCRIPTS["ewmrs"]`, `_ROUTING` (`--disable-metar/nws/wpc` → `ewmrs`; `--disable-goes/ctam/stormprob` includes `ewmrs`), `resolve_services` (`omit_ewmrs`, `EDGEWARN_DISABLE_EWMRS`), `preflight_topology` (`{"edgewarn","ewmrs"}` requires `ingest`), `--render-consumer` injection, `--disable-ewmrs/--mrms-core-only` flags | Remove `ewmrs` service; re-route shared flags to remaining owners; delete `disable_ewmrs` / `mrms-core-only`-implies-EWMRS-off semantics (see §3 Q3) |
| `src/edgewarn_cli/run.py` | `TOPOLOGIES = {all: (ingest,edgewarn,ewmrs,nexrad), ewmrs: (ingest,edgewarn,ewmrs)}`, `WORKERS=(ingest,core,ewmrs,nexrad)`, `_WORKER_TO_SERVICE`, `_WRAPPER_OWNED_OPTIONS` (`--disable-ewmrs`), `launcher_args.disable_ewmrs/metar/nws/wpc` | Delete `ewmrs` topology/mode; `all` becomes `(ingest,edgewarn,nexrad)`; `ewmrs` mode removed (breaking CLI change, needs deprecation handling per §6) |
| `src/util/cli.py` | `build_service_parser("ewmrs")`, `--render-consumer` help referencing EWMRS, `add_service_enablement_flags` (`--disable-ewmrs`), `add_mrms_core_only_flag` help listing EWMRS | Delete `ewmrs` branch; rewrite help strings |
| `src/util/io.py:249`, `src/run_ingest.py:72` | `disable_ewmrs` overlay / `--render-consumer` default from `run.disable_ewmrs` | Delete overlay; make ingest protection unconditional or drop it |
| `config/runtime.yaml` (`run.disable_ewmrs`, `run.disable_metar/nws/wpc/goes`, `background_intervals.ewmrs_*`, `consumers.ewmrs_notification_seconds`, `goes_coordination.*`) | Topology + cadence knobs | Delete `disable_ewmrs`, `ewmrs_consumer_*`, `ewmrs_notification_seconds`; decide per-§3 whether METAR/NWS/WPC/GOES keys move or die |
| `config/ewmrs_pipeline.yaml` + `config/ewmrs_render.yaml` + schemas | Entire catalogs | Delete files + remove from `common/config/loader.py` catalog list, `src/config/loader.js`, `tests/architecture/test_catalog_baseline.py`, `test_catalog_invariants.py`, config-baseline fixtures |
| `config/api.yaml` + schema | EWMRS route/service gating knobs | Delete EWMRS route entries |
| `pyproject.toml` | `run_ewmrs` py-module, `EWMRS*` packages, `EWMRS` package-data | Delete entries |
| `src/run.py` (retired shim) | Migration message naming `run_ewmrs.py` | Update message |
| `Dockerfile`, `compose.yaml`, `docker/edgewarn-entrypoint.sh` | EWMRS service/profile/commands | Delete service blocks |

### 2.3 API (Node)

- `src/api/services/serviceRegistry.js` — heartbeat discovery over canonical names; EWMRS gating for render/RAP/WPC routes.
- `src/api/routes/v3/index.js` — render-products / RAP / WPC route families.
- `src/api/openapi/v3.yaml` — schemas + paths for the above.
- `src/api/server.js:28` — `--compat=ewmrs` deprecation shim.
- `src/config/mrms-products.js:33` — `ewmrs_render.mrms_layers` lookup.
- Docs: `docs/api/unified_v3.md` (EWMRS binary chunks, `X-EWMRS-Format-Version`), `docs/api/ewmrs_api_endpoints.md` (entire file), `docs/api/api_endpoints.md`, `docs/api/api_implementation.md`, `docs/api/deployment.md` (EWMRS row, consumer backlog, systemd unit).

### 2.4 Tests, fixtures, benchmarks, scripts

Delete or rewrite (no EWMRS import may remain):

- Integration: `tests/integration/test_ewmrs_pipeline.py`, `tests/integration/handoff/test_ewmrs_consumer.py`, `tests/integration/processes/test_ewmrs_service_registration.py`, EWMRS sections of `test_run_all_launcher.py`, `test_package_run.py`, `test_runtime_handoff.py`, `test_runtime_background.py`, `test_runtime_correctness_reproductions.py`, `test_mrms_migration_startup.py`, `test_ingest_service_runtime.py`, `test_tandem_coordinator.py`, `serialization/test_binary_contracts.py`, `serialization/test_atomic_publication.py`.
- Unit: `tests/unit/rendering/test_ewmrs_{rap_uint16,goes,layer_pool,render,transform}.py`, `test_mrms_ewmrs.py`, `tests/unit/enrichment/test_rap_uint16_pipeline.py`, `tests/unit/test_run_ewmrs.py`, EWMRS cases in `test_service_import_isolation.py`, `test_entrypoint_import_safety.py`, `tests/core/test_pipeline.py`, `test_tandem_coordinator.py`, config tests (`test_configure_command.py`, `test_loader.py`, `test_mrms_consumers.py`, `test_mrms_v2.py`).
- Architecture/packaging: `tests/architecture/test_catalog_baseline.py`, `test_catalog_invariants.py`, `test_independent_ingest_config.py`, `test_boundary_audit.py`, `test_known_drift.py`, `test_mrms_ingestion_contract.py`; `tests/packaging/test_installed_command.py`, `test_package_cli.py`; `tests/api/test_service_registry.js`, `test_binary_contracts.js`, `test_unified_app.js` (+ Jest fixtures/snapshots referencing ewmrs heartbeats/routes).
- Baselines/fixtures: `tests/config_baseline/{cli_defaults,cli_shadowing_util_cli,environment_variables_python,ewmrs_chunk_format,ewmrs_goes_render_layers,ewmrs_mrms_render_layers,independent_ingest_settings,mrms_membership_lists}.json`; `tests/fixtures/config/mrms_v2_validation.json` (if EWMRS sectioned); `tests/fixtures/serialization/{README.md,generate_fixtures.py}` + any committed `.bin`/`.json` EWMRS fixtures.
- Benchmarks/scripts: `benchmarks/benchmark_{ewmrs_render,goes_pipeline,rap_uint16}.py`, EWMRS sections of `benchmark_realtime_pipeline_memory.py`; `scripts/{plot_colormaps,plot_goes_*,plot_rap_uint16_fields,render_goes_*_previews,verify_goes_tiles_conus,qualify_mrms_release}.py` EWMRS imports.
- Docs: `docs/core/{README,configuration,ingestion,goes_pipeline,service_registry,configurable-mrms-phase*,production_hardening}.md`, `docs/ctam/internal-api-limits.md`, root `README.md`, `INSTALLATION.md`, `CHANGELOG.md`, `AGENTS.md`, `src/AGENTS.md`, `src/EWMRS/AGENTS.md`, `src/EWMRS/render/AGENTS.md`, `src/common/AGENTS.md`, `src/common/ingest/wpc/AGENTS.md`, `tests/core/AGENTS.md`, `tests/core/ingest/AGENTS.md`, `src/util/AGENTS.md`.

## 3. Decisions required before implementation

| # | Question | Recommendation | If decided otherwise |
| --- | --- | --- | --- |
| Q1 | Do the public v3 render/RAP/WPC routes die with EWMRS, or are they rehomed? | Delete routes + `ewmrs_api_endpoints.md`; return 404 with existing removed-route behavior. They are EWMRS products with no remaining writer. | Keep routes as static-file serving of legacy `gui/` with a new owner (ingest or API-only). Then this plan must add a "static legacy Sobolev" phase instead of deletion, and §5 changes. |
| Q2 | Do GOES ABI ingest/render, METAR, NWS, WPC accessory loops die, or move to ingest/Core? | Delete with EWMRS (simplest, matches "retire"). NWS zone assets (`assets/nws_zones/`, `edgewarn sync-nws-zones`) become dead code and are deleted too. | Rehome: move each loop to `ingest` or Core with new ownership docs, new config sections, new heartbeats-as-children, and new tests. That roughly halves the deletion benefit; split into a follow-up "accessory rehoming" plan. |
| Q3 | What happens to `run.disable_ewmrs`, `--disable-ewmrs`, `EDGEWARN_DISABLE_EWMRS`, `--mrms-core-only`, `--render-consumer`, `--disable-metar/nws/wpc/goes`? | Delete `disable_ewmrs` everywhere (config, schema, CLI, env, `configure` allowlist, baseline JSON). Keep `--disable-goes` only if Q2 rehouses GOES ingest; otherwise delete. Delete `--disable-metar/nws/wpc` with their loops. Simplify `--mrms-core-only` help (no longer "disables EWMRS"). Make ingest `--render-consumer` unconditional-or-deleted (no consumer left to protect). | Retain any flag as a no-op compat shim only with a logged deprecation + scheduled removal; document in `§6`. |
| Q4 | Is `gui/` output + `wpc/surface_analysis/` + `state/realtime/consumers/ewmrs-inputs-v1/` deleted from disk by code, or left for operators? | Code stops writing/reading/cleaning them; no automatic disk purge (destructive). Document operator cleanup command. Remove EWMRS GUI cleanup while keeping NEXRAD/Core cleanup constrained to base dir. | Add an explicit one-shot `edgewarn prune-ewmrs-artifacts` migration; needs its own safety review (base-dir containment tests). |
| Q5 | May historical plans/docs keep the word "EWMRS"? | Yes for `plans/*.md` history and `CHANGELOG.md` release notes (frozen records). Everywhere else — zero mentions. State this exemption in the acceptance criterion so `rg -i ewmrs` has an explicit allowlist. | Zero-tolerance rewrite of history (not recommended; rewrites published docs). |

Decisions (2026-10-13): Q1 delete routes; Q2 delete accessory loops and their unused libraries/assets; Q3 remove retired flags (retain the Core GLM control under a neutral name); Q4 leave existing operator artifacts untouched; Q5 retain plans and changelog history. NEXRAD settings move to nexrad.yaml. The retired topology receives the generic unknown-service migration hint so executable code retains no retired service name.

## 4. Design: end state

```text
edgewarn run        -> ingest + edgewarn + nexrad
edgewarn run core   -> ingest + edgewarn
edgewarn run nexrad -> nexrad
edgewarn run ewmrs  -> REMOVED (error with migration hint)
edgewarn run ingest -> ingest (unchanged)

run_all.py --services ingest,edgewarn,nexrad   (ewmrs rejected as unknown)
CANONICAL_SERVICE_NAMES = ("ingest", "edgewarn", "nexrad")
ROUTE_SERVICE_REQUIREMENTS = { cells/alerts/...: edgewarn, radar-sites: nexrad }
```

- 18-file config catalog shrinks to 16 (`ewmrs_pipeline`, `ewmrs_render` gone). `common/config/loader.py` `CATALOG` + JS loader + `validate_all_configs` + `npm run validate-config` updated together.
- Filesystem contract: `<BASE_DIR>/gui/` EWMRS float16 / RAP Uint16 writers gone; `wpc/surface_analysis/` writer gone; `state/realtime/consumers/ewmrs-inputs-v1/` + `ingest/v1/render-ready/` writers gone. Readers (Node API) drop those paths. `state/realtime/services/ewmrs.json` never written; stale files are ignored/deleted by operator guidance, never auto-trusted.
- Handoff: ingest publishes only Core readiness (`core-start-ready`, `core-integration-ready`, `core-final-ready`, `terminal`); render outbox, per-layer acks, render fingerprints deleted.
- CLI: `edgewarn run ewmrs` and `--args ewmrs` rejected with "EWMRS was retired in vX; use ...". `--args` workers become `(ingest, core, nexrad)`.
- API: removed EWMRS families behave like other removed legacy routes (404 + RFC 9457 problem). `openapi/v3.yaml` version bumped; `unified_v3.md` EWMRS-chunk section deleted.

## 5. Implementation sequence

Read applicable subtree `AGENTS.md` files before each phase. Each phase is a
reviewable commit with the stated completion check. Suggested order minimizes
the window where references dangle.

- [ ] **Phase 0 — Lock decisions + baseline.** Record §3 answers here. Add a temporary EWMRS-reference inventory test (fails if count grows) so the retirement diff only shrinks. Capture `grep -ri ewmrs --include='*.py' --include='*.js' --include='*.yaml' --include='*.json' --include='*.md' src config docs tests | wc -l` baseline in the commit message.
- [ ] **Phase 1 — Config + schemas + loaders.** Delete the two YAML catalogs + schemas; remove from Python `loader.py` catalog table, `src/config/loader.js`, `mrms-products.js`; purge `runtime.yaml` EWMRS keys + `api.yaml` gates; update `edgewarn configure` allowlist, `tests/config_baseline/*.json`, `tests/fixtures/config/*`. Completion: `npm run validate-config` + `PYTHONPATH=src python -m common.config.validate` pass on the 16-file catalog.
- [ ] **Phase 2 — Python runtime deletion.** Delete `src/EWMRS/`, `src/run_ewmrs.py`, `src/util/runtime/ewmrs_{service,consumer}.py`; strip EWMRS branches from `background.py`, `goes.py` (delete file if Q2=delete), `ingest_handoff.py`, `handoff.py`, `services.py`, `primary_service.py`, `cycle.py`, `coordinator.py`, `goes_readiness.py`, `mrms/main.py`, `NEXRAD/*`, `EdgeWARN/pipeline.py`, `edgewarn_cli/migrate_mrms.py`, `util/io.py`, `run_ingest.py`, `run.py` shim text. Completion: `python -c "import run_ewmrs"` fails; no `EWMRS` import remains under `src/`.
- [ ] **Phase 3 — Launchers + CLI + packaging.** Update `run_all.py`, `edgewarn_cli/run.py`, `util/cli.py`, `pyproject.toml`, `Dockerfile`, `compose.yaml`, `docker/edgewarn-entrypoint.sh`. `edgewarn run ewmrs` errors actionably. Completion: `edgewarn run --help` shows no ewmrs; `run_all.py --services ewmrs` rejected; `pip install -e .` + `edgewarn --version` work.
- [ ] **Phase 4 — Node API + OpenAPI.** Remove EWMRS routes/services/compat shim, `X-EWMRS-*` headers, static `gui/` EWMRS readers; bump OpenAPI version; delete `docs/api/ewmrs_api_endpoints.md` (leave a redirect stub pointing at `unified_v3.md` + CHANGELOG if desired). Completion: Jest contract tests assert 404 on old render/RAP/WPC paths (or new static behavior if Q1=keep).
- [ ] **Phase 5 — Tests + fixtures + benchmarks + scripts.** Delete/rewrite every file in §2.4; regenerate `tests/config_baseline/*`, serialization fixtures via `generate_fixtures.py`; remove EWMRS benchmarks/plots or retarget them (delete if they import `EWMRS`). Completion: full `pytest` + `npm test` green with zero `ewmrs` imports.
- [ ] **Phase 6 — Docs + guides.** Rewrite `AGENTS.md`, `src/AGENTS.md`, `README.md`, `INSTALLATION.md`, `docs/core/*`, `docs/api/*`, `docs/ctam/*`; delete `src/EWMRS/AGENTS.md` tree; update CLI help snapshots, deployment systemd units, container examples. Completion: docs build/lint (if any) passes; no stale `run ewmrs` / `run_ewmrs.py` / `ewmrs_pipeline.yaml` instructions remain outside frozen history.
- [ ] **Phase 7 — Final sweep + release note.** Run the acceptance sweep in §7, delete the Phase-0 inventory test (or convert it to a permanent zero-tolerance guard), add `CHANGELOG.md` entry with breaking-change + operator cleanup instructions, and remove `build/` stale artifacts by rebuilding (never commit `build/` edits).

## 6. Compatibility, migration, and rollback

- Breaking change, major-version-worthy: `edgewarn run ewmrs`, `python src/run_ewmrs.py`, `--args ewmrs`, `--disable-ewmrs`, `EDGEWARN_DISABLE_EWMRS`, `--disable-metar/nws/wpc` (if Q2=delete), EWMRS API families, and the two deleted YAML files all disappear. Keep a one-release actionable error for `edgewarn run ewmrs` ("retired in <version>; EWMRS rendering/accessories removed, see <docs>") rather than silently mapping it to another topology.
- Deployment: stop all services; deploy code + 16-file config tree together (old code cannot validate the new tree and vice versa — ship atomically). Drain/delete `state/realtime/consumers/ewmrs-inputs-v1/` and `services/ewmrs.json`; do not translate render acks into Core success. Validate with both config validators before starting workers.
- Rollback: stop new workers first, restore prior code + 18-file config tree from version control, resume from Core/NEXRAD cursors. New-only inputs are rediscovered by ingest; never synthesize EWMRS outputs.
- Operator artifact cleanup (opt-in, documented): `rm -rf <BASE_DIR>/gui/<ewmrs-layers> <BASE_DIR>/wpc/surface_analysis state/realtime/consumers/ewmrs-inputs-v1 state/realtime/services/ewmrs.json` — exact layer list in release note; no code auto-deletes outside its owned cleanup.

## 7. Verification and acceptance

Use temporary base directories; never point cleanup/migration tests at a real
operational tree. After activating the `EdgeWARN` conda environment:

```bash
# Zero-mention sweep (allowlist: plans/* history + CHANGELOG.md past releases, per §3 Q5)
grep -ri "ewmrs" src config docs tests scripts benchmarks package.json pyproject.toml compose.yaml Dockerfile | grep -v "plans/" || echo "CLEAN"
PYTHONPATH=src python -m common.config.validate
npm run validate-config
python -m pytest tests/unit tests/architecture tests/packaging -q
python -m pytest tests/integration -q
npm test
npm run test:coverage
```

| Area | Required checks |
| --- | --- |
| Mention sweep | Case-insensitive `ewmrs` search clean except explicit allowlist; `git status` shows deleted `src/EWMRS/`, `src/run_ewmrs.py`, `config/ewmrs_*.yaml` + schemas |
| Config parity | Both validators pass; catalog baseline/invariant tests updated to 16 files; `edgewarn configure ewmrs_pipeline.*` rejected |
| CLI/topology | `edgewarn run --help`, `run_all.py --help` show no ewmrs; `ewmrs` mode/worker rejected; `core` = `(ingest,edgewarn)`; `all` = `(ingest,edgewarn,nexrad)`; signal/shutdown tests leave no orphans |
| Runtime | Core + ingest + NEXRAD start without EWMRS imports (import-isolation tests); no `services/ewmrs.json` written; stale file ignored; ingest publishes no render outbox |
| API | OpenAPI validates; removed EWMRS routes 404 (or specified legacy-static behavior); heartbeat/registry tests cover 3 services; no `X-EWMRS-*` headers |
| Binary contract | No writer for float16 chunks / RAP Uint16 / NEXRAD-EWMRS coupling; remaining NEXRAD/Core serialization fixtures pass |
| Docs | No live doc references `run_ewmrs.py`, `ewmrs_pipeline.yaml`, `ewmrs_render.yaml`, `run ewmrs` except frozen history + retirement note |

Done when the sweep is clean, both config validators pass, the full Python and
Node suites are green, and the release note documents the breaking CLI/API/
config removals plus operator artifact cleanup.
