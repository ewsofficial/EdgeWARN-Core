# Shared implementations

`config/` owns validated catalogs and overlays. `ingest/` owns acquisition,
registry/inventory, and replay protection. `pipeline/` owns staged Core readiness.
Preserve detection readiness before integration and immutable final optional
snapshots. Missing required inputs must fail closed. Bound concurrency and
retention, and maintain async acquisition with the existing sync/HTTPS fallback.
