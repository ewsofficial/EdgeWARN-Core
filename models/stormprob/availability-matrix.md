# StormProb input availability matrix (Phase 0)

**Status:** contract-freeze only; no runtime changes.
**Model contract:** `models/stormprob/manifest.json` (StormProb commit
`e428f53ec33adac9197a3cda865f599e5f9c5aff`, working tree dirty — see manifest).
**Feature order:** `current_feature_names` in the motion checkpoint training config,
135 entries: 51 scalars + 6 `morphology.*` + 74 `wind_field.*` + 4 derived.
**Reference sample:** `/home/yuchenwei/Projects/data/StormProb/test/20220413/data/cells/100083.json`
(centroid `[lat, lon]`, longitudes in 0–360 domain, timestamps naive ISO, e.g.
`2022-04-13T20:52:42`).
**EdgeWARN sources checked:** `config/integration.yaml` (`rap_products`, `derived`,
`probsevere_field_map`, `stats_datasets`), `src/EdgeWARN/process/detect/tools/morphology.py`,
`src/EdgeWARN/process/detect/tools/save.py`, `src/EdgeWARN/process/integrate/integrate_rap.py`.

Legend: COVERED = an EdgeWARN integration path populates the key today.
COVERED* = populated but with a documented risk. GAP = no path found.

## 1. Scalar property features (51)

| # | StormProb feature | EdgeWARN source | Status | Notes |
|---|---|---|---|---|
| 1 | MUCAPE | ProbSevere map `MUCAPE` | COVERED | Upstream ProbSevere feed dependent |
| 2 | MLCAPE | ProbSevere map `MLCAPE` | COVERED | |
| 3 | MLCIN | ProbSevere map `MLCIN` | COVERED | |
| 4 | CAPE_M10M30 | ProbSevere map `CAPE_M10M30` | COVERED | |
| 5 | DCAPE | ProbSevere map `DCAPE` | COVERED | |
| 6 | PWAT | ProbSevere map `PWAT` | COVERED | |
| 7 | temp_2m | RAP `t2m`, kelvin_to_celsius | COVERED* | Training sample ~25.7 → Celsius, matches transform. Training units not recorded; pin in Phase 1 |
| 8 | dewpoint_2m | RAP `d2m`, kelvin_to_celsius | COVERED* | Same units caveat |
| 9 | dewpoint_depression | RAP derived `temp_2m - dewpoint_2m` | COVERED | |
| 10 | freezing_level_height | RAP derived `freezing_level_m / 1000` (km) | COVERED | |
| 11 | freezing_level_m | RAP `gh` isothermZero (m) | COVERED | |
| 12 | Wetbulb_0C_Hgt | ProbSevere map `Wetbulb_0C_Hgt <- WETBULB_0C_HGT` | COVERED | Casing-sensitive mapping |
| 13 | EBShear | ProbSevere map `EBShear <- EBSHEAR` | COVERED | |
| 14 | MeanWind_1-3kmAGL | ProbSevere map `MeanWind_1-3kmAGL` | COVERED | |
| 15 | SRH01km | ProbSevere map `SRH01km <- SRH01KM` | COVERED | |
| 16 | SRH02km | ProbSevere map `SRH02km <- SRW02KM` | COVERED* | **Suspected mapping typo** (`SRW02KM` vs `SRH02KM`); verify against live ProbSevere feed before Phase 1 extractor |
| 17 | SRW46km | ProbSevere map `SRW46km <- SRW46KM` | COVERED | |
| 18 | u10m | RAP `u10` heightAboveGround/10 | COVERED | |
| 19 | v10m | RAP `v10` heightAboveGround/10 | COVERED | |
| 20 | Ref0 | MRMS stats `Ref0` (max) | COVERED | |
| 21 | Ref5 | MRMS stats `Ref5` (max) | COVERED | |
| 22 | Ref10 | ProbSevere map `Ref10 <- REF10` | COVERED* | MRMS stats catalog has Ref0/Ref5/Ref15 only; Ref10/Ref20 rely on ProbSevere upstream |
| 23 | Ref15 | MRMS stats `Ref15` (max) | COVERED | |
| 24 | Ref20 | ProbSevere map `Ref20 <- REF20` | COVERED* | Same upstream dependency as Ref10 |
| 25 | maxRALA | MRMS stats `maxRALA` (max) | COVERED | |
| 26 | maxPrecipRate | MRMS stats `maxPrecipRate` (max) | COVERED | |
| 27 | MESH | ProbSevere map `MESH` | COVERED | |
| 28 | VIL | ProbSevere map `VIL` | COVERED | |
| 29 | maxVIL | MRMS stats `maxVIL` (max) | COVERED | |
| 30 | p50VIL | MRMS stats `p50VIL` (p50) | COVERED | |
| 31 | p90VIL | MRMS stats `p90VIL` (p90) | COVERED | |
| 32 | p95VIL | MRMS stats `p95VIL` (p95) | COVERED | |
| 33 | maxVILDensity | MRMS stats `maxVILDensity` (max) | COVERED | |
| 34 | p50VILDensity | MRMS stats `p50VILDensity` (p50) | COVERED | |
| 35 | p90VILDensity | MRMS stats `p90VILDensity` (p90) | COVERED | |
| 36 | p95VILDensity | MRMS stats `p95VILDensity` (p95) | COVERED | |
| 37 | maxVII | MRMS stats `maxVII` (max) | COVERED | |
| 38 | EchoTop50 | ProbSevere map `EchoTop50 <- EchoTop_50` | COVERED | |
| 39 | maxEchoTop18 | MRMS stats `maxEchoTop18` (max) | COVERED | |
| 40 | p90EchoTop18 | MRMS stats `p90EchoTop18` (p90) | COVERED | |
| 41 | p95EchoTop18 | MRMS stats `p95EchoTop18` (p95) | COVERED | |
| 42 | maxEchoTop30 | MRMS stats `maxEchoTop30` (max) | COVERED | |
| 43 | p90EchoTop30 | MRMS stats `p90EchoTop30` (p90) | COVERED | |
| 44 | p90EchoTop50 | MRMS stats `p90EchoTop50` (p90) | COVERED | |
| 45 | MaxLLAz | ProbSevere map `MaxLLAz <- MAXLLAZ` | COVERED | |
| 46 | maxAzShearLow | MRMS stats `maxAzShearLow` (max) | COVERED | |
| 47 | p95AzShearLow | MRMS stats `p95AzShearLow` (p95) | COVERED | |
| 48 | p98LLAz | ProbSevere map `p98LLAz <- P98LLAZ` | COVERED | |
| 49 | maxAzShearMid | MRMS stats `maxAzShearMid` (max) | COVERED | |
| 50 | p95AzShearMid | MRMS stats `p95AzShearMid` (p95) | COVERED | |
| 51 | p98MLAz | ProbSevere map `p98MLAz <- P98MLAZ` | COVERED | |

