import json

from EWMRS.rap import uint16_pipeline


def test_cleanup_old_rap_uint16_layers_returns_removed_count(monkeypatch, tmp_path):
    gui_rap_dir = tmp_path / "gui" / "RAP"
    layer_dir = gui_rap_dir / "LayerA"
    layer_dir.mkdir(parents=True)

    for timestamp in (
        "20260708-120000",
        "20260708-115900",
        "20260708-115800",
        "20260708-115700",
        "20260708-115600",
    ):
        ts_dir = layer_dir / timestamp
        ts_dir.mkdir()
        (ts_dir / "data.u16").write_bytes(b"test")

    (layer_dir / "index.json").write_text(json.dumps({"timestamps": ["20260708-120000"]}))

    monkeypatch.setattr(uint16_pipeline.fs, "GUI_RAP_DIR", gui_rap_dir)

    removed = uint16_pipeline.cleanup_old_rap_uint16_layers(max_timestamps=3)

    assert removed == 2
    remaining = sorted(path.name for path in layer_dir.iterdir() if path.is_dir())
    assert remaining == ["20260708-115800", "20260708-115900", "20260708-120000"]
