"""Re-export the pinned StormProb checkpoints as fixed batch-128 ONNX graphs.

Requires a sibling StormProb checkout with the pinned checkpoints, PyTorch,
ONNX, and ONNX Runtime. Graphs are checked against the original checkpoint
models at history lengths 1, 2, and 30 before replacing packaged assets.
"""
from __future__ import annotations

import hashlib
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
from stormprob.model.residual import ResidualMotionCorrectionNetwork, load_residual_model_state  # noqa: E402


def _dense_final(rnn, sequence, lengths):
    output, _ = rnn(sequence)
    index = lengths.clamp(min=1).sub(1).reshape(-1, 1, 1).expand(-1, 1, output.shape[-1])
    return output.gather(1, index).squeeze(1)


def _radial_hidden(self, radial_history, statistics_history, environmental_history, history_mask):
    if self.radial_norm_enabled:
        radial_history = self._normalize_stream(radial_history, "radial")
    if self.stats_norm_enabled:
        mean, scale = self.stats_mean[:1], self.stats_scale[:1]
        statistics_history = (torch.clamp(statistics_history, min=self.stats_clip_min[:1],
                                          max=self.stats_clip_max[:1]) - mean) / scale
    if self.env_norm_enabled:
        environmental_history = self._normalize_env(environmental_history)
    valid = history_mask.to(radial_history.dtype).unsqueeze(-1)
    sequence = torch.cat((radial_history * valid, statistics_history * valid,
                          environmental_history * valid), dim=-1)
    lengths = history_mask.sum(1)
    positions = torch.arange(30, device=sequence.device)[None]
    source = (30 - lengths[:, None] + positions).clamp(max=29)
    compact = sequence.gather(1, source[..., None].expand_as(sequence))
    compact = compact * (positions < lengths[:, None]).unsqueeze(-1)
    return _dense_final(self.lstm, compact, lengths)


def _lstm_forward(self, history_sequence, history_mask):
    lengths = history_mask.long().sum(dim=1)
    return _dense_final(self.lstm, history_sequence, lengths) * (lengths > 0).unsqueeze(1)


def _gru_forward(self, history_sequence, history_mask):
    lengths = history_mask.long().sum(dim=1)
    positions = torch.arange(30, device=history_sequence.device)[None]
    source = (30 - lengths[:, None] + positions).clamp(max=29)
    compact = history_sequence.gather(1, source[..., None].expand_as(history_sequence))
    compact = compact * (positions < lengths[:, None]).unsqueeze(-1)
    return _dense_final(self.gru, compact, lengths) * (lengths > 0).unsqueeze(1)


class RadialExport(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, radial_history, statistics_history, current_features, history_mask):
        return self.model.coefficient_distribution(
            radial_history, statistics_history, current_features[:, None, :].expand(-1, 30, -1), history_mask
        )


class MotionExport(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, current_features, history_sequence, history_mask,
                trajectory_sequence, trajectory_mask):
        return self.model(current_features, history_sequence, history_mask,
                          trajectory_sequence, trajectory_mask).reshape(128, 4, 2)


