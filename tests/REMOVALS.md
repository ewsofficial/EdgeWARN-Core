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
