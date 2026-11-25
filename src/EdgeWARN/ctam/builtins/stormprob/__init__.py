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
from EdgeWARN.stormprob import MODEL_VERSION, assets, field, onnx_runtime, postprocess
from EdgeWARN.stormprob.database import FULL_HISTORY_LIMIT, LEADS, StormProbRepository, _time
from EdgeWARN.stormprob.features import HISTORY_STEPS
from EdgeWARN.stormprob.envelope import geojson_to_alert_ring, swept_envelope_0_30
from EdgeWARN.stormprob.features import predict_initial_wind


RADIAL_CHECKPOINT_ID = "radial-joint-e7"
MOTION_CHECKPOINT_ID = "motion-residual-e16"


class InputNotReady(ValueError):
    """The committed current observation failed the input coverage gate."""


def _utc(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None
            else moment.astimezone(timezone.utc))


def _geojson_polygon(cell: dict[str, Any]) -> Any:
    return cell.get("polygon") or cell.get("bbox")


class _CycleHistoryRepository:
    """Repository view that reads each cell's committed track once per cycle.

    The cycle's preflight and the adapter's prepare step each asked SQLite for
    ``model_inputs`` (a full-track read) and a ``feature_history`` window, so
    every candidate cell paid four queries and connection opens. Inputs are
    committed before CTAM runs and nothing in between writes feature rows, so
    the full track read once serves every window: ``feature_history`` orders
    rows newest first with unique analysis times, and a ``limit`` is a prefix
    of that order. Everything else is delegated unchanged.
    """

    def __init__(self, repository: StormProbRepository):
        self._repository = repository
        self._tracks: dict[tuple[str, str | None], list[dict]] = {}
        self._inputs: dict[tuple[str, str | None], dict] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    @staticmethod
    def _key(cell_id: Any, through: Any | None) -> tuple[str, str | None]:
        return (str(cell_id), None if through is None else _time(through))

    def _track(self, cell_id: Any, through: Any | None) -> list[dict]:
        key = self._key(cell_id, through)
        rows = self._tracks.get(key)
        if rows is None:
            rows = self._repository.feature_history(
                cell_id, limit=FULL_HISTORY_LIMIT, through=through)
            self._tracks[key] = rows
        return rows

    def feature_history(self, cell_id: Any, limit: int | None = None,
                        before: Any | None = None,
                        through: Any | None = None) -> list[dict]:
        limit = HISTORY_STEPS if limit is None else limit
        if before is not None or limit > FULL_HISTORY_LIMIT:
            return self._repository.feature_history(
                cell_id, limit=limit, before=before, through=through)
        if limit < 1:
            return []
        return self._track(cell_id, through)[:limit]

    def model_inputs(self, cell_id: Any, *, through: Any | None = None) -> dict:
        # Preflight and prepare both ask for each cell's inputs. Callers only
        # read the result, so the memoized dict itself is returned.
        key = self._key(cell_id, through)
        inputs = self._inputs.get(key)
        if inputs is None:
            inputs = self._repository.model_inputs(
                cell_id, through=through, history_rows=self._track(cell_id, through))
            self._inputs[key] = inputs
        return inputs


