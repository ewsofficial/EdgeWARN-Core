"""Tests for EWMRS colormap rendering and image conversion."""
from pathlib import Path
import json
import threading

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

import EWMRS.render.render as render_module
import EWMRS.render.render as render
from EWMRS.render.config import TILE_SIZE
import util.file as fs


class _FakeDataset:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return _FakeDataArray(self._data)

    @property
    def latitude(self):
        coords = MagicMock()
        coords.values = np.array([35.0, 36.0])
        coords.values.shape = (2,)
        return coords


class _FakeDataArray:
    def __init__(self, data):
        self._data = data

    @property
    def values(self):
        return self._data


class TestGetCmap:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.cmap_file = tmp_path / "colormaps.json"
        cmap_data = [
            {
                "colormaps": [
                    {
                        "name": "TestCmap",
                        "interpolate": True,
                        "thresholds": [
                            {"value": 0, "rgb": [0, 0, 0, 255]},
                            {"value": 50, "rgb": [128, 128, 128, 255]},
                            {"value": 100, "rgb": [255, 255, 255, 255]},
                        ],
                    },
                    {
                        "name": "DiscreteCmap",
                        "interpolate": False,
                        "thresholds": [
                            {"value": 0, "rgb": [0, 0, 0, 255]},
                            {"value": 10, "rgb": [255, 0, 0, 255]},
                            {"value": 20, "rgb": [0, 255, 0, 255]},
                        ],
                    },
                ]
            }
        ]
        self.cmap_file.write_text(json.dumps(cmap_data))
        render_module._COLORMAP_CACHE.clear()
        self._orig = fs.GUI_COLORMAP_JSON
        fs.GUI_COLORMAP_JSON = self.cmap_file
        yield
        fs.GUI_COLORMAP_JSON = self._orig
        render_module._COLORMAP_CACHE.clear()

    def test_returns_cached_colormap_on_second_call(self):
        renderer = render.GUILayerRenderer(
            _FakeDataset(np.array([[0]])), Path("/tmp"), "TestCmap", "test", "2026-03-17T20:00:00"
        )
        result1 = renderer._get_cmap()
        result2 = renderer._get_cmap()
        assert result1 is result2

    def test_cache_shared_across_instances(self):
        r1 = render.GUILayerRenderer(_FakeDataset(np.array([[0]])), Path("/tmp"), "TestCmap", "t1", "2026-03-17T20:00:00")
        r2 = render.GUILayerRenderer(_FakeDataset(np.array([[0]])), Path("/tmp"), "TestCmap", "t2", "2026-03-17T20:00:00")
        assert r1._get_cmap() is r2._get_cmap()

    def test_unknown_colormap_key_raises(self):
        renderer = render.GUILayerRenderer(_FakeDataset(np.array([[0]])), Path("/tmp"), "NoSuchCmap", "t", "2026-03-17T20:00:00")
        with pytest.raises(ValueError, match="Colormap 'NoSuchCmap' not found"):
            renderer._get_cmap()

    def test_interpolate_flag_parsed(self):
        renderer = render.GUILayerRenderer(_FakeDataset(np.array([[0]])), Path("/tmp"), "TestCmap", "t", "2026-03-17T20:00:00")
        thresholds, colors, colors_uint8, interpolate = renderer._get_cmap()
        assert interpolate is True
        assert len(thresholds) == 3
        assert colors_uint8.dtype == np.uint8

    def test_discrete_flag_parsed(self):
        renderer = render.GUILayerRenderer(_FakeDataset(np.array([[0]])), Path("/tmp"), "DiscreteCmap", "t", "2026-03-17T20:00:00")
        thresholds, colors, colors_uint8, interpolate = renderer._get_cmap()
        assert interpolate is False


