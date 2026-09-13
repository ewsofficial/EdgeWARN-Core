"""Phase 5 promotion, audit, and first-frame forecast regressions."""
from datetime import datetime, timezone
import json

import numpy as np
import pytest

from EdgeWARN.ctam.builtins.stormprob import BuiltinStormProbAdapter, StormProbCycleService
from EdgeWARN.stormprob import onnx_runtime
from EdgeWARN.stormprob import postprocess
from EdgeWARN.stormprob.audit import audit
from EdgeWARN.stormprob.database import StormProbRepository
from EdgeWARN.stormprob.deployment import mode, promoted
from EdgeWARN.stormprob.records import build_observation_record


def _cell(when="2024-05-01T12:00:00", *, wind=True):
    cell = {"id": 101, "timestamp": when, "centroid": [35.0, 265.0],
            "bbox": [[34.99, 264.99], [34.99, 265.01],
                     [35.01, 265.01], [35.01, 264.99]],
            "properties": {"wind_field": {"u850": 5.0, "v850": 2.0} if wind else {}}}
    cell["stormprob"] = {"observation": build_observation_record(cell)}
    return cell


def test_shadow_is_default_and_promotion_is_explicit(monkeypatch):
    monkeypatch.delenv("STORMPROB_MODE", raising=False)
    assert mode() == "shadow" and not promoted()
    monkeypatch.setenv("STORMPROB_MODE", "promoted")
    assert promoted()
    monkeypatch.setenv("STORMPROB_MODE", "rollback")
    assert mode() == "rollback" and not promoted()
    monkeypatch.setenv("STORMPROB_MODE", "typo")
    with pytest.raises(ValueError, match="STORMPROB_MODE"):
        mode()


def test_audit_missing_database_is_a_failed_gate(tmp_path):
    assert audit(StormProbRepository(tmp_path))["failures"] == ["database-not-found"]


