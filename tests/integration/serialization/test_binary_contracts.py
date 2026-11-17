"""Independent NEXRAD binary producer and API fixture contracts."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from NEXRAD import render as nexrad_render


TIMESTAMP = "20260317-200000"


# ---------------------------------------------------------------------------
# Shared cross-language fixture inventory
# ---------------------------------------------------------------------------

FIXTURE_SERIALIZATION_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "serialization"
if str(FIXTURE_SERIALIZATION_DIR) not in sys.path:
    sys.path.insert(0, str(FIXTURE_SERIALIZATION_DIR))

import generate_fixtures  # noqa: E402  (single source for the committed inventory)


def _committed_inventory():
    # Only encoder outputs participate: documentation (README.md) and the
    # generator itself are not produced by build_all and are not served.
    return sorted(
        path.relative_to(FIXTURE_SERIALIZATION_DIR).as_posix()
        for path in FIXTURE_SERIALIZATION_DIR.rglob("*")
        if path.is_file()
        and path.name != "generate_fixtures.py"
        and path.suffix != ".md"
        and "__pycache__" not in path.parts
    )


class TestSharedFixtureInventory:
    """The committed Node-consumable fixtures must match a fresh production encode.

    ``tests/fixtures/serialization/generate_fixtures.py`` is the single source
    for the artifacts ``tests/api/test_binary_contracts.js`` serves through the
    real API. These cases regenerate every artifact in a disposable tree and
    refuse to let the committed bytes drift from the current encoders, so the
    Python producer and the JavaScript consumer can never disagree silently.

   gzip members compare decompressed: the NEXRAD writer must stay on the
    ``gzip.open``/``gzip.GzipFile`` seams the atomic-publication regression
    test drives, and those entry points stamp the wrapper header with the
    clock and the atomic temp name. Nobody consumes those wrapper bits --
    the contract is the decompressed layout, which the cases below pin
    field by field -- so the inventory compares payloads, not timestamps.
    """

    @pytest.mark.parametrize("relative", _committed_inventory())
    def test_committed_fixture_bytes_match_fresh_encoder(self, tmp_path, relative):
        generate_fixtures.build_all(tmp_path)
        fresh = (tmp_path / relative).read_bytes()
        committed = (FIXTURE_SERIALIZATION_DIR / relative).read_bytes()
        if Path(relative).suffix == ".gz":
            assert gzip.decompress(committed) == gzip.decompress(fresh), relative
        else:
            assert committed == fresh, relative

    def test_inventory_covers_every_encoder_output(self, tmp_path):
        generate_fixtures.build_all(tmp_path)
        freshly_generated = sorted(
            path.relative_to(tmp_path).as_posix()
            for path in tmp_path.rglob("*")
            if path.is_file()
        )
        assert freshly_generated == _committed_inventory()


def _rap_layer(outdir):
    return {
        "name": "CAPE",
        "outdir": str(outdir),
        "filter": {"typeOfLevel": "surface"},
        "scale": {"min": 0.0, "max": 100.0},
        "short_names": {"CAPE"},
        "units": "J/kg",
    }


def _rap_message():
    return {"shortName": "CAPE", "typeOfLevel": "surface", "level": 0}


class TestNexradFieldContract:
    def _write_field(self, path):
        data = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, np.nan]], dtype=np.float32)
        azimuths = np.array([0.5, 1.5], dtype=np.float32)
        ranges = np.array([125.0, 250.0, 375.0], dtype=np.float32)
        nexrad_render._write_nexrad_variable_bin(path, data, azimuths, ranges)
        return data, azimuths, ranges

    def test_field_filename_matches_node_radarfile_naming(self):
        name = nexrad_render.nexrad_render_variable_bin_name("ktlh", TIMESTAMP, "0.5", "DBZH")
        # Node ancillary.js radarFile: `${site}_${product}_${elevation}_${value}.bin.gz`.
        assert name == f"KTLH_DBZH_0.5_{TIMESTAMP}.bin.gz"

    def test_field_bytes_decode_independently(self, tmp_path):
        path = tmp_path / "KTLH_DBZH_0.5_20260317-200000.bin.gz"
        data, azimuths, ranges = self._write_field(path)

        stream = gzip.decompress(path.read_bytes())
        assert stream[: len(nexrad_render.NEXRAD_FIELD_MAGIC)] == nexrad_render.NEXRAD_FIELD_MAGIC
        offset = len(nexrad_render.NEXRAD_FIELD_MAGIC)
        naz, nrange = np.frombuffer(stream[offset : offset + 8], dtype="<u4")
        assert (naz, nrange) == (2, 3)
        offset += 8
        n = naz * nrange
        decoded = np.frombuffer(stream[offset : offset + n * 2], dtype="<f2").reshape(naz, nrange)
        assert decoded.astype(np.float32) == pytest.approx(data, nan_ok=True)
        offset += n * 2
        assert np.frombuffer(stream[offset : offset + naz * 4], dtype="<f4") == pytest.approx(azimuths)
        offset += naz * 4
        assert np.frombuffer(stream[offset : offset + nrange * 4], dtype="<f4") == pytest.approx(ranges)
        assert offset + nrange * 4 == len(stream)  # no trailing bytes

    def test_contiguous_doppler_skips_dbzh(self):
        sweep = type("Sweep", (), {"waveform": "contiguous_doppler"})()
        assert nexrad_render._should_serialize_variable(sweep, "DBZH") is False
        assert nexrad_render._should_serialize_variable(sweep, "VELH") is True
