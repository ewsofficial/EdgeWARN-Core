import json
import bz2
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import util.file as fs
import common.ingest.nexrad.service as nexrad_service_module
from common.ingest.nexrad import parser as nexrad_parser
from common.ingest.nexrad.grouping import ingest_readiness_elevation_ids
from common.ingest.nexrad.models import ChunkKey
from common.ingest.nexrad.service import NexradIngestService
from common.ingest.nexrad.writer import (
    elevation_manifest_path,
    elevation_netcdf_path,
    runtime_dir,
    runtime_scan_path,
    site_manifest_path,
)


def _artifact(site, volume_id, elevation, timestamp, *, group_names=None):
    return SimpleNamespace(
        site=site,
        volume_id=volume_id,
        volume_timestamp=timestamp,
        scan_timestamp=timestamp,
        elevation=elevation,
        elevation_timestamp=timestamp,
        first_sweep_index=0,
        last_sweep_index=1,
        first_sweep_timestamp=timestamp,
        last_sweep_timestamp=timestamp,
        member_group_names=list(group_names or [f"{elevation}-group"]),
        member_sweeps=[],
        waveforms_present=set(),
        supplemental=False,
        netcdf_path=None,
        ar2v_path=None,
    )


def _chunks(site="KTLH", volume_id="999", stamp="20260507-150000", last_number=2):
    return [
        ChunkKey(
            site=site,
            volume_id=volume_id,
            chunk_number=number,
            chunk_type="S" if number == 1 else "I",
            key=f"{site}/{volume_id}/{stamp}-{number:03d}-{'S' if number == 1 else 'I'}",
        )
        for number in range(1, last_number + 1)
    ]


def _volume_header(site="KTLH"):
    header = bytearray(24)
    header[:4] = b"AR2V"
    header[20:24] = site.encode("ascii")[:4]
    return bytes(header)


def _msg31_record(*, radial_status, elevation_number=1, elevation_angle=0.5, collect_ms=1000, collect_date=1):
    prefix = struct.pack(
        ">4sIHHfBBHBBBBf",
        b"TEST",
        collect_ms,
        collect_date,
        1,
        90.0,
        0,
        0,
        nexrad_parser.MSG_31_PREFIX_LEN,
        1,
        radial_status,
        elevation_number,
        0,
        elevation_angle,
    )

    block_pointer_table = bytearray(struct.pack(">HH", 0, 0))
    block_pointer_table.extend(struct.pack(">" + "I" * nexrad_parser.MSG_31_BLOCK_POINTERS, *([0] * nexrad_parser.MSG_31_BLOCK_POINTERS)))
    body = prefix + bytes(block_pointer_table)

    size_words = (12 + nexrad_parser.MSG_HEADER_LEN + len(body) - 12) // 2
    msg_header = struct.pack(
        ">HBBHHIHH",
        size_words,
        0,
        31,
        1,
        collect_date,
        collect_ms,
        1,
        1,
    )
    return (b"\x00" * 12) + msg_header + body


def _compressed_first_chunk(site="KTLH"):
    record_stream = _msg31_record(radial_status=0) + _msg31_record(radial_status=2, collect_ms=2000)
    payload = bytearray(_volume_header(site))
    payload.extend(struct.pack(">I", 1))
    payload.extend(bz2.compress(record_stream))
    return bytes(payload)


@pytest.mark.asyncio
async def test_stream_ingest_async_only_downloads_new_chunks_for_partial_volume(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)
    fetch_calls = []

    async def _async_chunk_fetcher(chunk, **_kwargs):
        fetch_calls.append(chunk.chunk_number)
        return f"chunk-{chunk.chunk_number}".encode("ascii")

    service = NexradIngestService(async_chunk_fetcher=_async_chunk_fetcher)
    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        service,
        "_run_worker_parse",
        lambda state, site, volume_id, scan_timestamp, seen_elevation_keys, first_elevation_timestamp, **_kwargs: first_elevation_timestamp,
    )

    first_result = await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=2),
        s3_client=object(),
        base_dir=tmp_path,
    )
    second_result = await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=4),
        s3_client=object(),
        base_dir=tmp_path,
    )

    runtime_path = runtime_scan_path("KTLH", "999")
    state_path = service._runtime_state_path("KTLH", "999")
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert first_result.chunks_downloaded == 2
    assert second_result.chunks_downloaded == 2
    assert fetch_calls == [1, 2, 3, 4]
    assert runtime_path.read_bytes() == b"chunk-1chunk-2chunk-3chunk-4"
    assert len(state["downloaded_chunk_keys"]) == 4


