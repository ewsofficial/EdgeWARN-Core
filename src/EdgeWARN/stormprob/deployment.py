"""Explicit StormProb promotion gate shared by publication and tracking."""
from __future__ import annotations

import os


def mode() -> str:
    value = os.environ.get("STORMPROB_MODE", "shadow").strip().lower()
    if value not in {"shadow", "promoted", "rollback"}:
        raise ValueError("STORMPROB_MODE must be shadow, promoted, or rollback")
    return value


def promoted() -> bool:
    return mode() == "promoted"


def rollback() -> bool:
    return mode() == "rollback"
