"""Generate ``tests/data/stormprob_joint_parity.npz`` from the pinned PyTorch checkpoints.

Requires the sibling StormProb checkout used by ``export_stormprob_batch128.py``.
The fixture holds:
- ``joint_*``: one cell's Fourier/centroid distribution, explicit noise, and the
  PyTorch member radii/centres (``sample_joint`` parity, 1e-4 km).
- ``batch_*`` / ``torch_*``: one cached 128-cell batch of ONNX inputs and the
  PyTorch outputs for the end-to-end ONNX + numpy vs PyTorch check.
Run: ``python scripts/generate_stormprob_parity_fixture.py``
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "export_stormprob_batch128", ROOT / "scripts/export_stormprob_batch128.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)

from stormprob.model.radial_morphology.geometry import MIN_RADIUS_KM, fourier_decode  # noqa: E402
from stormprob.model.radial_morphology.sampling import stochastic_mode_mask  # noqa: E402

ENSEMBLE = 20
CELL = 0
OUTPUT = ROOT / "tests/data/stormprob_joint_parity.npz"


def main():
    manifest = json.loads((ROOT / "models/stormprob/manifest.json").read_text())
    radial = export._load_radial(manifest)
    motion = export._load_motion(manifest)
    batch = export._cached_batch()
    with torch.no_grad():
        radial_out = export._original_radial(radial, batch)
        motion_out = export._original_motion(motion, batch)

        rng = np.random.default_rng(42)
        shape_noise = rng.standard_normal((1, ENSEMBLE, 4, 33), dtype=np.float32)
        centroid_noise = rng.standard_normal((1, ENSEMBLE, 4, 2), dtype=np.float32)
        base = rng.uniform(-20, 20, (1, 4, 2)).astype(np.float32)
        mean = radial_out["coefficient_mean"][CELL:CELL + 1]
        log_std = radial_out["coefficient_log_std"][CELL:CELL + 1]
        offset = radial_out["centroid_offset_km"][CELL:CELL + 1]
        cholesky = radial_out["centroid_cholesky"][CELL:CELL + 1]
        current = batch["radial_history"][CELL:CELL + 1, -1]
        mask = stochastic_mode_mask(33, radial.stochastic_modes, dtype=mean.dtype)
        coefficients = mean[:, None] + log_std.exp()[:, None] * mask * torch.from_numpy(shape_noise)
        residual = fourier_decode(coefficients, radial.radial_size)
        residual = radial.max_change_km * torch.tanh(residual / radial.max_change_km)
        radii = (current[:, None, None] + residual).clamp_min(MIN_RADIUS_KM)
        centres = (torch.from_numpy(base) + offset)[:, None] + (
            cholesky[:, None] @ torch.from_numpy(centroid_noise)[..., None])[..., 0]

    arrays = {
        "joint_mean": mean, "joint_log_std": log_std, "joint_offset": offset,
        "joint_cholesky": cholesky, "joint_current_radii": current,
        "joint_ref_radii": radii, "joint_ref_centres": centres,
    }
    data = {key: value.numpy() for key, value in arrays.items()}
    data.update(joint_base=base, joint_shape_noise=shape_noise, joint_centroid_noise=centroid_noise)
    data.update({f"batch_{key}": value.numpy() for key, value in batch.items()})
    data.update({f"torch_{key}": value.numpy() for key, value in {**radial_out, **motion_out}.items()})
    OUTPUT.parent.mkdir(exist_ok=True)
    np.savez_compressed(OUTPUT, **data)
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
