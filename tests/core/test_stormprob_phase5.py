"""Production StormProb forecast, publication, and alert regressions."""
from datetime import datetime, timezone

import numpy as np

from EdgeWARN.ctam.builtins.stormprob import BuiltinStormProbAdapter, StormProbCycleService
from EdgeWARN.stormprob import onnx_runtime, postprocess
from EdgeWARN.stormprob.audit import audit
from EdgeWARN.stormprob.database import StormProbRepository
from EdgeWARN.stormprob.records import build_observation_record


def _cell(when="2024-05-01T12:00:00", *, wind=True):
    cell = {"id": 101, "timestamp": when, "centroid": [35.0, 265.0],
            "bbox": [[34.99, 264.99], [34.99, 265.01],
                     [35.01, 265.01], [35.01, 264.99]],
            "properties": {"wind_field": {"u850": 5.0, "v850": 2.0} if wind else {}}}
    cell["stormprob"] = {"observation": build_observation_record(cell)}
    return cell


def test_audit_missing_database_is_a_failed_gate(tmp_path):
    assert audit(StormProbRepository(tmp_path))["failures"] == ["database-not-found"]


def test_unready_committed_row_skips_before_model_load(tmp_path, monkeypatch):
    repo = StormProbRepository(tmp_path)
    cell = _cell(wind=False)
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    monkeypatch.setattr(onnx_runtime, "load_sessions", lambda *args: (_ for _ in ()).throw(AssertionError("model loaded")))
    BuiltinStormProbAdapter(StormProbCycleService(repo)).run(cell)
    assert cell["modules"]["StormProb"]["status"] == "skipped"
    assert len(cell["modules"]["StormProb"]["leads"]) == 4


def test_first_frame_produces_four_leads_from_committed_features(tmp_path, monkeypatch):
    repo = StormProbRepository(tmp_path)
    cell = _cell()
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    assert sum(repo.model_inputs(101)["history_mask"]) == 1
    monkeypatch.setattr(onnx_runtime, "load_sessions", lambda *args: (object(), object()))
    monkeypatch.setattr(onnx_runtime, "load_calibrator", lambda *args: {
        "parameters": [{"calibrated_probabilities": [i / 20 for i in range(21)]} for _ in range(4)]})
    monkeypatch.setattr(onnx_runtime, "infer_pair", lambda *args, **kwargs: {
        "coefficient_mean": np.zeros((1, 4, 33), np.float32),
        "coefficient_log_std": np.full((1, 4, 33), -20, np.float32),
        "residual_motion_mps": np.zeros((1, 4, 2), np.float32)})
    BuiltinStormProbAdapter(StormProbCycleService(repo)).run(cell)
    result = cell["modules"]["StormProb"]
    assert result["status"] == "success"
    assert [lead["lead_minutes"] for lead in result["leads"]] == [15, 30, 45, 60]
    assert [lead["east_km"] for lead in result["leads"]] == [4.5, 9.0, 13.5, 18.0]
    assert result["leads"][0]["metadata"]["valid_time"] == datetime(
        2024, 5, 1, 12, 15, tzinfo=timezone.utc).isoformat()


def test_polygon_wrap_preserves_closed_public_rings():
    masks = np.zeros((4, 201, 201), dtype=np.bool_)
    masks[0, 98:103, 98:103] = True
    polygon = postprocess.polygons_from_masks(masks, [35, 0.01])[0]["geometry"]
    assert polygon["type"] == "MultiPolygon"
    for part in polygon["coordinates"]:
        for ring in part:
            assert ring[0] == ring[-1]
            assert all(0 <= lon <= 360 and -90 <= lat <= 90 for lon, lat in ring)


def test_alert_is_labeled_as_a_swept_envelope():
    class Service:
        def previous_alert(self, cell_id):
            return None

    polygon = {"type": "Polygon", "coordinates": [[[264.99, 34.99], [265.01, 34.99],
        [265.01, 35.01], [264.99, 35.01], [264.99, 34.99]]]}
    cell = _cell()
    cell["modules"] = {"StormProb": {"status": "success", "leads": [
        {"lead_minutes": 15, "polygon": polygon}, {"lead_minutes": 30, "polygon": polygon}]}}
    alerts = BuiltinStormProbAdapter(Service()).alerts(cell)
    assert len(alerts) == 1
    assert cell["modules"]["StormProb"]["alert_geometry"]["geometry_kind"] == "swept-envelope-0-30min"
