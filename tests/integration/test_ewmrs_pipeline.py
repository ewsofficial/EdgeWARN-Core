import gzip
import json
import os
from datetime import datetime, timezone

import EWMRS.pipeline as ewmrs_pipeline
import numpy as np
import rasterio.transform
import xarray as xr
from common.ingest.manifest import CycleInputManifest
from common.ingest.manifest import staged_input_from_path


class _FakeFuture:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class _FakeExecutor:
    def __init__(self, max_workers, initializer=None):
        self.max_workers = max_workers
        self.initializer = initializer
        self.futures = []

    def __enter__(self):
        if self.initializer is not None:
            self.initializer()
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, func, layer):
        future = _FakeFuture(func(layer))
        self.futures.append(future)
        return future


class _FakeQueue:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)


class _FakeEvent:
    def __init__(self):
        self.wait_calls = 0

    def wait(self):
        self.wait_calls += 1


def _manifest(dt):
    return CycleInputManifest(cycle_time=dt)


def test_run_render_pipeline_collects_layer_results(monkeypatch):
    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    created = {}
    cleanup_calls = []
    layers = [
        {"name": "LayerOne"},
        {"name": "LayerTwo"},
    ]

    monkeypatch.setattr(ewmrs_pipeline, "get_file_list", lambda: layers)
    monkeypatch.setattr(ewmrs_pipeline, "cleanup_old_gui_files", lambda max_age_minutes: cleanup_calls.append(max_age_minutes))
    monkeypatch.setattr(ewmrs_pipeline, "_render_layer", lambda layer: (layer["name"], [f"{layer['name']}.png"]))
    monkeypatch.setattr(
        "concurrent.futures.ProcessPoolExecutor",
        lambda max_workers, initializer=None: created.setdefault("executor", _FakeExecutor(max_workers=max_workers, initializer=initializer)),
    )
    monkeypatch.setattr("concurrent.futures.as_completed", lambda futures: list(futures))

    results = ewmrs_pipeline.run_render_pipeline(dt)
    fake_executor = created["executor"]

    assert results == {
        "LayerOne": ["LayerOne.png"],
        "LayerTwo": ["LayerTwo.png"],
    }
    assert fake_executor.max_workers == 2
    assert fake_executor.initializer is ewmrs_pipeline._worker_initializer
    assert cleanup_calls == [120]


def test_run_render_pipeline_binds_manifest_path_before_worker_submit(
    monkeypatch,
    tmp_path,
):
    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    pinned = source_dir / "MRMS_Test_20260317-200000.grib2"
    newer = source_dir / "MRMS_Test_20260317-200200.grib2"
    pinned.write_bytes(b"pinned")
    newer.write_bytes(b"newer")
    manifest = CycleInputManifest(
        cycle_time=dt,
        inputs=(
            staged_input_from_path(
                "Test",
                pinned,
                source="test",
                family="mrms",
            ),
        ),
    )
    submitted = []

    monkeypatch.setattr(
        ewmrs_pipeline,
        "_render_layer",
        lambda layer: submitted.append(layer) or (layer["name"], ["tile.png"]),
    )
    monkeypatch.setattr(
        "concurrent.futures.ProcessPoolExecutor",
        lambda max_workers, initializer=None: _FakeExecutor(
            max_workers=max_workers,
            initializer=initializer,
        ),
    )
    monkeypatch.setattr(
        "concurrent.futures.as_completed",
        lambda futures: list(futures),
    )
    monkeypatch.setattr(
        ewmrs_pipeline,
        "cleanup_old_gui_files",
        lambda **_kwargs: None,
    )

    ewmrs_pipeline.run_render_pipeline(
        dt,
        layers=[
            {
                "name": "Test",
                "filepath": source_dir,
                "outdir": tmp_path / "gui",
            }
        ],
        input_manifest=manifest,
    )

    assert submitted[0]["input_path"] == str(pinned)
    assert submitted[0]["input_manifest_bound"] is True


