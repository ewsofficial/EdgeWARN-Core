import asyncio
import inspect
from datetime import datetime, timezone

import common.ingest.mrms.downloader as ingest_downloader
from common.ingest.mrms.config import GoesIngestSpec


def test_atomic_netcdf_publish_hides_partial_output(tmp_path):
    destination = tmp_path / "OR_GLM-L2-LCFA_merged_20260419-001000.nc"

    class Dataset:
        def to_netcdf(self, path, *, engine):
            assert path != destination
            assert path.name.startswith(f".{destination.name}.")
            assert path.name.endswith(".part")
            assert engine == "netcdf4"
            path.write_bytes(b"complete-netcdf")

    ingest_downloader._write_netcdf_atomically(Dataset(), destination)

    assert destination.read_bytes() == b"complete-netcdf"
    assert list(tmp_path.glob("*.part")) == []


def test_concurrent_netcdf_writers_never_share_a_temporary_path(tmp_path):
    """Two writers of one scan (the 4c HDF5 .part race) use distinct temp files."""
    destination = tmp_path / "OR_GLM-L2-LCFA_merged_20260419-001000.nc"
    seen = []

    class Dataset:
        def __init__(self, payload):
            self.payload = payload

        def to_netcdf(self, path, *, engine):
            seen.append(path)
            if len(seen) == 1:
                # The second writer starts while the first is still writing.
                ingest_downloader._write_netcdf_atomically(Dataset(b"second"), destination)
            path.write_bytes(self.payload)

    ingest_downloader._write_netcdf_atomically(Dataset(b"first"), destination)

    assert len(seen) == 2 and seen[0] != seen[1]
    assert destination.read_bytes() == b"first"
    assert list(tmp_path.glob("*.part")) == []


def test_download_goes_product_filters_to_requested_abi_channel(monkeypatch, tmp_path):
    dt = datetime(2026, 4, 19, 0, 10, tzinfo=timezone.utc)
    spec = GoesIngestSpec(
        product="ABI-L1b-RadC",
        outdir=tmp_path / "VisibleBlue",
        channel_id="C01",
        channel_name="visible_blue",
        filename_matcher=r"(?:_|-)M\dC01_",
    )
    captured = {}

    class DummyFinder:
        def __init__(self, *args, **kwargs):
            pass

        def lookup_files(self, prefixes):
            return [
                ("ABI-L1b-RadC/2026/109/00/OR_ABI-L1b-RadC-M6C05_G19_s20261090008000.nc", dt.replace(minute=8)),
                ("ABI-L1b-RadC/2026/109/00/OR_ABI-L1b-RadC-M6C01_G19_s20261090010000.nc", dt),
                ("ABI-L1b-RadC/2026/109/00/OR_ABI-L1b-RadC-M6C01_G19_s20261090000000.nc", dt.replace(minute=0)),
            ]

    class DummyDownloader:
        def __init__(self, *args, **kwargs):
            pass

        def download_matching(self, file_list, outdir):
            captured["file_list"] = file_list
            captured["outdir"] = outdir
            outdir.mkdir(parents=True, exist_ok=True)
            target = outdir / "OR_ABI-L1b-RadC-M6C01_G19_s20261090010000.nc"
            target.write_text("abi")
            return target

        def download_all_matching(self, file_list, outdir):
            raise AssertionError("ABI channel ingest should select one best-match file")

        def decompress_file(self, gz_path):
            return gz_path

    monkeypatch.setattr(ingest_downloader, "FileFinder", DummyFinder)
    monkeypatch.setattr(ingest_downloader, "FileDownloader", DummyDownloader)
    monkeypatch.setattr(ingest_downloader, "_cleanup_goes_outdir_sync", lambda *args, **kwargs: None)

    result = ingest_downloader.download_goes_product(spec, dt)

    assert [path for path, _ in captured["file_list"]] == [
        "ABI-L1b-RadC/2026/109/00/OR_ABI-L1b-RadC-M6C01_G19_s20261090010000.nc",
        "ABI-L1b-RadC/2026/109/00/OR_ABI-L1b-RadC-M6C01_G19_s20261090000000.nc",
    ]
    assert captured["outdir"] == spec.outdir
    assert result == [spec.outdir / "OR_ABI-L1b-RadC-M6C01_G19_s20261090010000.nc"]


def test_download_all_goes_files_async_iterates_glm_and_abi_specs(monkeypatch, tmp_path):
    dt = datetime(2026, 4, 19, 0, 10, tzinfo=timezone.utc)
    specs = [
        GoesIngestSpec("GLM-L2-LCFA", tmp_path / "GLM"),
        GoesIngestSpec(
            product="ABI-L1b-RadC",
            outdir=tmp_path / "VisibleBlue",
            channel_id="C01",
            channel_name="visible_blue",
            filename_matcher=r"(?:_|-)M\dC01_",
        ),
    ]
    seen = []

    class DummyClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummySession:
        def client(self, *args, **kwargs):
            return DummyClientContext()

    class DummyAsyncFinder:
        def __init__(self, *args, **kwargs):
            pass

        async def async_lookup_files(self, prefixes):
            return []

    async def fake_download(
        goes_spec,
        dt_arg,
        hour_lookback,
        s3_client,
        parent_trace_id=None,
        perf_maps=None,
        preloaded_files=None,
    ):
        seen.append((goes_spec.product, goes_spec.channel_id, dt_arg, hour_lookback))
        return []

    monkeypatch.setattr(ingest_downloader, "get_goes_modifiers", lambda: specs)
    monkeypatch.setattr(ingest_downloader.aioboto3, "Session", lambda: DummySession())
    monkeypatch.setattr(ingest_downloader, "AsyncFileFinder", DummyAsyncFinder)
    monkeypatch.setattr(ingest_downloader, "_download_goes_product_async", fake_download)

    asyncio.run(ingest_downloader.download_all_goes_files_async(dt, hour_lookback=2))

    assert seen == [
        ("GLM-L2-LCFA", None, dt, 2),
        ("ABI-L1b-RadC", "C01", dt, 2),
    ]