class TestColormapInterpolation:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.cmap_file = tmp_path / "colormaps.json"
        cmap_data = [
            {
                "colormaps": [
                    {
                        "name": "InterpCmap",
                        "interpolate": True,
                        "thresholds": [
                            {"value": 0, "rgb": [0, 0, 0, 255]},
                            {"value": 50, "rgb": [128, 128, 128, 255]},
                            {"value": 100, "rgb": [255, 255, 255, 255]},
                        ],
                    },
                    {
                        "name": "DiscreteCmap",
                        "interpolate": False,
                        "thresholds": [
                            {"value": 0, "rgb": [0, 0, 0, 255]},
                            {"value": 10, "rgb": [255, 0, 0, 255]},
                            {"value": 20, "rgb": [0, 255, 0, 255]},
                        ],
                    },
                ]
            }
        ]
        self.cmap_file.write_text(json.dumps(cmap_data))
        render_module._COLORMAP_CACHE.clear()
        self._orig = fs.GUI_COLORMAP_JSON
        fs.GUI_COLORMAP_JSON = self.cmap_file
        yield
        fs.GUI_COLORMAP_JSON = self._orig
        render_module._COLORMAP_CACHE.clear()

    def _render_data(self, data, colormap_key, outdir):
        ds = _FakeDataset(data)
        outdir.mkdir(parents=True, exist_ok=True)
        thresholds, colors, colors_uint8, interpolate = render._get_cached_cmap(colormap_key)
        return render._scalar_data_to_rgba(data, thresholds, colors, colors_uint8, interpolate)

    def test_interpolated_values_blend_colors(self, tmp_path):
        data = np.array([[0.0], [25.0], [50.0], [75.0], [100.0]])
        rgba = self._render_data(data, "InterpCmap", tmp_path / "out")
        r, g, b = rgba[:, 0, 0], rgba[:, 0, 1], rgba[:, 0, 2]
        assert r[0] == 0 and r[1] == 64  # midpoint interpolates
        assert r[4] == 255  # max value

    def test_out_of_range_high_gets_max_color(self, tmp_path):
        data = np.array([[200.0]])
        rgba = self._render_data(data, "InterpCmap", tmp_path / "out")
        assert rgba[0, 0, :3].tolist() == [255, 255, 255]

    def test_out_of_range_low_gets_min_color(self, tmp_path):
        data = np.array([[-10.0]])
        rgba = self._render_data(data, "InterpCmap", tmp_path / "out")
        assert rgba[0, 0, :3].tolist() == [0, 0, 0]

    def test_nan_values_are_transparent(self, tmp_path):
        data = np.array([[np.nan]])
        rgba = self._render_data(data, "InterpCmap", tmp_path / "out")
        assert rgba[0, 0, 3] == 0  # alpha = 0

    def test_below_first_threshold_is_transparent(self, tmp_path):
        data = np.array([[-5.0]])
        rgba = self._render_data(data, "InterpCmap", tmp_path / "out")
        assert rgba[0, 0, 3] == 0

    def test_discrete_colormap_uses_nearest_bin(self, tmp_path):
        data = np.array([[5.0], [15.0]])
        rgba = self._render_data(data, "DiscreteCmap", tmp_path / "out")
        assert rgba[0, 0, :3].tolist() == [0, 0, 0]  # < 10, clamp to first
        assert rgba[1, 0, :3].tolist() == [255, 0, 0]  # 15 in [10,20) bin -> red

    def test_discrete_beyond_max_clamps_to_last(self, tmp_path):
        data = np.array([[100.0]])
        rgba = self._render_data(data, "DiscreteCmap", tmp_path / "out")
        assert rgba[0, 0, :3].tolist() == [0, 255, 0]


