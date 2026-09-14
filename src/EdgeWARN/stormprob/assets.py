"""Locate the packaged StormProb ONNX graphs and calibrator."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def asset_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "models" / "stormprob"
        if (candidate / "manifest.json").is_file():
            return candidate
    raise FileNotFoundError("models/stormprob/manifest.json not found")


def manifest_path() -> Path:
    return asset_dir() / "manifest.json"
