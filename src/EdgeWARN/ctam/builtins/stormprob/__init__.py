"""Host-owned StormProb CTAM built-in.

The adapter is intentionally small: SQLite supplies the committed feature
window, ONNX Runtime supplies the two model outputs, and the versioned
postprocessor owns geometry.  A model failure is recorded on the cell and
never prevents other cells or external CTAM modules from running.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from EdgeWARN.alerts import AlertManager
from EdgeWARN.alerts.schema import AlertPayload
from EdgeWARN.stormprob import assets, onnx_runtime, postprocess
from EdgeWARN.stormprob.database import LEADS, StormProbRepository
from EdgeWARN.stormprob.envelope import geojson_to_alert_ring, swept_envelope_0_30
from EdgeWARN.stormprob.features import predict_initial_wind


MODEL_VERSION = "stormprob/v1"


class InputNotReady(ValueError):
    """The committed current observation failed the input coverage gate."""


def _utc(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None
            else moment.astimezone(timezone.utc))


def _geojson_polygon(cell: dict[str, Any]) -> Any:
    return cell.get("polygon") or cell.get("bbox")


class StormProbCycleService:
    """Host boundary for database reads and alert persistence."""

    def __init__(self, repository: StormProbRepository | None = None):
        self.repository = repository or StormProbRepository()

    def model_inputs(self, cell_id: Any, analysis_time: Any) -> dict:
        return self.repository.model_inputs(cell_id, through=analysis_time)

    @staticmethod
    def previous_alert(cell_id: Any) -> AlertPayload | None:
        return AlertManager.load("StormProb", cell_id)

    @staticmethod
    def publish(alerts: list[AlertPayload]) -> int:
        return AlertManager.publish_many(alerts)


class BuiltinStormProbAdapter:
    module_id = "stormprob"
    name = "StormProb"

    def __init__(self, service: StormProbCycleService | None = None):
        self._service = service or StormProbCycleService()
        self._sessions = None

    def _prepare(self, cell: dict[str, Any]) -> tuple[dict, dict]:
        timestamp = cell.get("timestamp")
        if not timestamp:
            raise ValueError("missing-analysis-time")
        inputs = self._service.model_inputs(cell.get("id"), timestamp)
        observation = self._service.repository.feature_history(
            cell.get("id"), limit=1, through=timestamp)[0]
        if (observation["analysis_time"] != _utc(timestamp).isoformat(timespec="microseconds")
                or not observation["inference_ready"]):
            raise InputNotReady("committed-observation-not-ready")
        return inputs, observation

    def _load_models(self):
        if self._sessions is None:
            model_dir = assets.asset_dir()
            self._sessions = onnx_runtime.load_sessions(
                model_dir, assets.manifest_path())
        return onnx_runtime.load_calibrator(assets.asset_dir(), assets.manifest_path())

    def _finish(self, cell: dict[str, Any], inputs: dict, observation: dict,
                outputs: dict, calibrator: dict) -> dict[str, Any]:
        timestamp = cell["timestamp"]
        initial = predict_initial_wind({"wind_field": {
            key.rsplit(".", 1)[1]: value
            for key, value in observation["raw_values"].items()
            if key.startswith("wind_field.")}})
        displacement = postprocess.displacement(
            [initial["u"], initial["v"]], outputs["residual_motion_mps"])
        radii = postprocess.sample_radii(outputs["coefficient_mean"],
            outputs["coefficient_log_std"],
            np.asarray(inputs["radial_history"])[-1:])
        original_polygon = cell.get("bbox")
        if original_polygon is None:
            geometry = cell.get("stormprob", {}).get("geometry", {})
            original_polygon = geometry.get("polygon_full")
        if original_polygon is None:
            raise ValueError("missing-original-polygon")
        analysis = _utc(timestamp)
        forecasts = []
        for index, lead in enumerate(LEADS):
            east, north = map(float, displacement[0, index])
            lat, lon = map(float, cell["centroid"])
            lon += east / (111.0 * math.cos(math.radians(lat)))
            lat += north / 111.0
            polygon = postprocess.operational_envelope(
                original_polygon, cell["centroid"], displacement[0, index],
                radii[0, :, index], calibrator, index)
            forecasts.append({
                "cell_id": str(cell["id"]), "analysis_time": timestamp,
                "lead_minutes": lead, "model_version": MODEL_VERSION,
                "valid_time": (analysis + timedelta(minutes=lead)).isoformat(),
                "radial_checkpoint_id": "radial-v7", "motion_checkpoint_id": "motion-best",
                "status": "ok", "reason": None,
                "east_km": east, "north_km": north,
                "predicted_centroid": [lat, lon], "polygon": polygon,
                "probability_threshold": postprocess.THRESHOLD,
                "metadata": {"geometry_kind": postprocess.OPERATIONAL_GEOMETRY_VERSION,
                              "buffer_km": postprocess.OPERATIONAL_BUFFER_KM,
                              "point_count": len(polygon["coordinates"][0]) - 1,
                              "valid_time": (analysis + timedelta(minutes=lead)).isoformat(),
                              "postprocess_version": postprocess.VERSION},
            })
        return {"status": "success", "model_version": MODEL_VERSION,
                "analysis_time": timestamp, "leads": forecasts,
                "initial_wind_mps": [initial["u"], initial["v"]]}

    def run(self, cell: dict[str, Any]) -> None:
        self.run_batch([cell])

    @staticmethod
    def _record_failure(cell: dict[str, Any], exc: Exception, started: float) -> None:
        cell.setdefault("modules", {})
        status = "skipped" if isinstance(exc, InputNotReady) else "error"
        cell["modules"]["StormProb"] = {
            "status": status, "reason": str(exc) if status == "skipped" else "inference-failed",
            "error": str(exc), "inference_duration_ms": (time.perf_counter() - started) * 1000,
            "analysis_time": cell.get("timestamp"), "model_version": MODEL_VERSION,
            "leads": [{"cell_id": str(cell.get("id")),
                       "analysis_time": cell.get("timestamp"),
                       "valid_time": ((
                           _utc(cell["timestamp"]) + timedelta(minutes=lead)
                       ).isoformat() if cell.get("timestamp") else None),
                       "lead_minutes": lead, "model_version": MODEL_VERSION,
                       "status": status, "reason": str(exc),
                       "metadata": {"geometry_kind": "instantaneous-probability-contour"}}
                      for lead in LEADS]}

    def run_batch(self, cells: list[dict[str, Any]]) -> None:
        """Infer up to 128 ready cells together, preserving per-cell outcomes."""
        if len(cells) > onnx_runtime.BATCH_SIZE:
            raise ValueError("StormProb batch exceeds graph capacity")
        fields = {
            "radial_history": ("radial_history", (30, 64), np.float32),
            "statistics_history": ("radial_statistics_history", (30, 1), np.float32),
            "current_features": ("current", (135,), np.float32),
            "history_mask": ("radial_history_mask", (30,), np.bool_),
            "history_sequence": ("history_sequence", (30, 135), np.float32),
            "trajectory_sequence": ("trajectory_sequence", (30, 16), np.float32),
            "trajectory_mask": ("trajectory_mask", (30,), np.bool_),
        }
        prepared = []
        for cell in cells:
            cell.setdefault("modules", {})
            started = time.perf_counter()
            try:
                inputs, observation = self._prepare(cell)
                for name, (source, shape, dtype) in fields.items():
                    actual = np.asarray(inputs[source], dtype=dtype).shape
                    if actual != shape:
                        raise ValueError(f"{name} shape {actual}; expected {shape}")
                prepared.append((cell, inputs, observation, started))
            except Exception as exc:
                self._record_failure(cell, exc, started)
        if not prepared:
            return
        try:
            calibrator = self._load_models()
            tensors = {}
            for name, (source, shape, dtype) in fields.items():
                batch = np.zeros((onnx_runtime.BATCH_SIZE, *shape), dtype=dtype)
                for index, (_, inputs, _, _) in enumerate(prepared):
                    batch[index] = np.asarray(inputs[source], dtype=dtype)
                tensors[name] = batch
            outputs = onnx_runtime.infer_pair(*self._sessions, **tensors)
        except Exception as exc:
            for cell, _, _, started in prepared:
                self._record_failure(cell, exc, started)
            return
        for index, (cell, inputs, observation, started) in enumerate(prepared):
            try:
                one = {name: value[index:index + 1] for name, value in outputs.items()}
                result = self._finish(cell, inputs, observation, one, calibrator)
                result["inference_duration_ms"] = (time.perf_counter() - started) * 1000
                for lead in result["leads"]:
                    lead["metadata"]["inference_duration_ms"] = result["inference_duration_ms"]
                cell["modules"][self.name] = result
            except Exception as exc:
                self._record_failure(cell, exc, started)

    def alerts(self, cell: dict[str, Any]) -> list[AlertPayload]:
        result = cell.get("modules", {}).get(self.name, {})
        if result.get("status") != "success":
            return []
        leads = {int(item["lead_minutes"]): item for item in result["leads"]}
        sweep = swept_envelope_0_30(current_polygon=_geojson_polygon(cell),
                                    lead_15=leads[15].get("polygon"),
                                    lead_30=leads[30].get("polygon"))
        result["alert_geometry"] = sweep
        if sweep.get("status") != "ok":
            return []
        effective = _utc(cell["timestamp"])
        previous = self._service.previous_alert(cell.get("id", "unknown_cell"))
        if previous and effective < previous.effective_time + timedelta(minutes=15):
            return []
        return [AlertPayload("TSTM", self.name, str(cell.get("id", "unknown_cell")),
            geojson_to_alert_ring(sweep["geometry"]), effective,
            effective + timedelta(minutes=30), threats={"geometry_kind": sweep["geometry_kind"]})]

    def publish_alerts(self, alerts: list[AlertPayload]) -> int:
        return self._service.publish(alerts)


__all__ = ["BuiltinStormProbAdapter", "StormProbCycleService", "MODEL_VERSION"]
