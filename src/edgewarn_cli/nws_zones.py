"""Installed-command entry point for maintaining NWS zone geometry assets."""

from __future__ import annotations

import argparse
from pathlib import Path


def add_nws_zones_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sync-nws-zones",
        help="download or refresh the NWS zone geometry prerequisite",
    )
    parser.add_argument("--assets-dir", type=Path, default=None)
    parser.add_argument("--zone-types", nargs="+", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--pause-seconds", type=float, default=None)
    parser.add_argument(
        "--progress", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--apply", action="store_true", help="write downloaded assets"
    )
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--config-path", type=Path, default=None)
    parser.set_defaults(handler=sync_from_namespace, parser=parser)


def sync_from_namespace(args: argparse.Namespace) -> int:
    """Run the existing synchronizer after resolving the installed config root."""
    import json

    from common.ingest.nws.zone_sync import NWSZoneSync, _resolve_zone_sync_args
    from edgewarn_cli.config_path import resolve_config_root

    args.config_dir = str(resolve_config_root(args.config_path))
    resolved = _resolve_zone_sync_args(args)
    syncer = NWSZoneSync(
        assets_dir=resolved.assets_dir,
        zone_types=resolved.zone_types,
        timeout_seconds=resolved.timeout_seconds,
        max_retries=resolved.max_retries,
        max_workers=resolved.max_workers,
        pause_seconds=resolved.pause_seconds,
        show_progress=resolved.progress,
    )
    report = syncer.sync(dry_run=not resolved.apply)
    report_json = json.dumps(report.to_dict(), indent=2)
    if resolved.report_path:
        resolved.report_path.write_text(report_json, encoding="utf-8")
    print(report_json)
    return 0