def test_real_render_executor_publishes_chunk_and_index(monkeypatch, tmp_path):
    import EWMRS.render.config as render_config
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "gui" / "CompRefQC"
    source_dir.mkdir()
    source = source_dir / "MRMS_MergedReflectivityQC_20260317-200000.nc"
    xr.Dataset(
        {"unknown": (("latitude", "longitude"), np.array([[20.0, 30.0], [40.0, 50.0]], dtype=np.float32))},
        coords={"latitude": [35.1, 35.0], "longitude": [-97.1, -97.0]},
    ).to_netcdf(source)

    bounds = (-10_810_000.0, 4_160_000.0, -10_790_000.0, 4_180_000.0)
    monkeypatch.setattr(ewmrs_pipeline, "WEB_MERCATOR_SHAPE", (4, 4))
    monkeypatch.setattr(
        ewmrs_pipeline,
        "WEB_MERCATOR_TRANSFORM",
        rasterio.transform.from_bounds(*bounds, 4, 4),
    )
    monkeypatch.setattr(render_config, "tile_size", lambda: 4)

    result = ewmrs_pipeline.run_render_pipeline(
        datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc),
        layers=[{
            "name": "MRMS_MergedReflectivityQC",
            "colormap_key": "NWS_Reflectivity",
            "filepath": source_dir,
            "outdir": output_dir,
        }],
        cleanup_after=False,
    )

    chunks = result["MRMS_MergedReflectivityQC"]
    assert chunks and all(path.is_file() for path in chunks)
    timestamp_index = json.loads((output_dir / "20260317-200000" / "index.json").read_text())
    product_index = json.loads((output_dir / "index.json").read_text())
    assert timestamp_index["chunks"] == [[0, 0]]
    assert timestamp_index["tile_grid"] == {"rows": 1, "cols": 1, "tile_size": 4}
    assert product_index["timestamps"] == ["20260317-200000"]


def test_render_layer_skips_partial_pinned_input(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    partial = source_dir / ".MRMS_Test_20260317-200000.grib2.part"
    partial.write_bytes(b"partial")

    name, output = ewmrs_pipeline._render_layer({
        "name": "Test",
        "filepath": source_dir,
        "outdir": tmp_path / "gui",
        "input_manifest_bound": True,
        "input_path": str(partial),
    })

    assert name == "Test"
    assert output is None


def test_run_goes_render_pipeline_is_explicit_no_op_without_layers(monkeypatch):
    monkeypatch.setattr(ewmrs_pipeline, "get_goes_file_list", lambda: [])

    results = ewmrs_pipeline.run_goes_render_pipeline(datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc))

    assert results == {}


def test_run_goes_render_pipeline_processes_configured_layers(monkeypatch):
    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    captured = {}

    monkeypatch.setattr(
        ewmrs_pipeline,
        "get_goes_file_list",
        lambda: [{"name": "GOES_ABI_C02_Reflectance", "source_type": "goes_abi"}],
    )

    def fake_run_render_pipeline(
        dt_arg,
        max_entries=10,
        layers=None,
        phase_name="EWMRS",
        cleanup_after=True,
        input_manifest=None,
    ):
        captured["dt"] = dt_arg
        captured["max_entries"] = max_entries
        captured["layers"] = list(layers or [])
        captured["phase_name"] = phase_name
        captured["cleanup_after"] = cleanup_after
        return {"GOES_ABI_C02_Reflectance": ["tile_0_0.png"]}

    monkeypatch.setattr(ewmrs_pipeline, "run_render_pipeline", fake_run_render_pipeline)

    results = ewmrs_pipeline.run_goes_render_pipeline(dt, max_entries=4)

    assert results == {"GOES_ABI_C02_Reflectance": ["tile_0_0.png"]}
    assert captured["dt"] == dt
    assert captured["max_entries"] == 4
    assert captured["phase_name"] == "GOES"
    assert captured["cleanup_after"] is False
    assert captured["layers"] == [{"name": "GOES_ABI_C02_Reflectance", "source_type": "goes_abi"}]


def _chunk_format():
    return {"version": 2, "encoding": "float16", "file_suffix": ".f16.gz", "compression": "gzip", "channels": 1, "value_kind": "scalar", "no_data": "nan", "bytes_per_component": 2, "pixel_row_order": "top_to_bottom", "grid_origin": "bottom_left"}


def _write_chunk(path, tile_size=350):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(bytes(tile_size * tile_size * 2), mtime=0))


