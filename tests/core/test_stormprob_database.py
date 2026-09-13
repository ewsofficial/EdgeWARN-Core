"""Phase 2 database commit, history, and migration regressions."""

import json
from datetime import datetime, timedelta

import pytest

from EdgeWARN.stormprob.database import LEADS, StormProbRepository
from EdgeWARN.stormprob.records import build_observation_record
from EdgeWARN.stormprob.migrate import migrate
from EdgeWARN.ctam.publication import CTAMPublicationCoordinator, PublicationError


def _cell(when="2024-05-01T12:00:00", cell_id=101):
    cell = {
        "id": cell_id,
        "timestamp": when,
        "centroid": [35.0, 265.0],
        "bbox": [[34.99, 264.99], [34.99, 265.01], [35.01, 265.01],
                 [35.01, 264.99], [34.99, 264.99]],
        "properties": {"wind_field": {"u850": 5.0, "v850": 2.0}},
    }
    cell["stormprob"] = {"observation": build_observation_record(cell)}
    return cell


def test_commit_cycle_is_atomic_idempotent_and_read_only(tmp_path):
    repo = StormProbRepository(tmp_path)
    cell = _cell()
    assert repo.commit_cycle("cycle1", cell["timestamp"], [cell]) == 1
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    repo.integrity_check()
    history = repo.feature_history(101)
    assert len(history) == 1
    assert len(history[0]["features"]) == 135
    assert len(history[0]["radial_profile"]) == 64
    with repo.reader() as db:
        statuses = db.execute("SELECT lead_minutes,status FROM forecasts ORDER BY lead_minutes").fetchall()
        assert [row[0] for row in statuses] == list(LEADS)
        assert all(row[1] in ("not-computed", "skipped") for row in statuses)
        with pytest.raises(Exception):
            db.execute("DELETE FROM cycles")
    assert repo.legacy_history(101)[0]["id"] == 101
    assert "stormprob" not in repo.legacy_history(101)[0]


def test_failed_cycle_does_not_leave_partial_rows(tmp_path):
    repo = StormProbRepository(tmp_path)
    good = _cell()
    bad = _cell(cell_id=102)
    bad["stormprob"]["observation"]["current_features_raw"][0] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        repo.commit_cycle("cycle1", good["timestamp"], [good, bad])
    repo.integrity_check()
    with repo.reader() as db:
        assert db.execute("SELECT count(*) FROM cycles").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM cell_observations").fetchone()[0] == 0


def test_history_model_inputs_and_reprocessing(tmp_path):
    repo = StormProbRepository(tmp_path)
    first = _cell()
    second = _cell("2024-05-01T12:05:00")
    repo.commit_cycle("cycle1", first["timestamp"], [first])
    repo.commit_cycle("cycle2", second["timestamp"], [second])
    assert len(repo.legacy_history(101)) == 2
    assert len(repo.legacy_history(101, before=second["timestamp"])) == 1
    inputs = repo.model_inputs(101)
    assert sum(inputs["history_mask"]) == 2
    assert inputs["current"][-2:] == [300.0, 2.0]
    assert sum(repo.model_inputs(101, through=first["timestamp"])["history_mask"]) == 1
    second["properties"]["revision"] = 2
    repo.commit_cycle("cycle2", second["timestamp"], [second])
    assert len(repo.legacy_history(101)) == 2
    assert repo.legacy_history(101)[-1]["properties"]["revision"] == 2


def test_legacy_import_hash_validation_and_backup(tmp_path):
    repo = StormProbRepository(tmp_path)
    legacy = tmp_path / "data" / "cells" / "101.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps([_cell() | {"stormprob": None}]))
    report = repo.import_legacy_file(legacy)
    assert report["imported"] == 1
    assert repo.import_legacy_file(legacy)["skipped"] is True
    assert len(repo.feature_history(101)) == 1
    backup = repo.backup(tmp_path / "data" / "stormprob" / "backups" / "stormprob-1.sqlite3")
    assert backup.exists()
    legacy.write_text("[]")
    with pytest.raises(ValueError, match="changed"):
        repo.import_legacy_file(legacy)


def test_recover_projection_after_database_commit(tmp_path):
    repo = StormProbRepository(tmp_path)
    cell = _cell()
    snapshot = tmp_path / "data" / "stormcells" / "stormcells_20240501-120000.json"
    repo.commit_cycle("2024-05-01T12:00:00", cell["timestamp"], [cell],
                      projection_cells=[{key: value for key, value in cell.items() if key != "stormprob"}],
                      projection_path=snapshot)
    restored = repo.recover_projections()
    assert snapshot in restored
    assert (tmp_path / "data" / "cells" / "101.json") in restored
    assert json.loads(snapshot.read_text())["features"][0]["id"] == 101
    assert repo.recover_projections() == []
    repo.mark_projection_published("2024-05-01T12:00:00")
    snapshot.unlink()
    assert repo.recover_projections() == []
    assert not snapshot.exists()


