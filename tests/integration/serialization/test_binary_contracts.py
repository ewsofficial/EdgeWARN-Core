"""Binary producer/consumer contracts: RAP uint16, EWMRS chunks, NEXRAD fields.

Phase 9 secondary coverage: production Python encoders emit the exact bytes
the Node API serves (``rapData`` octet-stream, render ``chunk`` float16
payloads, ``radarField`` gzip fields), but no test generated artifacts with
the real encoders and validated the wire properties independently. These
cases encode small synthetic fields with the production functions, then read
the bytes back with independent decoders (raw ``numpy.frombuffer`` struct
reads, never the encoder's own loader) and pin shape, dtype, endianness,
nodata, and the metadata the Node service derives its response headers from.

The companion Jest suite (``tests/api/test_binary_contracts.js``) serves
fixtures produced by these same encoders through the real repository and
ancillary stack and asserts headers plus byte passthrough. Together the two
sides prove the producer and consumer agree without sharing implementation.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from EWMRS.rap import uint16_pipeline
from EWMRS.rap.config import uint16_nodata, uint16_valid_max
from EWMRS.render.tiler import save_float16_chunk
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


class TestRapUint16Contract:
    def test_scale_maps_endpoints_nodata_and_clipping(self):
        values = np.array([[0.0, 50.0], [100.0, np.nan], [np.inf, -5.0], [250.0, 25.0]])
        encoded = uint16_pipeline.scale_to_uint16(values, {"min": 0.0, "max": 100.0})
        assert encoded.dtype == np.dtype("<u2")
        assert encoded.shape == values.shape
        assert int(encoded[0, 0]) == 0
        assert int(encoded[0, 1]) == round(uint16_valid_max() / 2)
        assert int(encoded[1, 0]) == uint16_valid_max()
        assert int(encoded[1, 1]) == uint16_nodata()  # NaN reserves nodata
        assert int(encoded[2, 0]) == uint16_nodata()  # non-finite reserves nodata
        assert int(encoded[2, 1]) == 0  # below-min clips to min
        assert int(encoded[3, 0]) == uint16_valid_max()  # above-max clips to max

    def test_inverted_scale_is_rejected(self):
        with pytest.raises(ValueError, match="greater than min"):
            uint16_pipeline.scale_to_uint16(np.ones((2, 2)), {"min": 10.0, "max": 10.0})

    def test_missing_value_masking(self):
        values = np.array([[-999.0, 20.0]])
        encoded = uint16_pipeline.scale_to_uint16(
            values, {"min": 0.0, "max": 100.0}, missing_value=-999.0
        )
        assert int(encoded[0, 0]) == uint16_nodata()
        assert int(encoded[0, 1]) != uint16_nodata()

    def test_written_artifact_decodes_independently(self, tmp_path):
        layer = _rap_layer(tmp_path / "RAP" / "CAPE")
        values = np.array(
            [[0.0, 33.3, 100.0], [np.nan, 50.0, 75.5]], dtype=np.float64
        )
        encoded = uint16_pipeline.scale_to_uint16(values, layer["scale"])
        data_path = tmp_path / "RAP" / "CAPE" / TIMESTAMP / "data.u16"
        data_path.parent.mkdir(parents=True)
        data_path.write_bytes(encoded.tobytes(order="C"))
        metadata = uint16_pipeline._build_metadata(
            layer=layer,
            message=_rap_message(),
            rap_path=tmp_path / "rap_20260317_2000.grib2",
            timestamp=TIMESTAMP,
            grid={"ni": 3, "nj": 2, "point_count": 6},
        )
        (data_path.parent / "metadata.json").write_text(json.dumps(metadata))

        # Independent read: raw little-endian uint16 shaped by the metadata.
        raw = (data_path.parent / "data.u16").read_bytes()
        assert len(raw) == 6 * 2
        grid = np.frombuffer(raw, dtype="<u2").reshape(metadata["shape"])
        assert metadata["shape"] == [2, 3]
        assert metadata["dtype"] == "uint16"
        assert metadata["byte_order"] == "little_endian"
        assert metadata["missing_value"] == uint16_nodata()
        assert grid[0, 0] == 0 and grid[0, 2] == uint16_valid_max()
        assert grid[1, 0] == uint16_nodata()

        # Invert the scale and recover the field within half a quantum.
        quantum = (layer["scale"]["max"] - layer["scale"]["min"]) / uint16_valid_max()
        valid = grid != uint16_nodata()
        recovered = grid[valid].astype(np.float64) / uint16_valid_max() * 100.0
        assert recovered == pytest.approx(values[valid], abs=quantum)

    def test_metadata_carries_every_field_node_headers_need(self, tmp_path):
        layer = _rap_layer(tmp_path / "RAP" / "CAPE")
        metadata = uint16_pipeline._build_metadata(
            layer=layer,
            message=_rap_message(),
            rap_path=tmp_path / "rap.grib2",
            timestamp=TIMESTAMP,
            grid={"ni": 3, "nj": 2, "point_count": 6},
        )
        # ancillary.js rapData derives X-Grid-Ni/Nj, X-Scale-Min/Max, X-Units,
        # X-Data-Type, X-Byte-Order, X-Missing-Value from exactly these keys.
        assert metadata["grid"]["ni"] == 3 and metadata["grid"]["nj"] == 2
        assert metadata["scale"] == {"min": 0.0, "max": 100.0}
        assert metadata["units"] == "J/kg"
        assert metadata["shape"] == [metadata["grid"]["nj"], metadata["grid"]["ni"]]


class TestEwmrsChunkContract:
    def test_float16_chunk_is_headerless_tight_payload(self, tmp_path):
        values = np.array([[1.0, np.nan], [3.5, -0.0]], dtype=np.float16)
        chunk_path = tmp_path / "chunk_0_0.f16.gz"
        save_float16_chunk(np.ascontiguousarray(values), chunk_path)

        payload = gzip.decompress(chunk_path.read_bytes())
        assert len(payload) == values.size * 2  # no header, 2 bytes per component
        assert np.frombuffer(payload, dtype="<f2").shape == (4,)

    def test_chunk_values_round_trip_including_nan_nodata(self, tmp_path):
        values = np.array([[10.25, np.nan, 0.0]], dtype=np.float16)
        chunk_path = tmp_path / "chunk_1_0.f16.gz"
        save_float16_chunk(np.ascontiguousarray(values), chunk_path)

        payload = gzip.decompress(chunk_path.read_bytes())
        decoded = np.frombuffer(payload, dtype=np.float16).reshape(values.shape)
        assert decoded[0, 0] == pytest.approx(10.25)
        assert np.isnan(decoded[0, 1])  # NaN is the chunk nodata sentinel
        assert decoded[0, 2] == pytest.approx(0.0)

    @pytest.mark.parametrize(
        "bad",
        [
            np.ones((2, 2), dtype=np.float32),  # wrong dtype
            np.ones((2, 2, 2), dtype=np.float16),  # bad channel count
            np.ones(4, dtype=np.float16),  # wrong dimensionality
        ],
    )
    def test_chunk_encoder_rejects_non_conforming_arrays(self, tmp_path, bad):
        with pytest.raises(ValueError):
            save_float16_chunk(bad, tmp_path / "chunk_0_0.f16.gz")


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
