# Configuration ownership

The Python and Node loaders validate the same 14 catalogs and schemas:

| Catalog | Owner |
| --- | --- |
| runtime | Service topology, resource limits, Core retries, supervision, logging, readiness cadence |
| historical | Historical selection and isolated processing |
| filesystem | Runtime base-directory defaults and generic cleanup defaults |
| detection | Detection, tracking selection, alert matching, polygon controls |
| lineage | Lineage and split/merge policy |
| integration | MRMS statistics, RAP features, GLM enrichment |
| scheduler | Discovery/check cadence and listing window |
| api_index | Core publication and retention |
| ingest | MRMS registry, bounded acquisition, scan-time GLM |
| nws | NWS active-alert fetching, geometry mapping, and zone-asset sync |
| nexrad | Level-II acquisition, selection, render cadence, retention, display mapping |
| synoptic_rap | RAP acquisition and source validation |
| api | HTTP security, gates, limits, caching, pagination, radar products |
| kalman | Tracking state and motion parameters |

Copy the complete `config/` tree, including schemas. Direct-service precedence
is `--config-dir`, `EDGEWARN_CONFIG_DIR`, then installation discovery. The package
command calls this `--config-path`. Runtime-root precedence is `--base-dir`,
`EDGEWARN_BASE_DIR`, legacy `BASE_DIR`, then `filesystem.yaml`.

```bash
edgewarn configure runtime.ingest.disk_budget_mb 8192
edgewarn configure nexrad.gui.max_workers 8
npm run validate-config
PYTHONPATH=src python -m common.config.validate
```

Edits are validated and atomic; all settings require process restart. Both
loaders reject unknown configuration names/keys and unsupported schema keywords.

## Acquisition and readiness

`ingest` uses schema version 2. `mrms.products` adds 18 optional products to three
protected inputs: reflectivity, PrecipFlag, and ProbSevere. Integration consumers
resolve full product identities; raw acquisition does not add public products.

`runtime.ingest` bounds listing, download, decode and auxiliary concurrency,
queue depth, retries, timeouts, retention, and shutdown. Raw retention must cover
the discovery window. `runtime.consumers.core_readiness_seconds` is bounded to
0.1–1 seconds. Core and ingest compare frozen dependency fingerprints including
CTAM/StormProb and GLM settings. Required missing inputs fail closed; optional
acquisition terminates at its deadline and cannot revise final snapshots.

NEXRAD GUI settings are owned by `nexrad.gui`; moment display keys are in
`nexrad.variable_colormaps`. Only NEXRAD owns `gui/NEXRAD` cleanup.

See [ingestion](ingestion.md), [service contracts](service_registry.md), and
[breaking migration](../../CHANGELOG.md).