def test_json_journal_requires_committed_database_cycle(tmp_path):
    repo = StormProbRepository(tmp_path)
    dependency = {"path": str(repo.path), "cycle_id": "cycle1"}
    target = tmp_path / "data" / "stormcells" / "stormcells.json"
    coordinator = CTAMPublicationCoordinator(tmp_path / "data" / "ctam" / "transactions")
    with pytest.raises(PublicationError, match="database is missing"):
        coordinator.publish({target: {"features": []}}, db_dependency=dependency)
    assert not target.exists()
    cell = _cell()
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    coordinator.publish({target: {"features": []}}, db_dependency=dependency)
    assert target.exists()


def test_resumable_migration_dry_run_and_timestamp_validation(tmp_path):
    source = tmp_path / "data" / "cells" / "101.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps([_cell() | {"stormprob": None}]))
    report = migrate(tmp_path, dry_run=True)
    assert report["source_entries"] == 1
    assert not StormProbRepository(tmp_path).path.exists()
    report = migrate(tmp_path)
    assert report["imported_entries"] == 1
    assert migrate(tmp_path)["skipped_files"] == 1


def test_integration_publication_commits_db_before_json_index(tmp_path, monkeypatch):
    import util.file as fs
    from EdgeWARN.process.integrate import pipeline

    monkeypatch.setattr(fs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(fs, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(fs, "CELL_DIR", tmp_path / "data" / "cells")
    monkeypatch.setattr(fs, "STORMCELL_DIR", tmp_path / "data" / "stormcells")
    snapshot = fs.STORMCELL_DIR / "stormcells_20240501-120000.json"
    cell = _cell()
    events = []
    original = pipeline._update_api_indexes

    def checked_index(cells, remove_old_cells, timestamp):
        assert StormProbRepository(tmp_path).feature_history(101)
        assert json.loads(snapshot.read_text())["features"][0]["id"] == 101
        assert StormProbRepository(tmp_path).index_projection() == ([], {})
        events.append("after-db-and-json")
        return original(cells, remove_old_cells, timestamp)

    monkeypatch.setattr(pipeline, "_update_api_indexes", checked_index)
    pipeline._publish_cycle(None, cell["timestamp"], [cell], snapshot, False)
    assert events == ["after-db-and-json"]
    assert "stormprob" not in json.loads(snapshot.read_text())["features"][0]
    assert "20240501-120000" in json.loads((fs.STORMCELL_DIR / "stormcell_index.json").read_text())["timestamps"]
    assert StormProbRepository(tmp_path).pending_projection_cycles() == []
    assert StormProbRepository(tmp_path).index_projection()[0] == ["20240501-120000"]


def test_reprocessing_explicitly_invalidates_deployed_forecasts(tmp_path):
    repo = StormProbRepository(tmp_path)
    cell = _cell()
    forecast = {
        "cell_id": 101, "analysis_time": cell["timestamp"], "lead_minutes": 15,
        "model_version": "stormprob-test/v1", "status": "ok", "east_km": 2.0,
        "north_km": 1.0, "predicted_centroid": [35.01, 265.02],
        "polygon": {"type": "Polygon", "coordinates": [[[265, 35], [266, 35],
                                                       [266, 36], [265, 35]]]},
        "probability_threshold": 0.25,
    }
    forecasts = [forecast | {"lead_minutes": lead} for lead in LEADS]
    with pytest.raises(ValueError, match="all four leads"):
        repo.commit_cycle("cycle1", cell["timestamp"], [cell], forecasts=[forecast])
    repo.commit_cycle("cycle1", cell["timestamp"], [cell], forecasts=forecasts)
    with repo.reader() as db:
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 8
    repo.commit_cycle("cycle1", cell["timestamp"], [cell], forecast_policy="preserve")
    with repo.reader() as db:
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 8
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    with repo.reader() as db:
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 4


def test_model_inputs_long_track_preserves_age_and_30_row_window(tmp_path):
    repo = StormProbRepository(tmp_path)
    start = datetime(2024, 5, 1, 12)
    for index in range(32):
        when = (start + timedelta(minutes=5 * index)).isoformat()
        cell = _cell(when)
        cell["centroid"][1] += index * 0.01
        cell["stormprob"]["observation"] = build_observation_record(cell)
        repo.commit_cycle(f"cycle{index}", when, [cell])
    inputs = repo.model_inputs(101)
    assert len(inputs["history_sequence"]) == 30
    assert all(inputs["history_mask"])
    assert inputs["current"][-2:] == [31 * 300.0, 30.0]
    assert inputs["trajectory_sequence"][-1][-2] == pytest.approx(31 * 5 / 60)


def test_daily_backup_is_idempotent(tmp_path):
    repo = StormProbRepository(tmp_path)
    cell = _cell()
    repo.commit_cycle("cycle1", cell["timestamp"], [cell])
    day = datetime(2024, 5, 1)
    backup = repo.backup_if_due(now=day)
    assert backup is not None and backup.exists()
    assert repo.backup_if_due(now=day) is None