def test_rollback_runs_legacy_adapter_and_clears_stale_stormprob(tmp_path, monkeypatch):
    import util.file as fs
    from EdgeWARN.alerts import AlertManager
    from EdgeWARN.ctam import run as ctam_run

    monkeypatch.setattr(fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(fs, "CELL_DIR", tmp_path / "data" / "cells")
    monkeypatch.setattr(fs, "EDGEWARN_ALERTS_IDS_DIR", tmp_path / "data" / "alerts" / "ids")
    monkeypatch.setattr(fs, "EDGEWARN_ALERTS_TS_DIR", tmp_path / "data" / "alerts" / "timestamps")
    monkeypatch.setattr(AlertManager, "publish_many", lambda alerts: len(alerts))
    cell = _cell()
    cell["dx"], cell["dy"], cell["dt"] = 1500.0, 800.0, 120.0
    cell["properties"]["wind_field"].update({
        "u700": 6.0, "v700": 2.0, "u500": 7.0, "v500": 3.0,
        "u250": 8.0, "v250": 4.0})
    cell["modules"] = {"StormProb": {"status": "success", "leads": []}}
    success, errors, alerts = ctam_run._run_builtin_stormcast_rollback([cell])
    assert "StormProb" not in cell["modules"]
    assert cell["modules"]["StormCast"]["status"] in {"success", "skipped"}, cell["modules"]["StormCast"]
    assert errors == 0


def test_shadow_suppresses_alerts_and_tracking_control(monkeypatch):
    from EdgeWARN.ctam import run as ctam_run
    from EdgeWARN.ctam import builtins
    from EdgeWARN.process.detect.track import StormCellTracker

    calls = []
    class FakeAdapter:
        name = "StormProb"
        def __init__(self, service):
            pass
        def run(self, cell):
            cell["modules"][self.name] = {"status": "success", "leads": [
                {"lead_minutes": 15, "east_km": 4.5, "north_km": 1.8}]}
        def alerts(self, cell):
            calls.append("alerts")
            return []
        def publish_alerts(self, alerts):
            calls.append("publish")
            return 0

    monkeypatch.setattr(builtins, "BuiltinStormProbAdapter", FakeAdapter)
    monkeypatch.setattr(builtins, "StormProbCycleService", lambda: None)
    cell = _cell()
    monkeypatch.delenv("STORMPROB_MODE", raising=False)
    ctam_run._run_builtin_stormprob([cell])
    assert calls == []
    assert StormCellTracker._get_stormprob_velocity(None, cell) == (None, None)
    monkeypatch.setenv("STORMPROB_MODE", "promoted")
    ctam_run._run_builtin_stormprob([cell])
    assert calls == ["alerts", "publish"]
    assert StormCellTracker._get_stormprob_velocity(None, cell) == pytest.approx((5, 2))
    monkeypatch.setenv("STORMPROB_MODE", "rollback")
    cell["modules"]["StormCast"] = {"status": "success", "u": 7.0, "v": -3.0}
    assert StormCellTracker._get_stormprob_velocity(None, cell) == (7.0, -3.0)


def test_shadow_projection_hides_forecast_but_database_keeps_it(tmp_path, monkeypatch):
    import util.file as fs
    from EdgeWARN.process.integrate import pipeline

    monkeypatch.delenv("STORMPROB_MODE", raising=False)
    monkeypatch.setattr(fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(fs, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(fs, "CELL_DIR", tmp_path / "data" / "cells")
    monkeypatch.setattr(fs, "STORMCELL_DIR", tmp_path / "data" / "stormcells")
    cell = _cell()
    cell["modules"] = {"StormProb": {"status": "skipped", "leads": [
        {"cell_id": "101", "analysis_time": cell["timestamp"],
         "lead_minutes": lead, "model_version": "stormprob/v1",
         "status": "skipped", "reason": "input-not-ready"}
        for lead in (15, 30, 45, 60)]}}
    snapshot = fs.STORMCELL_DIR / "stormcells_20240501-120000.json"
    pipeline._publish_cycle(None, cell["timestamp"], [cell], snapshot, False)
    public = json.loads(snapshot.read_text())["features"][0]
    assert "StormProb" not in public.get("modules", {})
    with StormProbRepository(tmp_path).reader() as db:
        assert db.execute("SELECT count(*) FROM forecasts WHERE model_version='stormprob/v1'").fetchone()[0] == 4


def test_audit_flags_systematic_missingness_and_future_source(tmp_path):
    repo = StormProbRepository(tmp_path)
    cell = _cell()
    cell["stormprob"]["observation"] = build_observation_record(
        cell, source_times={"rap": "2024-05-01T13:00:00"})
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    report = audit(repo, cycle_limit=1)
    assert report["observations"] == 1
    assert report["missing_fraction"]["MUCAPE"] == 1
    assert report["missing_fraction"]["wind_field.u850"] == 0
    assert "source-future:rap" in report["failures"]
    assert not report["passed"]


def test_unready_committed_row_skips_before_model_load(tmp_path, monkeypatch):
    repo = StormProbRepository(tmp_path)
    cell = _cell(wind=False)
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    monkeypatch.setattr(onnx_runtime, "load_sessions", lambda *args: pytest.fail("model loaded"))
    BuiltinStormProbAdapter(StormProbCycleService(repo)).run(cell)
    assert cell["modules"]["StormProb"]["status"] == "skipped"
    assert "committed-observation-not-ready" in cell["modules"]["StormProb"]["error"]
    assert len(cell["modules"]["StormProb"]["leads"]) == 4


def test_first_frame_four_leads_from_committed_features(tmp_path, monkeypatch):
    repo = StormProbRepository(tmp_path)
    cell = _cell()
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    assert sum(repo.model_inputs(101)["history_mask"]) == 1
    monkeypatch.setattr(onnx_runtime, "load_sessions", lambda *args: (object(), object()))
    monkeypatch.setattr(onnx_runtime, "load_calibrator", lambda *args: {
        "parameters": [{"calibrated_probabilities": [i / 20 for i in range(21)]}
                       for _ in range(4)]})
    monkeypatch.setattr(onnx_runtime, "infer_pair", lambda *args, **kwargs: {
        "coefficient_mean": np.zeros((1, 4, 33), np.float32),
        "coefficient_log_std": np.full((1, 4, 33), -20, np.float32),
        "residual_motion_mps": np.zeros((1, 4, 2), np.float32)})
    adapter = BuiltinStormProbAdapter(StormProbCycleService(repo))
    adapter.run(cell)
    result = cell["modules"]["StormProb"]
    assert result["status"] == "success"
    assert [lead["lead_minutes"] for lead in result["leads"]] == [15, 30, 45, 60]
    assert [lead["east_km"] for lead in result["leads"]] == pytest.approx([4.5, 9, 13.5, 18])
    assert all(lead["status"] == "ok" for lead in result["leads"])
    assert all(lead["polygon"]["type"] == "MultiPolygon" for lead in result["leads"])
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


def test_alert_refresh_cadence_uses_swept_envelope():
    class Service:
        previous = None
        def previous_alert(self, cell_id):
            return self.previous

    service = Service()
    adapter = BuiltinStormProbAdapter(service)
    cell = _cell()
    polygon = {"type": "Polygon", "coordinates": [[
        [264.99, 34.99], [265.01, 34.99], [265.01, 35.01],
        [264.99, 35.01], [264.99, 34.99]]]}
    cell["modules"] = {"StormProb": {"status": "success", "leads": [
        {"lead_minutes": 15, "polygon": polygon},
        {"lead_minutes": 30, "polygon": polygon}]}}
    first = adapter.alerts(cell)
    assert len(first) == 1
    assert cell["modules"]["StormProb"]["alert_geometry"]["geometry_kind"] == "swept-envelope-0-30min"
    service.previous = first[0]
    cell["timestamp"] = "2024-05-01T12:14:59"
    assert adapter.alerts(cell) == []
    cell["timestamp"] = "2024-05-01T12:15:00"
    assert len(adapter.alerts(cell)) == 1
