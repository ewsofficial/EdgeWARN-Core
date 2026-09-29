"""Disabled consumer diagnostics must never resolve or scan raw directories."""
from common.ingest.mrms.registry import build_registry


def test_render_disabled_layer_has_diagnostic_without_path_lookup(tmp_path, monkeypatch):
    from EWMRS.render import config
    from common.ingest.mrms import config as ingest
    registry = build_registry({"products": []}, tmp_path)
    monkeypatch.setattr(ingest, "get_registry", lambda: registry)
    monkeypatch.setattr(config, "_render_config", lambda: {"mrms_layers": [{
        "name": "MESH", "product": "MESH_00.50", "outdir": "old",
        "colormap_key": "mesh",
    }]})
    monkeypatch.setattr(config, "_resolve_dir", lambda _: (_ for _ in ()).throw(AssertionError("probe")))
    assert config.get_mrms_file_list() == []
    assert config.get_mrms_file_list(include_inactive=True)[0]["reason"] == "ingestion-disabled"


def test_stats_disabled_diagnostics_and_active_identity(tmp_path, monkeypatch):
    from EdgeWARN.process.integrate import config
    registry = build_registry({"products": []}, tmp_path)
    monkeypatch.setattr(config.fs, "MRMS_REGISTRY", registry)
    monkeypatch.setattr(config, "load_config", lambda *a, **kw: {"schema_version": 2})
    monkeypatch.setattr(config, "section", lambda *a: [
        {"name": "MESH", "product": "MESH_00.50", "key": "x", "method": "max"},
        {"name": "reflectivity", "product": "MergedReflectivityQCComposite_00.50", "key": "x", "method": "max"},
    ])
    assert len(config.get_datasets_config()) == 1
    diagnostic = config.get_datasets_config(include_inactive=True)
    assert diagnostic[0]["filepath"] is None
    assert diagnostic[0]["active"] is False
    assert diagnostic[1]["product"] == "MergedReflectivityQCComposite_00.50"


def test_pinned_selection_uses_identity_family_and_current_role(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from common.ingest.manifest import CycleInputManifest, StagedInput
    from EdgeWARN.process.integrate import pipeline
    from common.ingest.mrms import config as ingest
    monkeypatch.setattr(ingest, "get_registry", lambda: build_registry({"products": ["MRMS_MESH_00.50"]}, tmp_path))
    monkeypatch.setattr(pipeline.fs, "latest_files", lambda *a: (_ for _ in ()).throw(AssertionError("fallback")))
    now = datetime.now(timezone.utc)
    def record(product, role, family="mrms"):
        return StagedInput(product=product, path=str(tmp_path / family / role / product),
                           analysis_time=now, source="test", family=family, role=role)
    previous = record("MESH_00.50", "previous")
    wrong_family = record("MESH_00.50", "current", "rap")
    manifest = CycleInputManifest(now, (previous, wrong_family))
    assert pipeline._selected_input_path(tmp_path, manifest, "MESH_00.50") is None
    current = record("MESH_00.50", "current")
    assert pipeline._selected_input_path(tmp_path, manifest.with_inputs((current,)), "MESH_00.50") == current.local_path
    assert pipeline._selected_input_path(tmp_path, None, "VIL_00.50") is None


def test_detection_previous_only_cannot_become_single_current_frame(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from common.ingest.manifest import CycleInputManifest, StagedInput
    from EdgeWARN import pipeline
    from common.ingest.mrms import config as ingest
    monkeypatch.setattr(ingest, "get_registry", lambda: build_registry({"products": []}, tmp_path))
    now = datetime.now(timezone.utc)
    previous = StagedInput(product="MergedReflectivityQCComposite_00.50",
                           path=str(tmp_path / "previous.grib2"), analysis_time=now,
                           source="test", family="mrms", role="previous")
    monkeypatch.setattr(pipeline.fs, "latest_files", lambda *a: (_ for _ in ()).throw(AssertionError("fallback")))
    assert pipeline._prepare_realtime_detection_inputs(lambda _: None, CycleInputManifest(now, (previous,))) == (None,) * 6
