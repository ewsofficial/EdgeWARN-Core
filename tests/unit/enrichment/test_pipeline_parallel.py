import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from EdgeWARN.process.integrate import pipeline
from EdgeWARN.process.integrate.io.stat_files import StatFileHandler


@pytest.fixture
def sample_cells():
    return [
        {
            "id": 101,
            "timestamp": "2024-01-01T00:00:00",
            "centroid": [35.0, -97.0],
            "bbox": [[34.9, -97.1], [34.9, -96.9], [35.1, -96.9], [35.1, -97.1]],
            "properties": {"existing": "keep", "wind_field": {"legacy": 1}},
        },
        {
            "id": 102,
            "timestamp": "2024-01-01T00:00:00",
            "centroid": [36.0, -96.0],
            "bbox": [[35.9, -96.1], [35.9, -95.9], [36.1, -95.9], [36.1, -96.1]],
            "properties": {"existing": "keep-too"},
        },
    ]


def _stats_stub(_integrator, cells, *_args):
    for cell in cells:
        cell.setdefault("properties", {})["Ref0"] = float(cell["id"])
    return cells


def _azshear_stub(_integrator, cells, *_args):
    for cell in cells:
        cell.setdefault("properties", {})["azshear"] = {"buffer_km": 1.5, "cell": cell["id"]}
    return cells


def _probsevere_stub(_integrator, cells, *_args):
    for cell in cells:
        cell.setdefault("properties", {})["ProbSevere"] = 42.0
    return cells


def _glm_stub(cells, *_args):
    for cell in cells:
        props = cell.setdefault("properties", {})
        props["GLM_FLASH_COUNT"] = 3
        props["GLM_TOTAL_ENERGY"] = 9.5
    return cells


def _rap_stub(cells, *_args):
    for cell in cells:
        props = cell.setdefault("properties", {})
        wind = props.setdefault("wind_field", {})
        wind["u850"] = 10.0
        props["dewpoint_depression"] = 7.5
    return cells


def test_parallel_enrichment_matches_serial_output(sample_cells):
    integrator = MagicMock()

    with patch.object(pipeline, "_integrate_dataset_groups", side_effect=_stats_stub), \
         patch.object(pipeline, "_integrate_azshear", side_effect=_azshear_stub), \
         patch.object(pipeline, "_integrate_probsevere", side_effect=_probsevere_stub), \
         patch.object(pipeline, "_integrate_glm", side_effect=_glm_stub), \
         patch.object(pipeline, "_integrate_rap", side_effect=_rap_stub), \
         patch.object(pipeline, "_stats_owned_keys", return_value={"Ref0"}), \
         patch.object(pipeline, "_support_owned_keys", return_value={"ProbSevere", "GLM_FLASH_COUNT", "GLM_TOTAL_ENERGY", "wind_field", "dewpoint_depression"}):
        serial_result = pipeline._run_enrichment_serial(integrator, copy.deepcopy(sample_cells))
        parallel_result = pipeline._run_parallel_enrichment(integrator, copy.deepcopy(sample_cells))

    assert parallel_result == serial_result
    assert parallel_result[0]["properties"]["existing"] == "keep"
    assert parallel_result[0]["properties"]["wind_field"]["legacy"] == 1
    assert parallel_result[0]["properties"]["wind_field"]["u850"] == 10.0
    assert "azshear" not in parallel_result[0]["properties"]


def test_azshear_support_integration_disabled_bypasses_worker(sample_cells):
    integrator = MagicMock()

    with patch.object(pipeline, "_AZSHEAR_SUPPORT_ENABLED", False), \
         patch.object(pipeline, "_run_step", side_effect=lambda _name, action: action()):
        result = pipeline._integrate_azshear(integrator, copy.deepcopy(sample_cells))

    assert result == sample_cells
    integrator.integrate_azshear_features.assert_not_called()


def test_parallel_enrichment_ignores_unknown_patch_cell(sample_cells):
    merged = pipeline._merge_property_patch(
        copy.deepcopy(sample_cells),
        {
            "999": {"Ref0": 99.0},
            "101": {"Ref0": 10.0},
        },
    )

    assert merged[0]["properties"]["Ref0"] == 10.0
    assert "Ref0" not in merged[1]["properties"]


def test_merge_property_patch_preserves_nested_existing_values(sample_cells):
    merged = pipeline._merge_property_patch(
        copy.deepcopy(sample_cells),
        {
            "101": {"wind_field": {"u850": 10.0}},
        },
    )

    assert merged[0]["properties"]["wind_field"]["legacy"] == 1
    assert merged[0]["properties"]["wind_field"]["u850"] == 10.0


