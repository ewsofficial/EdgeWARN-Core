"""NOMADS RAP download and atomic staging tests."""

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.ingest.synoptic import https_async


def grib2(payload=b"weather data"):
    length = 16 + len(payload) + 4
    return b"GRIB\x00\x00\x00\x02" + length.to_bytes(8, "big") + payload + b"7777"


GRIB2 = grib2()
BASE_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rap/prod"
RAP_FIXTURE = Path(__file__).resolve().parents[3] / "fixtures/weather/rap.grib2.b64"


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


def test_real_rap_fixture_passes_grib2_framing_check(tmp_path):
    payload = base64.b64decode(RAP_FIXTURE.read_text())
    path = tmp_path / "rap.grib2"
    path.write_bytes(payload + payload)

    https_async.validate_grib2_file(path)


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
    "payload",
    [
        GRIB2[:-4],
        GRIB2[:-1],
        GRIB2 + grib2(b"second")[:-4],
        GRIB2 + b"trailing bytes",
    ],
)
async def test_nomads_rejects_truncated_or_unframed_chunked_response(
    monkeypatch, tmp_path, payload
):
    response = FakeResponse(chunks=[payload[:8], payload[8:]])
    monkeypatch.setattr(
        https_async.aiohttp,
        "ClientSession",
        lambda **_kwargs: FakeSession(response, []),
    )
    local_path = tmp_path / "rap.grib2"

    with pytest.raises(ValueError, match="GRIB2"):
        await https_async.download_synoptic_https_async(
            "rap.20260726/rap.t13z.awp130pgrbf00.grib2", local_path, BASE_URL
        )

    assert not local_path.exists()
    assert not (tmp_path / ".rap.grib2.nomads.part").exists()


@pytest.mark.asyncio
async def test_nomads_accepts_complete_concatenated_messages_without_length(
    monkeypatch, tmp_path
):
    payload = GRIB2 + grib2(b"second")
    response = FakeResponse(chunks=[payload[:11], payload[11:]])
    monkeypatch.setattr(
        https_async.aiohttp,
        "ClientSession",
        lambda **_kwargs: FakeSession(response, []),
    )
    local_path = tmp_path / "rap.grib2"

    assert await https_async.download_synoptic_https_async(
        "rap.20260726/rap.t13z.awp130pgrbf00.grib2", local_path, BASE_URL
    ) == local_path
    assert local_path.read_bytes() == payload


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
