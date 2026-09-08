from datetime import datetime, timezone

import pytest

import common.ingest.mrms.main as mrms_main


def test_get_ewmrs_modifiers_matches_render_directories(monkeypatch):
    render_layers = [
        {"filepath": "/data/shared/compref"},
        {"filepath": "/data/shared/azshear"},
    ]
    mrms_modifiers = [
        ("CONUS", "MergedReflectivityQCComposite_00.50", "/data/shared/compref"),
        ("CONUS", "MergedAzShear_0-2kmAGL_00.50", "/data/shared/azshear"),
        ("CONUS", "MESH_00.50", "/data/shared/mesh"),
    ]

    monkeypatch.setattr(mrms_main, "get_mrms_modifiers", lambda: mrms_modifiers)
    monkeypatch.setattr("EWMRS.render.config.get_file_list", lambda: render_layers)

    assert mrms_main.get_ewmrs_modifiers() == [
        "MergedReflectivityQCComposite_00.50",
        "MergedAzShear_0-2kmAGL_00.50",
    ]


def test_get_ewmrs_support_modifiers_excludes_detection_inputs(monkeypatch):
    monkeypatch.setattr(
        mrms_main,
        "get_ewmrs_modifiers",
        lambda: [
            "MergedReflectivityQCComposite_00.50",
            "PrecipFlag_00.00",
            "MergedAzShear_0-2kmAGL_00.50",
        ],
    )

    assert mrms_main.get_ewmrs_support_modifiers() == ["MergedAzShear_0-2kmAGL_00.50"]


@pytest.mark.asyncio
async def test_download_ewmrs_files_async_uses_render_subset(monkeypatch):
    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    mrms_modifiers = [
        ("CONUS", "MergedAzShear_0-2kmAGL_00.50", "/tmp/azshear"),
        ("CONUS", "MESH_00.50", "/tmp/mesh"),
    ]
    captured = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)

    async def fake_cleanup(folder, **kwargs):
        return None

    monkeypatch.setattr(mrms_main, "get_mrms_modifiers", lambda: mrms_modifiers)
    monkeypatch.setattr(mrms_main, "get_ewmrs_support_modifiers", lambda: ["MergedAzShear_0-2kmAGL_00.50"])
    monkeypatch.setattr(mrms_main, "run_ingestion_pipeline", fake_pipeline)
    monkeypatch.setattr(mrms_main.fs, "async_clean_old_files", fake_cleanup)
    monkeypatch.setattr(
        mrms_main,
        "download_all_files_async_internal",
        lambda *args, **kwargs: "download-task",
    )

    await mrms_main.download_ewmrs_files_async(dt, max_entries=7, remove_old_files=True)

    assert captured["cleanup_dirs"] == ["/tmp/azshear"]
    assert captured["cleanup_async"] is fake_cleanup
    assert captured["cleanup_kwargs"] == {"max_age_minutes": 60}
    assert "cleanup_message" not in captured
    assert captured["async_downloads"] == ["download-task"]
