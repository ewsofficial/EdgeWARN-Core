"""NEXRAD GUI render-loop tests (NEXRAD service owned, decomposition Phase 3)."""

import json
import os
from pathlib import Path

import NEXRAD.gui_pipeline as nexrad_gui


class _FakeFuture:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class _FakeExecutor:
    def __init__(self, max_workers):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, func, layer):
        return _FakeFuture(func(layer))


def test_render_pending_nexrad_gui_files_skips_existing_same_timestamp(monkeypatch, tmp_path):
    nexrad_root = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5"
    gui_root = tmp_path / "gui" / "NEXRAD"
    artifact_path = nexrad_root / "KTLH_0.5_20260507-150001.nc"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"artifact")
    artifact_path.with_suffix(".json").write_text(json.dumps({
        "site": "KTLH",
        "volume_id": "999",
        "scan_timestamp": "20260507-150000",
        "elevation": "0.5",
        "elevation_timestamp": "20260507-150001",
    }))
    existing_output = gui_root / "KTLH" / "0.5" / "KTLH_DBZH_0.5_20260507-150001.bin.gz"
    existing_output.parent.mkdir(parents=True, exist_ok=True)
    existing_output.write_bytes(b"existing")

    calls = []
    monkeypatch.setattr(nexrad_gui.fs, "NEXRAD_LEVEL2_DIR", tmp_path / "data" / "NEXRAD_Level2")
    monkeypatch.setattr(nexrad_gui.fs, "GUI_NEXRAD_DIR", gui_root)
    monkeypatch.setattr("NEXRAD.render.serialize_nexrad_elevation_artifacts", lambda *args, **kwargs: calls.append((args, kwargs)))

    rendered = nexrad_gui.render_pending_nexrad_gui_files()

    assert rendered == 0
    assert calls == []


def test_render_pending_nexrad_gui_files_skips_stale_source_artifacts(monkeypatch, tmp_path):
    nexrad_root = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5"
    gui_root = tmp_path / "gui" / "NEXRAD"
    artifact_path = nexrad_root / "KTLH_0.5_20260507-150001.nc"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"artifact")
    artifact_path.with_suffix(".json").write_text(json.dumps({
        "site": "KTLH",
        "volume_id": "999",
        "scan_timestamp": "20260507-150000",
        "elevation": "0.5",
        "elevation_timestamp": "20260507-150001",
    }))

    calls = []
    now = 1_800_000_000
    stale_mtime = now - (3 * 60 * 60)
    os.utime(artifact_path, (stale_mtime, stale_mtime))
    monkeypatch.setattr(nexrad_gui.fs, "NEXRAD_LEVEL2_DIR", tmp_path / "data" / "NEXRAD_Level2")
    monkeypatch.setattr(nexrad_gui.fs, "GUI_NEXRAD_DIR", gui_root)
    monkeypatch.setattr(nexrad_gui.time, "time", lambda: now)
    monkeypatch.setattr("NEXRAD.render.serialize_nexrad_elevation_artifacts", lambda *args, **kwargs: calls.append((args, kwargs)))

    rendered = nexrad_gui.render_pending_nexrad_gui_files()

    assert rendered == 0
    assert calls == []


def test_render_pending_nexrad_gui_files_normalizes_iso_sidecar_timestamps(monkeypatch, tmp_path):
    nexrad_root = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5"
    gui_root = tmp_path / "gui" / "NEXRAD"
    artifact_path = nexrad_root / "KTLH_0.5_20260519-132157.nc"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"artifact")
    artifact_path.with_suffix(".json").write_text(json.dumps({
        "site": "KTLH",
        "volume_id": "999",
        "scan_timestamp": "2026-05-19T13:21:57Z",
        "elevation": "0.5",
        "elevation_timestamp": "2026-05-19T13:21:57Z",
    }))

    captured = []

    def _fake_serialize(site, volume_id, scan_timestamp, artifacts):
        captured.append((site, volume_id, scan_timestamp, artifacts[0].elevation_timestamp))
        out_dir = gui_root / site / artifacts[0].elevation
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{site}_DBZH_{artifacts[0].elevation}_{artifacts[0].elevation_timestamp}.bin.gz").write_bytes(b"rendered")

    monkeypatch.setattr(nexrad_gui.fs, "NEXRAD_LEVEL2_DIR", tmp_path / "data" / "NEXRAD_Level2")
    monkeypatch.setattr(nexrad_gui.fs, "GUI_NEXRAD_DIR", gui_root)
    monkeypatch.setattr("NEXRAD.render.serialize_nexrad_elevation_artifacts", _fake_serialize)

    rendered = nexrad_gui.render_pending_nexrad_gui_files()

    assert rendered == 1
    assert captured == [("KTLH", "999", "20260519-132157", "20260519-132157")]