def test_current_render_paths_returns_sparse_cached_chunks(tmp_path):
    out_dir = tmp_path / "gui"
    tile_dir = out_dir / "20260317-200000"
    (tile_dir / "chunks").mkdir(parents=True)

    for chunk_name in ("chunk_1_0.f16.gz", "chunk_0_0.f16.gz", "chunk_5_3.f16.gz"):
        _write_chunk(tile_dir / "chunks" / chunk_name)
    (tile_dir / "index.json").write_text(json.dumps({
        "schema_version": 2, "timestamp": "20260317-200000", "representation": "binary_chunks", "chunk_format": _chunk_format(),
        "chunks": [[1, 0], [0, 0], [5, 3]],
        "tile_grid": {"rows": 10, "cols": 20, "tile_size": 350},
    }))

    (out_dir / "index.json").write_text(json.dumps({
        "schema_version": 2, "timestamps": ["20260317-200000"], "representation": "binary_chunks", "chunk_format": {**_chunk_format(), "media_type": "application/octet-stream"},
        "tile_grid": {"rows": 10, "cols": 20, "tile_size": 350},
    }))

    paths = ewmrs_pipeline._current_render_paths(out_dir, "2026-03-17T20:00:00")

    assert paths == [
        tile_dir / "chunks" / "chunk_0_0.f16.gz",
        tile_dir / "chunks" / "chunk_1_0.f16.gz",
        tile_dir / "chunks" / "chunk_5_3.f16.gz",
    ]


def test_current_render_paths_accepts_valid_zero_tile_timestamp(tmp_path):
    out_dir = tmp_path / "gui"
    tile_dir = out_dir / "20260317-200000"
    (tile_dir / "chunks").mkdir(parents=True)
    (tile_dir / "index.json").write_text(json.dumps({
        "schema_version": 2, "timestamp": "20260317-200000", "representation": "binary_chunks", "chunk_format": _chunk_format(), "chunks": [],
        "tile_grid": {"rows": 10, "cols": 20, "tile_size": 350},
    }))
    (out_dir / "index.json").write_text(json.dumps({
        "schema_version": 2, "timestamps": ["20260317-200000"], "representation": "binary_chunks", "chunk_format": {**_chunk_format(), "media_type": "application/octet-stream"},
        "tile_grid": {"rows": 10, "cols": 20, "tile_size": 350},
    }))

    paths = ewmrs_pipeline._current_render_paths(out_dir, "2026-03-17T20:00:00")

    assert paths == []


def test_current_render_paths_rejects_invalid_chunk_index(tmp_path):
    out_dir = tmp_path / "gui"
    tile_dir = out_dir / "20260317-200000"
    (tile_dir / "chunks").mkdir(parents=True)
    _write_chunk(tile_dir / "chunks" / "chunk_0_0.f16.gz")
    (tile_dir / "index.json").write_text(json.dumps({
        "schema_version": 2, "timestamp": "20260317-200000", "representation": "binary_chunks", "chunk_format": _chunk_format(), "chunks": [[0, 0], [20, 0]],
        "tile_grid": {"rows": 10, "cols": 20, "tile_size": 350},
    }))
    (out_dir / "index.json").write_text(json.dumps({
        "schema_version": 2, "timestamps": ["20260317-200000"], "representation": "binary_chunks", "chunk_format": {**_chunk_format(), "media_type": "application/octet-stream"},
        "tile_grid": {"rows": 10, "cols": 20, "tile_size": 350},
    }))

    paths = ewmrs_pipeline._current_render_paths(out_dir, "2026-03-17T20:00:00")

    assert paths is None
    assert ewmrs_pipeline._summarize_results({"A": [], "B": None}) == "1/2 layers succeeded"


def test_cleanup_old_gui_files_uses_dynamic_render_configuration(monkeypatch, tmp_path):
    stale_dir = tmp_path / "stale"
    active_dir = tmp_path / "active"
    stale_dir.mkdir()
    active_dir.mkdir()

    old_timestamp = "20260317-180000"
    active_timestamp_dir = active_dir / old_timestamp
    active_timestamp_dir.mkdir()
    (active_timestamp_dir / "tile_0_0.png").write_bytes(b"tile")
    (active_dir / "index.json").write_text(json.dumps({"timestamps": [old_timestamp]}))

    stale_timestamp_dir = stale_dir / old_timestamp
    stale_timestamp_dir.mkdir()
    (stale_timestamp_dir / "tile_0_0.png").write_bytes(b"tile")

    # A `monkeypatch.setattr(ewmrs_pipeline, "file_list", ..., raising=False)` used
    # to sit here, standing in for the `file_list = get_file_list()` snapshot that
    # cleanup was thought to consult. It never did: the snapshot lived in
    # `render/config.py`, and `pipeline.py` imports the accessors by name, so
    # nothing here ever read the attribute the setattr created. Its absence is
    # guarded where the snapshot actually was, by
    # `test_known_drift.py::test_render_config_no_longer_snapshots_the_file_list_at_import_time`.
    # What is left below is this test's own subject: cleanup calls `get_file_list()`
    # per use, so patching the accessor decides which directories it sweeps.
    monkeypatch.setattr(
        ewmrs_pipeline,
        "get_file_list",
        lambda: [{"outdir": active_dir}],
    )

    ewmrs_pipeline.cleanup_old_gui_files(max_age_minutes=0)

    assert not active_timestamp_dir.exists()
    # Ordered before the index.json assertion, which fails on this platform for an
    # unrelated reason (a "Bad file descriptor" rewriting the file in a temp dir).
    # Behind it, this check would never execute.
    #
    # Only what `get_file_list` named was swept, so an unlisted directory of the
    # same age is untouched.
    assert stale_timestamp_dir.exists()
    assert json.loads((active_dir / "index.json").read_text()) == []