class TestUpdateIndex:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.outdir = tmp_path / "gui" / "layer"
        self.outdir.mkdir(parents=True)
        self._orig = fs.GUI_COLORMAP_JSON
        cmap_data = [{"colormaps": [{"name": "T", "interpolate": True, "thresholds": [{"value": 0, "rgb": [0,0,0,255]}]}]}]
        fs.GUI_COLORMAP_JSON = tmp_path / "cmap.json"
        fs.GUI_COLORMAP_JSON.write_text(json.dumps(cmap_data))
        render_module._COLORMAP_CACHE.clear()
        yield
        fs.GUI_COLORMAP_JSON = self._orig
        render_module._COLORMAP_CACHE.clear()

    def _renderer(self, outdir=None):
        ds = _FakeDataset(np.array([[0.0]]))
        od = outdir or self.outdir
        return render.GUILayerRenderer(ds, od, "T", "Layer", "20260317-200000")

    def test_new_format_with_tile_grid(self, tmp_path):
        (tmp_path / "out").mkdir()
        r = self._renderer(tmp_path / "out")
        r._update_index("20260317-200000", tile_grid={"rows": 14, "cols": 28, "tile_size": 250})
        idx = json.loads((tmp_path / "out" / "index.json").read_text())
        assert idx["timestamps"] == ["20260317-200000"]
        assert idx["tile_grid"] == {"rows": 14, "cols": 28, "tile_size": 250}

    def test_format_includes_schema_without_tile_grid(self, tmp_path):
        (tmp_path / "out").mkdir()
        r = self._renderer(tmp_path / "out")
        r._update_index("20260317-200000", tile_grid=None)
        idx = json.loads((tmp_path / "out" / "index.json").read_text())
        assert idx["schema_version"] == 2
        assert idx["representation"] == "binary_chunks"
        assert "20260317-200000" in idx["timestamps"]

    def test_deduplicates_and_sorts_newest_first(self, tmp_path):
        (tmp_path / "out").mkdir()
        r = self._renderer(tmp_path / "out")
        r._update_index("20260317-200000", tile_grid={"rows": 1, "cols": 1, "tile_size": 100})
        r._update_index("20260317-180000", tile_grid={"rows": 1, "cols": 1, "tile_size": 100})
        r._update_index("20260317-190000", tile_grid={"rows": 1, "cols": 1, "tile_size": 100})
        idx = json.loads((tmp_path / "out" / "index.json").read_text())
        assert idx["timestamps"] == ["20260317-200000", "20260317-190000", "20260317-180000"]

    def test_preserves_tile_grid_from_old_format(self, tmp_path):
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "index.json").write_text(json.dumps(["20260317-180000"]))
        r = self._renderer(tmp_path / "out")
        r._update_index("20260317-190000", tile_grid={"rows": 1, "cols": 1, "tile_size": 100})
        idx = json.loads((tmp_path / "out" / "index.json").read_text())
        assert idx["tile_grid"] == {"rows": 1, "cols": 1, "tile_size": 100}

    def test_corrupt_json_gets_overwritten(self, tmp_path):
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "index.json").write_text("{ invalid json")
        r = self._renderer(tmp_path / "out")
        r._update_index("20260317-200000", tile_grid=None)
        idx = json.loads((tmp_path / "out" / "index.json").read_text())
        assert idx["schema_version"] == 2