def test_nexrad_discovery_ignores_partial_artifacts(monkeypatch, tmp_path):
    elevation_dir = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5"
    elevation_dir.mkdir(parents=True)
    complete = elevation_dir / "KTLH_0.5_20260519-132157.ar2v"
    partial = elevation_dir / "KTLH_0.5_20260519-132257.part.ar2v"
    for artifact in (complete, partial):
        artifact.write_bytes(b"artifact")
        artifact.with_suffix(".json").write_text(json.dumps({
            "scan_timestamp": "20260519-132157",
            "elevation_timestamp": "20260519-132157",
        }))

    monkeypatch.setattr(nexrad_gui.fs, "NEXRAD_LEVEL2_DIR", tmp_path / "data" / "NEXRAD_Level2")

    artifacts = list(nexrad_gui._iter_latest_nexrad_artifacts())

    assert [Path(item["artifact_path"]).name for item in artifacts] == [complete.name]


def test_nexrad_render_requires_valid_sidecar_before_serializing(monkeypatch, tmp_path):
    artifact_path = tmp_path / "KTLH_0.5_20260519-132157.ar2v"
    artifact_path.write_bytes(b"artifact")
    calls = []
    monkeypatch.setattr(
        "NEXRAD.render.serialize_nexrad_elevation_artifacts",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    rendered = nexrad_gui._render_pending_nexrad_gui_artifact({
        "site": "KTLH",
        "volume_id": "999",
        "scan_timestamp": "20260519-132157",
        "elevation": "0.5",
        "elevation_timestamp": "20260519-132157",
        "artifact_path": artifact_path,
    })

    assert rendered is False
    assert calls == []


def test_render_pending_nexrad_gui_files_uses_eight_workers(monkeypatch, tmp_path):
    gui_root = tmp_path / "gui" / "NEXRAD"
    nexrad_root = tmp_path / "data" / "NEXRAD_Level2"
    created = {}

    metadata_items = []
    for index in range(10):
        timestamp = f"20260507-1500{index:02d}"
        artifact_path = nexrad_root / f"K{index:03d}" / "0.5" / f"K{index:03d}_0.5_{timestamp}.nc"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(b"artifact")
        metadata_items.append(
            {
                "site": f"K{index:03d}",
                "volume_id": str(index),
                "scan_timestamp": timestamp,
                "elevation": "0.5",
                "elevation_timestamp": timestamp,
                "artifact_path": str(artifact_path),
                "member_group_names": [],
                "member_sweeps": [],
            }
        )

    monkeypatch.setattr(nexrad_gui.fs, "NEXRAD_LEVEL2_DIR", nexrad_root)
    monkeypatch.setattr(nexrad_gui.fs, "GUI_NEXRAD_DIR", gui_root)
    monkeypatch.setattr(nexrad_gui, "_iter_latest_nexrad_artifacts", lambda: metadata_items)
    monkeypatch.setattr(nexrad_gui, "_nexrad_source_artifact_is_fresh", lambda *args, **kwargs: True)
    monkeypatch.setattr(nexrad_gui, "_nexrad_gui_timestamp_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(nexrad_gui, "_render_pending_nexrad_gui_artifact", lambda metadata: True)
    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor",
        lambda max_workers: created.setdefault("executor", _FakeExecutor(max_workers=max_workers)),
    )
    monkeypatch.setattr("concurrent.futures.as_completed", lambda futures: list(futures))

    nexrad_gui._inflight_renders.clear()
    rendered = nexrad_gui.render_pending_nexrad_gui_files()

    assert rendered == 10
    assert created["executor"].max_workers == 8


def test_nexrad_inflight_tracking_skips_duplicate_submission(monkeypatch, tmp_path):
    artifact_path = tmp_path / "KTLH_0.5_20260507-150001.nc"
    metadata = {
        "site": "KTLH",
        "volume_id": "999",
        "scan_timestamp": "20260507-150001",
        "elevation": "0.5",
        "elevation_timestamp": "20260507-150001",
        "artifact_path": artifact_path,
    }
    submitted = []
    monkeypatch.setattr(nexrad_gui, "_iter_latest_nexrad_artifacts", lambda: [metadata, metadata])
    monkeypatch.setattr(nexrad_gui, "_nexrad_source_artifact_is_fresh", lambda *args, **kwargs: True)
    monkeypatch.setattr(nexrad_gui, "_nexrad_gui_timestamp_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(nexrad_gui, "_render_pending_nexrad_gui_artifact", lambda item: submitted.append(item) or True)
    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", _FakeExecutor)
    monkeypatch.setattr("concurrent.futures.as_completed", lambda futures: list(futures))
    nexrad_gui._inflight_renders.clear()

    rendered = nexrad_gui.render_pending_nexrad_gui_files()

    assert rendered == 1
    assert submitted == [metadata]
    assert nexrad_gui._inflight_renders == set()


def test_render_pending_nexrad_gui_files_renders_all_fresh_source_timestamps(monkeypatch, tmp_path):
    nexrad_root = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5"
    gui_root = tmp_path / "gui" / "NEXRAD"
    rendered_timestamps = []

    for timestamp in ("20260507-150001", "20260507-150011", "20260507-150021"):
        artifact_path = nexrad_root / f"KTLH_0.5_{timestamp}.nc"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(b"artifact")
        artifact_path.with_suffix(".json").write_text(json.dumps({
            "site": "KTLH",
            "volume_id": "999",
            "scan_timestamp": timestamp,
            "elevation": "0.5",
            "elevation_timestamp": timestamp,
        }))

    def _fake_serialize(site, volume_id, scan_timestamp, artifacts):
        artifact = artifacts[0]
        rendered_timestamps.append(artifact.elevation_timestamp)
        out_dir = gui_root / site / artifact.elevation
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{site}_DBZH_{artifact.elevation}_{artifact.elevation_timestamp}.bin.gz").write_bytes(b"rendered")

    monkeypatch.setattr(nexrad_gui.fs, "NEXRAD_LEVEL2_DIR", tmp_path / "data" / "NEXRAD_Level2")
    monkeypatch.setattr(nexrad_gui.fs, "GUI_NEXRAD_DIR", gui_root)
    monkeypatch.setattr("NEXRAD.render.serialize_nexrad_elevation_artifacts", _fake_serialize)

    rendered = nexrad_gui.render_pending_nexrad_gui_files()

    assert rendered == 3
    assert sorted(rendered_timestamps) == ["20260507-150001", "20260507-150011", "20260507-150021"]




def test_render_loop_sweeps_retention_each_poll_cycle(monkeypatch):
    """Phase 3: only the NEXRAD service cleans gui/NEXRAD -- so its loop must."""
    calls = {"rendered": 0, "cleaned": 0}

    monkeypatch.setattr(nexrad_gui, "render_pending_nexrad_gui_files", lambda **kwargs: calls.__setitem__("rendered", calls["rendered"] + 1) or 0)
    monkeypatch.setattr(nexrad_gui, "cleanup_old_nexrad_gui_files", lambda: calls.__setitem__("cleaned", calls["cleaned"] + 1) or 0)
    monkeypatch.setattr(nexrad_gui.time, "sleep", lambda seconds: None)

    attempts = {"count": 0}

    def quiescence():
        attempts["count"] += 1
        if attempts["count"] > 1:
            raise KeyboardInterrupt

    nexrad_gui.run_nexrad_render_loop(
        poll_interval_seconds=0.01,
        wait_for_quiescence=quiescence,
    )

    assert calls["rendered"] == 1
    assert calls["cleaned"] == 1
