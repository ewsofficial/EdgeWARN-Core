"""Compatibility services for direct users of the deprecated StormCast adapter.

Production CTAM execution supplies ``StormCastCycleService`` instead.  Keeping
the filesystem bridge here lets existing public ``StormCastModule`` imports
continue to work until the legacy adapter is removed.
"""
from __future__ import annotations

import json
from typing import Any

import util.file as fs
from EdgeWARN.alerts import AlertManager


def read_history(cell_id: Any) -> list[dict[str, Any]]:
    try:
        from EdgeWARN.stormprob.database import StormProbRepository
        history = StormProbRepository().legacy_history(cell_id)
        if history:
            return history
    except FileNotFoundError:
        pass
    path = fs.CELL_DIR / f"{cell_id}.json"
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def previous_alert(cell_id: Any):
    return AlertManager.load("StormCast", cell_id)
