# Integration Pipeline

Post-detection enrichment is implemented under `src/EdgeWARN/process/integrate`.

## Module Layout

```text
src/EdgeWARN/process/integrate/
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
    mrms_core_only=False,
    input_manifest=None,
)
```

`json_path` defaults to `None` in the signature but is required at runtime — `main()` raises `ValueError` if it is not supplied. When `mrms_core_only=True`, GLM and RAP integration steps are skipped.

`remove_old_cells=None` defers to `api_index.yaml`, which sets it per mode
(`realtime: true`, `historical: false`); passing a boolean overrides that.
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
6. CTAM execution unless `disable_ctam=True`
7. Save integrated stormcell JSON
8. Update cell history
9. Update API cell indexes and cleanup inactive cell files

## CTAM Handoff

When enabled, integration calls `EdgeWARN.ctam.run.run_ctam(cells, timestamp=...)` and persists module outputs under each cell's `modules` structure (plus grid outputs when applicable).

## API and History Side Effects

After save, integration updates:

- per-cell history files
- API cell index (`cell_index.json`)
- stale cell cleanup policy (default: remove inactive cells older than 2 hours)

These side effects are required for stable API behavior.

## Integrated Data Sources

The current integration configuration enriches storm cells with MRMS statistic groups such as reflectivity, NLDN density, echo tops, VIL/VIL density, VII, precipitation rate, RALA, and azimuthal shear summaries. It also copies selected ProbSevere fields, adds GLM flash count/energy when scan-time GLM files are available, and attaches RAP wind/environment fields used by StormProb and external CTAM modules.

MESH is not an MRMS statistic group here. It reaches cells only as a copied
ProbSevere field via `probsevere_field_map`, so it is absent from
`stats_datasets` and carries no percentile variants.

The optional AzShear support-feature integration path exists in `integrate_azshear.py` and `azshear/`, but the pipeline-level feature flag is currently disabled.
