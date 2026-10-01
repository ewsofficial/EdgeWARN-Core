from unittest.mock import patch
from types import SimpleNamespace

import pytest

from EdgeWARN import pipeline
from EdgeWARN.process.detect.config import DetectionConfig
from common.ingest.manifest import CycleInputManifest


@pytest.fixture(autouse=True)
def runtime_base(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.fs, "BASE_DIR", tmp_path)


CYCLE_TIME = pipeline.datetime(2026, 7, 26, 18, 0, tzinfo=pipeline.timezone.utc)


def _manifest(timestamp=None):
    return CycleInputManifest(cycle_time=timestamp or CYCLE_TIME)


def _pinned_state(**extra):
    """The state shape the realtime parent installs before each barrier.

    Core now consumes immutable committed snapshots, so the worker reads the
    exact pinned manifest for each phase instead of falling back to a rolling
    one.
    """
    manifest = _manifest().as_dict()
    state = {
        "detection_inputs_ready": True,
        "edgewarn_integration_inputs_ready": True,
        "optional_inputs_complete": True,
        "detection_manifest": manifest,
        "integration_manifest": manifest,
        "ctam_manifest": manifest,
    }
    state.update(extra)
    return state


def test_historical_cleanup_skips_cells_and_stormcells(tmp_path):
    data_dir = tmp_path / "data"
    rap_dir = data_dir / "RAP"
    composite_dir = data_dir / "CompRefQC"
    cell_dir = data_dir / "cells"
    stormcell_dir = data_dir / "stormcells"

    for directory in (rap_dir, composite_dir, cell_dir, stormcell_dir):
        directory.mkdir(parents=True)

    with patch.object(pipeline.fs, "CELL_DIR", cell_dir), \
         patch.object(pipeline.fs, "STORMCELL_DIR", stormcell_dir), \
         patch.object(pipeline.fs, "RAP_DIR", rap_dir), \
         patch.object(
             pipeline,
             "get_output_dirs",
             return_value=[composite_dir, cell_dir, stormcell_dir, composite_dir],
         ), \
         patch.object(pipeline.fs, "clean_old_files") as mock_clean:
        pipeline._cleanup_historical_data_dirs(pipeline.IOManager("[TestPipeline]"))

    cleaned_dirs = [call.args[0] for call in mock_clean.call_args_list]
    cleaned_max_files = [call.kwargs["max_files"] for call in mock_clean.call_args_list]

    assert composite_dir in cleaned_dirs
    assert rap_dir in cleaned_dirs
    assert cell_dir not in cleaned_dirs
    assert stormcell_dir not in cleaned_dirs
    assert cleaned_dirs.count(composite_dir) == 1
    assert all(max_files == 5 for max_files in cleaned_max_files)


def test_historical_pipeline_preserves_cell_and_stormcell_dirs(tmp_path):
    generated_path = tmp_path / "generated.json"
    generated_path.write_text("{}")
    with patch.object(pipeline, "_cleanup_historical_data_dirs"), \
         patch.object(
             pipeline,
            "run_staged_ingest_cycle",
            return_value=SimpleNamespace(
                detection_inputs_ready=True,
                optional_inputs_complete=True,
                errors={},
                input_manifest=_manifest(
                    pipeline.datetime(
                        2024,
                        1,
                        1,
                        12,
                        0,
                        tzinfo=pipeline.timezone.utc,
                    )
                ),
            ),
         ) as mock_ingest, \
         patch.object(pipeline, "run_edgewarn_detection_phase", return_value=generated_path) as mock_detect, \
         patch.object(pipeline, "run_edgewarn_integration_phase", return_value=True) as mock_integrate:
        generated_file, _ = pipeline.historical_pipeline(
            dt=pipeline.datetime(2024, 1, 1, 12, 0, tzinfo=pipeline.timezone.utc),
            lat_limits=(20, 55),
            lon_limits=(-130, -60),
            detection_config=DetectionConfig.from_yaml(),
        )

    assert generated_file == generated_path
    assert mock_ingest.call_args.kwargs["include_goes"] is False
    assert mock_ingest.call_args.kwargs["include_ewmrs"] is False
    assert mock_integrate.call_args.kwargs["remove_old_cells"] is False


def test_historical_pipeline_reports_incomplete_when_staged_inputs_are_missing(tmp_path):
    generated_path = tmp_path / "generated.json"
    generated_path.write_text("{}")
    with patch.object(pipeline, "_cleanup_historical_data_dirs"), \
         patch.object(
             pipeline,
             "run_staged_ingest_cycle",
             return_value=SimpleNamespace(
                detection_inputs_ready=True,
                errors={"rap_ingest": "RAP inputs unavailable"},
                input_manifest=_manifest(
                    pipeline.datetime(
                        2024,
                        1,
                        1,
                        12,
                        0,
                        tzinfo=pipeline.timezone.utc,
                    )
                ),
             ),
         ), \
         patch.object(pipeline, "run_edgewarn_detection_phase", return_value=generated_path), \
         patch.object(pipeline, "run_edgewarn_integration_phase") as mock_integrate:
        generated_file, _ = pipeline.historical_pipeline(
            dt=pipeline.datetime(2024, 1, 1, 12, 0, tzinfo=pipeline.timezone.utc),
            lat_limits=(20, 55),
            lon_limits=(-130, -60),
            detection_config=DetectionConfig.from_yaml(),
        )

    assert generated_file is None
    mock_integrate.assert_not_called()


