"""The per-cycle history view must return exactly what the repository returns."""

from datetime import datetime, timedelta

import pytest

from EdgeWARN.ctam.builtins.stormprob import StormProbCycleService
from EdgeWARN.stormprob.database import StormProbRepository
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


def test_view_delegates_other_repository_methods(repo):
    view = StormProbCycleService(repo).repository
    assert view.legacy_history(101) == repo.legacy_history(101)
    assert view.path == repo.path
