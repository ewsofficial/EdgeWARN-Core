"""Snapshot-name prefilter in alert matching changes no match.

``match_alerts_to_cells`` skips snapshot entries whose ``name`` cannot belong
to a matched event before reading their feature files. These tests compare it
with the unfiltered load on a registry that holds tornado upgrades, entries
without a name, and a mix of convective, flood and other events.
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from EdgeWARN.process.detect.tools import alert_matcher
from EdgeWARN.process.detect.tools.alert_matcher import (
    convective_flood_events,
    match_alerts_to_cells,
    snapshot_names_for_events,
)

_SNAPSHOT = "20260223-210000"
_TARGET = "2026-02-23T21:00:30Z"


@pytest.fixture(autouse=True)
def _fresh_caches():
    alert_matcher._ALERT_SNAPSHOT_CACHE.clear()
    alert_matcher._ALERT_GEOMETRY_CACHE.clear()
    yield
    alert_matcher._ALERT_SNAPSHOT_CACHE.clear()
    alert_matcher._ALERT_GEOMETRY_CACHE.clear()


def _square(lon, lat, half):
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - half, lat - half], [lon + half, lat - half],
            [lon + half, lat + half], [lon - half, lat + half], [lon - half, lat - half],
        ]],
    }


# (snapshot entry form, snapshot name, feature event, lon, lat, half-size)
_ALERTS = [
    ("dict", "Tornado Warning", "Tornado Warning", -97.0, 35.0, 0.3),
    ("dict", "Tornado Emergency", "Tornado Warning", -97.1, 35.1, 0.2),
    ("dict", "PDS Tornado Warning", "Tornado Warning", -96.9, 34.9, 0.25),
    ("dict", "Severe Thunderstorm Warning", "Severe Thunderstorm Warning", -97.0, 35.0, 0.5),
    ("dict", "Flash Flood Warning", "Flash Flood Warning", -97.05, 35.0, 0.4),
    ("dict", "Tornado Watch", "Tornado Watch", -97.0, 35.0, 2.0),
    ("dict", "Special Weather Statement", "Special Weather Statement", -96.8, 35.2, 0.3),
    ("dict", "Winter Storm Warning", "Winter Storm Warning", -97.0, 35.0, 1.0),
    ("dict", "Flood Watch", "Flood Watch", -97.0, 35.0, 1.5),
    ("dict", "Heat Advisory", "Heat Advisory", -97.0, 35.0, 3.0),
    ("dict", None, "Severe Thunderstorm Warning", -97.0, 35.05, 0.2),   # no name
    ("dict", None, "Winter Weather Advisory", -97.0, 35.0, 1.0),        # no name, other
    ("dict", 17, "Tornado Warning", -97.02, 35.02, 0.1),                # non-string name
    ("str", None, "Severe Thunderstorm Warning", -96.95, 35.0, 0.3),    # bare id entry
    ("str", None, "Dense Fog Advisory", -97.0, 35.0, 1.0),
]


def _write_registry(root: Path):
    registry = root / "alerts"
    (registry / "ids").mkdir(parents=True)
    (registry / "timestamps").mkdir(parents=True)
    entries = []
    for index, (form, name, event, lon, lat, half) in enumerate(_ALERTS):
        alert_id = f"urn:oid:2.49.0.1.840.0.{index:04d}.1"
        feature = {
            "id": f"https://api.weather.gov/alerts/{alert_id}",
            "type": "Feature",
            "geometry": _square(lon, lat, half),
            "properties": {
                "id": alert_id,
                "event": event,
                "expires": "2026-02-23T23:00:00Z",
            },
        }
        safe = alert_id.replace(":", "_").replace("/", "_") + ".json"
        (registry / "ids" / safe).write_text(json.dumps({"id": alert_id, "feature": feature}))
        if form == "str":
            entries.append(alert_id)
        else:
            entry = {"id": alert_id, "urn_oid": alert_id, "geometry": feature["geometry"]}
            if name is not None:
                entry["name"] = name
            entries.append(entry)
    (registry / "timestamps" / f"{_SNAPSHOT}.json").write_text(
        json.dumps({"timestamp": "2026-02-23T21:00:00Z", "count": len(entries), "alerts": entries}))
    return registry


def _cells():
    cells = []
    for index, (lon, lat) in enumerate([
        (-97.0, 35.0), (-97.1, 35.1), (-96.9, 34.9), (-96.8, 35.2), (-97.05, 35.05),
        (-96.95, 35.0), (-97.02, 35.02), (-95.5, 36.0), (-99.0, 33.0), (-97.4, 35.3),
    ]):
        lon %= 360  # cells carry 0-360 longitudes, as alert rings are normalized
        cells.append({
            "id": 1000 + index,
            "centroid": [lat, lon],
            "bbox": [[lat - 0.01, lon - 0.01], [lat - 0.01, lon + 0.01],
                     [lat + 0.01, lon + 0.01], [lat + 0.01, lon - 0.01]],
        })
    return cells


def _matches(cells):
    return [(cell["id"], list(cell["alerts"])) for cell in cells]


def _unfiltered(registry):
    """The pre-prefilter behavior: load every snapshot entry, then filter."""
    original = alert_matcher.snapshot_names_for_events
    alert_matcher.snapshot_names_for_events = lambda events: None
    try:
        return _matches(match_alerts_to_cells(_cells(), registry, _TARGET))
    finally:
        alert_matcher.snapshot_names_for_events = original
        alert_matcher._ALERT_SNAPSHOT_CACHE.clear()
        alert_matcher._ALERT_GEOMETRY_CACHE.clear()


def test_snapshot_names_include_tornado_upgrades():
    names = snapshot_names_for_events(convective_flood_events())
    assert convective_flood_events() <= names
    assert {"Tornado Emergency", "PDS Tornado Warning"} <= names


def test_snapshot_names_skip_upgrades_without_base_event():
    names = snapshot_names_for_events(frozenset({"Severe Thunderstorm Warning"}))
    assert names == frozenset({"Severe Thunderstorm Warning"})


def test_prefilter_matches_unfiltered_load(tmp_path):
    registry = _write_registry(tmp_path)
    expected = _unfiltered(registry)
    actual = _matches(match_alerts_to_cells(_cells(), registry, _TARGET))
    assert actual == expected
    assert sum(len(ids) for _cell, ids in actual) > 10
    matched = {alert_id for _cell, ids in actual for alert_id in ids}
    for index in (0, 1, 2, 10, 12, 13):  # upgrades, unnamed, non-string name, bare id
        assert f"urn:oid:2.49.0.1.840.0.{index:04d}.1" in matched


def test_prefilter_skips_feature_files_of_other_events(tmp_path, monkeypatch):
    registry = _write_registry(tmp_path)
    read = []
    original = Path.read_bytes

    def spy(self):
        read.append(self.name)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    match_alerts_to_cells(_cells(), registry, _TARGET)

    skipped = {f"urn_oid_2.49.0.1.840.0.{index:04d}.1.json" for index in (7, 8, 9)}
    kept = {f"urn_oid_2.49.0.1.840.0.{index:04d}.1.json" for index in (0, 1, 2, 10, 11, 12, 13, 14)}
    assert not skipped & set(read)
    assert kept <= set(read)


def test_prefilter_has_its_own_snapshot_cache_entry(tmp_path):
    registry = _write_registry(tmp_path)
    names = snapshot_names_for_events(convective_flood_events())
    filtered, filtered_key = alert_matcher._load_active_alerts_with_cache(
        registry, _TARGET, snapshot_names=names)
    full, full_key = alert_matcher._load_active_alerts_with_cache(registry, _TARGET)
    assert filtered_key != full_key
    assert len(full) == len(_ALERTS)
    assert len(filtered) < len(full)
    # The public loader is unchanged and still returns every alert.
    alert_matcher._ALERT_SNAPSHOT_CACHE.clear()
    assert len(alert_matcher.load_active_alerts(registry, _TARGET)) == len(_ALERTS)


def test_prefilter_matches_unfiltered_load_when_cached(tmp_path):
    registry = _write_registry(tmp_path)
    expected = _unfiltered(registry)
    first = _matches(match_alerts_to_cells(_cells(), registry, _TARGET))
    second = _matches(match_alerts_to_cells(copy.deepcopy(_cells()), registry, _TARGET))
    assert first == second == expected
