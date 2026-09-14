"""Phase 5 regression: a built-in forecast survives into the next track cycle."""
from __future__ import annotations

from EdgeWARN.ctam.builtins.stormprob import BuiltinStormProbAdapter
from EdgeWARN.process.detect.kalman import default_tracking_config
from EdgeWARN.process.detect.track import StormCellTracker


class _IO:
    def write_info(self, message): pass
    def write_debug(self, message): pass
    def write_warning(self, message): pass
    def write_error(self, message): pass


def test_stormprob_cycle_n_history_drives_cycle_n_plus_1_tracker():
    """The published history entry retains StormProb velocity for tracking."""
    cycle_n = {
        "id": 901,
        "timestamp": "2026-08-05T12:00:00+00:00",
        "centroid": [35.25, 262.75],
        "bbox": [[35.2, 262.7], [35.3, 262.8]],
        "num_gates": 100,
        "max_refl": 55.0,
        "tracking_mode": "active",
        "prediction_count": 0,
        "confidence": 1.0,
        "dx": 500.0,
        "dy": 250.0,
        "dt": 300.0,
        "properties": {
            "p100EchoTop30": 10.0,
            "EchoTop50": 8.0,
            "wind_field": {
                "u850": 12.0, "v850": 4.0,
                "u700": 14.0, "v700": 5.0,
                "u500": 18.0, "v500": 7.0,
                "u250": 22.0, "v250": 9.0,
            },
        },
        "modules": {},
    }
    adapter = BuiltinStormProbAdapter.__new__(BuiltinStormProbAdapter)
    adapter._sessions = None
    def forecast(cell):
        return {
        "status": "success", "model_version": "stormprob/v1",
        "analysis_time": cell["timestamp"], "leads": [
            {"lead_minutes": lead, "east_km": 3.0, "north_km": 1.5,
             "status": "ok", "metadata": {}} for lead in (15, 30, 45, 60)]}
    adapter.run_batch = lambda cells: [cell["modules"].update(StormProb=forecast(cell)) for cell in cells]
    BuiltinStormProbAdapter.run(adapter, cycle_n)
    assert cycle_n["modules"]["StormProb"]["status"] == "success"

    cycle_n["modules"]["StormProb"]["leads"] = [
        {"lead_minutes": 15, "east_km": 3.0, "north_km": 1.5}]
    cycle_n["modules"]["StormProb"]["status"] = "success"
    history_file_payload = [cycle_n]
    tracker = StormCellTracker(
        ps_old=None,
        ps_new=None,
        io_manager=_IO(),
        tracking_config=default_tracking_config(),
    )
    tracker.update_cells(
        entries=history_file_payload,
        updated_data=[
            {
                "id": 901,
                "centroid": [35.26, 262.76],
                "bbox": [[35.21, 262.71], [35.31, 262.81]],
                "num_gates": 101,
                "max_refl": 56.0,
            }
        ],
        timestamp="2026-08-05T12:05:00+00:00",
        dt_seconds=300.0,
    )

    kalman = tracker._kalman_filters[901]
    forecast = cycle_n["modules"]["StormProb"]
    assert kalman.state.u == forecast["leads"][0]["east_km"] * 1000 / 900
    assert kalman.state.v == forecast["leads"][0]["north_km"] * 1000 / 900
