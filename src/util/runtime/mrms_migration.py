"""Read-only startup gate for an interrupted offline MRMS migration."""

from __future__ import annotations

import json
from pathlib import Path


class IncompleteMrmsMigration(RuntimeError):
    """The selected catalog is not safe for service startup."""


def require_completed_migration(config_dir=None) -> None:
    """Refuse unfinished or unverifiable journals without creating any files."""
    if config_dir is None:
        from common.config.loader import config_root

        config_dir = config_root()
    root = Path(config_dir).expanduser().resolve()
    journal = root / ".mrms-migration" / "journal.json"
    message = (
        f"MRMS migration at {journal} is incomplete or invalid; keep services "
        "stopped and use migrate-mrms --resume or --rollback before startup"
    )
    try:
        # Stat explicitly: permission errors and a marker that is a regular
        # file must not be mistaken for an absent journal.
        try:
            journal.lstat()
        except FileNotFoundError:
            try:
                journal.parent.lstat()
            except FileNotFoundError:
                return
            if journal.parent.is_symlink() or not journal.parent.is_dir():
                raise IncompleteMrmsMigration(message)
            return
        if not journal.resolve().is_relative_to(root):
            raise IncompleteMrmsMigration(message)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
            or payload.get("status") not in {"complete", "rolled-back"}
        ):
            raise IncompleteMrmsMigration(message)
    except (OSError, ValueError, TypeError) as exc:
        raise IncompleteMrmsMigration(message) from exc