@pytest.mark.asyncio
async def test_stream_ingest_async_checkpoints_large_volume_before_download_end(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)
    parse_calls = []

    async def _async_chunk_fetcher(chunk, **_kwargs):
        return f"chunk-{chunk.chunk_number}".encode("ascii")

    service = NexradIngestService(
        async_chunk_fetcher=_async_chunk_fetcher,
        parse_checkpoint_chunk_interval=2,
    )
    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: False,
    )

    def _fake_run_worker_parse(state, site, volume_id, scan_timestamp, seen_elevation_keys, first_elevation_timestamp, **_kwargs):
        parse_calls.append(state.bytes_written)
        return first_elevation_timestamp

    monkeypatch.setattr(service, "_run_worker_parse", _fake_run_worker_parse)

    await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=5),
        s3_client=object(),
        base_dir=tmp_path,
    )

    assert parse_calls == [14, 28, 35]


@pytest.mark.asyncio
async def test_stream_ingest_async_clears_runtime_state_after_completion(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)
    completion_states = iter([False, True])

    async def _async_chunk_fetcher(chunk, **_kwargs):
        return f"chunk-{chunk.chunk_number}".encode("ascii")

    service = NexradIngestService(async_chunk_fetcher=_async_chunk_fetcher)
    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: next(completion_states),
    )
    monkeypatch.setattr(
        service,
        "_run_worker_parse",
        lambda state, site, volume_id, scan_timestamp, seen_elevation_keys, first_elevation_timestamp, **_kwargs: first_elevation_timestamp,
    )

    await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=2),
        s3_client=object(),
        base_dir=tmp_path,
    )

    runtime_path = runtime_scan_path("KTLH", "999")
    state_path = service._runtime_state_path("KTLH", "999")
    assert runtime_path.exists()
    assert state_path.exists()

    result = await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=3),
        s3_client=object(),
        base_dir=tmp_path,
    )

    assert result.complete is True
    assert result.chunks_downloaded == 1
    assert not runtime_path.exists()
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_stream_ingest_async_prunes_previous_runtime_volume_when_new_one_starts(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)

    async def _async_chunk_fetcher(chunk, **_kwargs):
        return f"chunk-{chunk.chunk_number}".encode("ascii")

    service = NexradIngestService(async_chunk_fetcher=_async_chunk_fetcher)
    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        service,
        "_run_worker_parse",
        lambda state, site, volume_id, scan_timestamp, seen_elevation_keys, first_elevation_timestamp, **_kwargs: first_elevation_timestamp,
    )

    prior_runtime_path = runtime_scan_path("KTLH", "998")
    prior_state_path = runtime_dir("KTLH") / "KTLH_998.json"
    prior_runtime_path.parent.mkdir(parents=True, exist_ok=True)
    prior_runtime_path.write_bytes(b"complete-volume")
    prior_state_path.write_text("{}", encoding="utf-8")

    await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=1),
        s3_client=object(),
        base_dir=tmp_path,
    )

    assert not prior_runtime_path.exists()
    assert not prior_state_path.exists()
    assert runtime_scan_path("KTLH", "999").exists()


