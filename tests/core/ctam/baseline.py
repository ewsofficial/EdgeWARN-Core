"""Snapshot harness for Phase 0 of the modular CTAM internal API plan.

These snapshots freeze the *observable output* of today's CTAM framework --
StormCast's per-cell payload, its alert payload, the stormcell snapshot shape,
and cell-history semantics -- so that later phases can prove the move to an
out-of-process internal API is behavior-preserving. Regenerate with::

    UPDATE_CTAM_BASELINE=1 python -m pytest tests/core/ctam

Regenerating is only correct when the source change was an intentional
behavior change; an unexplained diff here is the regression the plan is
guarding against.

Sibling of ``tests/architecture/baseline.py``, with three additions the CTAM
payloads require: ``datetime`` values (alert effective/expiry times), non-finite
floats (``max_refl`` and ``centroid`` are ``NaN`` for a degenerate cell), and
float rounding.

Floats are rounded to ``_FLOAT_PLACES``. StormCast's motion is computed through
``math.cos``/``math.radians`` in a flat-earth approximation, and libm differs in
the last bits between platforms. Baselines are generated on Windows and verified
on Linux in CI, so full-precision floats would produce failures that track the
runner rather than the code. 1e-9 is far below the significance of any value
here: motion is m/s, and forecast lat/lon are already rounded to 3 decimals by
``StormCastEngine._meters_to_latlon``.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path, PurePath, PurePosixPath

import pytest

import util.file as fs

BASELINE_DIR = Path(__file__).resolve().parents[2] / "ctam_baseline"

_UPDATE_ENV = "UPDATE_CTAM_BASELINE"

_FLOAT_PLACES = 9


def requires(*modules: str) -> None:
    """Skip when a scientific dependency is absent.

    ``EdgeWARN/__init__.py`` pulls in xarray, and StormCast's forecast path
    imports shapely, so these tests need the full ``EdgeWARN-dev`` environment.
    """
    for module in modules:
        pytest.importorskip(module)


def _path_attribute_map() -> dict[str, str]:
    """Map absolute ``util.file`` path values to their attribute names.

    Keeps snapshots independent of the machine's runtime base directory and
    path separator.
    """
    mapping: dict[str, str] = {}
    for name, value in vars(fs).items():
        if name.startswith("_") or not isinstance(value, PurePath):
            continue
        mapping.setdefault(str(value), name)
    return mapping


def normalize(value):
    """Reduce a CTAM value to a stable, JSON-serializable form."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: normalize(getattr(value, f.name)) for f in dataclasses.fields(value)}
        # ``AlertPayload.id`` is a derived property, not a field, but it is the
        # value that lands on disk as the alert filename.
        if hasattr(type(value), "id"):
            fields["@id"] = normalize(value.id)
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

    if isinstance(value, datetime):
        # Normalize to UTC so a snapshot does not encode the runner's offset,
        # but keep naive values distinguishable -- StormCast falls back to
        # ``datetime.now()`` (naive) when a timestamp is unparseable, and that
        # distinction is exactly what a Phase 5 regression would blur.
        if value.tzinfo is None:
            return {"@datetime_naive": value.isoformat()}
        return {"@datetime": value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, date):
        return {"@date": value.isoformat()}

    # bool before int/float: ``isinstance(True, int)`` is True.
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"@float": "nan"}
        if math.isinf(value):
            return {"@float": "inf" if value > 0 else "-inf"}
        rounded = round(value, _FLOAT_PLACES)
        # ``round`` on a numpy scalar returns a numpy scalar; ``float()`` keeps
        # the snapshot free of ``@repr`` fallbacks for numpy-backed values.
        return float(rounded)

    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (frozenset, set)):
        return {"@set": sorted(normalize(v) for v in value)}
    if isinstance(value, tuple):
        return {"@tuple": [normalize(v) for v in value]}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, (str, int)) or value is None:
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
        f"Missing CTAM baseline {path.name}. Run with {_UPDATE_ENV}=1 to create it."
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot == expected, (
        f"CTAM behavior drifted from baseline {path.name}. "
        f"If this change is intentional, re-run with {_UPDATE_ENV}=1 and review the diff."
    )
