"""Selected-layer submission and output validation for EWMRS (plan phase 6).

The raster stack is stubbed; these cover the seams the per-input render consumer
depends on: a persistent bounded pool, exact-path pinning, and the difference
between a rendered layer and a *published* one.
"""

import json
from pathlib import Path

import pytest

import EWMRS.pipeline as pipeline


@pytest.fixture(autouse=True)
def runtime_base(monkeypatch, tmp_path):
    import util.file as fs

    monkeypatch.setattr(pipeline.fs, "BASE_DIR", tmp_path)
    fs.initialize_filesystem(tmp_path)


class _FakePool:
    def __init__(self, *, raise_for=()):
        self.raise_for = set(raise_for)
        self.submitted = []
        self.workers = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def submit(self, function, layer):
        from concurrent.futures import Future

        self.submitted.append(layer)
        name = str(layer.get("name"))
        future = Future()
        if name in self.raise_for:
            future.set_exception(RuntimeError("worker died"))
        else:
            future.set_result((name, layer.get("_output", f"{name}.png")))
        return future

    def shutdown(self, **_kwargs):
        return None


def _record(layer):
    return (str(layer["name"]), f"{layer['name']}.png")


def _none_render(layer):
    return (str(layer["name"]), None)


def _explode(layer):
    raise RuntimeError("worker died")


def _publish_mrms(out_dir, timestamp, *, chunks=1, indexed=True, chunk_index=True):
    """Publish exactly what the renderer commits for one layer timestamp."""
    timestamp_dir = Path(out_dir) / timestamp
    (timestamp_dir / "chunks").mkdir(parents=True, exist_ok=True)
    for index in range(chunks):
        (timestamp_dir / "chunks" / f"chunk_{index}_0.f16.gz").write_bytes(b"\x00\x01")
    if chunk_index:
        pipeline.atomic_write_json(timestamp_dir / "index.json", {
            "schema_version": 2, "representation": "binary_chunks",
            "chunks": [[index, 0] for index in range(chunks)],
            "tile_grid": {"rows": 1, "cols": 1},
            "chunk_format": {"encoding": "float16", "file_suffix": ".f16.gz",
                             "compression": "gzip", "bytes_per_component": 2,
                             "channels": 1},
        })
    if indexed:
        pipeline.atomic_write_json(Path(out_dir) / "index.json", {
            "schema_version": 2, "representation": "binary_chunks",
            "timestamps": [timestamp],
            "tile_grid": {"rows": 1, "cols": 1},
        })
    return timestamp_dir


class TestPinnedLayer:
    def test_a_pinned_layer_never_falls_back_to_a_newer_latest_file(self, tmp_path):
        source = tmp_path / "MRMS_Product_20260930-120000.grib2"
        bound = pipeline.pinned_layer({"name": "Layer", "colormap_key": "c",
                                       "filepath": str(tmp_path), "outdir": str(tmp_path / "gui")},
                                      source)
        assert bound["input_path"] == str(source)
        assert bound["input_manifest_bound"] is True
        assert bound["source_type"] == "mrms"

    def test_a_missing_notified_path_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="exact notified path"):
            pipeline.pinned_layer({"name": "Layer", "outdir": str(tmp_path)}, None)