class StormProbCycleService:
    """Host boundary for database reads and alert persistence.

    A service lives for one CTAM cycle; its repository view memoizes each
    cell's committed history for that cycle.
    """

    def __init__(self, repository: StormProbRepository | None = None):
        self.repository = _CycleHistoryRepository(repository or StormProbRepository())
        self._previous_alerts: dict[str, AlertPayload] | None = None

    def model_inputs(self, cell_id: Any, analysis_time: Any) -> dict:
        return self.repository.model_inputs(cell_id, through=analysis_time)

    def preload_previous_alerts(self, cell_ids) -> None:
        self._previous_alerts = AlertManager.load_latest_for_cells(
            "StormProb", cell_ids
        )

    def previous_alert(self, cell_id: Any) -> AlertPayload | None:
        if self._previous_alerts is not None:
            return self._previous_alerts.get(str(cell_id))
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
        self._field_seconds = 0.0

    def _prepare(self, cell: dict[str, Any]) -> tuple[dict, dict]:
        if cell.get("tracking_mode") == "predicted":
            raise InputNotReady("predicted-track-no-current-observation")
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

    def _load_models(self) -> None:
        if self._sessions is None:
            self._sessions = onnx_runtime.load_sessions(
                assets.asset_dir(), assets.manifest_path())

    def _finish(self, cell: dict[str, Any], inputs: dict, observation: dict,
                outputs: dict) -> dict[str, Any]:
        timestamp = cell["timestamp"]
        initial = predict_initial_wind({"wind_field": {
            key.rsplit(".", 1)[1]: value
            for key, value in observation["raw_values"].items()
            if key.startswith("wind_field.")}})
        base = postprocess.base_displacement(
            [initial["u"], initial["v"]], outputs["residual_motion_mps"])
        radii, centres = postprocess.sample_joint(
            outputs["coefficient_mean"], outputs["coefficient_log_std"],
            np.asarray(inputs["radial_history"])[-1:], base,
            outputs["centroid_offset_km"], outputs["centroid_cholesky"])
        probability = postprocess.occupancy_probability(radii, centres)
        mean_km = base + outputs["centroid_offset_km"]
        cholesky = outputs["centroid_cholesky"][0]
        # Marginal std of L @ L.T: east = |l11|, north = sqrt(l21^2 + l22^2).
        centroid_std = np.stack((np.abs(cholesky[:, 0, 0]),
                                 np.hypot(cholesky[:, 1, 0], cholesky[:, 1, 1])), axis=-1)
        # These are the same committed PS polygon and PS-only centroid used
        # to construct the radial history supplied to the model.
        original_polygon = observation["polygon"]
        original_centroid = observation["centroid"]
        if original_polygon is None or original_centroid is None:
            raise ValueError("missing-original-ps-geometry")
        # The swept field is auxiliary: a raster failure is recorded on the cell
        # and never changes a lead, an alert, or a tracked cell.
        field_started = time.perf_counter()
        field_status = "ok"
        field_error = None
        target = cell.setdefault("stormprob", {})
        try:
            if not isinstance(target, dict):
                raise ValueError("invalid-stormprob-cell-record")
            target["field_patch"] = field.cell_patch(
                radii, centres, original_centroid, polygon_latlon=original_polygon)
        except Exception as exc:
            field_status = "error"
            field_error = str(exc) or type(exc).__name__
            if isinstance(target, dict):
                target["field_error"] = field_error
        self._field_seconds = (getattr(self, "_field_seconds", 0.0)
                               + time.perf_counter() - field_started)
        analysis = _utc(timestamp)
        forecasts = []
        centroid_lat, centroid_lon = map(float, original_centroid)
        for index, lead in enumerate(LEADS):
            east, north = map(float, mean_km[0, index])
            lon = centroid_lon + east / (111.0 * math.cos(math.radians(centroid_lat)))
            lat = centroid_lat + north / 111.0
            valid_time = (analysis + timedelta(minutes=lead)).isoformat()
            common = {
                "cell_id": str(cell["id"]), "analysis_time": timestamp,
                "lead_minutes": lead, "model_version": MODEL_VERSION,
                "valid_time": valid_time,
                "radial_checkpoint_id": RADIAL_CHECKPOINT_ID,
                "motion_checkpoint_id": MOTION_CHECKPOINT_ID,
            }
            metadata = {"postprocess_version": postprocess.VERSION,
                        "centroid_std_km": [float(v) for v in centroid_std[index]]}
            try:
                contour = postprocess.contour_polygon(probability[index])
                if contour is None:
                    forecasts.append({**common, "status": "no-polygon",
                                      "reason": "empty-0.25-contour",
                                      "east_km": east, "north_km": north,
                                      "predicted_centroid": [lat, lon],
                                      "probability_threshold": postprocess.THRESHOLD,
                                      "metadata": metadata})
                    continue
                polygon = postprocess.operational_envelope(
                    original_polygon, original_centroid, contour)
            except (ValueError, ArithmeticError) as exc:
                forecasts.append({**common, "status": "error",
                                  "reason": "geometry-failed", "error": str(exc),
                                  "metadata": metadata})
                continue
            forecasts.append({
                **common, "status": "ok", "reason": None,
                "east_km": east, "north_km": north,
                "predicted_centroid": [lat, lon], "polygon": polygon,
                "probability_threshold": postprocess.THRESHOLD,
                "metadata": {**metadata,
                             "geometry_kind": postprocess.OPERATIONAL_GEOMETRY_VERSION,
                             "buffer_km": postprocess.OPERATIONAL_BUFFER_KM,
                             "point_count": len(polygon["coordinates"][0]) - 1,
                             "valid_time": valid_time},
            })
        status = "success" if all(lead["status"] == "ok" for lead in forecasts) else "partial"
        return {"status": status, "model_version": MODEL_VERSION,
                "analysis_time": timestamp, "leads": forecasts,
                "field_status": field_status, "field_error": field_error,
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
        batch_started = time.perf_counter()
        self._field_seconds = 0.0
        fields = {
            "radial_history": ("radial_history", (30, 64), np.float32),
            "statistics_history": ("radial_statistics_history", (30, 1), np.float32),
            "current_features": ("current", (135,), np.float32),
            "history_mask": ("radial_history_mask", (30,), np.bool_),
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
        prepared_at = time.perf_counter()
        if not prepared:
            self.last_batch_timing = {
                "prepare_seconds": prepared_at - batch_started,
                "inference_seconds": 0.0,
                "postprocess_seconds": 0.0,
                "field_seconds": self._field_seconds,
            }
            return
        try:
            self._load_models()
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
            self.last_batch_timing = {
                "prepare_seconds": prepared_at - batch_started,
                "inference_seconds": time.perf_counter() - prepared_at,
                "postprocess_seconds": 0.0,
                "field_seconds": self._field_seconds,
            }
            return
        inferred_at = time.perf_counter()
        for index, (cell, inputs, observation, started) in enumerate(prepared):
            finish_started = time.perf_counter()
            try:
                one = {name: value[index:index + 1] for name, value in outputs.items()}
                result = self._finish(cell, inputs, observation, one)
                result["inference_duration_ms"] = (time.perf_counter() - started) * 1000
                for lead in result["leads"]:
                    lead["metadata"]["inference_duration_ms"] = result["inference_duration_ms"]
                cell["modules"][self.name] = result
            except Exception as exc:
                self._record_failure(cell, exc, started)
            finally:
                print(
                    "[StormProb] _finish "
                    f"cell_id={cell.get('id')!r} "
                    f"elapsed={time.perf_counter() - finish_started:.3f}s"
                )
        self.last_batch_timing = {
            "prepare_seconds": prepared_at - batch_started,
            "inference_seconds": inferred_at - prepared_at,
            "postprocess_seconds": time.perf_counter() - inferred_at,
            "field_seconds": self._field_seconds,
        }

    def alerts(self, cell: dict[str, Any]) -> list[AlertPayload]:
        result = cell.get("modules", {}).get(self.name, {})
        if result.get("status") not in {"success", "partial"}:
            return []
        leads = {int(item["lead_minutes"]): item for item in result["leads"]}
        if any(leads.get(lead, {}).get("status") != "ok" for lead in (15, 30)):
            return []
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
