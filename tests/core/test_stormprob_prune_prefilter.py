"""The projection prefilter in prune_inactive_cells never skips a pruned id."""

import json
import shutil
import sqlite3
from datetime import datetime, timedelta

import pytest

from EdgeWARN.stormprob import database
from EdgeWARN.stormprob.database import StormProbRepository, _projection_needles
from EdgeWARN.stormprob.records import build_observation_record


def _cell(when, cell_id, shift=0.0):
    cell = {
        "id": cell_id,
        "timestamp": when,
        "centroid": [35.0, 265.0 + shift],
        "bbox": [[34.99, 264.99 + shift], [34.99, 265.01 + shift], [35.01, 265.01 + shift],
                 [35.01, 264.99 + shift], [34.99, 264.99 + shift]],
        "properties": {"wind_field": {"u850": 5.0, "v850": 2.0}},
    }
    cell["stormprob"] = {"observation": build_observation_record(cell)}
    return cell


START = datetime(2024, 5, 1, 12)
CUTOFF = (START + timedelta(minutes=20)).isoformat()

# Ids observed only before the cutoff (pruned) and ones that stay active.
STALE = [7, 3.5, "b-7", "202", 1e20, -4, "x:y/z"]
ACTIVE = [101, "a-1", 2.25, "707", 70, "4"]
UNUSUAL = ["café", 'q"x', "True", "None", "nan", "a b", "[1]"]


def _build(path, stale, active, note=True):
    repo = StormProbRepository(path)
    for index in range(16):
        when = (START + timedelta(minutes=2 * index)).isoformat()
        ids = list(active) + (list(stale) if index < 6 else [])
        cells = [_cell(when, cell_id, offset * 0.01) for offset, cell_id in enumerate(ids)]
        projection = [{"id": cell_id, "timestamp": when} for cell_id in ids]
        # A projection that names a stale id only as a nested value, never as an id.
        if note:
            projection.append({"id": "other", "note": [str(cell_id) for cell_id in stale]})
        repo.commit_cycle(f"cycle{index:02d}", when, cells, projection_cells=projection)
    return repo


def _snapshot(path):
    db = sqlite3.connect(path)
    try:
        cycles = db.execute("SELECT cycle_id,projection_json,projection_hash FROM cycles "
                            "ORDER BY cycle_id").fetchall()
        cells = db.execute("SELECT DISTINCT cell_id FROM cell_observations ORDER BY cell_id").fetchall()
        forecasts = db.execute("SELECT COUNT(*) FROM forecasts").fetchone()
    finally:
        db.close()
    return cycles, cells, forecasts


def _prune_both(tmp_path, monkeypatch, stale, active):
    fast_dir, full_dir = tmp_path / "fast", tmp_path / "full"
    repo = _build(fast_dir, stale, active)
    shutil.copytree(fast_dir, full_dir)
    fast_result = repo.prune_inactive_cells(CUTOFF)
    with monkeypatch.context() as patch:
        patch.setattr(database, "_projection_needles", lambda ids: None)
        full_result = StormProbRepository(full_dir).prune_inactive_cells(CUTOFF)
    return (fast_result, _snapshot(repo.path),
            full_result, _snapshot(StormProbRepository(full_dir).path))


def test_prefilter_prunes_exactly_like_full_parse(tmp_path, monkeypatch):
    fast_result, fast_state, full_result, full_state = _prune_both(
        tmp_path, monkeypatch, STALE, ACTIVE)
    assert fast_result == full_result
    assert fast_state == full_state
    assert fast_result[2] == 6  # every cycle that held a stale id was scrubbed
    for _cycle, text, _hash in fast_state[0]:
        if text is None:
            continue
        ids = {str(cell.get("id")) for cell in json.loads(text)}
        assert not ids & {str(cell_id) for cell_id in STALE}


@pytest.mark.parametrize("unusual", UNUSUAL)
def test_unusual_ids_fall_back_to_full_parse(tmp_path, monkeypatch, unusual):
    fast_result, fast_state, full_result, full_state = _prune_both(
        tmp_path, monkeypatch, [unusual, 7], ACTIVE)
    assert fast_result == full_result
    assert fast_state == full_state


def test_prefilter_skips_projections_without_a_needle(tmp_path, monkeypatch):
    # Active ids such as 707 or "4" share digits with stale ones, and every
    # timestamp contains "202", but only exact "id" values count.
    repo = _build(tmp_path, STALE, ACTIVE, note=True)
    parsed = []
    original = database.fastjson.loads

    def counting(text):
        parsed.append(text)
        return original(text)

    monkeypatch.setattr(database.fastjson, "loads", counting)
    repo.prune_inactive_cells(CUTOFF)
    # Only the six cycles that held stale ids are parsed.
    assert len([text for text in parsed if text.startswith("[")]) == 6


@pytest.mark.parametrize("ids, expected", [
    ({"101"}, {'"101"', "101"}),
    ({"b-7"}, {'"b-7"'}),
    ({"3.5"}, {'"3.5"', "3.5"}),
    ({"1e+20"}, {'"1e+20"', "1e+20"}),
    ({"-4"}, {'"-4"', "-4"}),
    ({"007"}, {'"007"'}),           # not a JSON number literal
    ({"x:y/z"}, {'"x:y/z"'}),
])
def test_needles_cover_json_spellings(ids, expected):
    assert set(_projection_needles(ids)) == expected


@pytest.mark.parametrize("unusual", UNUSUAL + ["inf", "-Infinity", "false", "null", "a\tb", "{}"])
def test_needles_decline_unusual_ids(unusual):
    assert _projection_needles({"101", unusual}) is None


def test_needles_match_serialized_int_float_and_str_ids():
    for cell_id in (7, 3.5, "b-7", "202", 1e20, -4, 101.0, 2 ** 70):
        needles = _projection_needles({str(cell_id)})
        assert database._may_hold_ids(database._json([{"id": cell_id}]), needles)
        assert database._may_hold_ids(json.dumps([{"id": cell_id}], indent=2), needles)
        assert database._may_hold_ids(json.dumps([{"id" : cell_id}]), needles)


@pytest.mark.parametrize("text, held", [
    ('[{"id":70,"note":"7"}]', False),            # a longer number, a string "7"
    ('[{"id":"7"}]', True),                        # str id "7" prints as 7
    ('[{"id":"x","detail":{"id":7}}]', True),      # nested ids only cost a parse
    ('[{"id":"x","text":"\\"id\\":7"}]', False),   # escaped inside a string
    ('[{"cell_id":7,"id":"y"}]', False),
    ('[{"id":7.0}]', False),                      # str(7.0) is "7.0", not "7"
])
def test_may_hold_ids_reads_only_id_values(text, held):
    assert database._may_hold_ids(text, _projection_needles({"7"})) is held
    old_match = any(str(cell.get("id")) == "7" for cell in json.loads(text))
    assert not old_match or held
