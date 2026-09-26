import asyncio
from datetime import datetime, timezone

from common.ingest.mrms.s3_async import AsyncFileFinder
from common.ingest.mrms.s3_common import select_target_file
from common.ingest.mrms.s3_sync import FileFinder


TARGET = datetime(2026, 9, 23, 0, 42, tzinfo=timezone.utc)
OLD_KEY = "MRMS_20260923-004040.grib2.gz"
CURRENT_KEY = "MRMS_20260923-004239.grib2.gz"


class Paginator:
    def paginate(self, **_kwargs):
        return [{"Contents": [{"Key": OLD_KEY}, {"Key": CURRENT_KEY}]}]


class AsyncPaginator:
    async def paginate(self, **_kwargs):
        yield {"Contents": [{"Key": OLD_KEY}, {"Key": CURRENT_KEY}]}


class Client:
    def __init__(self, paginator):
        self.paginator = paginator

    def get_paginator(self, _name):
        return self.paginator


class Log:
    def write_error(self, message):
        raise AssertionError(message)

    def write_debug(self, _message):
        pass


def test_sync_lookup_preserves_seconds_and_selects_current_cycle():
    log = Log()
    files = FileFinder(TARGET, "unused", 2, log, client=Client(Paginator())).lookup_files("MRMS/")

    assert [timestamp.second for _, timestamp in files] == [39, 40]
    assert select_target_file(TARGET, files, log) == CURRENT_KEY


def test_async_lookup_preserves_seconds_and_selects_current_cycle():
    log = Log()
    files = asyncio.run(
        AsyncFileFinder(TARGET, "unused", 2, log, s3_client=Client(AsyncPaginator()))
        .async_lookup_files("MRMS/")
    )

    assert [timestamp.second for _, timestamp in files] == [39, 40]
    assert select_target_file(TARGET, files, log) == CURRENT_KEY