class _Queue:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(str(message))


class _Event:
    def wait(self):
        return True


def _run_edgewarn_worker(shared_state, optional_complete=None):
    pipeline.edgewarn_cycle_worker(
        _Queue(),
        shared_state,
        _Event(),
        _Event(),
        CYCLE_TIME,
        (20, 55),
        (-130, -60),
        DetectionConfig.from_yaml(),
        optional_complete_event=optional_complete,
    )


def test_edgewarn_worker_publishes_unavailable_state(monkeypatch):
    monkeypatch.setattr(pipeline.sys, "stdout", pipeline.sys.stdout)
    monkeypatch.setattr(pipeline.sys, "stderr", pipeline.sys.stderr)
    shared_state = {
        "detection_inputs_ready": False,
        "edgewarn_integration_inputs_ready": False,
    }

    _run_edgewarn_worker(shared_state)

    assert shared_state["edgewarn_stage"]["status"] == "unavailable"
    assert shared_state["edgewarn_stage"]["produced_artifacts"] == []


def test_edgewarn_worker_publishes_completed_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.sys, "stdout", pipeline.sys.stdout)
    monkeypatch.setattr(pipeline.sys, "stderr", pipeline.sys.stderr)
    generated = tmp_path / "stormcells_20260726-180000.json"
    generated.write_text("{}")
    shared_state = _pinned_state()
    monkeypatch.setattr(
        pipeline,
        "run_edgewarn_detection_phase",
        lambda *_args, **_kwargs: generated,
    )
    monkeypatch.setattr(
        pipeline,
        "run_edgewarn_integration_phase",
        lambda *_args, **_kwargs: True,
    )

    _run_edgewarn_worker(shared_state)

    assert shared_state["edgewarn_stage"] == {
        "status": "completed",
        "produced_artifacts": [str(generated)],
        "errors": [],
    }


def test_edgewarn_worker_exception_publishes_failed_state(monkeypatch):
    monkeypatch.setattr(pipeline.sys, "stdout", pipeline.sys.stdout)
    monkeypatch.setattr(pipeline.sys, "stderr", pipeline.sys.stderr)
    shared_state = _pinned_state()
    monkeypatch.setattr(
        pipeline,
        "run_edgewarn_detection_phase",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        _run_edgewarn_worker(shared_state)

    assert shared_state["edgewarn_stage"]["status"] == "failed"
    assert shared_state["edgewarn_stage"]["errors"] == ["boom"]


def test_edgewarn_worker_requires_the_pinned_detection_snapshot(monkeypatch, tmp_path):
    """A released detection barrier without its pinned snapshot fails truthfully."""
    monkeypatch.setattr(pipeline.sys, "stdout", pipeline.sys.stdout)
    monkeypatch.setattr(pipeline.sys, "stderr", pipeline.sys.stderr)
    shared_state = {
        "detection_inputs_ready": True,
        "edgewarn_integration_inputs_ready": True,
        "input_manifest": _manifest().as_dict(),
    }
    called = []
    monkeypatch.setattr(
        pipeline, "run_edgewarn_detection_phase",
        lambda *_a, **_k: called.append(True))

    _run_edgewarn_worker(shared_state)

    assert called == []
    assert shared_state["edgewarn_stage"]["status"] == "failed"
    assert "pinned detection snapshot" in shared_state["edgewarn_stage"]["errors"][0]


def test_edgewarn_worker_requires_the_pinned_integration_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.sys, "stdout", pipeline.sys.stdout)
    monkeypatch.setattr(pipeline.sys, "stderr", pipeline.sys.stderr)
    generated = tmp_path / "stormcells.json"
    generated.write_text("{}")
    shared_state = _pinned_state()
    shared_state["integration_manifest"] = {}
    monkeypatch.setattr(pipeline, "run_edgewarn_detection_phase",
                        lambda *_a, **_k: generated)
    integrated = []
    monkeypatch.setattr(pipeline, "run_edgewarn_integration_phase",
                        lambda *_a, **_k: integrated.append(True))

    _run_edgewarn_worker(shared_state)

    assert integrated == []
    assert shared_state["edgewarn_stage"]["status"] == "failed"
    assert "pinned integration snapshot" in shared_state["edgewarn_stage"]["errors"][0]


def test_edgewarn_worker_rejects_a_misaligned_final_snapshot(monkeypatch, tmp_path):
    """The optional snapshot is validated, not merely present."""
    monkeypatch.setattr(pipeline.sys, "stdout", pipeline.sys.stdout)
    monkeypatch.setattr(pipeline.sys, "stderr", pipeline.sys.stderr)
    generated = tmp_path / "stormcells.json"
    generated.write_text("{}")
    shared_state = _pinned_state(ctam_manifest=None)
    monkeypatch.setattr(pipeline, "run_edgewarn_detection_phase",
                        lambda *_a, **_k: generated)
    seen = {}

    def integration(*_args, **kwargs):
        provider = kwargs["final_input_provider"]
        with pytest.raises(RuntimeError, match="final optional-input snapshot"):
            provider()
        seen["raised"] = True
        return True

    monkeypatch.setattr(pipeline, "run_edgewarn_integration_phase", integration)

    _run_edgewarn_worker(shared_state, optional_complete=_Event())

    assert seen["raised"] is True
    assert shared_state["edgewarn_stage"]["status"] == "completed"
