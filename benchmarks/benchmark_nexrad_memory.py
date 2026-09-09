"""NEXRAD worker memory for simultaneous volumes (synthetic).

Thin entry point over the unified ``benchmarks/benchmark_nexrad.py`` sampler
(``--mode synthetic --execution subprocess``); extra CLI flags pass through.

This harness measures resident memory while running the NEXRAD worker parse path
for N simultaneous volumes, always using different radar IDs to avoid output
collisions. The benchmark uses synthetic sweep metadata so it can stress the
worker/export path without requiring live Level-II sample files.

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_memory.py --output-dir /tmp/nexrad_mem
"""

from __future__ import annotations

import sys


def main() -> int:
    from benchmark_nexrad import main as unified_main
    return unified_main(
        ["--mode", "synthetic", "--execution", "subprocess", *sys.argv[1:]]
    )


if __name__ == "__main__":
    raise SystemExit(main())
