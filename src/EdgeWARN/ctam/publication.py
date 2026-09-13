"""Atomic CTAM payload publication with a recoverable multi-file journal.

``os.replace`` is atomic for one file, not for a stormcell snapshot plus its
histories.  The journal makes each replacement explicit and allows startup to
roll forward a fully prepared transaction after a process death.  Indexes are
intentionally a callback executed only after every payload validates.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from util.atomic import atomic_write_json


class PublicationError(RuntimeError):
    pass


def _bytes(payload: Any) -> bytes:
    try: return json.dumps(payload, allow_nan=False, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError) as exc: raise PublicationError("payload is not finite JSON") from exc


def _hash(payload: bytes) -> str: return hashlib.sha256(payload).hexdigest()


def _require_db_cycle(dependency: Mapping[str, str] | None) -> None:
    if not dependency:
        return
    path = Path(dependency["path"])
    if not path.exists():
        raise PublicationError("committed StormProb database is missing")
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5) as db:
            row = db.execute("SELECT state FROM cycles WHERE cycle_id=?",
                             (dependency["cycle_id"],)).fetchone()
    except sqlite3.Error as exc:
        raise PublicationError("cannot verify StormProb cycle commit") from exc
    if row is None or row[0] not in ("inputs-committed", "forecasts-committed"):
        raise PublicationError("StormProb cycle is not committed")


class CTAMPublicationCoordinator:
    def __init__(self, journal_dir: Path | str, *, replace: Callable[[str, str], None] = os.replace) -> None:
        self.journal_dir = Path(journal_dir); self.replace = replace

    def recover(self) -> list[Path]:
        """Finish prepared replacements; quarantine irrecoverable journals."""
        if not self.journal_dir.exists(): return []
        recovered = []
        quarantine = self.journal_dir / "quarantine"; quarantine.mkdir(parents=True, exist_ok=True)
        for journal_path in self.journal_dir.glob("*.json"):
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("state") == "committed": continue
            try:
                _require_db_cycle(journal.get("db_dependency"))
                for item in journal["targets"]:
                    target, temporary = Path(item["target"]), Path(item["temporary"])
                    if target.exists() and _hash(target.read_bytes()) == item["post_hash"]: continue
                    if not temporary.exists() or _hash(temporary.read_bytes()) != item["post_hash"]:
                        raise PublicationError("prepared payload is missing or corrupt")
                    target.parent.mkdir(parents=True, exist_ok=True); self.replace(str(temporary), str(target))
                    json.loads(target.read_text(encoding="utf-8"))
                journal["state"] = "committed"; atomic_write_json(journal_path, journal)
                recovered.append(journal_path)
            except Exception:
                self.replace(str(journal_path), str(quarantine / journal_path.name))
        return recovered

    def publish(self, payloads: Mapping[Path | str, Any], *, publish_alerts: Callable[[], None] | None = None, publish_indexes: Callable[[], None] | None = None, transaction_id: str | None = None, db_dependency: Mapping[str, str] | None = None) -> Path:
        if not payloads: raise PublicationError("publication requires at least one payload")
        _require_db_cycle(db_dependency)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        transaction_id = transaction_id or uuid.uuid4().hex
        journal_path = self.journal_dir / f"{transaction_id}.json"
        targets = []
        for raw_target, payload in payloads.items():
            target = Path(raw_target); content = _bytes(payload)
            temporary = target.with_name(f".{target.name}.{transaction_id}.ctam-part")
            target.parent.mkdir(parents=True, exist_ok=True); temporary.write_bytes(content)
            # Parse the actual serialized bytes before the journal says it is prepared.
            json.loads(temporary.read_text(encoding="utf-8"))
            targets.append({"target": str(target), "temporary": str(temporary), "pre_hash": _hash(target.read_bytes()) if target.exists() else None, "post_hash": _hash(content), "replaced": False})
        journal = {"transaction_id": transaction_id, "state": "prepared", "targets": targets,
                   "db_dependency": dict(db_dependency) if db_dependency else None}
        atomic_write_json(journal_path, journal)
        try:
            for item in targets:
                self.replace(item["temporary"], item["target"]); item["replaced"] = True
                atomic_write_json(journal_path, journal)
                if _hash(Path(item["target"]).read_bytes()) != item["post_hash"]: raise PublicationError("replacement did not preserve serialized payload")
            # Alerts are host-owned payload publication, not a side effect of a
            # module process.  They happen only after every JSON target has
            # validated and before indexes make the cycle discoverable.
            if publish_alerts: publish_alerts()
            if publish_indexes: publish_indexes()
            journal["state"] = "committed"; atomic_write_json(journal_path, journal)
            return journal_path
        except Exception:
            # Leave the prepared journal and any not-yet-replaced sibling parts;
            # a subsequent coordinator recovers before touching these targets.
            raise
