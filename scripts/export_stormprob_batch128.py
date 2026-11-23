"""Re-export the pinned StormProb joint-centroid checkpoints as fixed batch-128 ONNX graphs.

Requires a sibling StormProb checkout with ``models/radial.pt`` (joint radial +
centroid) and ``models/residual.pt`` (motion), PyTorch, ONNX, and ONNX Runtime.
Source paths and SHA-256 hashes come from ``models/stormprob/manifest.json``.
Graphs are checked against the original checkpoint models on random and
cached test-split inputs at history lengths 1, 2, and 30 before replacing
packaged assets.

Graph contract (recorded in the manifest by this script):
- radial: ``radial_history, statistics_history, current_features, history_mask``
  -> ``coefficient_mean [B,4,33], coefficient_log_std [B,4,33],
  centroid_offset_km [B,4,2], centroid_cholesky [B,4,2,2]``. The centroid head
  is exported with ``base_centers = 0`` so the mean is additive in the frozen
  motion displacement; the Cholesky factor does not depend on the base.
- motion: ``current_features, trajectory_sequence, trajectory_mask``
  -> ``residual_motion_mps [B,4,2]`` (``reshape(B,4,-1)[..., :2]``).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import torch
import onnx
os.environ.setdefault("ORT_DISABLE_ALL_TELEMETRY", "1")
import onnxruntime as ort


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "StormProb"
sys.path.insert(0, str(SOURCE))

from stormprob.model.radial_morphology import RadialBoundaryNetwork  # noqa: E402
from stormprob.model.residual import ResidualMotionCorrectionNetwork  # noqa: E402
from stormprob.data.features import LEGACY_CURRENT_FEATURES  # noqa: E402
from stormprob.data.residual_dataset import StormMotionResidualDataset  # noqa: E402
from stormprob.data.radial_dataset import RadialBoundaryDataset  # noqa: E402

BATCH_SIZE = 128
HISTORY_STEPS = 30
LEAD_TIMES_MINUTES = (15.0, 30.0, 45.0, 60.0)
# The promoted checkpoint stores ``max_change_km: null``; training used the
# constructor default (30 km tanh bound), confirmed against
# artifacts/radial/new_objective_10ep/source/model.py.
DEFAULT_MAX_CHANGE_KM = 30.0

RADIAL_INPUTS = ["radial_history", "statistics_history", "current_features", "history_mask"]
RADIAL_OUTPUTS = ["coefficient_mean", "coefficient_log_std", "centroid_offset_km", "centroid_cholesky"]
MOTION_INPUTS = ["current_features", "trajectory_sequence", "trajectory_mask"]
MOTION_OUTPUTS = ["residual_motion_mps"]


def _dense_final(rnn, sequence, lengths):
    output, _ = rnn(sequence)
    index = lengths.clamp(min=1).sub(1).reshape(-1, 1, 1).expand(-1, 1, output.shape[-1])
    return output.gather(1, index).squeeze(1)


def _radial_hidden(self, radial_history, statistics_history, environmental_history, history_mask):
    """Dense equivalent of ``RadialBoundaryNetwork._hidden`` (packed-sequence LSTM)."""
    if self.radial_norm_enabled:
        radial_history = self._normalize_stream(radial_history, "radial")
    if self.stats_norm_enabled:
        size = statistics_history.shape[-1]
        statistics_history = (torch.clamp(statistics_history, self.stats_clip_min[:size],
                                          self.stats_clip_max[:size]) - self.stats_mean[:size]) / self.stats_scale[:size]
    if self.env_norm_enabled:
        environmental_history = self._normalize_env(environmental_history)
    valid = history_mask.to(radial_history.dtype).unsqueeze(-1)
    sequence = torch.cat((radial_history * valid, statistics_history * valid,
                          environmental_history * valid), dim=-1)
    lengths = history_mask.sum(1)
    positions = torch.arange(sequence.shape[1], device=sequence.device)[None]
    source = (sequence.shape[1] - lengths[:, None] + positions).clamp(max=sequence.shape[1] - 1)
    compact = sequence.gather(1, source[..., None].expand_as(sequence))
    compact = compact * (positions < lengths[:, None]).unsqueeze(-1)
    return _dense_final(self.lstm, compact, lengths)


def _gru_forward(self, history_sequence, history_mask):
    """Dense equivalent of ``MaskedGRUEncoder.forward`` (packed-sequence GRU)."""
    lengths = history_mask.long().sum(dim=1)
    positions = torch.arange(history_sequence.shape[1], device=history_sequence.device)[None]
    source = (history_sequence.shape[1] - lengths[:, None] + positions).clamp(max=history_sequence.shape[1] - 1)
    compact = history_sequence.gather(1, source[..., None].expand_as(history_sequence))
    compact = compact * (positions < lengths[:, None]).unsqueeze(-1)
    return _dense_final(self.gru, compact, lengths) * (lengths > 0).unsqueeze(1)


class RadialExport(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, radial_history, statistics_history, current_features, history_mask):
        environment = current_features[:, None, :].expand(-1, radial_history.shape[1], -1)
        mean, log_std = self.model.coefficient_distribution(
            radial_history, statistics_history, environment, history_mask)
        base = radial_history.new_zeros((radial_history.shape[0], mean.shape[1], 2))
        offset, cholesky = self.model.centroid_distribution(
            radial_history, statistics_history, environment, history_mask, base)
        return mean, log_std, offset, cholesky


class MotionExport(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, current_features, trajectory_sequence, trajectory_mask):
        raw = self.model(current_features, trajectory_sequence, trajectory_mask)
        return raw.reshape(BATCH_SIZE, 4, -1)[..., :2]


def _checkpoint(kind, manifest):
    checkpoint_path = Path(manifest["checkpoints"][kind]["source_path"])
    if not checkpoint_path.is_absolute():
        checkpoint_path = SOURCE / checkpoint_path
    if hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() != manifest["checkpoints"][kind]["sha256"]:
        raise ValueError(f"{kind} checkpoint hash does not match manifest")
    return torch.load(checkpoint_path, map_location="cpu", weights_only=False)


def _load_radial(manifest):
    checkpoint = _checkpoint("radial", manifest)
    config = dict(checkpoint["model_config"])
    if config.get("max_change_km") is None:
        config["max_change_km"] = DEFAULT_MAX_CHANGE_KM
    model = RadialBoundaryNetwork(**config)
    model.load_state_dict(checkpoint["model_state_dict"])
    return model.eval()


def _load_motion(manifest):
    checkpoint = _checkpoint("motion", manifest)
    config = {key: value for key, value in checkpoint["model_config"].items()
              if key in inspect.signature(ResidualMotionCorrectionNetwork).parameters}
    state = dict(checkpoint["model_state_dict"])
    for key, expected in (("along_track_scales", 1.), ("along_track_biases", 0.)):
        if key in state and not torch.all(state.pop(key) == expected):
            raise ValueError(f"Unsupported residual calibration: {key}")
    if "lead_times_minutes" in state and not torch.equal(
            state.pop("lead_times_minutes"), torch.tensor(LEAD_TIMES_MINUTES)):
        raise ValueError("Residual checkpoint must use 15/30/45/60-minute leads")
    model = ResidualMotionCorrectionNetwork(**config)
    model.load_state_dict(state)
    return model.eval()


def _mask_at_lengths(mask, length):
    """Keep only the trailing ``length`` valid steps of each row's history."""
    lengths = mask.sum(1)
    keep = torch.minimum(lengths, torch.full_like(lengths, length))
    positions = torch.arange(mask.shape[1], device=mask.device)[None]
    return positions >= (mask.shape[1] - keep)[:, None]