No scalar GAPs found in static config. Operational caveat: any ProbSevere/RAP/MRMS
source outage today writes `None`/skips the key, while StormProb requires finite
floats or the `-999` sentinel with explicit coverage gates. **Never fabricate zero
winds.** Phase 1 must add the sentinel + gate policy at integration time.

## 2. Morphology features (6)

| StormProb feature | EdgeWARN key | Status | Notes |
|---|---|---|---|
| morphology.aspect_ratio | `morphology.aspect_ratio` (minAreaRect) | COVERED* | Exact key match |
| morphology.branching_factor | `morphology.branching_factor` (skeleton junctions) | COVERED* | Exact key match |
| morphology.defect_bearing | `morphology.defect_bearing` (deg) | COVERED* | Exact key match |
| morphology.defect_max_depth | `morphology.defect_max_depth` | COVERED* | Exact key match |
| morphology.linearity | `morphology.linearity` | COVERED* | Exact key match |
| morphology.solidity | `morphology.solidity` | COVERED* | Exact key match |

Risks: (a) tiny-cell defaults (`linearity 0.0`, `solidity 1.0`, `aspect_ratio 1.0`)
are EdgeWARN conventions with unknown training-data equivalents; (b) detection
rounding is 3 decimals for polygon/centroid while integration output is 2 decimals —
radial profiles are resolution-sensitive, so Phase 1 retains full-precision geometry
pre-rounding per the plan; (c) training-data morphology provenance (algorithm that
produced the cached values) is undocumented — parity fixtures in
`models/stormprob/parity-fixtures/` pin the current behavior.

## 3. Wind profile (74 = 37×u + 37×v)

Levels (hPa): 100, 125, 150, 175, 200, 225, 250, 275, 300, 325, 350, 375, 400, 425,
450, 475, 500, 525, 550, 575, 600, 625, 650, 675, 700, 725, 750, 775, 800, 825, 850,
875, 900, 925, 950, 975, 1000 — **exact match** with `PRESSURE_LEVELS_HPA` and with
`config/integration.yaml: rap_products.isobaric_levels_mb` (37 levels).

