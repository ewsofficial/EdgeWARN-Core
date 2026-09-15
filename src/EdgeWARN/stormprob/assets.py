"""Locate the packaged StormProb ONNX graphs and calibrator."""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path


_REQUIRED_ASSETS = (
    "manifest.json",
    "normalization-stats.json",
    "isotonic_calibrator.json",
    "stormprob_radial.onnx",
    "stormprob_motion.onnx",
)


def _candidates() -> tuple[Path, ...]:
    """Return source-checkout and installed-prefix asset locations."""
    here = Path(__file__).resolve()
    source_candidates = tuple(
        parent / "models" / "stormprob" for parent in (here, *here.parents)
    )
    # setuptools data-files install below sys.prefix.  Keep this explicit so
    # an installed wheel does not depend on the repository being present.
    installed = Path(sys.prefix) / "models" / "stormprob"
    return (installed, *source_candidates)


@lru_cache(maxsize=1)
def asset_dir() -> Path:
    for candidate in _candidates():
        if (candidate / "manifest.json").is_file():
            return candidate
    raise FileNotFoundError(
        "StormProb assets are not installed; expected models/stormprob/manifest.json"
    )


def validate_assets() -> Path:
    """Validate the complete deployed asset set before service startup."""
    directory = asset_dir()
    missing = [name for name in _REQUIRED_ASSETS if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"StormProb assets incomplete in {directory}: missing {', '.join(missing)}"
        )
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest["onnx_export"]["deployment_status"] not in {"packaged", "external"}:
            raise ValueError("manifest deployment status is not deployable")
        for model in ("radial", "motion"):
            filename = manifest["onnx_export"]["models"][model]["file"]
            if filename not in _REQUIRED_ASSETS:
                raise ValueError(f"manifest references unexpected {model} asset {filename!r}")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid StormProb asset manifest in {directory}") from exc
    return directory


def manifest_path() -> Path:
    return asset_dir() / "manifest.json"
