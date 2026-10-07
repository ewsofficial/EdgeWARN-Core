import pytest
from util.atomic import atomic_write_json


def test_nonfinite_json_never_replaces_committed_artifact(tmp_path):
    path = tmp_path / "snapshot.json"
    atomic_write_json(path, {"centroid": [35.0, 265.0]})
    original = path.read_bytes()
    with pytest.raises(ValueError):
        atomic_write_json(path, {"centroid": [float("nan"), 265.0]})
    assert path.read_bytes() == original
