"""NEXRAD worker pool vs subprocess memory usage (synthetic).

Thin entry point over the unified ``benchmarks/benchmark_nexrad.py`` sampler
(``--mode synthetic --execution compare``); extra CLI flags pass through.

Measures the real production difference:
- subprocess: spawns fresh Python interpreter via subprocess.run (no shared memory, ~114 MB import baseline)
- pool: forks from parent via ProcessPoolExecutor (copy-on-write shared memory)

Usage:
    PYTHONPATH=src python benchmarks/benchmark_nexrad_pool_memory.py --output-dir /tmp/nexrad_pool
"""

from __future__ import annotations

import sys


def main() -> int:
    from benchmark_nexrad import main as unified_main
    return unified_main(
        ["--mode", "synthetic", "--execution", "compare", *sys.argv[1:]]
    )


if __name__ == "__main__":
    raise SystemExit(main())