class TestLayerOutputCompleteness:
    def test_a_complete_chunk_set_and_index_is_complete(self, tmp_path):
        out_dir = tmp_path / "gui" / "Layer"
        source = tmp_path / "MRMS_Product_20260930-120000.grib2"
        source.write_bytes(b"x")
        _publish_mrms(out_dir, "20260930-120000")
        layer = pipeline.pinned_layer(
            {"name": "Layer", "colormap_key": "c", "filepath": str(tmp_path),
             "outdir": str(out_dir)}, source)
        assert pipeline.layer_output_complete(layer) is True

    def test_a_missing_chunk_is_an_incomplete_publication(self, tmp_path):
        out_dir = tmp_path / "gui" / "Layer"
        source = tmp_path / "MRMS_Product_20260930-120000.grib2"
        source.write_bytes(b"x")
        timestamp_dir = _publish_mrms(out_dir, "20260930-120000")
        layer = pipeline.pinned_layer(
            {"name": "Layer", "colormap_key": "c", "filepath": str(tmp_path),
             "outdir": str(out_dir)}, source)
        (timestamp_dir / "chunks" / "chunk_0_0.f16.gz").unlink()
        assert pipeline.layer_output_complete(layer) is False

    def test_a_missing_index_is_an_incomplete_publication(self, tmp_path):
        out_dir = tmp_path / "gui" / "Layer"
        source = tmp_path / "MRMS_Product_20260930-120000.grib2"
        source.write_bytes(b"x")
        _publish_mrms(out_dir, "20260930-120000", chunk_index=False)
        layer = pipeline.pinned_layer(
            {"name": "Layer", "colormap_key": "c", "filepath": str(tmp_path),
             "outdir": str(out_dir)}, source)
        assert pipeline.layer_output_complete(layer) is False

    def test_rap_requires_data_metadata_and_an_indexed_timestamp(self, tmp_path):
        out_dir = tmp_path / "gui" / "RAP" / "Layer"
        source = tmp_path / "RAP.20260930-12z.awp130pgrbf00.grib2"
        source.write_bytes(b"x")
        layer = {"name": "RAP_Layer", "outdir": str(out_dir), "input_path": str(source),
                 "source_type": "rap_uint16", "render_timestamp": "20260930-120000"}
        assert pipeline.layer_output_complete(layer) is False
        (out_dir / "20260930-120000").mkdir(parents=True)
        (out_dir / "20260930-120000" / "data.u16").write_bytes(b"\x00\x01")
        assert pipeline.layer_output_complete(layer) is False
        (out_dir / "20260930-120000" / "metadata.json").write_text("{}")
        assert pipeline.layer_output_complete(layer) is False
        pipeline.atomic_write_json(out_dir / "index.json",
                                   {"timestamps": ["20260930-120000"]})
        assert pipeline.layer_output_complete(layer) is True
        pipeline.atomic_write_json(out_dir / "index.json", {"timestamps": []})
        assert pipeline.layer_output_complete(layer) is False


class TestRenderLayerPool:
    def test_a_layer_returning_none_is_a_failed_job(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_render_layer", _none_render)
        pool = pipeline.RenderLayerPool(max_workers=1)
        try:
            assert pool.render([{"name": "A", "outdir": None}]) == {"A": None}
        finally:
            pool.shutdown()

    def test_a_raising_worker_is_a_failed_job_not_a_lost_one(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_render_layer", _explode)
        pool = pipeline.RenderLayerPool(max_workers=1)
        try:
            assert pool.render([{"name": "A"}]) == {"A": None}
        finally:
            pool.shutdown()

    def test_a_closed_pool_refuses_new_work(self, monkeypatch):
        pool = pipeline.RenderLayerPool(max_workers=1)
        pool.shutdown()
        with pytest.raises(RuntimeError, match="shut down"):
            pool.render([{"name": "A"}])
        # Shutdown is idempotent.
        pool.shutdown()

    def test_the_pool_is_long_lived_across_many_batches(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_render_layer", _record)
        pool = pipeline.RenderLayerPool(max_workers=1)
        executor = pool._executor
        try:
            for index in range(5):
                assert pool.render([{"name": f"L{index}"}]) == {f"L{index}": f"L{index}.png"}
            assert pool._executor is executor
        finally:
            pool.shutdown()

    def test_the_worker_budget_comes_from_the_catalog(self):
        assert 1 <= pipeline.render_worker_budget() <= pipeline.worker_max_workers()
        assert 1 <= pipeline.render_worker_budget("MRMS")


class TestSelectedLayerRendering:
    def test_only_the_requested_layers_are_submitted(self, monkeypatch):
        fake = _FakePool()
        monkeypatch.setattr("concurrent.futures.ProcessPoolExecutor",
                            lambda **_kwargs: fake)
        monkeypatch.setattr(pipeline, "layer_output_complete", lambda _layer: True)
        pool = pipeline.RenderLayerPool(max_workers=1)
        try:
            layers = [{"name": "A", "outdir": None}, {"name": "B", "outdir": None}]
            results = pipeline.render_input_layers(pool, layers)
            assert results == {"A": True, "B": True}
            assert [layer["name"] for layer in fake.submitted] == ["A", "B"]
        finally:
            pool.shutdown()

    def test_an_unusable_publication_is_reported_as_a_failure(self, monkeypatch):
        fake = _FakePool()
        monkeypatch.setattr("concurrent.futures.ProcessPoolExecutor",
                            lambda **_kwargs: fake)
        monkeypatch.setattr(pipeline, "layer_output_complete", lambda _layer: False)
        pool = pipeline.RenderLayerPool(max_workers=1)
        try:
            results = pipeline.render_input_layers(
                pool, [{"name": "A", "outdir": None, "_output": None}])
            assert results == {"A": False}
        finally:
            pool.shutdown()
