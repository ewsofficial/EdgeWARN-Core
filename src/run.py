"""Retired monolithic runner entry point.

``run.py`` previously owned the combined analysis and accessory topology and
NEXRAD in one process tree. That topology no longer exists, so forwarding this
command to only ``run_edgewarn.py`` would silently omit operational services.
Use the explicit service entry points (or ``run_all.py``) instead.
"""

import sys


def main() -> int:
    print(
        "run.py has been retired because it cannot preserve the former "
        "all-services topology. Use one of:\n"
        "  python src/run_all.py\n"
        "  python src/run_edgewarn.py\n"
        "  python src/run_nexrad.py",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