def test_download_all_goes_files_async_uses_shared_abi_lookup(monkeypatch, tmp_path):
    dt = datetime(2026, 4, 19, 0, 10, tzinfo=timezone.utc)
    specs = [
        GoesIngestSpec("GLM-L2-LCFA", tmp_path / "GLM"),
        GoesIngestSpec(
            product="ABI-L1b-RadC",
            outdir=tmp_path / "VisibleBlue",
            channel_id="C01",
            channel_name="visible_blue",
            filename_matcher=r"(?:_|-)M\dC01_",
        ),
        # Visible red (C02) removed from ingest; only include C01 here
    ]

    shared_lookup_result = [("ABI-L1b-RadC/2026/109/00/fake.nc", dt)]
    lookup_calls = []
    seen_preloaded = []

    class DummyClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummySession:
        def client(self, *args, **kwargs):
            return DummyClientContext()

    class DummyAsyncFinder:
        def __init__(self, dt_arg, bucket_arg, max_entries, io_manager, s3_client=None):
            lookup_calls.append(("init", max_entries))

        async def async_lookup_files(self, prefixes):
            lookup_calls.append(("lookup", tuple(prefixes)))
            return shared_lookup_result

    async def fake_download(
        goes_spec,
        dt_arg,
        hour_lookback,
        s3_client,
        parent_trace_id=None,
        perf_maps=None,
        preloaded_files=None,
    ):
        seen_preloaded.append((goes_spec.channel_id, preloaded_files))
        return []

    monkeypatch.setattr(ingest_downloader, "get_goes_modifiers", lambda: specs)
    monkeypatch.setattr(ingest_downloader.aioboto3, "Session", lambda: DummySession())
    monkeypatch.setattr(ingest_downloader, "AsyncFileFinder", DummyAsyncFinder)
    monkeypatch.setattr(ingest_downloader, "_download_goes_product_async", fake_download)

    asyncio.run(ingest_downloader.download_all_goes_files_async(dt, hour_lookback=2))

    assert lookup_calls == [
        ("init", 96),
        ("lookup", ("ABI-L1b-RadC/2026/109/00/", "ABI-L1b-RadC/2026/108/23/")),
    ]
    assert (None, None) in seen_preloaded
    assert ("C01", shared_lookup_result) in seen_preloaded
    assert len(seen_preloaded) == 2


def test_download_all_goes_files_sync_uses_shared_abi_lookup(monkeypatch, tmp_path):
    dt = datetime(2026, 4, 19, 0, 10, tzinfo=timezone.utc)
    specs = [
        GoesIngestSpec("GLM-L2-LCFA", tmp_path / "GLM"),
        GoesIngestSpec(
            product="ABI-L1b-RadC",
            outdir=tmp_path / "VisibleBlue",
            channel_id="C01",
            channel_name="visible_blue",
            filename_matcher=r"(?:_|-)M\dC01_",
        ),
    ]

    shared_lookup_result = [("ABI-L1b-RadC/2026/109/00/fake.nc", dt)]
    lookup_calls = []
    seen_preloaded = []

    class DummyFinder:
        def __init__(self, dt_arg, bucket_arg, max_entries, io_manager):
            lookup_calls.append(("init", max_entries))

        def lookup_files(self, prefixes):
            lookup_calls.append(("lookup", tuple(prefixes)))
            return shared_lookup_result

    def fake_download(goes_spec, dt_arg, hour_lookback, preloaded_files=None):
        seen_preloaded.append((goes_spec.channel_id, preloaded_files))
        return []

    monkeypatch.setattr(ingest_downloader, "get_goes_modifiers", lambda: specs)
    monkeypatch.setattr(ingest_downloader, "FileFinder", DummyFinder)
    monkeypatch.setattr(ingest_downloader, "download_goes_product", fake_download)

    ingest_downloader.download_all_goes_files(dt, hour_lookback=2)

    assert lookup_calls == [
        ("init", 96),
        ("lookup", ("ABI-L1b-RadC/2026/109/00/", "ABI-L1b-RadC/2026/108/23/")),
    ]
    assert seen_preloaded == [
        (None, None),
        ("C01", shared_lookup_result),
    ]


def test_goes_search_depth_is_fixed_and_not_a_caller_knob():
    """No GOES entry point may advertise a max_entries it would discard.

    The depth is fixed at 96 because GLM publishes roughly 180 objects an hour,
    so the MRMS depth of 10 would never list back to the requested scan. These
    functions used to take a max_entries the GOES paths silently ignored, which
    read as a working knob at every call site.
    """
    assert ingest_downloader._goes_search_max_entries() == 96

    goes_entry_points = (
        ingest_downloader.download_goes_product,
        ingest_downloader._download_goes_product_async,
        ingest_downloader.download_goes_specs,
        ingest_downloader.download_goes_specs_async,
        ingest_downloader.download_all_goes_files,
        ingest_downloader.download_all_goes_files_async,
    )
    for func in goes_entry_points:
        assert "max_entries" not in inspect.signature(func).parameters, func.__name__
