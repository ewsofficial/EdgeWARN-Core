"""Top-level ``edgewarn`` console command.

Service dispatch and safe configuration editing are imported lazily while the
parser is constructed; importing :mod:`edgewarn_cli` remains side-effect free.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Sequence


_DISTRIBUTION_NAME = "edgewarn-core"
_SOURCE_PACKAGE_JSON = Path(__file__).resolve().parents[2] / "package.json"


def _release_version() -> str:
    """Return installed metadata first, with a source-checkout fallback."""
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        try:
            with _SOURCE_PACKAGE_JSON.open("r", encoding="utf-8") as handle:
                value = json.load(handle).get("version")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return "unknown"
        return str(value) if value else "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edgewarn",
        description="Run and configure EdgeWARN backend services.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_release_version()}",
    )
    subparsers = parser.add_subparsers(dest="command")

    from edgewarn_cli.run import add_run_parser
    from edgewarn_cli.configure import add_configure_parser
    from edgewarn_cli.nws_zones import add_nws_zones_parser

    add_run_parser(subparsers)
    add_configure_parser(subparsers)
    add_nws_zones_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the package command and return its process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