def _write_completion_sidecars(site, volume_id, scan_timestamp):
    for index, elev in enumerate(ingest_readiness_elevation_ids()):
        elevation_timestamp = f"{scan_timestamp[:-2]}{index:02d}"
        nc_path = elevation_netcdf_path(site, elev, elevation_timestamp)
        nc_path.parent.mkdir(parents=True, exist_ok=True)
        nc_path.write_bytes(b"ok")
        manifest_path = elevation_manifest_path(site, elev, elevation_timestamp)
        manifest_path.write_text(
            json.dumps(
                {
                    "site": site,
                    "volume_id": volume_id,
                    "volume_timestamp": scan_timestamp,
                    "scan_timestamp": scan_timestamp,
                    "elevation": elev,
                    "elevation_timestamp": elevation_timestamp,
                    "first_sweep_timestamp": elevation_timestamp,
                    "last_sweep_timestamp": elevation_timestamp,
                    "member_sweeps": [
                        {
                            "group_name": f"{elev}-0",
                            "sweep_index": 0,
                            "fixed_angle": float(elev),
                            "elevation_number": None,
                            "waveform": "batch",
                            "timestamp": elevation_timestamp,
                        }
                    ],
                    "netcdf_path": str(nc_path),
                }
            ),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_stream_ingest_async_writes_site_manifest_only_after_completion(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)
    completion_states = iter([False, True])

    async def _async_chunk_fetcher(chunk, **_kwargs):
        return f"chunk-{chunk.chunk_number}".encode("ascii")

    service = NexradIngestService(async_chunk_fetcher=_async_chunk_fetcher)
    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: next(completion_states),
    )

    def _fake_run_worker_parse(state, site, volume_id, scan_timestamp, seen_elevation_keys, first_elevation_timestamp, **_kwargs):
        _write_completion_sidecars(site, volume_id, scan_timestamp)
        return first_elevation_timestamp

    monkeypatch.setattr(service, "_run_worker_parse", _fake_run_worker_parse)

    await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=2),
        s3_client=object(),
        base_dir=tmp_path,
    )
    assert not site_manifest_path("KTLH").exists()

    await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=3),
        s3_client=object(),
        base_dir=tmp_path,
    )

    manifest = json.loads(site_manifest_path("KTLH").read_text(encoding="utf-8"))
    assert manifest["volumes"][0]["volume_id"] == "999"
    assert manifest["volumes"][0]["volume_timestamp"] == "20260507-150000"
    assert manifest["volumes"][0]["sweeps"] == [
        {
            "sweep_index": 0,
            "group_name": "0.5-0",
            "elevation": 0.5,
            "timestamp": "20260507-150000",
            "waveform": "batch",
        },
        {
            "sweep_index": 0,
            "group_name": "0.9-0",
            "elevation": 0.9,
            "timestamp": "20260507-150001",
            "waveform": "batch",
        },
        {
            "sweep_index": 0,
            "group_name": "1.3-0",
            "elevation": 1.3,
            "timestamp": "20260507-150002",
            "waveform": "batch",
        },
        {
            "sweep_index": 0,
            "group_name": "1.8-0",
            "elevation": 1.8,
            "timestamp": "20260507-150003",
            "waveform": "batch",
        },
        {
            "sweep_index": 0,
            "group_name": "2.4-0",
            "elevation": 2.4,
            "timestamp": "20260507-150004",
            "waveform": "batch",
        },
        {
            "sweep_index": 0,
            "group_name": "3.1-0",
            "elevation": 3.1,
            "timestamp": "20260507-150005",
            "waveform": "batch",
        },
        {
            "sweep_index": 0,
            "group_name": "4.0-0",
            "elevation": 4.0,
            "timestamp": "20260507-150006",
            "waveform": "batch",
        },
    ]