| StormProb keys | EdgeWARN source | Status | Notes |
|---|---|---|---|
| `wind_field.u100 … u1000` | RAP `key_template wind_field.u{level}` × 37 levels | COVERED* | Ordering differs (EdgeWARN lists 1000→100); a named extractor removes order dependence |
| `wind_field.v100 … v1000` | RAP `key_template wind_field.v{level}` × 37 levels | COVERED* | Same |

Risks: RAP GRIB level availability/coverage per cycle is unverified (no live data in
`~/EdgeWARN_input` at freeze time); every missing level must use `-999` + coverage
gate, never `0.0`. `initial_u/v` derivation (`predict_motion_vector`, mean 0–6 km
wind via standard atmosphere) raises when no usable paired level exists — that must
become a machine-readable skip reason, not a silent fallback.

## 4. Derived features (4)

| StormProb feature | Derivation | Status | Notes |
|---|---|---|---|
| initial_u | mean 0–6 km wind, `stormprob/model/initial_pred.py` | COVERED* | Reimplement versioned in Phase 1; needs paired u/v levels |
| initial_v | same | COVERED* | Same |
| storm_age_seconds | current − first-track timestamp | COVERED | From committed track rows |
| valid_history_length | min(index+1, 30) | COVERED | From committed track rows |

## 5. Geometry / identity semantics

| Concern | Training data | EdgeWARN today | Assessment |
|---|---|---|---|
| centroid | `[lat, lon]` float | `[lat, lon]` float | Match |
| polygon points | `bbox`: `[[lat, lon], …]` | detection polygon `[[lat, lon], …]` | Match (key name differs: `bbox` vs detection polygon) |
| longitude domain | 0–360 (e.g. 273.809) | `save.py` normalizes to `% 360` | Match at detection; API domain must be re-verified in Phase 4 |
| timestamps | naive ISO (`2022-04-13T20:52:42`) | stormcells `latest_timestamp` same style | Match; TZ-naive convention must be pinned, not silently changed |
| lineage | `parent_ids`, `split_from`, `event_type`, `tracking_mode` keys present | `config/lineage.yaml` + tracker exist | Keys exist both sides, but surveyed training data (4500+ files across train/val/test) has **no non-empty `split_from`/`parent_ids`** and only `ACTIVE`/`active` markers — split/merge training convention is unobserved; Phase 1 must define lineage mapping explicitly |
| motion state | `dx/dy/dt` on stormcells features | detection vector math exists | Re-derive from DB rows in Phase 1; do not mix conventions |
| precision | bbox 2dp / centroid 3dp in cached files | integration 2dp, detection polygon/centroid 3dp | Full-precision retention is Phase 1 work (plan §Phase 1) |
| longitude wrap | never wraps in US domain (verified 0 wrap cases) | `% 360` normalization exists | Wrap handling covered by synthetic parity fixture `longitude_wrap` |

## 6. Environment / reproducibility blockers found in Phase 0

1. **License/provenance (BLOCKED):** no `LICENSE*` file in the StormProb checkout;
   weights + calibrator are referenced by path + SHA-256 in `manifest.json` but **not
   copied** into this repo. Confirm before Phase 3 packaging.
2. **Conda env name:** this repo requests `EdgeWARN-dev`, but `conda env list` shows
   only `EdgeWARN` (plus `base`, `onnxtest`). All Phase 0 Python runs used `EdgeWARN`.
3. **Dataset caches absent:** `artifacts/caches/radial_corrected/` (referenced by both
   checkpoints' training configs) does not exist in the StormProb checkout, so
   cache-based eval/dataset rebuilds are unavailable; parity fixtures were built
   directly from `data/cells` tracks + checkpoints instead.
4. **StormProb tree dirty:** `scripts/radial_morphology/train.py`,
   `stormprob/model/radial_morphology/__init__.py`, `model.py` modified at freeze
   commit (recorded in manifest). Re-verify hash before Phase 3 export.
5. **Per-feature units** are not recorded in StormProb; assumed EdgeWARN conventions
   must be pinned per feature in Phase 1 (temperature Celsius confirmed via sample
   values ≈ 20–26).
6. **No live EdgeWARN output** in `~/EdgeWARN_input` (only `stations_cache.json`) at
   freeze time, so this matrix is static-config + training-sample based; re-validate
   against live integrated cycles in Phase 5.
