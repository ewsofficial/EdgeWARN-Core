"""Resumable import of legacy cell history and stormcell snapshots.

Run with ``python -m EdgeWARN.stormprob.migrate --base-dir <BASE_DIR>``.
This keeps all source JSON; use ``--dry-run`` to inspect counts first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .database import StormProbRepository, _time


def source_files(base_dir: Path) -> list[Path]:
    data = base_dir / "data"
    # Snapshots first, then per-cell histories. Histories usually contain the
    # richer integrated values and therefore win on duplicate observation keys.
    return [*sorted((data / "stormcells").glob("stormcells_*.json")),
            *sorted(path for path in (data / "cells").glob("*.json")
                    if path.name != "cell_index.json")]


def inspect_file(path: Path) -> dict:
    content = path.read_bytes()
    payload = json.loads(content)
    entries = payload.get("features", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError(f"{path} is not a legacy cell array")
    count = 0
    for item in entries:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        timestamp = item.get("timestamp") or (item.get("properties") or {}).get("timestamp")
        if timestamp is None:
            continue
        _time(timestamp)
        count += 1
    if count != len(entries):
        raise ValueError(f"{path}: {len(entries) - count} entries lack identity/timestamp")
    return {"path": str(path), "entries": len(entries), "sha256": hashlib.sha256(content).hexdigest()}


def migrate(base_dir: Path | str, *, dry_run: bool = False) -> dict:
    base_dir = Path(base_dir)
    files = source_files(base_dir)
    inspected = [inspect_file(path) for path in files]
    report = {"source_files": len(files), "source_entries": sum(item["entries"] for item in inspected),
              "files": inspected, "dry_run": dry_run}
    if dry_run:
        return report
    repo = StormProbRepository(base_dir)
    if repo.path.exists():
        backup_dir = repo.path.parent / "backups"
        backup = backup_dir / f"stormprob-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.sqlite3"
        report["backup"] = str(repo.backup(backup))
        repo.prune_backups(backup_dir)
    imported = []
    for path, expected in zip(files, inspected):
        result = repo.import_legacy_file(path)
        if result["sha256"] != expected["sha256"] or result["entries"] != expected["entries"]:
            raise RuntimeError(f"legacy source changed during migration: {path}")
        imported.append(result)
    repo.integrity_check()
    if sum(item["entries"] for item in imported) != report["source_entries"]:
        raise RuntimeError("legacy migration entry count mismatch")
    report["imported_entries"] = sum(item["imported"] for item in imported)
    report["skipped_files"] = sum(bool(item["skipped"]) for item in imported)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.base_dir, dry_run=args.dry_run), indent=2))


if __name__ == "__main__":
    main()