def test_main_aborts_index_updates_when_publication_fails(sample_cells):
    with patch.object(pipeline, "StatFileHandler") as MockHandler, \
         patch.object(pipeline, "StormCellIntegrator"), \
         patch.object(pipeline, "_run_parallel_enrichment", return_value=copy.deepcopy(sample_cells)), \
         patch.object(pipeline, "_run_ctam_if_enabled", side_effect=lambda cells, *_args, **_kwargs: cells), \
         patch.object(pipeline, "_publish_cycle", side_effect=RuntimeError("disk full")), \
         patch.object(pipeline, "_update_api_indexes") as mock_indexes:
        handler = MockHandler.return_value
        handler.load_json.return_value = (copy.deepcopy(sample_cells), "2024-01-01T00:00:00")

        with pytest.raises(RuntimeError, match="disk full"):
            pipeline.main(json_path="stormcells.json", remove_old_cells=False, disable_ctam=True)

    mock_indexes.assert_not_called()


def test_stat_file_handler_write_json_is_atomic_on_failure(tmp_path):
    target = tmp_path / "stormcells.json"
    original = {"features": [{"id": 1}], "latest_timestamp": "t0"}
    target.write_text(json.dumps(original))

    handler = StatFileHandler(MagicMock())

    with patch("EdgeWARN.process.integrate.io.stat_files.json.dump", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            handler.write_json({"features": [{"id": 2}]}, target)

    assert json.loads(target.read_text()) == original
    leftovers = list(tmp_path.glob("tmp*"))
    assert leftovers == []


def test_stat_file_handler_write_json_replaces_target_atomically(tmp_path):
    target = tmp_path / "stormcells.json"
    target.write_text(json.dumps({"features": [], "latest_timestamp": "old"}))

    handler = StatFileHandler(MagicMock())
    updated = {"features": [{"id": 7}], "latest_timestamp": "new"}

    handler.write_json(updated, target)

    assert json.loads(target.read_text()) == updated


@pytest.mark.parametrize("disable_ctam", [False, True])
def test_final_optional_enrichment_precedes_observation_and_single_publication(
    monkeypatch, tmp_path, sample_cells, disable_ctam,
):
    from datetime import datetime, timezone
    from common.ingest.manifest import CycleInputManifest, staged_input_from_path
    from EdgeWARN.stormprob.database import StormProbRepository
    dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    base = CycleInputManifest(dt)
    from EdgeWARN.ctam.preflight import STORMPROB_MRMS_SOURCES
    from EdgeWARN.stormprob.features import UNIVERSAL_PROPERTY_FEATURES
    # Final readiness includes every required source. The observation double
    # supplies complete feature quality so the real dependency gate still runs.
    records = []
    for source in STORMPROB_MRMS_SOURCES:
        source_path = tmp_path / f"MRMS_{source.product}_20240101-000000.grib2"
        source_path.touch()
        records.append(staged_input_from_path(
            source.product, source_path, source="test", family="mrms"))
    rap_path = tmp_path / "RAP_20240101-000000.grib2"
    rap_path.touch()
    records.append(staged_input_from_path("RAP", rap_path, source="test", family="rap"))
    final = base.with_inputs(tuple(records))
    order = []
    handler = MagicMock()
    handler.load_json.return_value = (copy.deepcopy(sample_cells), dt.isoformat())
    monkeypatch.setattr(pipeline, "StatFileHandler", lambda _: handler)
    monkeypatch.setattr(pipeline, "StormCellIntegrator", lambda _: object())
    def enrich(_integrator, cells, **kwargs):
        if kwargs["input_manifest"].inputs:
            order.append("final-enrichment")
            assert not kwargs["include_glm"] and not kwargs["include_rap"]
            cells[0]["properties"]["Ref0"] = 42
        else:
            order.append("base-enrichment")
        return cells
    def final_input():
        order.append("optional-complete")
        return final
    def observe(cells, timestamp, manifest):
        assert cells[0]["properties"]["Ref0"] == 42
        assert manifest is final
        for cell in cells:
            cell["stormprob"] = {"observation": {
                "inference_ready": True,
                "quality": {name: "ok" for name in UNIVERSAL_PROPERTY_FEATURES},
            }}
        order.append("observation")
        return cells
    def ctam(cells, timestamp, disabled, **kwargs):
        assert disabled == disable_ctam
        assert kwargs["input_manifest"] is final
        order.append("ctam")
        return cells, None
    monkeypatch.setattr(pipeline, "_run_parallel_enrichment", enrich)
    monkeypatch.setattr(pipeline, "_attach_stormprob_inputs", observe)
    monkeypatch.setattr(pipeline, "_run_ctam_if_enabled", ctam)
    monkeypatch.setattr(pipeline, "_publish_cycle", lambda *args, **kwargs: order.append("publish"))
    monkeypatch.setattr(StormProbRepository, "commit_cycle", lambda *args, **kwargs: order.append("commit"))
    pipeline.main(json_path=tmp_path / "cells.json", input_manifest=base,
                  disable_ctam=disable_ctam, final_input_provider=final_input)
    expected = ["base-enrichment", "optional-complete", "final-enrichment"]
    if not disable_ctam:
        expected.append("observation")
    assert order == expected + ["commit", "ctam", "publish"]
