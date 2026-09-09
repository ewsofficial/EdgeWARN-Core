# Shared binary serialization fixtures

These files are **the** cross-language binary contract. They are produced by the
real Python production encoders via `generate_fixtures.py` and consumed directly
by the Node API tests:

- `tests/integration/serialization/test_binary_contracts.py` decodes the
  encoder outputs independently (`numpy.frombuffer` struct reads, never the
  encoder's own loader) and refuses to let these committed bytes drift from a
  fresh encode (`TestSharedFixtureInventory`).
- `tests/api/test_binary_contracts.js` copies these very files into a
  disposable runtime base directory, serves them through the real Express
  stack, and validates response headers plus byte passthrough.

## Regenerating

```bash
conda activate EdgeWARN
PYTHONPATH=src python tests/fixtures/serialization/generate_fixtures.py
```

Regeneration is byte-for-byte deterministic: the RAP write is raw
little-endian `uint16` (no headers), and both gzip producers pin `mtime=0`
with no FNAME field. Commit the regenerated files together with the Python
contract change that required them.

## Inventory

| Artifact | Producer encoder | Node consumer |
| --- | --- | --- |
| `RAP/CAPE/20260317-200000/data.u16` + `metadata.json` | `EWMRS.rap.uint16_pipeline` | `ancillary.rapData` / `rapMetadata` |
| `render/CompRefQC/.../chunk_0_0.f16.gz` + index pair | `EWMRS.render.tiler.save_float16_chunk` | `renders.chunk` |
| `nexrad/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz` | `NEXRAD.render._write_nexrad_variable_bin` | `ancillary.radarField` |
| `wpc/surface_analysis/wpc_sfc_20260317-200000.geojson` | `common.ingest.wpc` parser + converter | `ancillary.wpcSurface` |