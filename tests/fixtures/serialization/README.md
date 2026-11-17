# Shared NEXRAD serialization fixture

`nexrad/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz` is produced by
`NEXRAD.render` and served unchanged by the v3 radar field route.

Regenerate with the EdgeWARN environment active:

```bash
PYTHONPATH=src python tests/fixtures/serialization/generate_fixtures.py
```

Python tests decode the header, float16 values, and float32 axes independently.
The Jest companion verifies API byte passthrough and headers.
