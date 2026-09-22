"""NOMADS RAP download and atomic staging tests."""

from datetime import datetime, timezone

import pytest

from common.ingest.synoptic import https_async


GRIB2 = b"GRIB\x00\x00\x00\x02" + b"weather data"
BASE_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rap/prod"


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, chunks=None, content_length=None):
        self.status = status
        self.content = FakeContent(chunks if chunks is not None else [GRIB2])
        self.content_length = content_length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status != 200:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self, response, requested):
        self.response = response
        self.requested = requested

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url):
        self.requested.append(url)
        return self.response


@pytest.mark.asyncio
async def test_nomads_download_uses_dated_key_and_publishes_grib2(monkeypatch, tmp_path):
    requested = []
    response = FakeResponse(chunks=[GRIB2[:3], GRIB2[3:]], content_length=len(GRIB2))
    monkeypatch.setattr(
        https_async.aiohttp,
        "ClientSession",
        lambda **_kwargs: FakeSession(response, requested),
    )
    dt = datetime(2026, 3, 1, 0, 5, tzinfo=timezone.utc)
    previous = dt.replace(day=28, month=2, hour=23)
    key = f"rap.{previous:%Y%m%d}/rap.t{previous:%H}z.awp130pgrbf00.grib2"
    local_path = tmp_path / "RAP.20260228-23z.awp130pgrbf00.grib2"

    result = await https_async.download_synoptic_https_async(key, local_path, BASE_URL)

    assert result == local_path
    assert requested == [f"{BASE_URL}/{key}"]
    assert local_path.read_bytes() == GRIB2
    assert not (tmp_path / f".{local_path.name}.nomads.part").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,error",
    [
        (FakeResponse(status=404), FileNotFoundError),
        (FakeResponse(status=500), RuntimeError),
        (FakeResponse(chunks=[b"<html>error</html>"]), ValueError),
        (FakeResponse(content_length=len(GRIB2) + 1), IOError),
        (FakeResponse(chunks=[]), ValueError),
    ],
)
async def test_nomads_failure_does_not_publish_or_leave_part(
    monkeypatch, tmp_path, response, error
):
    monkeypatch.setattr(
        https_async.aiohttp,
        "ClientSession",
        lambda **_kwargs: FakeSession(response, []),
    )
    local_path = tmp_path / "RAP.20260726-13z.awp130pgrbf00.grib2"
    local_path.write_bytes(b"existing")

    with pytest.raises(error):
        await https_async.download_synoptic_https_async(
            "rap.20260726/rap.t13z.awp130pgrbf00.grib2", local_path, BASE_URL
        )

    assert local_path.read_bytes() == b"existing"
    assert not (tmp_path / f".{local_path.name}.nomads.part").exists()
