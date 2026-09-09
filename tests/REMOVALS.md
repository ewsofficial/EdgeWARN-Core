# Test remediation record

This file records intentional removals where a deleted test or fixture could
otherwise look like lost regression coverage.

- The three skipped `serialize_nexrad_render_intermediate` tests targeted a
  retired function. Dense binary decode remains covered by
  `test_serialize_nexrad_elevation_artifacts_decodes_grouped_ar2v_directly`,
  and orientation is covered against the current serializer by
  `test_current_nexrad_serializer_normalizes_azimuth_orientation`.
- The RAP `mock_datasets` fixture was never requested by a test. Extractor
  mapping remains covered by the active `test_integrate_rap_*` cases.
- `azshear_constants.json`, `stormcell_entry_field_inventory.json`,
  `stormcell_grid_only_synthetic_entry.json`, and
  `stormcell_snapshot_envelope.json` had no baseline-harness or source
  consumer. Dormant snapshots cannot detect a production regression.
- Empty Python markers and contributor guides below empty Jest subdirectories
  had no pytest, Jest, or helper-import consumer. The active API tests remain
  directly under `tests/api/`.
- Phase 9 NEXRAD benchmark consolidation removed the unreachable
  `run_benchmark`/`run_subprocess_benchmark`/`run_pool_benchmark` bodies and
  synthetic-volume builders from `benchmark_nexrad_memory.py` and
  `benchmark_nexrad_pool_memory.py` (both are thin shims over the unified
  `benchmarks/benchmark_nexrad.py` sampler) after proving the copied builders
  no longer ran: they imported the retired `parser.RawSweep`/`RawVolume`
  model and patched a `worker.parse_raw_volume_file` entry point the worker
  no longer calls, so every synthetic child died silently on an empty result
  queue. The unified sampler rebuilds the volume against the current
  `models.RawVolumeBuffer`/`RawSweepRange` API and is smoke-run for real
  (`--execution subprocess|pool|compare`, plus the stage profiler).
  No CI lane ever executed the removed bodies (benchmarks are excluded from
  default pytest discovery), so no regression contract was lost.
- Phase 9 doc-citation repair updated moved paths in
  `docs/ctam/internal-api-limits.md` and
  `plans/modular-ctam-phase0-findings.md` (`tests/benchmarks/` to
  `benchmarks/`, `tests/core/config/baseline.py` to
  `tests/architecture/baseline.py`, `tests/core/process/integrate/...` to
  `tests/unit/enrichment/...`). The findings plans are historical records;
  only the file paths were modernized so `test_every_cited_file_exists`
  keeps proving the cited arguments are checkable. All cited line numbers
  remain within their (moved, otherwise identical) files.