def test_run_worker_parse_advances_latest_elevation_timestamp(monkeypatch, tmp_path):
    fs.initialize_filesystem(tmp_path)
    service = NexradIngestService()
    runtime_path = runtime_scan_path("KTLH", "999")
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_bytes(b"partial")

    payloads = iter([
        SimpleNamespace(
            visible_sweeps=2,
            saved_sweep_count=1,
            saved_elevations=[_artifact("KTLH", "999", "0.5", "20260507-150001", group_names=["g0"])],
            parse_error=None,
            child_rss_kb=123.0,
            buffer_trimmed=False,
            runtime_size=None,
        ),
        SimpleNamespace(
            visible_sweeps=2,
            saved_sweep_count=1,
            saved_elevations=[_artifact("KTLH", "999", "0.5", "20260507-150011", group_names=["g0"])],
            parse_error=None,
            child_rss_kb=123.0,
            buffer_trimmed=False,
            runtime_size=None,
        ),
    ])

    class _FakeFuture:
        def result(self, timeout=None):
            return next(payloads)

    class _FakePool:
        def submit(self, **_kwargs):
            return _FakeFuture()

    monkeypatch.setattr(nexrad_service_module, "get_nexrad_pool", lambda: _FakePool())

    state = SimpleNamespace(file_path=str(runtime_path), parse_errors=[], bytes_written=len(b"partial"))
    seen_elevation_exports = {"0.5:g0": "20260507-150001"}
    elevation_timestamps_by_id = {"0.5": "20260507-150001"}

    first_timestamp = service._run_worker_parse(
        state,
        "KTLH",
        "999",
        "20260507-150000",
        seen_elevation_exports,
        "20260507-150001",
        elevation_timestamps_by_id=elevation_timestamps_by_id,
        base_dir=tmp_path,
    )
    second_timestamp = service._run_worker_parse(
        state,
        "KTLH",
        "999",
        "20260507-150000",
        seen_elevation_exports,
        first_timestamp,
        elevation_timestamps_by_id=elevation_timestamps_by_id,
        base_dir=tmp_path,
    )

    assert first_timestamp == "20260507-150001"
    assert second_timestamp == "20260507-150001"
    assert seen_elevation_exports == {"0.5:g0": "20260507-150011"}
    assert elevation_timestamps_by_id == {"0.5": "20260507-150011"}


def test_run_worker_parse_resets_offsets_after_trim(monkeypatch, tmp_path):
    fs.initialize_filesystem(tmp_path)
    service = NexradIngestService()
    runtime_path = runtime_scan_path("KTLH", "999")
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_bytes(b"partial-runtime")

    class _FakeFuture:
        def result(self, timeout=None):
            return SimpleNamespace(
                visible_sweeps=1,
                saved_sweep_count=1,
                saved_elevations=[_artifact("KTLH", "999", "0.5", "20260507-150001", group_names=["g0"])],
                parse_error=None,
                child_rss_kb=123.0,
                buffer_trimmed=True,
                runtime_size=12,
            )

    class _FakePool:
        def submit(self, **_kwargs):
            return _FakeFuture()

    monkeypatch.setattr(nexrad_service_module, "get_nexrad_pool", lambda: _FakePool())

    state = SimpleNamespace(file_path=str(runtime_path), parse_errors=[], bytes_written=len(b"partial-runtime"))
    seen_elevation_exports = {}
    elevation_timestamps_by_id = {}

    first_timestamp = service._run_worker_parse(
        state,
        "KTLH",
        "999",
        "20260507-150000",
        seen_elevation_exports,
        None,
        elevation_timestamps_by_id=elevation_timestamps_by_id,
        base_dir=tmp_path,
    )

    assert first_timestamp == "20260507-150001"
    assert state.bytes_written == 12
    assert seen_elevation_exports == {"0.5:g0": "20260507-150001"}
    assert elevation_timestamps_by_id == {"0.5": "20260507-150001"}


def test_run_worker_parse_submits_runtime_file_without_offset_bookkeeping(monkeypatch, tmp_path):
    fs.initialize_filesystem(tmp_path)
    service = NexradIngestService()
    runtime_path = runtime_scan_path("KTLH", "999")
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_bytes(b"normalized-runtime")
    captured = {}

    class _FakeFuture:
        def result(self, timeout=None):
            return SimpleNamespace(
                visible_sweeps=0,
                saved_sweep_count=0,
                saved_elevations=[],
                parse_error=None,
                child_rss_kb=123.0,
                buffer_trimmed=False,
                runtime_size=None,
            )

    class _FakePool:
        def submit(self, **kwargs):
            captured.update(kwargs)
            return _FakeFuture()

    monkeypatch.setattr(nexrad_service_module, "get_nexrad_pool", lambda: _FakePool())

    state = SimpleNamespace(file_path=str(runtime_path), parse_errors=[], bytes_written=len(b"normalized-runtime"))

    service._run_worker_parse(
        state,
        "KTLH",
        "999",
        "20260507-150000",
        {},
        None,
        elevation_timestamps_by_id={},
        base_dir=tmp_path,
    )

    assert "volume_path" in captured
    assert "trim_buffer" in captured
    assert captured["volume_path"] != str(runtime_path)
    assert not Path(captured["volume_path"]).exists()
    assert runtime_path.read_bytes() == b"normalized-runtime"


