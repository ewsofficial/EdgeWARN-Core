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