def _original_radial(model, tensors):
    environment = tensors["current_features"][:, None, :].expand(-1, tensors["radial_history"].shape[1], -1)
    mean, log_std = model.coefficient_distribution(
        tensors["radial_history"], tensors["statistics_history"], environment, tensors["history_mask"])
    base = tensors["radial_history"].new_zeros((tensors["radial_history"].shape[0], mean.shape[1], 2))
    offset, cholesky = model.centroid_distribution(
        tensors["radial_history"], tensors["statistics_history"], environment, tensors["history_mask"], base)
    return {"coefficient_mean": mean, "coefficient_log_std": log_std,
            "centroid_offset_km": offset, "centroid_cholesky": cholesky}


def _original_motion(model, tensors):
    raw = model(tensors["current_features"], tensors["trajectory_sequence"], tensors["trajectory_mask"])
    return {"residual_motion_mps": raw.reshape(BATCH_SIZE, 4, -1)[..., :2]}


def _adapted_radial(module, tensors):
    return dict(zip(RADIAL_OUTPUTS, module(
        tensors["radial_history"], tensors["statistics_history"],
        tensors["current_features"], tensors["history_mask"])))


def _adapted_motion(module, tensors):
    return dict(zip(MOTION_OUTPUTS, [module(
        tensors["current_features"], tensors["trajectory_sequence"], tensors["trajectory_mask"])]))