def test_run_worker_parse_recycles_pool_after_timeout(monkeypatch, tmp_path):
    fs.initialize_filesystem(tmp_path)
    service = NexradIngestService()
    runtime_path = runtime_scan_path("KTLH", "999")
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_bytes(b"partial")
    recycle_calls = []

    class _FakeFuture:
        def cancel(self):
            recycle_calls.append("cancel")

        def result(self, timeout=None):
            raise FuturesTimeoutError()

    class _FakePool:
        def submit(self, **_kwargs):
            return _FakeFuture()

    monkeypatch.setenv("NEXRAD_WORKER_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(nexrad_service_module, "get_nexrad_pool", lambda: _FakePool())
    monkeypatch.setattr(nexrad_service_module, "shutdown_nexrad_pool", lambda wait=False, **kwargs: recycle_calls.append(("shutdown", wait)))

    state = SimpleNamespace(file_path=str(runtime_path), parse_errors=[], bytes_written=len(b"partial"))

    result = service._run_worker_parse(
        state,
        "KTLH",
        "999",
        "20260507-150000",
        {},
        None,
        elevation_timestamps_by_id={},
        base_dir=tmp_path,
    )

    assert result is None
    assert recycle_calls == ["cancel", ("shutdown", False)]
    assert state.parse_errors == ["worker parse timed out after 1s; recycling NEXRAD worker pool"]


def test_run_worker_parse_recycles_pool_after_broken_worker(monkeypatch, tmp_path):
    fs.initialize_filesystem(tmp_path)
    service = NexradIngestService()
    runtime_path = runtime_scan_path("KTLH", "999")
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_bytes(b"partial")
    recycle_calls = []

    class _FakeFuture:
        def result(self, timeout=None):
            raise BrokenProcessPool("worker terminated abruptly")

    class _FakePool:
        def submit(self, **_kwargs):
            return _FakeFuture()

    monkeypatch.setattr(nexrad_service_module, "get_nexrad_pool", lambda: _FakePool())
    monkeypatch.setattr(nexrad_service_module, "shutdown_nexrad_pool", lambda wait=False, **kwargs: recycle_calls.append(wait))

    state = SimpleNamespace(file_path=str(runtime_path), parse_errors=[], bytes_written=len(b"partial"))

    result = service._run_worker_parse(
        state,
        "KTLH",
        "999",
        "20260507-150000",
        {},
        None,
        elevation_timestamps_by_id={},
        base_dir=tmp_path,
    )

    assert result is None
    assert recycle_calls == [False]
    assert state.parse_errors == ["worker terminated abruptly"]


def test_stream_ingest_sync_resets_volume_state_after_boundary(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)
    service = NexradIngestService(chunk_fetcher=lambda *_args, **_kwargs: b"payload")
    parse_calls = []

    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: False,
    )

    boundary_states = iter([(True, 1), (False, 0)])
    monkeypatch.setattr(
        nexrad_service_module,
        "detect_next_volume_offset",
        lambda *_args, **_kwargs: next(boundary_states),
    )
    monkeypatch.setattr(
        nexrad_service_module,
        "split_at_boundary",
        lambda payload, _offset: (payload[:1], payload[1:]),
    )

    def _fake_run_worker_parse(state, site, volume_id, scan_timestamp, seen_elevation_exports, first_elevation_timestamp, **kwargs):
        elevation_timestamps_by_id = kwargs["elevation_timestamps_by_id"]
        parse_calls.append({
            "bytes_written": state.bytes_written,
            "seen": dict(seen_elevation_exports),
            "timestamps": dict(elevation_timestamps_by_id),
            "first": first_elevation_timestamp,
        })
        seen_elevation_exports["0.5:g0"] = "20260507-150001"
        elevation_timestamps_by_id["0.5"] = "20260507-150001"
        return first_elevation_timestamp or "20260507-150001"

    monkeypatch.setattr(service, "_run_worker_parse", _fake_run_worker_parse)

    service._stream_ingest_volume(
        "KTLH",
        "999",
        _chunks(last_number=1),
        s3_client=object(),
        base_dir=tmp_path,
    )

    assert parse_calls == [
        {
            "bytes_written": 1,
            "seen": {},
            "timestamps": {},
            "first": None,
        },
        {
            "bytes_written": len(b"ayload"),
            "seen": {},
            "timestamps": {},
            "first": None,
        },
    ]


