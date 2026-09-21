"""MRMS HTTPS failover after S3 misses and fetch failures."""

import asyncio
from datetime import datetime, timezone

import pytest

from common.ingest.mrms import downloader
from common.ingest.mrms.https_client import HttpsFileDownloader


TARGET = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("s3_files", [[], [("remote.gz", TARGET)]])
def test_async_s3_miss_or_failed_fetch_uses_https(monkeypatch, tmp_path, s3_files):
    source = tmp_path / "MRMS_Test_20260317-200000.grib2"
    source.write_bytes(b"data")

    class S3Finder:
        def __init__(self, *_args, **_kwargs):
            pass

        async def async_lookup_files(self, *_args, **_kwargs):
            return s3_files

    class S3Downloader:
        def __init__(self, *_args, **_kwargs):
            pass

        async def async_download_matching(self, *_args, **_kwargs):
            return None

    class HttpsFinder:
        def __init__(self, *_args, **_kwargs):
            pass

        async def find_files(self, *_args, **_kwargs):
            return [source.name]

    class HttpsDownloader:
        def __init__(self, *_args, **_kwargs):
            pass

        async def download_matching(self, *_args, **_kwargs):
            return source

    monkeypatch.setattr(downloader, "AsyncFileFinder", S3Finder)
    monkeypatch.setattr(downloader, "AsyncFileDownloader", S3Downloader)
    monkeypatch.setattr(downloader, "HttpsFileFinder", HttpsFinder)
    monkeypatch.setattr(downloader, "HttpsFileDownloader", HttpsDownloader)
    label, record = asyncio.run(
        downloader.download_modifier_async("CONUS", "Test", tmp_path, TARGET, 10, object())
    )
    assert label == "Test"
    assert record.source == "https"
    assert record.local_path == source


def test_sync_s3_miss_uses_https(monkeypatch, tmp_path):
    source = tmp_path / "MRMS_Test_20260317-200000.grib2"
    source.write_bytes(b"data")

    class S3Finder:
        def __init__(self, *_args, **_kwargs):
            pass

        def lookup_files(self, *_args, **_kwargs):
            return []

    class HttpsFinder:
        def __init__(self, *_args, **_kwargs):
            pass

        def find_files_sync(self, *_args, **_kwargs):
            return [source.name]

    class HttpsDownloader:
        def __init__(self, *_args, **_kwargs):
            pass

        def download_matching_sync(self, *_args, **_kwargs):
            return source

    monkeypatch.setattr(downloader, "FileFinder", S3Finder)
    monkeypatch.setattr(downloader, "FileDownloader", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(downloader, "HttpsFileFinder", HttpsFinder)
    monkeypatch.setattr(downloader, "HttpsFileDownloader", HttpsDownloader)
    label, record = downloader.download_modifier_sync("CONUS", "Test", tmp_path, TARGET, 10)
    assert label == "Test"
    assert record.source == "https"
    assert record.local_path == source


def test_https_match_rejects_unrelated_timestamp():
    downloader = HttpsFileDownloader(TARGET)
    assert downloader._select_matching_url(
        ["MRMS_Test_20260317-195000.grib2.gz"]
    ) is None
