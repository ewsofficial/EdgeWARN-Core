"""Stream a RAP analysis from the NOMADS HTTPS mirror into the RAP cache."""

import os
from pathlib import Path

import aiohttp

from common.ingest.synoptic.config import (
    rap_nomads_chunk_size_bytes,
    rap_nomads_timeout_seconds,
)


def validate_grib2_file(path: Path) -> None:
    """Check every GRIB2 message boundary without loading the file into memory."""
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        offset = 0
        if size < 20:
            raise ValueError(f"incomplete GRIB2 file: {path}")

        while offset < size:
            source.seek(offset)
            header = source.read(16)
            if len(header) != 16 or header[:4] != b"GRIB" or header[7] != 2:
                raise ValueError(f"invalid GRIB2 message at offset {offset}: {path}")
            length = int.from_bytes(header[8:16], "big")
            end = offset + length
            if length < 20 or end > size:
                raise ValueError(f"incomplete GRIB2 message at offset {offset}: {path}")
            source.seek(end - 4)
            if source.read(4) != b"7777":
                raise ValueError(f"missing GRIB2 end marker at offset {offset}: {path}")
            offset = end


async def download_synoptic_https_async(s3_key: str, local_path: Path, base_url: str) -> Path:
    """Fetch the same dated RAP key as S3, publishing only a complete GRIB2 file."""
    url = f"{base_url.rstrip('/')}/{s3_key}"
    part_path = local_path.with_name(f".{local_path.name}.nomads.part")
    local_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        timeout = aiohttp.ClientTimeout(total=rap_nomads_timeout_seconds())
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 404:
                    raise FileNotFoundError(url)
                response.raise_for_status()
                written = 0
                with open(part_path, "wb") as output:
                    async for chunk in response.content.iter_chunked(
                        rap_nomads_chunk_size_bytes()
                    ):
                        if chunk:
                            written += len(chunk)
                            output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())

                expected = response.content_length
                if expected is not None and written != expected:
                    raise IOError(
                        f"incomplete NOMADS download: expected {expected} bytes, got {written}"
                    )
                validate_grib2_file(part_path)

        os.replace(part_path, local_path)
        return local_path
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