def main():
    assets = ROOT / "models/stormprob"
    manifest_path = assets / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    radial_ckpt = torch.load(SOURCE / "artifacts/radial/v7_capped_12ep/best.pt", map_location="cpu", weights_only=False)
    motion_ckpt = torch.load(SOURCE / "artifacts/residual/best_model/best.pt", map_location="cpu", weights_only=False)
    for kind in ("radial", "motion"):
        checkpoint_path = Path(manifest["checkpoints"][kind]["source_path"])
        if not checkpoint_path.is_absolute():
            checkpoint_path = SOURCE / checkpoint_path
        if hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() != manifest["checkpoints"][kind]["sha256"]:
            raise ValueError(f"{kind} checkpoint hash does not match manifest")
    radial = RadialBoundaryNetwork(**radial_ckpt["model_config"])
    radial.load_state_dict(radial_ckpt["model_state_dict"])
    motion = ResidualMotionCorrectionNetwork(**motion_ckpt["model_config"])
    load_residual_model_state(motion, motion_ckpt["model_state_dict"])
    radial.eval()
    motion.eval()
    varied = {
        "radial": [torch.randn(128, 30, 64), torch.randn(128, 30, 1),
                   torch.randn(128, 135), torch.zeros(128, 30, dtype=torch.bool)],
        "motion": [torch.randn(128, 135), torch.randn(128, 30, 135),
                   torch.zeros(128, 30, dtype=torch.bool), torch.randn(128, 30, 16),
                   torch.zeros(128, 30, dtype=torch.bool)],
    }
    for length in (1, 2, 30):
        varied["radial"][3][length - 1, -length:] = True
        varied["motion"][2][length - 1, -length:] = True
        varied["motion"][4][length - 1, -length:] = True
    with torch.no_grad():
        original_radial = radial.coefficient_distribution(
            varied["radial"][0], varied["radial"][1],
            varied["radial"][2][:, None, :].expand(-1, 30, -1), varied["radial"][3])
        original_motion = motion(*varied["motion"]).reshape(128, 4, 2)
    # Dense RNNs permit fixed-batch export; gather the last valid state to
    # reproduce the packed-sequence result for each cell's history length.
    radial._hidden = types.MethodType(_radial_hidden, radial)
    encoder_forward = _lstm_forward if hasattr(motion.history_encoder, "lstm") else _gru_forward
    motion.history_encoder.forward = types.MethodType(encoder_forward, motion.history_encoder)
    motion.trajectory_encoder.forward = types.MethodType(_gru_forward, motion.trajectory_encoder)
    with torch.no_grad():
        replacement_radial = radial.coefficient_distribution(
            varied["radial"][0], varied["radial"][1],
            varied["radial"][2][:, None, :].expand(-1, 30, -1), varied["radial"][3])
        replacement_motion = motion(*varied["motion"]).reshape(128, 4, 2)
    for original, replacement in zip(original_radial, replacement_radial):
        np.testing.assert_allclose(replacement.numpy(), original.numpy(), rtol=1e-4, atol=1e-4)
    np.testing.assert_allclose(replacement_motion.numpy(), original_motion.numpy(), rtol=1e-4, atol=1e-4)
    radial, motion = RadialExport(radial.eval()).eval(), MotionExport(motion.eval()).eval()
    examples = {
        "radial": (torch.zeros(128, 30, 64), torch.zeros(128, 30, 1),
                   torch.zeros(128, 135), torch.ones(128, 30, dtype=torch.bool)),
        "motion": (torch.zeros(128, 135), torch.zeros(128, 30, 135),
                   torch.ones(128, 30, dtype=torch.bool), torch.zeros(128, 30, 16),
                   torch.ones(128, 30, dtype=torch.bool)),
    }
    models = {"radial": radial, "motion": motion}
    with tempfile.TemporaryDirectory(dir=assets) as temp:
        staged = {}
        for kind, model in models.items():
            info = manifest["onnx_export"]["models"][kind]
            path = Path(temp) / info["file"]
            args = examples[kind]
            with torch.no_grad():
                expected = model(*args)
                if isinstance(expected, torch.Tensor):
                    expected = (expected,)
                torch.onnx.export(model, args, path, input_names=info["inputs"],
                                  output_names=info["outputs"], opset_version=18,
                                  dynamo=False)
            graph = onnx.load(path)
            onnx.checker.check_model(graph)
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            actual = session.run(None, {name: value.numpy() for name, value in zip(info["inputs"], args)})
            for name, reference, result in zip(info["outputs"], expected, actual):
                np.testing.assert_allclose(result, reference.numpy(), rtol=1e-4, atol=1e-4,
                                           err_msg=f"{kind}:{name}")
            varied_actual = session.run(None, {name: value.numpy() for name, value in zip(info["inputs"], varied[kind])})
            reference = original_radial if kind == "radial" else (original_motion,)
            for name, expected_varied, result in zip(info["outputs"], reference, varied_actual):
                np.testing.assert_allclose(result, expected_varied.numpy(), rtol=1e-4, atol=1e-4,
                                           err_msg=f"{kind}:{name}:varied")
            staged[kind] = path
        for kind, path in staged.items():
            target = assets / manifest["onnx_export"]["models"][kind]["file"]
            path.replace(target)
            info = manifest["onnx_export"]["models"][kind]
            info["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            info["bytes"] = target.stat().st_size
    manifest["onnx_export"]["batch_size"] = 128
    manifest["onnx_export"]["exporter"] = "torch.onnx.export(dynamo=False); dense recurrent export adapters"
    manifest["onnx_export"]["torch_version"] = torch.__version__
    manifest["onnx_export"]["onnx_version"] = onnx.__version__
    manifest["onnx_export"]["onnxruntime_version"] = ort.__version__
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
