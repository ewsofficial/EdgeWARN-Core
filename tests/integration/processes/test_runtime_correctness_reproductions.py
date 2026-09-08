"""Failure reproductions for the runtime-correctness remediation plan.

These tests describe the intended safe behavior before the production fixes
land.  They are strict expected failures so the test-only Phase 0 commit keeps
the repository green.  Each remediation phase must remove the corresponding
``xfail`` marker when it makes the behavior pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError

import numpy as np
import pytest


PHASE_3 = pytest.mark.xfail(
    strict=True,
    reason="Phase 3: transactional ingest/publication is not implemented",
)
PHASE_4 = pytest.mark.xfail(
    strict=True,
    reason="Phase 4: single-frame and historical correctness is not implemented",
)
PHASE_5 = pytest.mark.xfail(
    strict=True,
    reason="Phase 5: NEXRAD/WPC lifecycle ownership is not implemented",
)


def test_scheduler_rejects_common_time_when_one_required_source_is_empty(monkeypatch):
    """Every required source must participate in the timestamp intersection."""
    from EdgeWARN.schedule.scheduler import MRMSUpdateChecker

    checker = MRMSUpdateChecker.__new__(MRMSUpdateChecker)
    checker.verbose = False
    target = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    results = iter(({target}, set(), {target}))
    monkeypatch.setattr(checker, "_get_modifier_times", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(checker, "check_https_fallback", lambda *_args, **_kwargs: None)

    modifiers = [
        ("CONUS", "MergedReflectivityQCComposite_00.50", Path("/unused/refl")),
        ("CONUS", "PrecipFlag_00.00", Path("/unused/ptype")),
        ("CONUS", None, Path("/unused/probsevere")),
    ]

    assert checker.latest_common_minute_1h(modifiers, reference_dt=target) is None


@pytest.mark.parametrize(
    ("stage_status", "exit_status", "retryable"),
    [
        ("unavailable", 0, True),
        ("failed", 0, True),
        ("failed", 17, True),
        ("completed", 0, False),
    ],
)
def test_cycle_outcome_contract_covers_failure_and_retry_states(
    stage_status,
    exit_status,
    retryable,
):
    """Unavailable, caught exceptions, and child exits must be authoritative."""
    from util.runtime.cycle import CycleOutcome, CycleStageResult, CycleStatus

    status = CycleStatus(stage_status)
    stage = CycleStageResult(
        status=status,
        produced_artifacts=(),
        errors=(() if status is CycleStatus.COMPLETED else ("reproduced failure",)),
        worker_exit_status=exit_status,
    )
    outcome = CycleOutcome(
        timestamp=datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc),
        stages={"edgewarn": stage},
        retryable=retryable,
    )

    assert outcome.retryable is retryable
    assert outcome.completed is (status is CycleStatus.COMPLETED and exit_status == 0)


def test_single_frame_detects_current_radar_and_updates_index(monkeypatch, tmp_path):
    """A previous snapshot is context, never a substitute for current detection."""
    import util.file as fs
    from EdgeWARN.process.detect import main as detect_main

    original_base = fs.BASE_DIR
    try:
        fs._define_paths(tmp_path)
        fs.STORMCELL_DIR.mkdir(parents=True, exist_ok=True)
        prior = fs.STORMCELL_DIR / "stormcells_20260726-175800.json"
        prior.write_text(
            json.dumps(
                {
                    "latest_timestamp": "2026-07-26T17:58:00+00:00",
                    "features": [{"id": 9, "centroid": [35.0, -97.0]}],
                }
            ),
            encoding="utf-8",
        )
        radar = tmp_path / "MRMS_20260726-180000.grib2"
        radar.write_bytes(b"current radar")
        detector = MagicMock(return_value=([{"id": 10, "centroid": [35.1, -96.9]}], None))

        monkeypatch.setattr(
            detect_main.DetectionDataHandler,
            "find_timestamp",
            lambda _path: "2026-07-26T18:00:00+00:00",
        )
        monkeypatch.setattr(detect_main, "_detect_with_optional_probsevere", detector)
        monkeypatch.setattr(
            detect_main,
            "match_alerts_to_cells",
            lambda entries, *_args, **_kwargs: entries,
        )

        output_path, _ = detect_main.main(
            radar,
            None,
            None,
            None,
            None,
            None,
            (20, 55),
            (-130, -60),
            cleanup_stormcells=False,
        )

        detector.assert_called_once()
        output = json.loads(output_path.read_text(encoding="utf-8"))
        assert [feature["id"] for feature in output["features"]] == [10]
        index = json.loads((fs.STORMCELL_DIR / "stormcell_index.json").read_text(encoding="utf-8"))
        assert "20260726-180000" in index["timestamps"]
    finally:
        fs._define_paths(original_base)


def test_single_frame_keeps_newer_radar_when_optional_inputs_are_missing(monkeypatch, tmp_path):
    """A missing current ProbSevere/precipitation input is degraded, not stale."""
    import util.file as fs
    from EdgeWARN.process.detect import main as detect_main

    original_base = fs.BASE_DIR
    try:
        fs._define_paths(tmp_path)
        old_radar = tmp_path / "MRMS_20260726-175800.grib2"
        new_radar = tmp_path / "MRMS_20260726-180000.grib2"
        old_radar.write_bytes(b"old")
        new_radar.write_bytes(b"new")
        detector = MagicMock(return_value=([], None))
        monkeypatch.setattr(
            detect_main.DetectionDataHandler,
            "find_timestamp",
            lambda path: "2026-07-26T18:00:00+00:00" if Path(path) == new_radar else "2026-07-26T17:58:00+00:00",
        )
        monkeypatch.setattr(detect_main, "_detect_with_optional_probsevere", detector)
        monkeypatch.setattr(detect_main, "match_alerts_to_cells", lambda entries, *_args, **_kwargs: entries)

        output_path, _ = detect_main.main(
            old_radar, new_radar, None, None, None, None,
            (20, 55), (-130, -60), cleanup_stormcells=False,
        )

        assert detector.call_args.args[0] == new_radar
        assert output_path.name == "stormcells_20260726-180000.json"
    finally:
        fs._define_paths(original_base)


def test_mrms_batch_fails_when_decompression_returns_none(monkeypatch, tmp_path):
    """A downloaded gzip is not staged until decompression succeeds."""
    from common.ingest.mrms import downloader

    target_time = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    gzip_path = tmp_path / "MRMS_Test_20260726-180000.grib2.gz"
    gzip_path.write_bytes(b"not relevant")

    class Finder:
        def __init__(self, *_args, **_kwargs):
            pass

        def lookup_files(self, *_args, **_kwargs):
            return [("remote.grib2.gz", target_time)]

    class Download:
        def __init__(self, *_args, **_kwargs):
            pass

        def download_matching(self, *_args, **_kwargs):
            return gzip_path

        def decompress_file(self, _path):
            return None

    monkeypatch.setattr(downloader, "FileFinder", Finder)
    monkeypatch.setattr(downloader, "FileDownloader", Download)

    label, ok = downloader.download_modifier_sync(
        "CONUS",
        "Test",
        tmp_path,
        target_time,
        10,
    )

    assert label == "Test"
    assert ok is None


class _DisconnectingBody:
    def __init__(self):
        self._sent = False

    async def iter_chunks(self):
        if not self._sent:
            self._sent = True
            yield b"partial"
        raise ConnectionError("mid-stream disconnect")


class _S3Client:
    async def get_object(self, **_kwargs):
        return {"Body": _DisconnectingBody()}


class _AsyncFile:
    """Small deterministic aiofiles stand-in for disconnect fault injection."""

    def __init__(self, path, mode):
        self._handle = open(path, mode)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self._handle.close()
        return False

    async def write(self, payload):
        return self._handle.write(payload)


def test_mrms_s3_disconnect_leaves_no_final_filename(monkeypatch, tmp_path):
    import common.ingest.mrms.s3_async as s3_async

    target_time = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    final_path = tmp_path / "MRMS_Test_20260726-180000.grib2.gz"
    monkeypatch.setattr(s3_async.aiofiles, "open", _AsyncFile)
    downloader = s3_async.AsyncFileDownloader(
        target_time,
        "bucket",
        MagicMock(),
        s3_client=_S3Client(),
    )

    result = asyncio.run(
        downloader.async_download_matching(
            [(f"prefix/{final_path.name}", target_time)],
            tmp_path,
        )
    )

    assert result is None
    assert not final_path.exists()


def test_synoptic_s3_disconnect_leaves_no_final_filename(monkeypatch, tmp_path):
    import common.ingest.synoptic.s3_async as synoptic_s3

    final_path = tmp_path / "rap.grib2"
    monkeypatch.setattr(synoptic_s3.aiofiles, "open", _AsyncFile)
    downloader = synoptic_s3.AsyncSynopticFileDownloader(
        "bucket",
        MagicMock(),
        s3_client=_S3Client(),
    )

    with pytest.raises(ConnectionError):
        asyncio.run(downloader.async_download_file("rap/key", final_path))

    assert not final_path.exists()


def test_mrms_https_disconnect_leaves_no_final_filename(monkeypatch, tmp_path):
    import common.ingest.mrms.https_client as https_client

    class Content:
        def __init__(self):
            self.calls = 0

        async def read(self, _size):
            self.calls += 1
            if self.calls == 1:
                return b"partial"
            raise ConnectionError("mid-stream disconnect")

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def get(self, _url):
            return Response()

    monkeypatch.setattr(https_client.aiohttp, "ClientSession", Session)
    target_time = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    final_path = tmp_path / "MRMS_Test_20260726-180000.grib2.gz"

    result = asyncio.run(
        https_client.HttpsFileDownloader(target_time).download_matching(
            [f"https://example.invalid/{final_path.name}"],
            tmp_path,
        )
    )

    assert result is None
    assert not final_path.exists()


def test_gzip_failure_after_partial_output_leaves_no_final_filename(monkeypatch, tmp_path):
    import gzip
    import common.ingest.mrms.s3_async as s3_async

    gzip_path = tmp_path / "input.grib2.gz"
    output_path = tmp_path / "input.grib2"
    with gzip.open(gzip_path, "wb") as handle:
        handle.write(b"complete compressed content")

    def fail_after_partial(source, destination, **_kwargs):
        destination.write(source.read(3))
        raise OSError("simulated disk failure")

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(s3_async.shutil, "copyfileobj", fail_after_partial)
    monkeypatch.setattr(s3_async.asyncio, "to_thread", run_inline)
    downloader = s3_async.AsyncFileDownloader(
        datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc),
        "bucket",
        MagicMock(),
        s3_client=MagicMock(),
    )

    assert asyncio.run(downloader.async_decompress_file(gzip_path)) is None
    assert not output_path.exists()


def _historical_args(tmp_path, start, end):
    return SimpleNamespace(
        start=start.isoformat(),
        end=end.isoformat(),
        lat=[20, 55],
        lon=[-130, -60],
        base_dir=tmp_path,
        profile=False,
        disable_ctam=False,
        disable_tracking=False,
        disable_polygon_expansion=False,
        refl_threshold=37.5,
        min_seed_percentage=0.001,
        drop_offset=10.0,
        config_dir=None,
    )


@pytest.mark.parametrize("failure_mode", ["no_artifact", "exception"])
def test_historical_failed_timestamp_is_retried(monkeypatch, tmp_path, failure_mode):
    import process_historical

    start = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=1)
    calls = []

    class Checker:
        def __init__(self, **_kwargs):
            pass

        def latest_common_minute_1h(self, *_args, **_kwargs):
            return start

    def pipeline(*_args, **_kwargs):
        calls.append(start)
        if len(calls) == 1 and failure_mode == "exception":
            raise RuntimeError("reproduced historical failure")
        return None

    monkeypatch.setattr(process_historical.io_manager, "get_historical_args", lambda: _historical_args(tmp_path, start, end))
    monkeypatch.setattr(process_historical, "initialize_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(process_historical, "MRMSUpdateChecker", Checker)
    monkeypatch.setattr(process_historical, "historical_pipeline", pipeline)
    monkeypatch.setattr(process_historical.mrms_config, "get_check_modifiers", lambda: [("CONUS", "Test", tmp_path)])
    monkeypatch.setattr(process_historical.time, "sleep", lambda _seconds: None)

    process_historical.main()

    assert len(calls) == 2


def test_historical_output_validation_rejects_previous_timestamp(tmp_path):
    import process_historical

    requested = datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
    stale = tmp_path / "stormcells.json"
    stale.write_text(
        json.dumps({"latest_timestamp": "2026-07-26T17:58:00+00:00"}),
        encoding="utf-8",
    )

    assert not process_historical._validated_historical_output(stale, requested)


def test_historical_alert_target_before_oldest_snapshot_returns_no_alerts(tmp_path):
    from EdgeWARN.process.detect.tools.alert_matcher import load_active_alerts

    registry = tmp_path / "alerts"
    ids_dir = registry / "ids"
    timestamps_dir = registry / "timestamps"
    ids_dir.mkdir(parents=True)
    timestamps_dir.mkdir(parents=True)
    alert_id = "urn:example:future"
    safe_id = alert_id.replace(":", "_").replace("/", "_") + ".json"
    (ids_dir / safe_id).write_text(
        json.dumps({"feature": {"id": alert_id, "properties": {"event": "Tornado Warning"}}}),
        encoding="utf-8",
    )
    (timestamps_dir / "20260726-180000.json").write_text(
        json.dumps({"alerts": [alert_id]}),
        encoding="utf-8",
    )

    assert load_active_alerts(registry, "2026-07-26T17:00:00+00:00") == []


def test_wpc_paths_follow_runtime_base_directory(tmp_path):
    import util.file as fs
    from common.ingest.wpc import downloader as wpc_downloader

    original_base = fs.BASE_DIR
    try:
        fs._define_paths(tmp_path)
        assert wpc_downloader.get_latest_output_filepath().is_relative_to(tmp_path)
    finally:
        fs._define_paths(original_base)


def test_wpc_fallback_returns_actual_analysis_timestamp(monkeypatch):
    from common.ingest.wpc import downloader as wpc_downloader

    requested = datetime(2026, 7, 26, 18, 30, tzinfo=timezone.utc)
    fallback = datetime(2026, 7, 26, 15, 0, tzinfo=timezone.utc)
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return b"fallback surface"

    def urlopen(url, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise HTTPError(url, 404, "not found", None, None)
        return Response()

    monkeypatch.setattr(wpc_downloader.urllib.request, "urlopen", urlopen)

    assert wpc_downloader.download_coded_surface(requested) == ("fallback surface", fallback)


def test_wpc_download_uses_normal_tls_verification(monkeypatch):
    from common.ingest.wpc import downloader as wpc_downloader

    contexts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return b"surface"

    def urlopen(_url, **kwargs):
        contexts.append(kwargs["context"])
        return Response()

    monkeypatch.setattr(wpc_downloader.urllib.request, "urlopen", urlopen)
    wpc_downloader.download_coded_surface(
        datetime(2026, 7, 26, 18, 30, tzinfo=timezone.utc)
    )

    assert contexts[0].check_hostname is True
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED


def test_wpc_cleanup_matches_timestamped_names_and_preserves_latest(monkeypatch, tmp_path):
    from common.ingest.wpc import main as wpc_main

    timestamped = tmp_path / "wpc_sfc_20260726-120000.geojson"
    latest = tmp_path / "latest.geojson"
    timestamped.write_text("{}", encoding="utf-8")
    latest.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(timestamped, (old, old))
    os.utime(latest, (old, old))
    monkeypatch.setattr(wpc_main.fs, "WPC_SFC_DIR", tmp_path)

    wpc_main.clean_old_files(max_age_minutes=1)

    assert not timestamped.exists()
    assert latest.exists()


def test_concurrent_nexrad_pool_creation_builds_one_generation(monkeypatch):
    import common.ingest.nexrad.worker_pool as worker_pool

    created = []
    created_lock = threading.Lock()

    class Pool:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def shutdown(self, wait=True):
            pass

    def create(max_workers):
        with created_lock:
            created.append(max_workers)
        time.sleep(0.05)
        return Pool(max_workers)

    monkeypatch.setattr(worker_pool, "_POOL", None)
    monkeypatch.setattr(worker_pool, "_POOL_SIZE", 0)
    monkeypatch.setattr(worker_pool, "_VOLUME_COUNT", 0)
    monkeypatch.setattr(worker_pool, "NexradWorkerPool", create)

    with ThreadPoolExecutor(max_workers=8) as executor:
        pools = list(executor.map(lambda _index: worker_pool.get_nexrad_pool(3), range(8)))

    assert len(created) == 1
    assert len({id(pool) for pool in pools}) == 1


def test_json_reader_never_observes_partial_index(monkeypatch, tmp_path):
    from EdgeWARN.api_integration import index_manager as index_module
    from util.atomic import atomic_output_path

    storm_dir = tmp_path / "stormcell"
    cell_dir = tmp_path / "cell"
    storm_dir.mkdir()
    cell_dir.mkdir()
    index_path = cell_dir / "cell_index.json"
    index_path.write_text('{"cellIds":[],"lastUpdated":"old"}', encoding="utf-8")
    manager = index_module.APIIndexManager(MagicMock())
    manager.cell_index_path = index_path
    manager.cell_timestamps = {"1": time.time()}
    started = threading.Event()
    release = threading.Event()

    def slow_atomic_write(_path, _data, **_kwargs):
        with atomic_output_path(_path) as temporary:
            temporary.write_text('{"cellIds":', encoding="utf-8")
            started.set()
            release.wait(timeout=2)
            with temporary.open("a", encoding="utf-8") as handle:
                handle.write('[1],"lastUpdated":"new"}')

    monkeypatch.setattr(index_module, "atomic_write_json", slow_atomic_write)
    writer = threading.Thread(target=manager._write_cell_index)
    writer.start()
    assert started.wait(timeout=2)
    parse_errors = []
    for _ in range(30):
        try:
            json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            parse_errors.append(exc)
    release.set()
    writer.join(timeout=2)

    assert not parse_errors


def test_binary_reader_never_observes_partial_nexrad_artifact(monkeypatch, tmp_path):
    from NEXRAD import render as nexrad

    path = tmp_path / "field.bin.gz"
    old_payload = b"OLD_COMPLETE"
    path.write_bytes(old_payload)
    started = threading.Event()
    release = threading.Event()

    class SlowHandle:
        def __init__(self, target):
            self.handle = open(target, "wb")
            self.first = True

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.handle.close()
            return False

        def write(self, payload):
            written = self.handle.write(payload)
            self.handle.flush()
            if self.first:
                self.first = False
                started.set()
                release.wait(timeout=2)
            return written

    class ValidatedHandle:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _size):
            return nexrad.NEXRAD_FIELD_MAGIC

    monkeypatch.setattr(nexrad.gzip, "open", lambda target, _mode: SlowHandle(target))
    monkeypatch.setattr(nexrad.gzip, "GzipFile", lambda **_kwargs: ValidatedHandle())
    writer = threading.Thread(
        target=nexrad._write_nexrad_variable_bin,
        args=(
            path,
            np.ones((2, 2), dtype=np.float32),
            np.array([0.0, 1.0], dtype=np.float32),
            np.array([0.0, 1000.0], dtype=np.float32),
        ),
    )
    writer.start()
    assert started.wait(timeout=2)
    observations = [path.read_bytes() for _ in range(30)]
    release.set()
    writer.join(timeout=2)

    assert observations == [old_payload] * len(observations)


def test_drain_log_queue_uses_get_nowait_not_empty(monkeypatch):
    import queue
    from util.runtime.logging import drain_log_queue

    q = queue.Queue()
    q.put("a")
    q.put("b")
    q.put("c")
    calls = []

    monkeypatch.setattr("builtins.print", lambda msg: calls.append(msg))
    drain_log_queue(q)

    assert calls == ["a", "b", "c"]
    assert q.empty()


def test_supervisor_restarts_dead_process(monkeypatch, tmp_path):
    import multiprocessing

    from util.runtime.processes import AccessorySupervisor

    # File-sentinel worker on purpose: the child is killed below, and a killed
    # process can die holding a shared multiprocessing.Event's semaphore lock,
    # which poisons the event for every later set()/wait()/clear() -- including
    # the parent's, hanging the suite. A filesystem sentinel has no such state.
    stop_file = tmp_path / "stop"

    def worker(stop_path):
        while not os.path.exists(stop_path):
            time.sleep(0.01)

    supervisor = AccessorySupervisor(
        health_path=str(tmp_path / "health.json"),
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.1,
        restart_window_seconds=60,
    )
    supervisor.add("test", worker, args=(str(stop_file),), enabled=True)
    supervisor.start_all()
    assert len([p for p in supervisor._process_info if p["process"] is not None and p["process"].is_alive()]) == 1

    # Kill the process
    proc = supervisor._process_info[0]["process"]
    proc.kill()
    proc.join(timeout=2)
    assert not proc.is_alive()

    # Supervisor check should detect death and restart
    supervisor.check()
    time.sleep(0.2)
    supervisor.check()

    new_proc = supervisor._process_info[0]["process"]
    assert new_proc is not None
    assert new_proc.is_alive()
    assert new_proc.pid != proc.pid
    stop_file.write_text("stop")
    new_proc.join(timeout=2)
    assert not new_proc.is_alive()

    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert "test" in health


def test_supervisor_crash_loop_disables_restarts(monkeypatch, tmp_path):
    from util.runtime.processes import AccessorySupervisor

    calls = []
    def worker():
        pass  # exits immediately

    supervisor = AccessorySupervisor(
        health_path=str(tmp_path / "health.json"),
        max_restarts=2,
        restart_window_seconds=60,
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    supervisor.add("crashy", worker, enabled=True, daemon=True)

    # Cycle check several times; the process keeps dying immediately
    for _ in range(10):
        supervisor.check()
        time.sleep(0.05)

    crashy_info = supervisor._process_info[0]
    assert not crashy_info["enabled"], "crash-loop should disable restarts"

    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert health["crashy"]["status"] == "crashed"


def test_nws_temp_file_removed_on_failure(monkeypatch, tmp_path):
    """Temp file from NWS download is cleaned up even when processing fails."""
    io_writes = []
    monkeypatch.setattr(
        "common.ingest.nws.main.io_manager",
        type("FakeIO", (), {"write_info": lambda *a: None, "write_error": lambda *a: None})(),
    )

    temp_path = tmp_path / "nws_download.json"
    temp_path.write_text("{}", encoding="utf-8")

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def read(self):
            return b"{}"

    class FakeTF:
        def __init__(self, **kw):
            self.name = str(temp_path)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def write(self, data):
            return len(data)

    import common.ingest.nws.main as nws_main
    monkeypatch.setattr(nws_main.io_manager, "write_info", lambda *a: None)
    monkeypatch.setattr(nws_main.io_manager, "write_error", lambda *a: None)
    monkeypatch.setattr(nws_main.urllib.request, "urlopen", lambda *a, **kw: FakeResp())
    monkeypatch.setattr(nws_main.tempfile, "NamedTemporaryFile", lambda **kw: FakeTF(**kw))
    monkeypatch.setattr(nws_main, "_process_nws_file_with_registry", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("processing failed")))
    monkeypatch.setattr(nws_main, "_get_registry", lambda: type("FakeReg", (), {
        "cleanup_old_timestamps": lambda s, t: None,
        "alert_count": 0,
    })())

    with pytest.raises(RuntimeError, match="processing failed"):
        nws_main.download_alerts(datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc))

    assert not temp_path.exists(), f"temp file {temp_path} was not cleaned up"


def test_default_goes_coordinator_keeps_readiness_false_when_both_paths_throw(
    monkeypatch,
    tmp_path,
):
    from common.pipeline import coordinator
    from common.ingest.manifest import staged_input_from_path
    from common.ingest.mrms.downloader import DownloadBatchResult

    async def async_failure(*_args, **_kwargs):
        raise RuntimeError("async GOES failure")

    def sync_failure(*_args, **_kwargs):
        raise RuntimeError("sync GOES failure")

    async def structured_success(dt, *_args, **_kwargs):
        path = tmp_path / f"MRMS_Test_{dt:%Y%m%d-%H%M%S}.grib2"
        path.write_bytes(b"data")
        return DownloadBatchResult(
            attempted=("Test",),
            downloaded=(
                staged_input_from_path(
                    "Test",
                    path,
                    source="test",
                    family="mrms",
                ),
            ),
            failed=(),
        )

    monkeypatch.setattr(coordinator.mrms_ingest, "download_detection_files_async", structured_success)
    monkeypatch.setattr(coordinator.mrms_ingest, "download_integration_files_async", structured_success)
    monkeypatch.setattr(coordinator, "download_all_goes_files_async", async_failure)
    monkeypatch.setattr(coordinator, "download_all_goes_files", sync_failure)

    state = asyncio.run(
        coordinator.run_staged_ingest_cycle(
            datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc),
            lambda _message: None,
            include_goes=True,
            include_rap=False,
            include_ewmrs=False,
        )
    )

    assert state.ewmrs_goes_inputs_ready is False
    assert state.edgewarn_integration_inputs_ready is False
    assert "goes_ingest" in state.errors
