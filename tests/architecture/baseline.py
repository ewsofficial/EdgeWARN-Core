"""Snapshot harness for Phase 0 of the source-configuration extraction.

These snapshots freeze the *effective* values the current source-code
configuration functions return, so that later phases can prove the move into
``config/*.yaml`` is value-preserving. Regenerate with::

    UPDATE_CONFIG_BASELINE=1 python -m pytest tests/architecture

Regenerating is only correct when the source change was an intentional
behavior change; an unexplained diff here is the regression the plan is
guarding against.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path, PurePosixPath, PurePath

import pytest

import util.file as fs

BASELINE_DIR = Path(__file__).resolve().parents[1] / "config_baseline"

_UPDATE_ENV = "UPDATE_CONFIG_BASELINE"


def requires(*modules: str) -> None:
    """Skip when a scientific dependency is absent.

    ``EdgeWARN/__init__.py`` pulls in xarray, so importing an otherwise pure
    catalog module such as ``EdgeWARN.process.integrate.config`` requires the
    full ``EdgeWARN-dev`` environment.
    """
    for module in modules:
        pytest.importorskip(module)


def _path_attribute_map() -> dict[str, str]:
    """Map absolute ``util.file`` path values to their attribute names.

    The catalogs identify output directories by ``util.file`` attribute, so
    resolving back to the attribute name keeps snapshots independent of the
    machine's runtime base directory and path separator.
    """
    mapping: dict[str, str] = {}
    for name, value in vars(fs).items():
        if name.startswith("_") or not isinstance(value, PurePath):
            continue
        mapping.setdefault(str(value), name)
    return mapping


def normalize(value):
    """Reduce a config value to a stable, JSON-serializable form."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: normalize(getattr(value, f.name)) for f in dataclasses.fields(value)}
        # Derived properties are behavior, not storage, but a migration that
        # silently changes them would still be a regression.
        for prop in ("label", "is_glm"):
            if hasattr(type(value), prop):
                fields[f"@{prop}"] = normalize(getattr(value, prop))
        return {"@type": type(value).__name__, **fields}

    if isinstance(value, PurePath):
        attribute = _path_attribute_map().get(str(value))
        if attribute:
            return f"fs:{attribute}"
        base = Path(fs.BASE_DIR)
        try:
            return f"base:{PurePosixPath(Path(value).relative_to(base).as_posix())}"
        except ValueError:
            return f"abs:{Path(value).name}"

    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (frozenset, set)):
        return {"@set": sorted(normalize(v) for v in value)}
    if isinstance(value, tuple):
        return {"@tuple": [normalize(v) for v in value]}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return {"@repr": repr(value)}


def assert_baseline(name: str, value) -> None:
    """Compare ``value`` against the committed snapshot called ``name``."""
    snapshot = normalize(value)
    path = BASELINE_DIR / f"{name}.json"
    serialized = json.dumps(snapshot, indent=2, sort_keys=False) + "\n"

    if os.environ.get(_UPDATE_ENV) == "1":
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")
        return

    assert path.exists(), (
        f"Missing config baseline {path.name}. Run with {_UPDATE_ENV}=1 to create it."
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot == expected, (
        f"Effective configuration drifted from baseline {path.name}. "
        f"If this change is intentional, re-run with {_UPDATE_ENV}=1 and review the diff."
    )
