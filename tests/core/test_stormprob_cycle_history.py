"""The per-cycle history view must return exactly what the repository returns."""

from datetime import datetime, timedelta

import pytest

from EdgeWARN.ctam.builtins.stormprob import StormProbCycleService
from EdgeWARN.stormprob.database import FULL_HISTORY_LIMIT, StormProbRepository
from EdgeWARN.stormprob.records import build_observation_record


def _cell(when, cell_id=101, shift=0.0):
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


@pytest.fixture
def repo(tmp_path):
    repo = StormProbRepository(tmp_path)
    start = datetime(2024, 5, 1, 12)
    for index in range(34):
        when = (start + timedelta(minutes=2 * index)).isoformat()
        cells = [_cell(when, 101, index * 0.01)]
        if index % 3 == 0:
            cells.append(_cell(when, "b-7", -index * 0.01))
        repo.commit_cycle(f"cycle{index}", when, cells)
    return repo


THROUGH = [None, "2024-05-01T12:00:00", "2024-05-01T12:30:00",
           "2024-05-01T13:06:00", "2024-05-01T13:06:00+00:00"]


@pytest.mark.parametrize("through", THROUGH)
@pytest.mark.parametrize("cell_id", [101, "101", "b-7"])
def test_view_matches_repository(repo, through, cell_id):
    view = StormProbCycleService(repo).repository
    for _ in range(2):  # second pass is served from the memo
        assert view.model_inputs(cell_id, through=through) == repo.model_inputs(cell_id, through=through)
        for limit in (None, 1, 5, 30, 1_000_000):
            assert (view.feature_history(cell_id, limit=limit, through=through)
                    == repo.feature_history(cell_id, limit=limit, through=through))
    assert view.feature_history(cell_id, limit=0, through=through) == []
    assert (view.feature_history(cell_id, limit=3, before="2024-05-01T12:20:00")
            == repo.feature_history(cell_id, limit=3, before="2024-05-01T12:20:00"))


def test_missing_cell_raises_like_repository(repo):
    view = StormProbCycleService(repo).repository
    with pytest.raises(ValueError, match="no committed features"):
        view.model_inputs(999)
    assert view.feature_history(999, limit=30) == []


def test_one_query_per_cell_per_cycle(repo, monkeypatch):
    calls = []
    original = StormProbRepository.feature_history

    def counting(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(StormProbRepository, "feature_history", counting)
    view = StormProbCycleService(repo).repository
    through = "2024-05-01T13:06:00"
    # Preflight and prepare, as run in one CTAM cycle.
    view.model_inputs(101, through=through)
    view.feature_history(101, limit=30, through=through)
    view.model_inputs(101, through=through)
    view.feature_history(101, limit=1, through=through)
    assert len(calls) == 1


def test_view_memoizes_model_inputs_per_cell_and_time(repo, monkeypatch):
    calls = []
    original = StormProbRepository.model_inputs

    def counting(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(StormProbRepository, "model_inputs", counting)
    view = StormProbCycleService(repo).repository
    first = view.model_inputs(101, through="2024-05-01T13:06:00")
    assert view.model_inputs("101", through="2024-05-01T13:06:00+00:00") is first
    assert view.model_inputs(101, through="2024-05-01T12:30:00") is not first
    assert len(calls) == 2


def _full_track_reference(repo, cell_id, through=None):
    """model_inputs as computed before the 30-row window: every row, then slice."""
    from EdgeWARN.stormprob.features import HISTORY_STEPS
    from EdgeWARN.stormprob.tracks import build_trajectory_sequence

    rows = repo.feature_history(cell_id, limit=FULL_HISTORY_LIMIT, through=through)
    history = [row for row in reversed(rows)
               if row.get("lineage", {}).get("tracking_mode") != "predicted"]
    times = [datetime.fromisoformat(row["analysis_time"]).timestamp() for row in history]
    trajectory, _ = build_trajectory_sequence([row["centroid"] for row in history], times,
                                              HISTORY_STEPS)
    vectors = []
    for index, row in enumerate(history):
        vector = list(row["features"])
        vector[-2] = times[index] - times[0]
        vector[-1] = float(min(index + 1, HISTORY_STEPS))
        vectors.append(vector)
    return trajectory[-HISTORY_STEPS:], vectors[-HISTORY_STEPS:]


@pytest.mark.parametrize("through", THROUGH)
@pytest.mark.parametrize("cell_id", [101, "b-7"])
def test_model_inputs_window_matches_full_track(repo, through, cell_id):
    inputs = repo.model_inputs(cell_id, through=through)
    trajectory, vectors = _full_track_reference(repo, cell_id, through)
    pad = len(inputs["trajectory_sequence"]) - len(trajectory)
    assert inputs["trajectory_sequence"][pad:] == trajectory
    assert inputs["current"] == vectors[-1]
    assert inputs["history_mask"].count(True) == len(trajectory)


def test_model_inputs_rejects_non_monotonic_rows_outside_window(repo):
    rows = repo.feature_history(101, limit=FULL_HISTORY_LIMIT)
    assert len(rows) > 31  # the oldest rows fall outside the 30-row window
    tampered = [dict(row) for row in rows]
    tampered[-2]["analysis_time"] = tampered[-1]["analysis_time"]  # second-oldest row
    with pytest.raises(ValueError, match="increase monotonically"):
        repo.model_inputs(101, history_rows=tampered)


def test_view_delegates_other_repository_methods(repo):
    view = StormProbCycleService(repo).repository
    assert view.legacy_history(101) == repo.legacy_history(101)
    assert view.path == repo.path
