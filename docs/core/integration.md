# Integration Pipeline

Post-detection enrichment is implemented under `src/EdgeWARN/process/integrate`.

## Module Layout

```text
src/EdgeWARN/process/integrate/
├── __init__.py
├── main.py
├── pipeline.py
├── config.py
├── integrate.py
├── integrate_glm.py
├── integrate_rap.py
├── integrate_azshear.py
├── azshear/
├── core/
├── geometry/
├── io/
├── history.py
├── grid_index.py
└── utils.py
```

## Entry Point

`src/EdgeWARN/process/integrate/main.py` exports `pipeline.main`.

Primary call pattern:

```python
main(
    json_path=None,
    remove_old_cells=None,
    disable_ctam=False,
    disable_ctam_modules=False,
    mrms_core_only=False,
    input_manifest=None,
)
```

`json_path` defaults to `None` in the signature but is required at runtime — `main()` raises `ValueError` if it is not supplied. When `mrms_core_only=True`, GLM and RAP integration steps are skipped.

`remove_old_cells=None` uses the realtime default (`True`). Historical callers
pass the historical setting explicitly (`False`); passing a boolean overrides
the default.
`input_manifest` accepts the `CycleInputManifest` the tandem coordinator builds,
so integration reads the exact files the cycle staged.

## Integration Stages

`pipeline.py` runs enrichment in parallel worker branches and merges property patches back onto the same storm-cell set.

Major stages:

1. Dataset stats integration (the 25 `stats_datasets` entries in `config/integration.yaml`, read through `config.py`)
2. ProbSevere field integration
3. GLM integration (`GLM_FLASH_COUNT`, `GLM_TOTAL_ENERGY`) — skipped when `mrms_core_only=True`
4. RAP integration (wind/environment fields) — skipped when `mrms_core_only=True`
5. Optional AzShear support integration (currently feature-flagged)
6. Attach StormProb inputs and commit pending observation state
7. CTAM execution unless `disable_ctam=True`; `disable_ctam_modules=True` keeps
   built-in StormProb and skips external modules
8. Publish the cleaned projection, cell history, public CTAM routes, StormProb
   forecasts, and input manifest through the publication coordinator
9. Clean inactive cell files when `remove_old_cells` is true, then write each
   API index once after the replacement files are available

## CTAM Handoff

When enabled, integration calls `EdgeWARN.ctam.run.run_ctam_result(...)` with a
cycle-normalized timestamp plus `json_path`, `input_manifest`, and the external
module disable flag. Module outputs are persisted under each cell's `modules`
structure; committed public routes are published separately.

## API and History Side Effects

The publication coordinator updates:

- per-cell history files, using one SQLite read snapshot for touched cell IDs
- API indexes (`stormcell_index.json` and `cell_index.json`)
- stale cell cleanup policy (inactive cells older than 120 minutes, only when
  `remove_old_cells` is true)

These side effects are required for stable API behavior.

## Integrated Data Sources

The current integration configuration enriches storm cells with MRMS statistic groups such as reflectivity, NLDN density, echo tops, VIL/VIL density, VII, precipitation rate, RALA, and azimuthal shear summaries. It also copies selected ProbSevere fields, adds GLM flash count/energy when scan-time GLM files are available, and attaches RAP wind/environment fields used by StormProb and external CTAM modules.

MESH is not an MRMS statistic group here. It reaches cells only as a copied
ProbSevere field via `probsevere_field_map`, so it is absent from
`stats_datasets` and carries no percentile variants.

The optional AzShear support-feature integration path exists in `integrate_azshear.py` and `azshear/`, but the pipeline-level feature flag is currently disabled.
