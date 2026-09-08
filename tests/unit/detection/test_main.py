import os
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import util.file as fs
from EdgeWARN.process.detect import main as detect_main
from EdgeWARN.process.detect.config import DetectionConfig


def test_detect_main_preserves_stormcell_dir_when_cleanup_disabled(tmp_path):
    fs._define_paths(tmp_path)
    stormcell_file = fs.STORMCELL_DIR / "stormcells_20240101-120000.json"
    fs.STORMCELL_DIR.mkdir(parents=True, exist_ok=True)
    stormcell_file.write_text("{}")

    old_time = datetime.now() - timedelta(hours=3)
    os.utime(stormcell_file, (old_time.timestamp(), old_time.timestamp()))

    detect_main.main(
        None,
        None,
        None,
        None,
        None,
        None,
        (20, 55),
        (-130, -60),
        DetectionConfig.from_yaml(),
        cleanup_stormcells=False,
    )

    assert stormcell_file.exists()


def test_detect_main_cleans_stormcell_dir_when_cleanup_enabled(tmp_path):
    fs._define_paths(tmp_path)
    stormcell_file = fs.STORMCELL_DIR / "stormcells_20240101-120000.json"
    fs.STORMCELL_DIR.mkdir(parents=True, exist_ok=True)
    stormcell_file.write_text("{}")

    old_time = datetime.now() - timedelta(hours=3)
    os.utime(stormcell_file, (old_time.timestamp(), old_time.timestamp()))

    detect_main.main(
        None,
        None,
        None,
        None,
        None,
        None,
        (20, 55),
        (-130, -60),
        DetectionConfig.from_yaml(),
        cleanup_stormcells=True,
    )

    assert not stormcell_file.exists()


def test_detect_main_single_frame_generates_vectors_from_history(tmp_path):
    fs._define_paths(tmp_path)
    fs.CELL_DIR.mkdir(parents=True, exist_ok=True)

    radar_file = tmp_path / "radar.grib2"
    radar_file.write_text("stub")

    history_path = fs.CELL_DIR / "1.json"
    history_path.write_text(json.dumps([
        {
            "timestamp": "2024-01-01T11:55:00",
            "centroid": [35.0, -97.0],
        }
    ]))

    with patch.object(detect_main.DetectionDataHandler, "find_timestamp", return_value="2024-01-01T12:00:00"), \
         patch.object(detect_main, "_detect_with_optional_probsevere", return_value=([
             {"id": 1, "centroid": [35.1, -96.9]}
         ], None)), \
         patch.object(detect_main, "match_alerts_to_cells", side_effect=lambda entries, *_args, **_kwargs: entries):
        output_file, _ = detect_main.main(
            radar_file,
            None,
            None,
            None,
            None,
            None,
            (20, 55),
            (-130, -60),
            DetectionConfig.from_yaml(),
            cleanup_stormcells=False,
        )

    saved = json.loads(output_file.read_text())
    feature = saved["features"][0]

    assert feature["timestamp"] == "2024-01-01T12:00:00"
    assert feature["dt"] == 300.0
    assert "dx" in feature
    assert "dy" in feature


def test_detect_main_dual_frame_preserves_previous_snapshot_for_vectors(tmp_path):
    fs._define_paths(tmp_path)
    fs.STORMCELL_DIR.mkdir(parents=True, exist_ok=True)

    previous_file = fs.STORMCELL_DIR / "stormcells_20240101-115800.json"
    previous_file.write_text(json.dumps({
        "latest_timestamp": "2024-01-01T11:58:00",
        "features": [
            {
                "id": 1,
                "timestamp": "2024-01-01T11:58:00",
                "centroid": [35.0, -97.0],
                "num_gates": 10,
                "max_refl": 45.0,
                "bbox": [],
            }
        ],
    }))

    radar_old = tmp_path / "radar_old.grib2"
    radar_new = tmp_path / "radar_new.grib2"
    ps_old = tmp_path / "ps_old.json"
    ps_new = tmp_path / "ps_new.json"
    pt_old = tmp_path / "pt_old.grib2"
    pt_new = tmp_path / "pt_new.grib2"

    for path in [radar_old, radar_new, ps_old, ps_new, pt_old, pt_new]:
        path.write_text("stub")

    class MockHandler:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def find_timestamp(_path):
            return "2024-01-01T12:00:00"

        def load_probsevere(self):
            return {}

    class FakeTracker:
        def __init__(self, *_args, **_kwargs):
            pass

        def detect_lineage_events(self, *_args, **_kwargs):
            return None

        def update_cells(self, entries_old, entries_new, timestamp=None, **_kwargs):
            cell = entries_old[0]
            updated = entries_new[0]
            cell["centroid"] = updated["centroid"]
            cell["num_gates"] = updated["num_gates"]
            cell["max_refl"] = updated["max_refl"]
            cell["bbox"] = updated["bbox"]
            cell["timestamp"] = timestamp
            return [cell]

        def save_lineage_buffer(self, *_args, **_kwargs):
            return True

    with patch.object(detect_main, "DetectionDataHandler", MockHandler), \
         patch.object(detect_main, "StormCellTracker", FakeTracker), \
         patch.object(detect_main.TrackingConfig, "from_yaml", return_value=None), \
         patch.object(detect_main.AssignmentConfig, "from_yaml", return_value=None), \
         patch.object(detect_main.KalmanConfig, "from_yaml", return_value=None), \
         patch.object(detect_main, "_detect_with_optional_probsevere", return_value=(
             [{"id": 1, "centroid": [35.1, -96.9], "num_gates": 12, "max_refl": 50.0, "bbox": []}],
             {},
             (None, None, None),
         )), \
         patch.object(detect_main, "match_alerts_to_cells", side_effect=lambda entries, *_args, **_kwargs: entries):
        output_file, _ = detect_main.main(
            radar_old,
            radar_new,
            ps_old,
            ps_new,
            pt_old,
            pt_new,
            (20, 55),
            (-130, -60),
            DetectionConfig.from_yaml(),
            cleanup_stormcells=False,
        )

    saved = json.loads(output_file.read_text())
    feature = saved["features"][0]

    assert feature["timestamp"] == "2024-01-01T12:00:00"
    assert feature["dt"] == 120.0
    assert "dx" in feature
    assert "dy" in feature
