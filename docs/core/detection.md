# Detection Pipeline

Storm-cell detection is implemented under `src/EdgeWARN/process/detect`.

## Module Layout

```text
src/EdgeWARN/process/detect/
├── main.py                  # Orchestration entry point
├── config.py                # Detection configuration
├── detect.py                # Core cell extraction logic
├── track.py                 # Tracking + lineage updates
├── kalman/                  # Kalman tracking components
├── lineage/                 # Merge/split lineage logic
└── tools/                   # Gate mapping, loading, saving, vectors, alerts, morphology
```

## Main Entry Point

`src/EdgeWARN/process/detect/main.py`:

```python
main(
    radar_old,
    radar_new,
    ps_old,
    ps_new,
    pt_old,
    pt_new,
    lat_bounds,
    lon_bounds,
    detection_config=None,    # DetectionConfig; loaded through the configured catalog root
    radar_old_obj=None,       # cached prior-radar dataset, optional
    ps_old_obj=None,          # cached prior-ProbSevere dataset, optional
    pt_old_obj=None,          # cached prior-PrecipType dataset, optional
    disable_tracking=False,
    disable_polygon_expansion=False,
    cleanup_stormcells=True,
)
```

The detector writes its persisted runtime artifact to `<BASE_DIR>/data/stormcells/stormcells_{timestamp}.json`; callers cannot redirect it. It returns a `(path, cached_datasets)` tuple, or `(None, None)` when no radar frame is available.

`refl_threshold`, `min_seed_percentage` and `drop_offset` are carried on `detection_config` rather than passed individually.

`disable_polygon_expansion` skips ProbSevere polygon-to-radar gate mapping and
watershed-style expansion. The ProbSevere geometry is still normalized,
rasterized, and used to build the detection mask.

## Detection Modes

- **Dual-frame mode**: runs detection on new scan, uses prior scan/context for tracking
- **Single-frame fallback**: runs without tracking when any new radar,
  ProbSevere, or PrecipType input is unavailable; available old/current inputs
  are reused

## Core Processing Steps

1. Null missing input paths after existence checks
2. Resolve the scan timestamp from radar input, with UTC/raw-string fallbacks
3. Load prior stormcell state from the StormProb database first, then an older
   JSON snapshot, then an old-scan re-detection fallback
4. Detect cells from radar/ProbSevere/PrecipType inputs
5. If tracking enabled:
   - run lineage event detection (merge/split)
   - run cell tracking updates and Kalman continuity
6. Compute vectors via `StormVectorCalculator`
7. Match configured/allowlisted NWS alert types to cells using configured buffers
8. Save `stormcells_YYYYMMDD-HHMMSS.json`
9. Return the integration input; integration publishes the public indexes after
   its database/publication commit

## Tracking and Lineage

Tracking is handled by `StormCellTracker` in `track.py` with:

- merge/split lineage detection
- continuity support for temporary detection drops
- Kalman-assisted state evolution using `src/EdgeWARN/process/detect/kalman`

Tracking can be disabled via pipeline flags (`--disable-tracking`).

## Output Compatibility

Detection outputs feed integration, CTAM, alerting, and API index updates, so the saved stormcell schema and timestamp naming are treated as compatibility-sensitive interfaces.