def _cached_batch():
    """First 128 test-split samples from the radial cache (training-style batch)."""
    base = StormMotionResidualDataset.from_cache(
        SOURCE / "artifacts/caches/radial/test_dataset_cache.pt",
        history_steps=HISTORY_STEPS, current_feature_names=LEGACY_CURRENT_FEATURES)
    dataset = RadialBoundaryDataset(
        base, SOURCE / "artifacts/caches/radial/test_64.pt", history_steps=HISTORY_STEPS)
    items = [dataset[i] for i in range(BATCH_SIZE)]
    return {
        "radial_history": torch.stack([item["radial_history"] for item in items]),
        "statistics_history": torch.stack([item["radial_statistics_history"] for item in items]),
        "current_features": torch.stack([item["current_features"] for item in items]),
        "history_mask": torch.stack([item["radial_history_mask"] for item in items]),
        "trajectory_sequence": torch.stack([item["trajectory_sequence"] for item in items]),
        "trajectory_mask": torch.stack([item["trajectory_mask"] for item in items]),
    }


def _length_batches(cached):
    batches = {}
    for length in (1, 2, HISTORY_STEPS):
        batch = dict(cached)
        batch["history_mask"] = _mask_at_lengths(cached["history_mask"], length)
        batch["trajectory_mask"] = _mask_at_lengths(cached["trajectory_mask"], length)
        batches[length] = batch
    return batches


