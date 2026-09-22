# PrecipRate Readiness Checklist

This change removes PrecipRate only from scan discovery readiness. Downstream integration, StormProb enrichment, EWMRS rendering, and API serving remain unchanged.

## Configuration

- [X] Remove `PrecipRate_00.00` from `mrms.check_products` in `config/ingest.yaml`.
- [X] Leave `PrecipRate_00.00` in `mrms.products`.
- [X] Leave the PrecipRate `directory_map` entry unchanged.
- [X] Leave the PrecipRate dataset in `config/integration.yaml`, so `maxPrecipRate` continues populating downstream cells and StormProb inputs.
- [X] Leave the `MRMS_PrecipRate` layer in `config/ewmrs_render.yaml`.
- [X] Leave the API product catalog, filesystem paths, renderer configuration, StormProb feature order, and CTAM-facing output unchanged.

## Readiness and downstream behavior

- [X] Confirm `get_check_modifiers()` excludes PrecipRate.
- [X] Confirm `get_integration_modifiers()` still includes PrecipRate.
- [X] Confirm `get_ewmrs_modifiers()` still includes PrecipRate.
- [X] Confirm the primary scheduler uses the reduced readiness list for scan selection.
- [X] Confirm historical processing uses the reduced readiness list for scan selection.
- [X] Confirm the selected cycle still downloads, integrates, and renders PrecipRate through the existing downstream paths.
- [X] Confirm no change is made to the StormProb `maxPrecipRate` feature slot or missing-value behavior.

## Tests and baselines

- [X] Remove PrecipRate from `tests/config_baseline/mrms_readiness_catalog.json`.
- [X] Update the readiness count assertion in `tests/architecture/test_catalog_baseline.py` from 12 to 11.
- [X] Keep `tests/config_baseline/mrms_ingest_catalog.json` unchanged.
- [X] Keep `tests/config_baseline/integration_datasets.json` unchanged.
- [X] Add or update assertions that readiness excludes PrecipRate while integration and EWMRS modifier lists include it.
- [ ] Verify delayed PrecipRate does not prevent scan discovery with a runtime/scheduler test.
- [ ] Verify the selected cycle still performs downstream PrecipRate integration and rendering with an end-to-end fixture.

## Documentation and validation

- [X] Update `docs/core/ingestion.md` to state that PrecipRate is excluded from scan discovery readiness but remains a downstream integration and render input.
- [ ] Run:

```bash
npm run validate-config
PYTHONPATH=src python -m common.config.validate
python -m pytest tests/architecture tests/core/ingest/mrms tests/unit/rendering
```

## Acceptance condition

Scan selection can proceed when PrecipRate is delayed, while the selected cycle continues to download, integrate, and render PrecipRate using the existing downstream contracts.