def test_cleanup_old_gui_files_never_touches_nexrad_outputs(monkeypatch, tmp_path):
    """Decomposition Phase 3: only the NEXRAD service cleans NEXRAD files.

    EWMRS GUI cleanup sweeps only the layers it owns; everything beneath
    ``gui/NEXRAD`` is left for ``NEXRAD.gui_pipeline`` even when it is stale.
    """
    import NEXRAD.gui_pipeline as nexrad_gui

    active_dir = tmp_path / "active"
    nexrad_root = tmp_path / "gui" / "NEXRAD"
    stale_elevation_dir = nexrad_root / "KTLH" / "0.5"

    active_dir.mkdir()
    stale_elevation_dir.mkdir(parents=True)
    stale_file = stale_elevation_dir / "KTLH_DBZH_0.5_20260317-170000.bin.gz"
    stale_file.write_bytes(b"stale")

    now = 1_800_000_000
    stale_mtime = now - (3 * 60 * 60)
    os.utime(stale_file, (stale_mtime, stale_mtime))

    monkeypatch.setattr(ewmrs_pipeline, "get_file_list", lambda: [{"outdir": active_dir}])
    monkeypatch.setattr(ewmrs_pipeline.fs, "GUI_NEXRAD_DIR", nexrad_root)
    monkeypatch.setattr(nexrad_gui.fs, "GUI_NEXRAD_DIR", nexrad_root)
    monkeypatch.setattr(nexrad_gui.time, "time", lambda: now)

    ewmrs_pipeline.cleanup_old_gui_files(max_age_minutes=120)

    # Untouched: NEXRAD retention belongs to the NEXRAD service...
    assert stale_file.exists()

    # ...which does remove the same artifact when its own sweep runs.
    nexrad_gui.cleanup_old_nexrad_gui_files(max_age_minutes=120)
    assert not stale_file.exists()


def test_mrms_required_layer_failures_gate_only_required_layers(monkeypatch):
    """Phase 4: required-only gating lives in one helper shared by the consumer.

    Optional-layer failures never fail a stage; unlisted layers are treated as
    optional; an empty result set fails outright.
    """
    monkeypatch.setattr(
        ewmrs_pipeline,
        "get_mrms_file_list",
        lambda: [
            {"name": "Required", "filepath": "/x", "required": True},
            {"name": "Optional", "filepath": "/x", "required": False},
        ],
    )
    failed_required, failed_optional = ewmrs_pipeline.mrms_required_layer_failures({
        "Required": ["out.png"],
        "Optional": None,
        "Unlisted": None,
    })
    assert failed_required == []
    assert failed_optional == ["Optional", "Unlisted"]

    failed_required, _ = ewmrs_pipeline.mrms_required_layer_failures({
        "Required": None,
        "Optional": ["out.png"],
    })
    assert failed_required == ["Required"]

    failed_required, failed_optional = ewmrs_pipeline.mrms_required_layer_failures({})
    assert failed_required == [] and failed_optional == []


def test_ewmrs_goes_worker_runs_goes_phase(monkeypatch):
    queue = _FakeQueue()
    captured = {}

    def fake_run_goes_render_pipeline(dt, max_entries=10, input_manifest=None):
        captured["goes_dt"] = dt
        captured["goes_max_entries"] = max_entries
        return {}

    monkeypatch.setattr(ewmrs_pipeline, "run_goes_render_pipeline", fake_run_goes_render_pipeline)

    dt = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    ewmrs_pipeline.ewmrs_goes_worker(
        queue,
        dt,
        max_entries=3,
        input_manifest=_manifest(dt),
    )

    assert captured == {
        "goes_dt": dt,
        "goes_max_entries": 3,
    }
    assert any("Starting EWMRS GOES render phase" in message for message in queue.messages)
    assert any("0/0 layers succeeded" in message for message in queue.messages)