class TestConvertToPng:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.cmap_file = tmp_path / "colormaps.json"
        cmap_data = [
            {
                "colormaps": [
                    {
                        "name": "TestCmap",
                        "interpolate": True,
                        "thresholds": [{"value": 0, "rgb": [0, 0, 0, 255]}, {"value": 100, "rgb": [255, 255, 255, 255]}],
                    }
                ]
            }
        ]
        self.cmap_file.write_text(json.dumps(cmap_data))
        render_module._COLORMAP_CACHE.clear()
        self._orig = fs.GUI_COLORMAP_JSON
        fs.GUI_COLORMAP_JSON = self.cmap_file
        yield
        fs.GUI_COLORMAP_JSON = self._orig
        render_module._COLORMAP_CACHE.clear()

    def _make_renderer(self, data, outdir, timestamp="20260317-200000"):
        ds = _FakeDataset(data)
        return render.GUILayerRenderer(ds, outdir, "TestCmap", "TestLayer", timestamp)

    def test_tile_output_false_is_removed(self, tmp_path):
        data = np.array([[50.0, 75.0], [25.0, 100.0]])
        r = self._make_renderer(data, tmp_path / "out")
        with pytest.raises(ValueError, match="no longer writes flat PNG"):
            r.convert_to_png(tile_output=False)

    def test_timestamp_seconds_forced_to_zero(self, tmp_path):
        data = np.full((TILE_SIZE, TILE_SIZE), 50.0)
        r = self._make_renderer(data, tmp_path / "out", timestamp="2026-03-17T20:45:33")
        paths, ts = r.convert_to_png(tile_output=True)
        assert ts == "20260317-204500"
        assert ts[-2:] == "00"

    def test_tile_output_true_writes_timestamp_directory(self, tmp_path):
        side = TILE_SIZE * 2
        data = np.linspace(0.0, 100.0, side * side, dtype=np.float32).reshape(side, side)
        r = self._make_renderer(data, tmp_path / "out")
        paths, ts = r.convert_to_png(tile_output=True)
        assert len(paths) == 4
        assert (tmp_path / "out" / ts / "chunks" / "chunk_0_0.f16.gz").exists()
        idx = json.loads((tmp_path / "out" / "index.json").read_text())
        tile_idx = json.loads((tmp_path / "out" / ts / "index.json").read_text())
        assert idx["tile_grid"] == {"rows": 2, "cols": 2, "tile_size": TILE_SIZE}
        assert idx["timestamps"] == [ts]
        assert tile_idx["chunks"] == [[0, 0], [1, 0], [0, 1], [1, 1]]
        assert tile_idx["chunk_format"]["encoding"] == "float16"
        assert tile_idx["tile_grid"] == {"rows": 2, "cols": 2, "tile_size": TILE_SIZE}

    def test_tile_output_true_skips_fully_transparent_tiles(self, tmp_path):
        side = TILE_SIZE * 2
        data = np.full((side, side), np.nan, dtype=np.float32)
        r = self._make_renderer(data, tmp_path / "out")

        paths, ts = r.convert_to_png(tile_output=True)

        assert paths == []
        assert (tmp_path / "out" / ts).is_dir()
        assert list((tmp_path / "out" / ts / "chunks").glob("chunk_*.f16.gz")) == []
        idx = json.loads((tmp_path / "out" / "index.json").read_text())
        tile_idx = json.loads((tmp_path / "out" / ts / "index.json").read_text())
        assert idx["tile_grid"] == {"rows": 2, "cols": 2, "tile_size": TILE_SIZE}
        assert idx["timestamps"] == [ts]
        assert tile_idx["chunks"] == []

    def test_tile_output_true_writes_only_non_transparent_tiles(self, tmp_path):
        side = TILE_SIZE * 2
        data = np.full((side, side), np.nan, dtype=np.float32)
        data[TILE_SIZE:, :TILE_SIZE] = 50.0
        data[:TILE_SIZE, TILE_SIZE:] = 75.0
        r = self._make_renderer(data, tmp_path / "out")

        paths, ts = r.convert_to_png(tile_output=True)

        expected = {
            tmp_path / "out" / ts / "chunks" / "chunk_0_0.f16.gz",
            tmp_path / "out" / ts / "chunks" / "chunk_1_1.f16.gz",
        }
        assert set(paths) == expected
        assert set((tmp_path / "out" / ts / "chunks").glob("chunk_*.f16.gz")) == expected
        tile_idx = json.loads((tmp_path / "out" / ts / "index.json").read_text())
        assert tile_idx["chunks"] == [[0, 0], [1, 1]]

    def test_rerender_clears_stale_tiles_before_writing(self, tmp_path):
        side = TILE_SIZE * 2
        outdir = tmp_path / "out"
        opaque = np.full((side, side), 50.0, dtype=np.float32)
        transparent = np.full((side, side), np.nan, dtype=np.float32)

        first_renderer = self._make_renderer(opaque, outdir)
        first_paths, ts = first_renderer.convert_to_png(tile_output=True)
        assert len(first_paths) == 4

        second_renderer = self._make_renderer(transparent, outdir)
        second_paths, second_ts = second_renderer.convert_to_png(tile_output=True)

        assert second_ts == ts
        assert second_paths == []
        assert list((outdir / ts / "chunks").glob("chunk_*.f16.gz")) == []
        tile_idx = json.loads((outdir / ts / "index.json").read_text())
        assert tile_idx["chunks"] == []

    def test_unknown_data_key_raises(self, tmp_path, monkeypatch):
        class _BrokenDataset:
            def __getitem__(self, key):
                raise KeyError("unknown")

        r = render.GUILayerRenderer(_BrokenDataset(), tmp_path / "out", "TestCmap", "L", "20260317-200000")
        with pytest.raises(KeyError):
            r.convert_to_png(tile_output=False)