def main():
    assets = ROOT / "models/stormprob"
    manifest_path = assets / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    radial = _load_radial(manifest)
    motion = _load_motion(manifest)

    # Dense RNNs permit fixed-batch export; gather the last valid state to
    # reproduce the packed-sequence result for each cell's history length.
    radial._hidden = types.MethodType(_radial_hidden, radial)
    motion.trajectory_encoder.forward = types.MethodType(_gru_forward, motion.trajectory_encoder)

    varied = {
        "radial_history": torch.randn(BATCH_SIZE, HISTORY_STEPS, 64),
        "statistics_history": torch.randn(BATCH_SIZE, HISTORY_STEPS, 1),
        "current_features": torch.randn(BATCH_SIZE, 135),
        "history_mask": torch.zeros(BATCH_SIZE, HISTORY_STEPS, dtype=torch.bool),
        "trajectory_sequence": torch.randn(BATCH_SIZE, HISTORY_STEPS, 16),
        "trajectory_mask": torch.zeros(BATCH_SIZE, HISTORY_STEPS, dtype=torch.bool),
    }
    cached = _length_batches(_cached_batch())

    export_radial = RadialExport(radial).eval()
    export_motion = MotionExport(motion).eval()

    # Packed originals vs dense adapters, on random and cached inputs at
    # history lengths 1, 2, and 30.
    for adapted_of, original_of, module, original in (
            (_adapted_radial, _original_radial, export_radial, radial),
            (_adapted_motion, _original_motion, export_motion, motion)):
        with torch.no_grad():
            adapted = adapted_of(module, varied)
            original_outputs = original_of(original, varied)
        for name, reference in original_outputs.items():
            np.testing.assert_allclose(
                reference.numpy(), adapted[name].detach().numpy(), rtol=1e-4, atol=1e-4,
                err_msg=f"packed-vs-dense varied: {name}")

    for length, batch in cached.items():
        for adapted_of, original_of, module, original in (
                (_adapted_radial, _original_radial, export_radial, radial),
                (_adapted_motion, _original_motion, export_motion, motion)):
            with torch.no_grad():
                adapted = adapted_of(module, batch)
                original_outputs = original_of(original, batch)
            for name, reference in original_outputs.items():
                np.testing.assert_allclose(
                    reference.numpy(), adapted[name].detach().numpy(), rtol=1e-4, atol=1e-4,
                    err_msg=f"packed-vs-dense cached length {length}: {name}")

    examples = {
        "radial": (torch.zeros(BATCH_SIZE, HISTORY_STEPS, 64), torch.zeros(BATCH_SIZE, HISTORY_STEPS, 1),
                   torch.zeros(BATCH_SIZE, 135), torch.ones(BATCH_SIZE, HISTORY_STEPS, dtype=torch.bool)),
        "motion": (torch.zeros(BATCH_SIZE, 135), torch.zeros(BATCH_SIZE, HISTORY_STEPS, 16),
                   torch.ones(BATCH_SIZE, HISTORY_STEPS, dtype=torch.bool)),
    }
    with tempfile.TemporaryDirectory(dir=assets) as temp:
        staged = {}
        for kind, module, inputs, outputs in (
                ("radial", export_radial, RADIAL_INPUTS, RADIAL_OUTPUTS),
                ("motion", export_motion, MOTION_INPUTS, MOTION_OUTPUTS)):
            path = Path(temp) / manifest["onnx_export"]["models"][kind]["file"]
            args = examples[kind]
            with torch.no_grad():
                expected = module(*args)
                if isinstance(expected, torch.Tensor):
                    expected = (expected,)
                torch.onnx.export(module, args, path, input_names=inputs,
                                  output_names=outputs, opset_version=18,
                                  dynamo=False)
            graph = onnx.load(path)
            onnx.checker.check_model(graph)
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            actual = session.run(None, {name: value.numpy() for name, value in zip(inputs, args)})
            for name, reference, result in zip(outputs, expected, actual):
                np.testing.assert_allclose(result, reference, rtol=1e-4, atol=1e-4,
                                           err_msg=f"{kind}:{name}")

            def run_session(batch):
                values = session.run(None, {name: batch[name].numpy() for name in inputs})
                return dict(zip(outputs, values))

            original_model = radial if kind == "radial" else motion
            original_of = _original_radial if kind == "radial" else _original_motion
            with torch.no_grad():
                original_varied = original_of(original_model, varied)
                original_cached = {length: original_of(original_model, batch)
                                   for length, batch in cached.items()}
            for name, reference in original_varied.items():
                result = run_session(varied)[name]
                np.testing.assert_allclose(result, reference.numpy(), rtol=1e-4, atol=1e-4,
                                           err_msg=f"{kind}:{name}:varied")
            for length, batch in cached.items():
                for name, reference in original_cached[length].items():
                    result = run_session(batch)[name]
                    np.testing.assert_allclose(result, reference.numpy(), rtol=1e-4, atol=1e-4,
                                               err_msg=f"{kind}:{name}:cached:{length}")
            staged[kind] = path
        for kind, path in staged.items():
            target = assets / manifest["onnx_export"]["models"][kind]["file"]
            path.replace(target)
            info = manifest["onnx_export"]["models"][kind]
            info["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            info["bytes"] = target.stat().st_size
            info["inputs"] = RADIAL_INPUTS if kind == "radial" else MOTION_INPUTS
            info["outputs"] = RADIAL_OUTPUTS if kind == "radial" else MOTION_OUTPUTS
    manifest["onnx_export"]["batch_size"] = BATCH_SIZE
    manifest["onnx_export"]["history_steps"] = HISTORY_STEPS
    manifest["onnx_export"]["exporter"] = "torch.onnx.export(dynamo=False); dense recurrent export adapters"
    manifest["onnx_export"]["torch_version"] = torch.__version__
    manifest["onnx_export"]["onnx_version"] = onnx.__version__
    manifest["onnx_export"]["onnxruntime_version"] = ort.__version__
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print("export complete; graph hashes and contracts recorded in manifest")


if __name__ == "__main__":
    main()
