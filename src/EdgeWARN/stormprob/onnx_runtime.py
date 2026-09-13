"""Load the paired StormProb ONNX graphs once per inference worker.

This module has no PyTorch or StormProb checkout dependency. The caller supplies
the deployed asset directory and the versioned manifest path explicitly.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np


class ModelUnavailable(RuntimeError):
    """A graph, operator, hash, or execution provider is unavailable."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4)
def load_sessions(model_dir: str | Path, manifest_path: str | Path,
                  provider: str = "CPUExecutionProvider"):
    """Return cached (radial, motion) sessions, failing on a partial pair."""
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise ModelUnavailable("onnxruntime is not installed") from exc
    if provider not in ort.get_available_providers():
        raise ModelUnavailable(f"ONNX Runtime provider unavailable: {provider}")
    manifest = json.loads(Path(manifest_path).read_text())
    assets = manifest["onnx_export"]["models"]
    if manifest["onnx_export"]["deployment_status"] not in {"packaged", "external"}:
        raise ModelUnavailable("StormProb ONNX assets are not ready for deployment")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    sessions = []
    for key in ("radial", "motion"):
        info = assets[key]
        path = Path(model_dir) / info["file"]
        if not path.is_file() or _hash(path) != info["sha256"]:
            raise ModelUnavailable(f"Missing or hash-mismatched StormProb {key} graph: {path}")
        try:
            session = ort.InferenceSession(str(path), sess_options=options, providers=[provider])
        except Exception as exc:
            raise ModelUnavailable(f"Cannot initialize StormProb {key} graph: {exc}") from exc
        if session.get_providers() != [provider]:
            raise ModelUnavailable(f"StormProb {key} provider mismatch: {session.get_providers()}")
        if [item.name for item in session.get_inputs()] != info["inputs"]:
            raise ModelUnavailable(f"StormProb {key} graph input contract mismatch")
        if [item.name for item in session.get_outputs()] != info["outputs"]:
            raise ModelUnavailable(f"StormProb {key} graph output contract mismatch")
        sessions.append(session)
    return tuple(sessions)


@lru_cache(maxsize=4)
def load_calibrator(model_dir: str | Path, manifest_path: str | Path) -> dict:
    """Load and hash-check the paired lead-specific calibration table."""
    manifest = json.loads(Path(manifest_path).read_text())
    expected = manifest["deployment_parameters"]["calibrator"]["sha256"]
    path = Path(model_dir) / "isotonic_calibrator.json"
    if not path.is_file() or _hash(path) != expected:
        raise ModelUnavailable(f"Missing or hash-mismatched StormProb calibrator: {path}")
    calibrator = json.loads(path.read_text())
    if len(calibrator.get("parameters", [])) != 4:
        raise ModelUnavailable("StormProb calibrator does not contain four leads")
    return calibrator


def infer_pair(radial_session, motion_session, *, radial_history, statistics_history,
               current_features, history_mask, history_sequence, trajectory_sequence,
               trajectory_mask):
    """Execute both fixed batch-1 graphs and return named float32 outputs."""
    tensors = {
        "radial_history": np.asarray(radial_history, dtype=np.float32),
        "statistics_history": np.asarray(statistics_history, dtype=np.float32),
        "current_features": np.asarray(current_features, dtype=np.float32),
        "history_mask": np.asarray(history_mask, dtype=np.bool_),
        "history_sequence": np.asarray(history_sequence, dtype=np.float32),
        "trajectory_sequence": np.asarray(trajectory_sequence, dtype=np.float32),
        "trajectory_mask": np.asarray(trajectory_mask, dtype=np.bool_),
    }
    required = {"radial_history": (1, 30, 64), "statistics_history": (1, 30, 1),
                "current_features": (1, 135), "history_mask": (1, 30),
                "history_sequence": (1, 30, 135), "trajectory_sequence": (1, 30, 16),
                "trajectory_mask": (1, 30)}
    for key, shape in required.items():
        if tensors[key].shape != shape:
            raise ValueError(f"{key} shape {tensors[key].shape}; expected {shape}")
    try:
        mean, log_std = radial_session.run(None, {key: tensors[key] for key in
            ("radial_history", "statistics_history", "current_features", "history_mask")})
        residual, = motion_session.run(None, {key: tensors[key] for key in
            ("current_features", "history_sequence", "history_mask",
             "trajectory_sequence", "trajectory_mask")})
    except Exception as exc:
        raise ModelUnavailable(f"StormProb ONNX inference failed: {exc}") from exc
    outputs = {"coefficient_mean": mean, "coefficient_log_std": log_std,
               "residual_motion_mps": residual}
    for name, value in outputs.items():
        if value.dtype != np.float32 or not np.isfinite(value).all():
            raise ModelUnavailable(f"StormProb {name} is non-finite or not float32")
    if mean.shape != (1, 4, 33) or log_std.shape != (1, 4, 33) or residual.shape != (1, 4, 2):
        raise ModelUnavailable("StormProb ONNX output shape mismatch")
    return outputs
