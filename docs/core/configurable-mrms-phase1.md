# Configurable MRMS ingestion: Phase 1 contract and impact

Phase 1 characterizes the shipped v1 runtime. It does not enable v2, change
acquisition or readiness, move directories, or enforce new CTAM/StormProb gates.
The implementation sequence is in the
[implementation plan](../../plans/configurable-mrms-ingestion-plan.md#12-implementation-phases).

## Baseline and intended contract

The checked-in [contract fixture](../../tests/fixtures/config/mrms_ingestion_contract.json)
separates `v1_baseline` from `v2_target`. Its values are reviewed snapshots, not
an additional runtime catalog. Update baseline assertions deliberately in the
phase that changes behavior; do not regenerate them to hide a regression.

- **Current:** 21 ingested identities, ten discovery inputs, three detection
  modifiers, 18 non-detection products participating in the integration barrier,
  25 statistics from 14 distinct MRMS products, and 16 MRMS render/API products.
- **Target:** precisely composite reflectivity, PrecipFlag, and ProbSevere are
  reserved and used for discovery. The other 18 products remain shipped defaults
  but become configurable additions. The fixture explicitly lists all 18.
  Empty additions will still include the reserved set. Enabled StormProb and
  external modules will impose their own deployment requirements without
  expanding that set.
- **Discovery delta:** EchoTop 18/30/50, low/mid AzShear, VIL density, and VII
  are discovery inputs today and must leave that set in the coordinated change.
- **Timing:** current coordinator callbacks wait for detection, integration,
  RAP, and requested GOES acquisition. Callback order is detection, EWMRS MRMS,
  base integration, EWMRS GOES, final Core integration. All receive the same
  completed manifest. Phase 5 must deliberately replace this wait with separate
  snapshots and bounded optional completion.
- **EWMRS availability:** `mrms-ready` is a best-effort per-layer scan trigger,
  even when detection or integration is incomplete. It is not an all-products
  gate. Preserve this behavior while changing callback timing. RAP remains an
  exact-pinned-input handoff.

## Per-product impact

Every row uses the same current raw retention: `ingest.mrms.remove_old_files=true`,
60 minutes from `ingest.mrms.cleanup_max_age_minutes`, and the default ten-file
cap from `filesystem.cleanup_defaults.max_files`. Cleanup currently orders by
mtime; Phase 5 owns encoded-time history selection and cross-process pin safety.

All rows produce `family=mrms` staged records whose `product` is the first
column. Standard rows use CONUS gzip/GRIB2 acquisition; **ProbSevere is the
special JSON adapter**, with region `ProbSevere` and null source modifier but
manifest identity `ProbSevere`. This distinction must survive selectors and
migration. In Phase 2 these adapters become explicit definitions.

Raw basenames below live under `<base-dir>/data`. Render/API IDs also name
existing `<base-dir>/gui` directories and remain unchanged; `—` means no
configured MRMS render/API product. Raw ingestion must not invent a public
product. “Required” and “optional” classify the intended base Core contract;
the current integration barrier still includes every non-detection product.

| Manifest identity | Classification / current phase | Compatibility alias | Raw basename: v1 → v2 target | Direct consumer | Preserved render/API ID |
| --- | --- | --- | --- | --- | --- |
| `EchoTop_18_00.50` | optional / integration barrier today | `MRMS_ECHOTOP18_DIR` | `MRMS_EchoTop18` → `MRMS_EchoTop_18` | maxEchoTop18, p95EchoTop18, p90EchoTop18 | `MRMS_EchoTop18` |
| `EchoTop_30_00.50` | optional / integration barrier today | `MRMS_ECHOTOP30_DIR` | `MRMS_EchoTop30` → `MRMS_EchoTop_30` | maxEchoTop30, p90EchoTop30 | `MRMS_EchoTop30` |
| `EchoTop_50_00.50` | optional / integration barrier today | `MRMS_ECHOTOP50_DIR` | `MRMS_EchoTop50` → `MRMS_EchoTop_50` | p90EchoTop50 | — |
| `RadarQualityIndex_00.00` | optional / integration barrier today | `MRMS_RQI_DIR` | `MRMS_RadarQualityIndex` → `MRMS_RadarQualityIndex` | Acquired; no configured statistic or render layer | — |
| `MESH_00.50` | optional / integration barrier today | `MRMS_MESH_DIR` | `MRMS_MESH` → `MRMS_MESH` | Render only | `MRMS_MESH` |
| `NLDN_CG_001min_AvgDensity_00.00` | optional / integration barrier today | `MRMS_NLDN_DIR` | `MRMS_NLDN_CG_001min_AvgDensity` → `MRMS_NLDN_CG_001min_AvgDensity` | maxCGFlashDensity | `MRMS_NLDN_CG_001min_AvgDensity` |
| `PrecipRate_00.00` | optional / integration barrier today | `MRMS_PRECIPRATE_DIR` | `MRMS_PrecipRate` → `MRMS_PrecipRate` | maxPrecipRate | `MRMS_PrecipRate` |
| `RadarOnly_QPE_01H_00.00` | optional / integration barrier today | `MRMS_QPE_DIR` | `MRMS_QPE` → `MRMS_RadarOnly_QPE_01H` | Render only | `MRMS_QPE` |
| `MergedAzShear_0-2kmAGL_00.50` | optional / integration barrier today | `MRMS_AZSHEARLOW_DIR` | `MRMS_MergedAzShear_0-2kmAGL` → `MRMS_MergedAzShear_0-2kmAGL` | maxAzShearLow, p95AzShearLow | `MRMS_MergedAzShear_0-2kmAGL` |
| `MergedAzShear_3-6kmAGL_00.50` | optional / integration barrier today | `MRMS_AZSHEARMID_DIR` | `MRMS_MergedAzShear_3-6kmAGL` → `MRMS_MergedAzShear_3-6kmAGL` | maxAzShearMid, p95AzShearMid | `MRMS_MergedAzShear_3-6kmAGL` |
| `VIL_Density_00.50` | optional / integration barrier today | `MRMS_DVIL_DIR` | `MRMS_VILDensity` → `MRMS_VIL_Density` | maxVILDensity, p95VILDensity, p90VILDensity, p50VILDensity | `MRMS_VILDensity` |
| `ProbSevere` | required / detection | `MRMS_PROBSEVERE_DIR` | `MRMS_ProbSevere` → `MRMS_ProbSevere` | Detection polygons; ProbSevere fields, including StormProb MESH | — |
| `MergedRhoHV_00.50` | optional / integration barrier today | `MRMS_RHOHV_DIR` | `MRMS_MergedRhoHV` → `MRMS_MergedRhoHV` | Acquired; no configured statistic or render layer | — |
| `PrecipFlag_00.00` | required / detection | `MRMS_PRECIPTYP_DIR` | `MRMS_PrecipFlag` → `MRMS_PrecipFlag` | Detection precipitation classification | — |
| `MergedReflectivityAtLowestAltitude_00.50` | optional / integration barrier today | `MRMS_RALA_DIR` | `MRMS_ReflectivityAtLowestAltitude` → `MRMS_MergedReflectivityAtLowestAltitude` | maxRALA | `MRMS_ReflectivityAtLowestAltitude` |
| `MergedReflectivityQCComposite_00.50` | required / detection | `MRMS_COMPOSITE_DIR` | `MRMS_MergedReflectivityQC` → `MRMS_MergedReflectivityQCComposite` | Detection; current/previous comparison | `MRMS_MergedReflectivityQC` |
| `VII_00.50` | optional / integration barrier today | `MRMS_VII_DIR` | `MRMS_VII` → `MRMS_VII` | maxVII | `MRMS_VII` |
| `VIL_00.50` | optional / integration barrier today | `MRMS_VIL_DIR` | `MRMS_VIL` → `MRMS_VIL` | maxVIL, p95VIL, p90VIL, p50VIL | `MRMS_VIL` |
| `Reflectivity_0C_00.50` | optional / integration barrier today | `MRMS_REF_0C_DIR` | `MRMS_ReflectivityAt0C` → `MRMS_Reflectivity_0C` | Ref0 | `MRMS_ReflectivityAt0C` |
| `Reflectivity_-5C_00.50` | optional / integration barrier today | `MRMS_REFM5C_DIR` | `MRMS_ReflectivityAtM5C` → `MRMS_Reflectivity_-5C` | Ref5 | `MRMS_ReflectivityAtM5C` |
| `Reflectivity_-15C_00.50` | optional / integration barrier today | `MRMS_REFM15C_DIR` | `MRMS_ReflectivityAtM15C` → `MRMS_Reflectivity_-15C` | Ref15 | `MRMS_ReflectivityAtM15C` |

The ten changed basenames are pinned explicitly in the tests. The remaining
11 do not move. RQI and RhoHV are acquisition-only in the current configured
consumer lists; their lack of consumers does not make their ingestion entries
obsolete. NWS aliases and unused FLASH path definitions are outside this MRMS
migration. Never rename them by prefix matching.

## Source, manifest, and publication contracts

`config/ingest.yaml` currently owns the bucket, key/filename templates, HTTPS
endpoints, directory map, split token, and network/retention settings. The
fixture snapshots them in full. Tests exercise all 21 S3/HTTPS identities,
including ProbSevere's previous-hour marker across midnight and minute markers.
Phase 2 introduces code-owned source definitions; Phase 4 activates them.
Operational limits remain configurable.

`common.ingest.mrms.downloader` gives ProbSevere its non-null manifest label.
`common.ingest.manifest` carries source, analysis time, path, family, and role.
The coordinator adds previous detection records by encoded time today, but
validates and publishes one combined manifest after acquisition. Phase 5 owns
phase snapshots, ingest outcome reports, pinning, retry and replay semantics.
Strict durable handoff records must not gain unversioned fields.

The 16 render identities, output aliases, colormaps, required flags, API storage
names, legacy file prefixes, and float16 chunk format are pinned together.
`EWMRS.pipeline` scans layers independently; `util.runtime.ewmrs_consumer`
advances its checkpoint after a best-effort scan and retries handler exceptions.
`src/api/config/product-catalog.json` indexes rendered products, not arbitrary
raw directories. API v3 and legacy-route 404 behavior remain release constraints.

## Cross-cutting ownership and implementation phases

| Producer/consumer | Dependency classification and current behavior | Owning implementation phases |
| --- | --- | --- |
| Python/Node configuration loaders and editor | Required deployment contract; v1 manually assigns aliases and memberships. Pure v2 validation and migration diagnostics must agree across runtimes. | 2 definitions; 3 validation/planning; 8 catalog switch |
| Scheduler (`EdgeWARN.schedule`) | Required discovery gate currently checks ten products through the compatibility ingest imports. | 2 protected discovery definitions; 4 acquisition conversion |
| Sync/async S3, HTTPS, all-files acquisition | Required protected and optional additions share acquisition/fallback paths. Download results must retain full elevation-bearing identity. | 2 sources/registry; 4 acquisition and bounds |
| `util.file` and startup/child processes | Required path binding; aliases are currently globals. Startup must bind one frozen registry, create enabled directories only after preflight, and verify child parity. | 4 paths; 6 preflight; 8 migration |
| `EdgeWARN.pipeline` / detection entry points | Required composite, PrecipFlag, ProbSevere current inputs; previous inputs support scan comparison. Legacy calls can select latest files. | 5 snapshots/history; 7 identity/eligibility readers |
| Tracking and lineage (`process.detect`) | Derived cell geometry, motion, previous scan and lineage state; no separate reservation of enrichment products. Preserve fixed-sequence behavior. | 5 replay/pins; 7 consumer regression; 8 qualification |
| Statistics and AzShear enrichment | Optional for base Core; 14 products yield 25 configured statistics, with additional low/mid AzShear processing. Current selection uses directory equality. | 3 product-field conversion; 5 final enrichment; 7 eligibility |
| StormProb features/records/assets and built-in CTAM adapter | Derived dependency contract: 13 statistic products (all except NLDN), ProbSevere fields, RAP winds/environment, morphology, geometry, valid history/masks, model and normalization assets. Current missing-field sentinels do not satisfy the proposed all-input gate. Raw MESH is **not** the source of feature MESH. | 3 disable-option preparation; 5 final inputs; 6 fatal gates and disable propagation; 8 qualification |
| External CTAM manifest/discovery/runner | Required or optional according to each declared input. Current selector admission depends on enabled catalog membership and discovery can discard invalid candidates. No module is automatically reserved. | 6 explicit declarations, frozen discovery, preflight, runtime availability |
| CTAM internal API/SDK | Required declared-access boundary; metadata/content permissions must enforce selectors and isolate external execution failures. | 6 token permissions and typed violations |
| CTAM/StormProb disabled paths | Disabled consumers must skip their own dependency/asset gates without removing the three Core inputs. Dedicated StormProb disable is a future option, not available in this phase. | 3 option schema; 6 CLI/startup/execution |
| RAP and GLM | Separate ingestion owners; RAP/scan-time GLM readiness must be preserved. GLM and NLDN enrichment support does not imply StormProb model dependencies. | 5 existing gates; 6 feature dependency audit |
| EWMRS render configuration, pipeline and consumer | Optional per-layer raw availability; independent scanning and source timestamps. Requires producer/local registry agreement in target behavior. | 3 product-field conversion; 7 descriptor/liveness/eligibility |
| Alerts, StormProb forecasts and shared history/database | Derived outputs; failed enabled StormProb dependencies must stop new forecasts/alerts, preserve old committed records and shared history. Trained padding masks stay valid. | 5 final snapshots; 6 fatal propagation; 8 retention/replay |
| API indexes/snapshots and service health | Derived publication plus required heartbeats/service gates. Raw additions create no implicit API product; producer descriptor is separate from handoff wire records. | 5 atomic reports; 7 agreement/publication; 8 API regression |
| Historical processing | Required reuse of Core ingest and manifests; same dependencies/deadlines but isolated realtime state. | 4 acquisition; 5 replay; 6 startup/fatal policy; 7 readers |
| `EdgeWARN.ingest.mrms.*` | Compatibility-only import aliases for shared `common.ingest.mrms.*`; tests pin all nine forwarding modules. | 4 compatibility exports; 7 caller audit |
| Diagnostics/scripts and direct/legacy callers | Compatibility path access; audit any latest-file fallback for enabled identity before filesystem probing. | 7 reader audit |
| Migration/cleanup | Required operational boundary: ten raw renames, stopped producers/consumers, drained backlog, no active old-path references, preserved committed records. | 3 offline dry-run planner; 5 pin policy; 8 apply/resume/rollback |
| NWS and unused FLASH `MRMS_*` aliases | Out-of-scope/unused for this catalog, not candidates for inferred ingestion or raw renaming. | 8 exclusion regression |

The StormProb dependency row is a source inventory, not an implemented gate.
Phase 6 must validate feature-by-feature freshness, role, enrichment, assets,
and fatal propagation. Module top-level optionality must not excuse a missing
required ingestion declaration.

## Compatibility and release boundary

Legacy Python `MRMS_*_DIR` aliases for these 21 products will remain supported
through the rest of the **3.x release series** after v2 ships. Removal is no
earlier than **4.0**, with an explicit deprecation/migration notice. When v2 is
activated, enabled aliases resolve to canonical paths; disabled optional aliases
must not expose stale directories. The support promise preserves names, not
old raw directory locations. No alias behavior changes in Phase 1.

Exact generated names with hyphens, such as
`MRMS_MergedAzShear_3-6kmAGL`, will use `fs.MRMS_PATHS[name]` or
`getattr(fs, name)`; they cannot be written as a Python dotted identifier.
These interfaces belong to Phase 4 and are not yet implemented.

Ingest v2 adds the three code-owned products to operator additions; only the
ingest document changes its schema version. Ten raw directories change names,
while GUI directories, output names, binary payloads and public API identities
remain stable. Preserve full upstream elevations in acquisition and manifests.
Reject duplicate IDs and case/elevation path collisions rather than merging
sources. Configuration changes require restart.

Do not ship the default-catalog switch until Phases 2–8, including migration
apply/resume/rollback, consumer eligibility, preflight and bounded task shutdown,
pass their exit gates. Phase 1 does not make migration commands available.

## Phase 1 verification

Run in the `EdgeWARN` environment:

```bash
python -m pytest tests/core/ingest/mrms tests/architecture \
  tests/core/test_tandem_coordinator.py tests/integration/test_tandem_coordinator.py \
  tests/integration/handoff
```

The added controlled-event tandem tests distinguish current acquisition waits
from EWMRS best-effort availability without live NOAA requests or latency
thresholds. Fixtures and writes use temporary runtime roots. Existing handoff
coverage remains the authority for retry/checkpoint and exact RAP behavior.

Verification on 2026-09-26: **357 passed** in the `EdgeWARN` environment.
The complete run required execution outside the restricted sandbox: socket
restrictions blocked multiprocessing managers and stalled existing async
shutdown tests inside it. No runtime changes were needed for the passing run.
