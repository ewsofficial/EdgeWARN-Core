#!/usr/bin/env python3
"""Synchronize the NWS zone assets required by the alert-ingest pipeline.

Run from the repository root with the ``EdgeWARN-dev`` environment active:

    python scripts/sync_nws_zones.py

The underlying maintenance command defaults to a dry run.  This convenience
entry point always applies the fetched updates so it is safe to use as the
pipeline's explicit preflight step.  All normal zone-sync options, including
``--config-dir`` and ``--assets-dir``, are passed through unchanged.
"""

from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"


def main() -> int:
    """Run the zone-sync command, applying updates by default."""
    sys.path.insert(0, str(SOURCE_ROOT))
    from common.ingest.nws import zone_sync

    if "--apply" not in sys.argv[1:]:
        sys.argv.insert(1, "--apply")
    return zone_sync.main()


if __name__ == "__main__":
    raise SystemExit(main())
