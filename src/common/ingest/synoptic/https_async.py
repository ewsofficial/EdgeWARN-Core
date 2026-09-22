"""Stream a RAP analysis from the NOMADS HTTPS mirror into the RAP cache."""

import os
from pathlib import Path

import aiohttp

from common.ingest.synoptic.config import (
    rap_nomads_chunk_size_bytes,
    rap_nomads_timeout_seconds,
)


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
                header = bytearray()
                with open(part_path, "wb") as output:
                    async for chunk in response.content.iter_chunked(
                        rap_nomads_chunk_size_bytes()
                    ):
                        if chunk:
                            if len(header) < 8:
                                header.extend(chunk[: 8 - len(header)])
                            written += len(chunk)
                            output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())

                if len(header) < 8 or header[:4] != b"GRIB" or header[7] != 2:
                    raise ValueError(f"NOMADS response is not GRIB2: {url}")
                expected = response.content_length
                if expected is not None and written != expected:
                    raise IOError(
                        f"incomplete NOMADS download: expected {expected} bytes, got {written}"
                    )

        os.replace(part_path, local_path)
        return local_path
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
