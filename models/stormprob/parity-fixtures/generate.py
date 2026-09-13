"""Phase 0 generator for the StormProb replacement contract.

Runs under the `EdgeWARN` conda env with the StormProb checkout on sys.path.
Produces, under EdgeWARN-Core/models/stormprob/:
  - manifest.json            : versioned model/deployment contract
  - normalization-stats.json : full normalization vectors extracted from checkpoints
  - availability-matrix.md   : field-by-field EdgeWARN coverage matrix
  - parity-fixtures/*.json   : frozen PyTorch reference parity fixtures
  - parity-fixtures/generate.py : frozen reproduction script reference (this file)

No production code is modified. No weights are copied into the repo:
license/provenance confirmation is still pending (see manifest).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch

STORMPROB_ROOT = Path("/home/yuchenwei/Projects/StormProb")
REPO_ROOT = Path("/home/yuchenwei/Projects/EdgeWARN-Core")
OUT_DIR = REPO_ROOT / "models" / "stormprob"
FIX_DIR = OUT_DIR / "parity-fixtures"
DATA_ROOT = Path("/home/yuchenwei/Projects/data/StormProb/test/20220413/data/cells")

RADIAL_CKPT = STORMPROB_ROOT / "artifacts/radial/v7_capped_12ep/best.pt"
MOTION_CKPT = STORMPROB_ROOT / "artifacts/residual/best_model/best.pt"
CALIBRATOR_PATH = STORMPROB_ROOT / "artifacts/radial/v7_capped_12ep/isotonic_calibrator.json"
SCORECARD_T025 = STORMPROB_ROOT / "artifacts/radial/v7_capped_12ep/test_instantaneous_calibrated_t025.json"

sys.path.insert(0, str(STORMPROB_ROOT))
from stormprob.model.initial_pred import predict_motion_vector  # noqa: E402
from stormprob.model.radial_morphology.model import (  # noqa: E402
    RadialBoundaryNetwork,
    entry_to_radial_profile,
)
from stormprob.model.residual.features import (  # noqa: E402
    build_current_feature_vector,
    build_trajectory_sequence,
)
from stormprob.model.residual.inference import load_residual_model_state  # noqa: E402
from stormprob.model.residual.network import ResidualMotionCorrectionNetwork  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_state() -> dict:
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=STORMPROB_ROOT,
                         capture_output=True, text=True).stdout.strip()
    log = subprocess.run(["git", "log", "--oneline", "-3"], cwd=STORMPROB_ROOT,
                         capture_output=True, text=True).stdout.strip()
    status = subprocess.run(["git", "status", "--short"], cwd=STORMPROB_ROOT,
                            capture_output=True, text=True).stdout.strip()
    return {"commit": rev, "log": log, "dirty": status}


def load_track(cell_id: str) -> list[dict]:
    entries = json.loads((DATA_ROOT / f"{cell_id}.json").read_text())
    if isinstance(entries, dict):
        entries = [entries]
    return [e for e in entries if isinstance(e, dict)]


def tensors_for_index(entries: list[dict], index: int, history_steps: int = 30) -> dict:
    """Build the exact model input tensors for entries[:index+1]."""
    current_track = [build_current_feature_vector(entries, i) for i in range(index + 1)]
    first = max(0, index - history_steps + 1)
    valid_rows = current_track[first:index + 1]
    pad = history_steps - len(valid_rows)
    history = [[0.0] * len(current_track[0])] * pad + valid_rows
    history_mask = [False] * pad + [True] * len(valid_rows)
    traj_seq, traj_mask = build_trajectory_sequence(entries, index, history_steps)
    radii_rows, stats_rows = [], []
    for e in entries[first:index + 1]:
        radii, stats = entry_to_radial_profile(e, n_angles=64, max_radius_km=100.0)
        radii_rows.append(radii.astype(np.float32))
        stats_rows.append(stats[:1].astype(np.float32))  # log-area only (exclude_ellipse_fit_inputs)
    radial = np.stack(radii_rows).astype(np.float32)
    stats = np.stack(stats_rows).astype(np.float32)
    if pad:
        radial = np.concatenate([np.zeros((pad, 64), np.float32), radial])
        stats = np.concatenate([np.zeros((pad, 1), np.float32), stats])
    return {
        "current": np.asarray(current_track[index], dtype=np.float32),
        "history": np.asarray(history, dtype=np.float32),
        "history_mask": np.asarray(history_mask, dtype=bool),
        "trajectory": np.asarray(traj_seq, dtype=np.float32),
        "trajectory_mask": np.asarray(traj_mask, dtype=bool),
        "radial": radial,
        "radial_stats": stats,
    }


def run_reference(t: dict, radial_model, motion_model, calibrator: dict,
                  ensemble_samples: int = 20, seed: int = 42) -> dict:
    torch.manual_seed(seed)
    with torch.no_grad():
        current = torch.as_tensor(t["current"])[None]
        history = torch.as_tensor(t["history"])[None]
        hmask = torch.as_tensor(t["history_mask"])[None]
        traj = torch.as_tensor(t["trajectory"])[None]
        tmask = torch.as_tensor(t["trajectory_mask"])[None]
        radial = torch.as_tensor(t["radial"])[None]
        stats = torch.as_tensor(t["radial_stats"])[None]
        env = current[:, None].expand(-1, history.shape[1], -1)
        residual = motion_model(current, history, hmask,
                                trajectory_sequence=traj,
                                trajectory_mask=tmask).view(1, -1, 2)
        mean, log_std = radial_model.coefficient_distribution(radial, stats, env, hmask)
        samples = radial_model.sample_predictions(radial, stats, env, hmask, ensemble_samples)
    return {"residual": residual, "mean": mean, "log_std": log_std, "samples": samples}


LEAD_MINUTES = [15.0, 30.0, 45.0, 60.0]


def displacements(initial_uv: list[float], residual: torch.Tensor) -> list[dict]:
    out = []
    for lead_idx, lead_min in enumerate(LEAD_MINUTES):
        u = float(initial_uv[0] + residual[0, lead_idx, 0])
        v = float(initial_uv[1] + residual[0, lead_idx, 1])
        secs = lead_min * 60.0
        out.append({"lead_minutes": lead_min, "u_mps": u, "v_mps": v,
                    "east_km": u * secs / 1000.0, "north_km": v * secs / 1000.0})
    return out


def occupancy_and_polygons(samples: torch.Tensor, centres: torch.Tensor,
                           calibrator: dict, threshold: float = 0.25,
                           half_width_km: float = 100.0, resolution_km: float = 1.0,
                           centroid_latlon: list[float] | None = None) -> dict:
    """Ensemble occupancy raster, isotonic calibration, 0.25 decision masks + polygons.

    Returns packed-bit decision masks (not full float grids) plus GeoJSON-ish
    polygons in [lon, lat] order. Full float grids are reproducible via the
    frozen generator script with the pinned seed.
    """
    import math as _math
    cells = int(2 * half_width_km / resolution_km) + 1
    axis = torch.linspace(-half_width_km, half_width_km, cells)
    east, north = torch.meshgrid(axis, axis, indexing="xy")
    n_rays = samples.shape[-1]
    angle = 2 * _math.pi * torch.arange(n_rays) / n_rays
    masks = []
    for s in range(samples.shape[1]):
        for lead in range(samples.shape[2]):
            radii = samples[0, s, lead]
            cx, cy = float(centres[0, lead, 0]), float(centres[0, lead, 1])
            dx = east - cx
            dy = north - cy
            ang = torch.remainder(torch.atan2(dy, dx), 2 * _math.pi) * n_rays / (2 * _math.pi)
            lo = torch.floor(ang).long().remainder(n_rays)
            hi = (lo + 1) % n_rays
            frac = ang - torch.floor(ang)
            r = radii[lo] + (radii[hi] - radii[lo]) * frac
            masks.append((torch.sqrt(dx.square() + dy.square()) <= r))
    masks = torch.stack(masks).view(samples.shape[1], len(LEAD_MINUTES), cells, cells)
    prob = masks.float().mean(0)  # [leads, cells, cells]
    # lead-specific isotonic calibration, quantized like the eval script
    cal = []
    for lead in range(len(LEAD_MINUTES)):
        levels = torch.tensor(calibrator["parameters"][lead]["calibrated_probabilities"])
        idx = torch.round(prob[lead] * samples.shape[1]).long().clamp(0, samples.shape[1])
        cal.append(levels[idx])
    cal = torch.stack(cal)
    try:
        from skimage import measure as _measure
        have_skimage = True
    except ImportError:
        have_skimage = False
    lead_out = []
    for lead in range(len(LEAD_MINUTES)):
        grid = (cal[lead].numpy() >= threshold)
        packed = base64.b64encode(np.packbits(grid).tobytes()).decode()
        polys = []
        status = "ok"
        if have_skimage and grid.any():
            for contour in _measure.find_contours(grid.astype(float), 0.5):
                # contour is (row, col); convert to east/north km
                ek = (contour[:, 1] - cells // 2) * resolution_km
                nk = (cells // 2 - contour[:, 0]) * resolution_km
                ek = ek + float(centres[0, lead, 0])
                nk = nk + float(centres[0, lead, 1])
                if centroid_latlon is not None:
                    lat0, lon0 = centroid_latlon
                    lats = lat0 + nk / 111.0
                    lons = lon0 + ek / (111.0 * _math.cos(_math.radians(lat0)))
                    ring = [[round(float(lo), 5), round(float(la), 5)] for lo, la in zip(lons, lats)]
                else:
                    ring = [[round(float(x), 3), round(float(y), 3)] for x, y in zip(ek, nk)]
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                polys.append(ring)
                if len(polys) >= 8:
                    break
        if not grid.any():
            status = "no-polygon:empty-0.25-contour"
        lead_out.append({"lead_minutes": LEAD_MINUTES[lead],
                         "decision_mask_bits": f"{cells}x{cells}",
                         "decision_mask_packbits_b64": packed,
                         "mean_occupancy": round(float(prob[lead].mean()), 6),
                         "max_occupancy": round(float(prob[lead].max()), 6),
                         "polygons_lonlat": polys,
                         "status": status})
    return {"leads": lead_out, "grid": {"half_width_km": half_width_km,
                                       "resolution_km": resolution_km, "cells": cells}}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIX_DIR.mkdir(parents=True, exist_ok=True)

    radial_ckpt = torch.load(RADIAL_CKPT, map_location="cpu", weights_only=False)
    motion_ckpt = torch.load(MOTION_CKPT, map_location="cpu", weights_only=False)
    calibrator = json.loads(CALIBRATOR_PATH.read_text())
    scorecard = json.loads(SCORECARD_T025.read_text())
    rm, mm = radial_ckpt["model_config"], motion_ckpt["model_config"]
    rt, mt = radial_ckpt.get("training_config", {}), motion_ckpt.get("training_config", {})
    state = git_state()

    current_names: list[str] = list(mt["current_feature_names"])
    traj_names: list[str] = list(mm["trajectory_feature_names"])
    n_coeff = 1 + 2 * int(rm["harmonics"])

    manifest = {
        "manifest_version": "0.1.0",
        "phase": "phase-0-contract-freeze",
        "stormprob_commit": state["commit"],
        "stormprob_log": state["log"],
        "stormprob_dirty": state["dirty"],
        "checkpoints": {
            "radial": {"source_path": str(RADIAL_CKPT), "sha256": sha256(RADIAL_CKPT),
                       "epoch": radial_ckpt.get("epoch")},
            "motion": {"source_path": str(MOTION_CKPT), "sha256": sha256(MOTION_CKPT),
                       "epoch": motion_ckpt.get("epoch")},
        },
        "provenance_license": {
            "status": "BLOCKED",
            "detail": ("No LICENSE/provenance file found in the StormProb checkout; "
                       "weights and calibrator are NOT copied into this repo. Manifest "
                       "references canonical source paths plus SHA-256. Confirm "
                       "license/provenance before packaging deployable assets (Phase 3)."),
        },
        "deployment_assets": {
            "status": "not-packaged",
            "required": ["stormprob_radial_v7.onnx", "stormprob_motion_best.onnx",
                         "isotonic_calibrator.json"],
        },
        "lead_order_minutes": LEAD_MINUTES,
        "radial_model": {
            "history_steps": 30, "n_rays": 64, "statistics": ["log_area"],
            "statistics_size": rm["statistics_size"],
            "environment_channels_repeated": 135, "environment_channels_expanded": 270,
            "recurrent": {"type": rm["recurrent_type"], "layers": rm["recurrent_layers"],
                          "hidden_size": rm["hidden_size"]},
            "fourier": {"harmonics": rm["harmonics"], "coefficients": n_coeff,
                        "stochastic_modes": rm["stochastic_modes"],
                        "max_std_km": rm["max_std_km"], "max_change_km": 30.0},
            "target_time_interpolation": rt.get("target_time_interpolation"),
            "exclude_ellipse_fit_inputs": rt.get("exclude_ellipse_fit_inputs"),
            "loss": {"name": rt.get("probabilistic_loss"), "mask_grid_size": rt.get("mask_grid_size"),
                     "mask_temperature_km": rt.get("mask_temperature_km"),
                     "lead_weights": rt.get("lead_weights")},
        },
        "motion_model": {
            "input_size": mm["input_size"], "history_steps": mt.get("history_steps"),
            "history_encoder": {"type": mm["history_encoder_type"],
                                "hidden_size": mm["lstm_hidden_size"],
                                "layers": mm["lstm_layers"]},
            "trajectory_branch": {"features": 16, "hidden_size": mm["trajectory_hidden_size"],
                                  "layers": mm["trajectory_layers"]},
            "heads": {"current_hidden": list(mm["current_hidden_sizes"]),
                      "fusion_hidden": list(mm["fusion_hidden_sizes"]),
                      "decoder_hidden": list(mm["decoder_hidden_sizes"]),
                      "lead_embedding_size": mm["lead_embedding_size"],
                      "lead_conditioned_decoders": mm["lead_conditioned_decoders"],
                      "deterministic_residual": True, "output": "[batch,4,2]-m/s"},
            "dropout": mm["dropout"],
            "target_time_interpolation": mt.get("target_time_interpolation"),
            "lead_time_tolerance_minutes": mt.get("lead_time_tolerance_minutes"),
        },
        "features": {
            "current_feature_order": current_names,
            "n_current": len(current_names),
            "n_property": len(current_names) - 4,
            "trajectory_feature_order": traj_names,
            "pressure_levels_hpa": [100, 125, 150, 175, 200, 225, 250, 275, 300, 325,
                                    350, 375, 400, 425, 450, 475, 500, 525, 550, 575,
                                    600, 625, 650, 675, 700, 725, 750, 775, 800, 825,
                                    850, 875, 900, 925, 950, 975, 1000],
            "missing_value_policy": ("finite float32 or -999 sentinel; radial env "
                                     "imputes per-feature median for values <= -900, clips to "
                                     "[clip_min,clip_max], normalizes (x-mean)/scale, appends "
                                     "binary missing mask (135 -> 270 channels); radial/stats "
                                     "streams clip+normalize with no imputation and are "
                                     "zeroed where history_mask is false; motion model uses "
                                     "missing_threshold -900 with the same median-impute "
                                     "scheme. Never substitute zero winds."),
            "units_note": ("Per-feature units are not recorded in the StormProb repo; "
                           "assumed EdgeWARN conventions (CAPE J/kg, temperatures C, "
                           "lengths km/m, winds m/s, reflectivity dBZ, shear s^-1 scaled). "
                           "Units must be pinned per feature in Phase 1."),
        },
        "tensor_shapes": {
            "radial_history": "[B,30,64] float32",
            "radial_statistics_history": "[B,30,1] float32 (log-area)",
            "history_mask": "[B,30] bool (left-pad false)",
            "current_features": "[B,135] float32",
            "history_sequence": "[B,30,135] float32",
            "trajectory_sequence": "[B,30,16] float32",
            "trajectory_mask": "[B,30] bool",
            "radial_coefficient_mean": "[B,4,33] float32",
            "radial_coefficient_log_std": "[B,4,33] float32",
            "ensemble_samples": "[B,20,4,64] float32",
            "motion_residual": "[B,4,2] float32 m/s",
        },
        "inference_math": {
            "motion_displacement_km": "(initial_wind_mps + residual_motion_mps) * lead_seconds / 1000",
            "initial_wind": "mean 0-6km wind from paired wind_field u/v levels (standard atmosphere)",
            "shape": ("sample radial Fourier distribution (seed 42, 20 members), add tanh-bounded "
                      "(30km) residual to current 64-ray profile, translate by predicted centroid, "
                      "occupancy raster, lead-specific isotonic calibration, 0.25 contour polygonization"),
            "primary_output": "four separate valid-time polygons (15/30/45/60); swept envelopes are separate products",
        },
        "deployment_parameters": {
            "ensemble_samples": 20, "ensemble_seed": 42,
            "train_ensemble_samples": rt.get("probabilistic_ensemble_samples"),
            "probability_threshold": 0.25,
            "grid": {"half_width_km": 100.0, "resolution_km": 1.0, "cells": 201},
            "calibrator": {"source_path": str(CALIBRATOR_PATH), "sha256": sha256(CALIBRATOR_PATH),
                           "method": calibrator["method"], "fit_split": calibrator["fit_split"],
                           "fit_samples": calibrator["fit_samples"],
                           "ensemble_samples": calibrator["ensemble_samples"]},
            "reference_scorecard": {"source_path": str(SCORECARD_T025),
                                    "sha256": sha256(SCORECARD_T025),
                                    "split": scorecard["split"], "samples": scorecard["samples"],
                                    "metrics": [{k: m[k] for k in
                                                 ("lead_minutes", "radial_crps_km", "operating_threshold",
                                                  "csi", "pod", "far")} for m in scorecard["metrics"]]},
        },
        "radial_geometry_convention": {
            "centroid": "[lat, lon]; training longitudes use 0-360 domain",
            "rays": "start east, rotate counterclockwise; east=dlon*111*cos(lat), north=dlat*111",
            "log_area": "0.5*|sum(east_i*north_{i+1}-east_{i+1}*north_i)|, log(max(area,1e-6))",
        },
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    norm = {"radial_model_config": {k: rm[k] for k in ("env_norm", "radial_norm", "stats_norm")},
            "motion_model_config": {k: mm[k] for k in (
                "current_feature_normalization_mean", "current_feature_normalization_scale",
                "current_feature_normalization_median", "current_feature_normalization_clip_min",
                "current_feature_normalization_clip_max", "current_feature_missing_threshold",
                "trajectory_normalization_mean", "trajectory_normalization_scale",
                "trajectory_normalization_clip_min", "trajectory_normalization_clip_max")}}
    (OUT_DIR / "normalization-stats.json").write_text(json.dumps(norm) + "\n")

    # ---- parity fixtures ----
    radial_model = RadialBoundaryNetwork(**dict(rm)).eval()
    radial_model.load_state_dict(radial_ckpt["model_state_dict"])
    motion_model = ResidualMotionCorrectionNetwork(**dict(mm)).eval()
    load_residual_model_state(motion_model, motion_ckpt["model_state_dict"])

    base = load_track("100083")     # 27 obs
    long = load_track("100096")     # 35 obs
    gapped = load_track("100636")   # contains 30-38 min gaps

    def entry_view(entries, i):
        e = entries[i]
        return {"timestamp": e.get("timestamp"), "centroid": [float(v) for v in e["centroid"]],
                "id": e.get("id"), "n_bbox": len(e.get("bbox", []))}

    cases: list[tuple[str, list[dict], int, str]] = [
        ("first_observation", base, 0, "single valid history row (all other rows left-padded)"),
        ("sparse_short_history", base, 2, "3 valid history rows"),
        ("regular_30_step", long, 29, "full 30-row history, no padding"),
        ("large_time_gap", gapped, 4,
         "forecast origin immediately after a ~38min scan gap (2022-04-13T23:30:43 follows 22:52:42)"),
    ]
    fixtures = []
    for name, entries, idx, note in cases:
        idx = min(idx, len(entries) - 1)
        t = tensors_for_index(entries, idx)
        ref = run_reference(t, radial_model, motion_model, calibrator)
        initial = predict_motion_vector(entries[idx])
        disp = displacements([initial["u"], initial["v"]], ref["residual"])
        centres = torch.tensor([[[d["east_km"], d["north_km"]] for d in disp]])
        geo = occupancy_and_polygons(ref["samples"], centres, calibrator,
                                     centroid_latlon=entries[idx]["centroid"])
        fix = {"name": name, "note": note, "synthetic": False,
               "origin": entry_view(entries, idx), "valid_history_rows": int(t["history_mask"].sum()),
               "inputs": {k: v.tolist() for k, v in t.items()},
               "initial_wind_mps": {"u": initial["u"], "v": initial["v"]},
               "motion_residual_mps": ref["residual"].view(-1, 2).tolist(),
               "displacement_km": disp,
               "fourier_mean": ref["mean"].tolist(), "fourier_log_std": ref["log_std"].tolist(),
               "occupancy": geo}
        (FIX_DIR / f"{name}.json").write_text(json.dumps(fix) + "\n")
        fixtures.append({"name": name, "file": f"{name}.json", "note": note,
                         "origin": fix["origin"], "valid_rows": fix["valid_history_rows"]})

    # missing-feature case: sentinel -999 on selected scalars, winds intact
    miss_entries = copy.deepcopy(base)
    for key in ("MUCAPE", "VIL", "MESH", "maxEchoTop18", "EBShear"):
        miss_entries[2]["properties"][key] = -999.0
    t = tensors_for_index(miss_entries, 2)
    ref = run_reference(t, radial_model, motion_model, calibrator)
    initial = predict_motion_vector(miss_entries[2])
    disp = displacements([initial["u"], initial["v"]], ref["residual"])
    centres = torch.tensor([[[d["east_km"], d["north_km"]] for d in disp]])
    geo = occupancy_and_polygons(ref["samples"], centres, calibrator,
                                 centroid_latlon=miss_entries[2]["centroid"])
    fix = {"name": "sparse_missing_features", "synthetic": "sentinel-injected",
           "note": "MUCAPE/VIL/MESH/maxEchoTop18/EBShear set to -999; winds intact; median-impute path",
           "origin": entry_view(miss_entries, 2), "valid_history_rows": 3,
           "inputs": {k: v.tolist() for k, v in t.items()},
           "initial_wind_mps": {"u": initial["u"], "v": initial["v"]},
           "motion_residual_mps": ref["residual"].view(-1, 2).tolist(),
           "displacement_km": disp, "fourier_mean": ref["mean"].tolist(),
           "fourier_log_std": ref["log_std"].tolist(), "occupancy": geo}
    (FIX_DIR / "sparse_missing_features.json").write_text(json.dumps(fix) + "\n")
    fixtures.append({"name": "sparse_missing_features", "file": "sparse_missing_features.json",
                     "note": fix["note"], "origin": fix["origin"], "valid_rows": 3})

    # synthetic split case: parent stem + divergent child
    parent = copy.deepcopy(base[:10])
    child = copy.deepcopy(base[:10])
    for k, e in enumerate(copy.deepcopy(base[10:13])):
        e = dict(e)
        e["centroid"] = [e["centroid"][0] + 0.05 * (k + 1), e["centroid"][1] + 0.07 * (k + 1)]
        e["split_from"] = base[9]["timestamp"]
        e["parent_ids"] = [base[9]["id"]]
        child.append(e)
    t = tensors_for_index(child, len(child) - 1)
    ref = run_reference(t, radial_model, motion_model, calibrator)
    initial = predict_motion_vector(child[-1])
    disp = displacements([initial["u"], initial["v"]], ref["residual"])
    centres = torch.tensor([[[d["east_km"], d["north_km"]] for d in disp]])
    geo = occupancy_and_polygons(ref["samples"], centres, calibrator,
                                 centroid_latlon=child[-1]["centroid"])
    fix = {"name": "split_lineage", "synthetic": True,
           "note": "child forked from 100083 stem with divergent centroids; lineage fields populated (no split examples exist in surveyed training data)",
           "origin": entry_view(child, len(child) - 1), "valid_history_rows": 13,
           "inputs": {k: v.tolist() for k, v in t.items()},
           "initial_wind_mps": {"u": initial["u"], "v": initial["v"]},
           "motion_residual_mps": ref["residual"].view(-1, 2).tolist(),
           "displacement_km": disp, "fourier_mean": ref["mean"].tolist(),
           "fourier_log_std": ref["log_std"].tolist(), "occupancy": geo}
    (FIX_DIR / "split_lineage.json").write_text(json.dumps(fix) + "\n")
    fixtures.append({"name": "split_lineage", "file": "split_lineage.json", "note": fix["note"],
                     "origin": fix["origin"], "valid_rows": 13})

    # synthetic longitude-wrap case: shift stem to the prime meridian
    wrapped = copy.deepcopy(base[:8])
    for e in wrapped:
        lon = e["centroid"][1] - 268.0
        e["centroid"] = [e["centroid"][0], lon if lon >= 0 else lon + 360.0]
        e["bbox"] = [[lat, (ln - 268.0) if (ln - 268.0) >= 0 else (ln - 268.0) + 360.0]
                     for lat, ln in e["bbox"]]
    t = tensors_for_index(wrapped, len(wrapped) - 1)
    ref = run_reference(t, radial_model, motion_model, calibrator)
    initial = predict_motion_vector(wrapped[-1])
    disp = displacements([initial["u"], initial["v"]], ref["residual"])
    centres = torch.tensor([[[d["east_km"], d["north_km"]] for d in disp]])
    geo = occupancy_and_polygons(ref["samples"], centres, calibrator,
                                 centroid_latlon=wrapped[-1]["centroid"])
    fix = {"name": "longitude_wrap", "synthetic": True,
           "note": "100083 stem shifted -268deg so centroids straddle 0/360; US training domain never wraps",
           "origin": entry_view(wrapped, len(wrapped) - 1), "valid_history_rows": 8,
           "inputs": {k: v.tolist() for k, v in t.items()},
           "initial_wind_mps": {"u": initial["u"], "v": initial["v"]},
           "motion_residual_mps": ref["residual"].view(-1, 2).tolist(),
           "displacement_km": disp, "fourier_mean": ref["mean"].tolist(),
           "fourier_log_std": ref["log_std"].tolist(), "occupancy": geo}
    (FIX_DIR / "longitude_wrap.json").write_text(json.dumps(fix) + "\n")
    fixtures.append({"name": "longitude_wrap", "file": "longitude_wrap.json", "note": fix["note"],
                     "origin": fix["origin"], "valid_rows": 8})

    (FIX_DIR / "index.json").write_text(json.dumps({
        "generator": "models/stormprob parity fixture set (Phase 0); reproduce with the frozen script",
        "ensemble_samples": 20, "ensemble_seed": 42, "threshold": 0.25,
        "grid": {"half_width_km": 100.0, "resolution_km": 1.0},
        "float_dtype": "float32",
        "note": ("decision masks stored as packbits base64 at the contracted 0.25 contour; "
                 "full float occupancy grids and member radii are reproducible from inputs + "
                 "pinned seed via the generator; Fourier mean/log_std and motion residuals stored exact"),
        "fixtures": fixtures}, indent=2) + "\n")
    print(json.dumps({"manifest": "ok", "fixtures": [f["name"] for f in fixtures]}, indent=2))


if __name__ == "__main__":
    main()
