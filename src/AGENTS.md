# Source agent guide

Runtime owners: `run_ingest.py` acquires MRMS/RAP/GLM and publishes Core readiness;
`run_edgewarn.py` analyzes pinned local inputs; `run_nexrad.py` supervises radar
ingest and rendering; `run_all.py` supervises subprocesses. `run.py` is retired.
The Node HTTP implementation is `src/api` and deploys separately.

Keep imports compatible with `pythonpath = src`. Use the EdgeWARN Conda
environment for Python checks. Runtime artifacts belong beneath the configured
base directory and are published atomically. Consider detection, tracking,
integration, CTAM, alerts, indexes, and readiness when changing shared code.