def test_stream_ingest_sync_normalizes_runtime_temp_file(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)
    service = NexradIngestService(chunk_fetcher=lambda *_args, **_kwargs: _compressed_first_chunk())

    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: False,
    )

    def _fake_run_worker_parse(state, *_args, **_kwargs):
        runtime_bytes = Path(state.file_path).read_bytes()
        parsed = nexrad_parser.parse_raw_volume_file_mmap(state.file_path)
        assert runtime_bytes.startswith(b"AR2V")
        assert runtime_bytes[28:31] != b"BZh"
        assert len(parsed.sweeps) == 1
        return None

    monkeypatch.setattr(service, "_run_worker_parse", _fake_run_worker_parse)

    service._stream_ingest_volume(
        "KTLH",
        "999",
        _chunks(last_number=1),
        s3_client=object(),
        base_dir=tmp_path,
    )

    runtime_path = runtime_scan_path("KTLH", "999")
    assert runtime_path.exists()
    assert runtime_path.read_bytes()[28:31] != b"BZh"


@pytest.mark.asyncio
async def test_stream_ingest_async_persists_latest_group_export_timestamp(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)

    async def _async_chunk_fetcher(chunk, **_kwargs):
        return f"chunk-{chunk.chunk_number}".encode("ascii")

    service = NexradIngestService(async_chunk_fetcher=_async_chunk_fetcher)
    parse_timestamps = iter(["20260507-150001", "20260507-150011"])

    monkeypatch.setattr(
        nexrad_service_module,
        "_required_elevation_paths_complete",
        lambda *_args, **_kwargs: False,
    )

    def _fake_run_worker_parse(state, site, volume_id, scan_timestamp, seen_elevation_exports, first_elevation_timestamp, **kwargs):
        timestamp = next(parse_timestamps)
        seen_elevation_exports["0.5:g0"] = timestamp
        kwargs["elevation_timestamps_by_id"]["0.5"] = timestamp
        return first_elevation_timestamp or timestamp

    monkeypatch.setattr(service, "_run_worker_parse", _fake_run_worker_parse)

    await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=1),
        s3_client=object(),
        base_dir=tmp_path,
    )
    await service._stream_ingest_volume_async(
        "KTLH",
        "999",
        _chunks(last_number=2),
        s3_client=object(),
        base_dir=tmp_path,
    )

    state_path = service._runtime_state_path("KTLH", "999")
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["seen_elevation_exports"] == {"0.5:g0": "20260507-150011"}
    assert state["seen_elevation_keys"] == ["0.5:g0"]
    assert state["elevation_timestamps_by_id"] == {"0.5": "20260507-150011"}


@pytest.mark.asyncio
async def test_retry_rolls_back_chunks_written_by_failed_pass(tmp_path, monkeypatch):
    fs.initialize_filesystem(tmp_path)
    failed = [False]
    async def fetch(chunk, **kwargs):
        if chunk.chunk_number == 2 and not failed[0]:
            failed[0] = True
            raise OSError("injected download failure")
        return f"chunk-{chunk.chunk_number}".encode()
    service = NexradIngestService(async_chunk_fetcher=fetch, in_volume_prefetch=1)
    monkeypatch.setattr(nexrad_service_module, "_required_elevation_paths_complete", lambda *a, **kw: False)
    monkeypatch.setattr(service, "_run_worker_parse", lambda *a, **kw: None)
    with pytest.raises(OSError):
        await service._stream_ingest_volume_async("KTLH", "999", _chunks(), s3_client=object(), base_dir=tmp_path)
    await service._stream_ingest_volume_async("KTLH", "999", _chunks(), s3_client=object(), base_dir=tmp_path)
    assert runtime_scan_path("KTLH", "999").read_bytes() == b"chunk-1chunk-2"
