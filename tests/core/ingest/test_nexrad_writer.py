import gzip
import json
import struct
from pathlib import Path

import netCDF4
import numpy as np
import util.file as fs
import xarray as xr
from NEXRAD.render import NEXRAD_FIELD_MAGIC, serialize_nexrad_elevation_artifacts

from common.ingest.nexrad.models import ElevationArtifact

from common.ingest.nexrad.writer import _dataset_encoding, _write_grouped_netcdf


class _Group:
    def __init__(self, dataset):
        self._dataset = dataset

    def to_dataset(self):
        return self._dataset


class _Tree:
    def __init__(self, dataset):
        self._dataset = dataset
        self.groups = ["/sweep_00"]

    def __getitem__(self, key):
        assert key == "/sweep_00"
        return _Group(self._dataset)




def _read_nexrad_bin(path: Path):
    with gzip.open(path, "rb") as handle:
        raw = handle.read()

    count_start = len(NEXRAD_FIELD_MAGIC)
    count_end = count_start + 2 * np.dtype("<u4").itemsize
    azimuth_count, range_count = np.frombuffer(raw[count_start:count_end], dtype="<u4")
    data_shape = (int(range_count), int(azimuth_count))
    data_count = data_shape[0] * data_shape[1]
    data_byte_length = data_count * np.dtype("<f2").itemsize
    azimuth_byte_length = int(azimuth_count) * np.dtype("<f4").itemsize
    range_byte_length = int(range_count) * np.dtype("<f4").itemsize
    data_start = count_end
    data_end = data_start + data_byte_length
    azimuth_end = data_end + azimuth_byte_length
    range_end = azimuth_end + range_byte_length
    data = np.frombuffer(raw[data_start:data_end], dtype="<f2").reshape(data_shape)
    azimuths = np.frombuffer(raw[data_end:azimuth_end], dtype="<f4")
    ranges = np.frombuffer(raw[azimuth_end:range_end], dtype="<f4")
    return data, azimuths, ranges


def _build_msg31_record(*, radial_status: int, azimuth_angle: float, elevation_angle: float, block_payloads: list[bytes]) -> bytes:
    body = bytearray()
    body.extend(
        struct.pack(
            ">4sIHHfBBHBBBBfBBH",
            b"AR2V",
            0,
            1,
            1,
            azimuth_angle,
            0,
            0,
            0,
            1,
            radial_status,
            1,
            0,
            elevation_angle,
            0,
            0,
            len(block_payloads),
        )
    )
    next_offset = 72
    pointers = []
    payload = bytearray()
    for block in block_payloads:
        pointers.append(next_offset)
        payload.extend(block)
        next_offset += len(block)
    pointers.extend([0] * (10 - len(pointers)))
    body.extend(struct.pack(">" + "I" * 10, *pointers))
    body.extend(payload)
    if len(body) % 2 != 0:
        body.append(0)

    message_header = struct.pack(
        ">HBBHHIHH",
        (16 + len(body)) // 2,
        1,
        31,
        1,
        1,
        0,
        1,
        1,
    )
    return b"\x00" * 12 + message_header + bytes(body)


def _build_generic_block(name: bytes, values, *, first_gate: int = 1000, gate_spacing: int = 1000, scale: float = 2.0, offset: float = 66.0, word_size: int = 8) -> bytes:
    dtype = ">u1" if word_size == 8 else ">u2"
    gate_bytes = np.asarray(values, dtype=dtype).tobytes()
    header = struct.pack(
        ">4sI H h h h h B B f f",
        name,
        0,
        len(values),
        first_gate,
        gate_spacing,
        0,
        0,
        0,
        word_size,
        scale,
        offset,
    )
    return header + gate_bytes


def _write_grouped_ar2v(path: Path, records: list[bytes], *, site: str = "KTLH") -> None:
    header = bytearray(b"AR2V")
    header.extend(b" " * 16)
    header.extend(site.encode("ascii")[:4].ljust(4, b" "))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + b"".join(records))


def test_write_grouped_netcdf_sanitizes_boolean_attrs(tmp_path):
    dataset = xr.Dataset(
        {"DBZH": (("azimuth",), [1.0, 2.0]), "CCORH": (("azimuth",), [0.0, 0.0])},
        coords={"azimuth": [0, 1]},
        attrs={"sails_cut": True, "meta": {"mode": "test"}},
    )
    dataset["DBZH"].attrs["has_mask"] = False

    path = tmp_path / "test.nc"
    _write_grouped_netcdf(path, {"complete": True}, _Tree(dataset), ["/sweep_00"])

    reopened = xr.open_dataset(path, group="sweep_00")
    try:
        assert reopened.attrs == {}
        assert reopened["DBZH"].attrs == {}
        assert "CCORH" in reopened.data_vars
    finally:
        reopened.close()


def test_dataset_encoding_preserves_packed_storage_and_fill_value():
    data = xr.DataArray(np.array([[1.0, np.nan]], dtype=np.float64), dims=("azimuth", "range"))
    data.encoding = {"dtype": np.dtype("uint8"), "scale_factor": 0.5, "add_offset": -33.0, "_FillValue": None}
    dataset = xr.Dataset({"DBZH": data})

    encoding = _dataset_encoding(dataset)

    assert encoding["DBZH"]["dtype"] == np.dtype("uint8")
    assert encoding["DBZH"]["scale_factor"] == 0.5
    assert encoding["DBZH"]["add_offset"] == -33.0
    assert encoding["DBZH"]["_FillValue"] == 255
    assert "zlib" not in encoding["DBZH"]


def test_write_grouped_netcdf_uses_packed_compressed_encoding(tmp_path):
    data = xr.DataArray(np.array([[1.0, np.nan]], dtype=np.float64), dims=("azimuth", "range"))
    data.encoding = {"dtype": np.dtype("uint8"), "scale_factor": 0.5, "add_offset": -33.0, "_FillValue": None}
    dataset = xr.Dataset({"DBZH": data}, attrs={"sails_cut": False})

    path = tmp_path / "packed.nc"
    _write_grouped_netcdf(path, {}, _Tree(dataset), ["/sweep_00"])

    handle = netCDF4.Dataset(path)
    try:
        variable = handle.groups["sweep_00"].variables["DBZH"]
        assert variable.dtype == np.dtype("uint8")
        assert variable.getncattr("_FillValue") == 255
    finally:
        handle.close()


def test_write_grouped_netcdf_preserves_all_variables(tmp_path):
    dataset = xr.Dataset(
        {
            "DBZH": (("azimuth",), [1.0, 2.0]),
            "VRADH": (("azimuth",), [0.1, 0.2]),
            "WRADH": (("azimuth",), [0.3, 0.4]),
            "RHOHV": (("azimuth",), [0.9, 0.95]),
            "noise": (("azimuth",), [5.0, 6.0]),
        },
        coords={"azimuth": [0, 1]},
    )
    path = tmp_path / "important.nc"
    _write_grouped_netcdf(path, {}, _Tree(dataset), ["/sweep_00"])

    reopened = xr.open_dataset(path, group="sweep_00")
    try:
        assert {"DBZH", "VRADH", "WRADH", "RHOHV", "noise"}.issubset(set(reopened.data_vars))
    finally:
        reopened.close()


def test_serialize_nexrad_elevation_artifacts_uses_artifact_timestamp_and_new_gui_layout(tmp_path):
    fs.initialize_filesystem(tmp_path)
    artifact_path = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5" / "KTLH_0.5_20260507-150001.ar2v"
    _write_grouped_ar2v(
        artifact_path,
        [
            _build_msg31_record(
                radial_status=0,
                azimuth_angle=0.0,
                elevation_angle=0.5,
                block_payloads=[_build_generic_block(b"DREF", [68, 70])],
            ),
            _build_msg31_record(
                radial_status=2,
                azimuth_angle=90.0,
                elevation_angle=0.5,
                block_payloads=[_build_generic_block(b"DREF", [72, 74])],
            ),
        ],
    )

    artifact = ElevationArtifact(
        site="KTLH",
        volume_id="999",
        volume_timestamp="20260507-150000",
        scan_timestamp="20260507-150000",
        elevation="0.5",
        elevation_timestamp="20260507-150001",
        first_sweep_index=0,
        last_sweep_index=0,
        first_sweep_timestamp=None,
        last_sweep_timestamp=None,
        member_group_names=[],
        member_sweeps=[],
        waveforms_present=set(),
        supplemental=False,
        ar2v_path=str(artifact_path),
    )

    manifest_path = serialize_nexrad_elevation_artifacts("KTLH", "999", "20260507-150000", [artifact])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["scan_timestamp"] == "20260507-150000"
    assert manifest["layers"][0]["scan_timestamp"] == "20260507-150001"
    assert manifest["layers"][0]["bin_path"].endswith("/gui/NEXRAD/KTLH/0.5/KTLH_DBZH_0.5_20260507-150001.bin.gz")


def test_serialize_nexrad_elevation_artifacts_writes_non_operational_elevations(tmp_path):
    fs.initialize_filesystem(tmp_path)
    artifact_path = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "1.3" / "KTLH_1.3_20260507-150001.ar2v"
    _write_grouped_ar2v(
        artifact_path,
        [
            _build_msg31_record(
                radial_status=0,
                azimuth_angle=0.0,
                elevation_angle=1.3,
                block_payloads=[_build_generic_block(b"DREF", [68, 70])],
            ),
            _build_msg31_record(
                radial_status=2,
                azimuth_angle=90.0,
                elevation_angle=1.3,
                block_payloads=[_build_generic_block(b"DREF", [72, 74])],
            ),
        ],
    )

    artifact = ElevationArtifact(
        site="KTLH",
        volume_id="999",
        volume_timestamp="20260507-150000",
        scan_timestamp="20260507-150000",
        elevation="1.3",
        elevation_timestamp="20260507-150001",
        first_sweep_index=0,
        last_sweep_index=0,
        first_sweep_timestamp=None,
        last_sweep_timestamp=None,
        member_group_names=[],
        member_sweeps=[],
        waveforms_present=set(),
        supplemental=False,
        ar2v_path=str(artifact_path),
    )

    manifest_path = serialize_nexrad_elevation_artifacts("KTLH", "999", "20260507-150000", [artifact])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["layers"][0]["canonical_elevation"] == "1.3"
    assert manifest["layers"][0]["bin_path"].endswith("/gui/NEXRAD/KTLH/1.3/KTLH_DBZH_1.3_20260507-150001.bin.gz")


def test_serialize_nexrad_elevation_artifacts_decodes_grouped_ar2v_directly(tmp_path):
    fs.initialize_filesystem(tmp_path)
    artifact_path = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5" / "KTLH_0.5_20260507-150001.ar2v"
    records = [
        _build_msg31_record(
            radial_status=0,
            azimuth_angle=0.0,
            elevation_angle=0.5,
            block_payloads=[
                _build_generic_block(b"DREF", [68, 70, 72]),
                _build_generic_block(b"DPHI", [1025, 1026, 1027], scale=2.0, offset=0.0, word_size=16),
            ],
        ),
        _build_msg31_record(
            radial_status=2,
            azimuth_angle=90.0,
            elevation_angle=0.5,
            block_payloads=[
                _build_generic_block(b"DREF", [74, 76, 78]),
                _build_generic_block(b"DPHI", [1028, 1029, 1030], scale=2.0, offset=0.0, word_size=16),
            ],
        ),
    ]
    _write_grouped_ar2v(artifact_path, records)

    artifact = ElevationArtifact(
        site="KTLH",
        volume_id="999",
        volume_timestamp="20260507-150000",
        scan_timestamp="20260507-150000",
        elevation="0.5",
        elevation_timestamp="20260507-150001",
        first_sweep_index=0,
        last_sweep_index=0,
        first_sweep_timestamp=None,
        last_sweep_timestamp=None,
        member_group_names=["/sweep_0"],
        member_sweeps=[],
        waveforms_present={"contiguous_surveillance"},
        supplemental=False,
        ar2v_path=str(artifact_path),
    )

    manifest_path = serialize_nexrad_elevation_artifacts("KTLH", "999", "20260507-150000", [artifact])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert [layer["variable_name"] for layer in manifest["layers"]] == ["DBZH", "PHIDP"]

    dbzh_layer = manifest["layers"][0]
    phidp_layer = manifest["layers"][1]
    dbzh_data, dbzh_azimuths, dbzh_ranges = _read_nexrad_bin(Path(dbzh_layer["bin_path"]))
    phidp_data, phidp_azimuths, phidp_ranges = _read_nexrad_bin(Path(phidp_layer["bin_path"]))

    np.testing.assert_allclose(dbzh_azimuths, np.array([0.0, 90.0], dtype=np.float32))
    np.testing.assert_allclose(phidp_azimuths, np.array([0.0, 90.0], dtype=np.float32))
    np.testing.assert_allclose(dbzh_ranges, np.array([1000.0, 2000.0, 3000.0], dtype=np.float32))
    np.testing.assert_allclose(phidp_ranges, np.array([1000.0, 2000.0, 3000.0], dtype=np.float32))
    np.testing.assert_allclose(dbzh_data, np.array([[1.0, 4.0], [2.0, 5.0], [3.0, 6.0]], dtype=np.float16))
    np.testing.assert_allclose(phidp_data, np.array([[0.5, 2.0], [1.0, 2.5], [1.5, 3.0]], dtype=np.float16))


def test_current_nexrad_serializer_normalizes_azimuth_orientation(tmp_path):
    """The served range-major grid follows ascending azimuth, not record order."""
    fs.initialize_filesystem(tmp_path)
    artifact_path = (
        tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5"
        / "KTLH_0.5_20260507-150001.ar2v"
    )
    records = []
    for index, (azimuth, values) in enumerate([
        (180.0, [68, 70]),
        (270.0, [72, 74]),
        (0.0, [76, 78]),
        (90.0, [80, 82]),
    ]):
        records.append(_build_msg31_record(
            radial_status=0 if index == 0 else (2 if index == 3 else 1),
            azimuth_angle=azimuth,
            elevation_angle=0.5,
            block_payloads=[_build_generic_block(b"DREF", values)],
        ))
    _write_grouped_ar2v(artifact_path, records)
    artifact = ElevationArtifact(
        site="KTLH",
        volume_id="999",
        volume_timestamp="20260507-150000",
        scan_timestamp="20260507-150000",
        elevation="0.5",
        elevation_timestamp="20260507-150001",
        first_sweep_index=0,
        last_sweep_index=0,
        first_sweep_timestamp=None,
        last_sweep_timestamp=None,
        member_group_names=["/sweep_0"],
        member_sweeps=[],
        waveforms_present={"contiguous_surveillance"},
        supplemental=False,
        ar2v_path=str(artifact_path),
    )

    manifest_path = serialize_nexrad_elevation_artifacts(
        "KTLH", "999", "20260507-150000", [artifact]
    )
    layer = json.loads(manifest_path.read_text())["layers"][0]
    data, azimuths, ranges = _read_nexrad_bin(Path(layer["bin_path"]))

    np.testing.assert_array_equal(azimuths, [0.0, 90.0, 180.0, 270.0])
    np.testing.assert_array_equal(ranges, [1000.0, 2000.0])
    np.testing.assert_array_equal(
        data,
        np.array([[5.0, 7.0, 1.0, 3.0], [6.0, 8.0, 2.0, 4.0]], dtype=np.float16),
    )


def test_serialize_nexrad_elevation_artifacts_matches_grouped_ar2v_by_member_sweep_index(tmp_path):
    fs.initialize_filesystem(tmp_path)
    artifact_path = tmp_path / "data" / "NEXRAD_Level2" / "KTLH" / "0.5" / "KTLH_0.5_20260507-150001.ar2v"
    records = [
        _build_msg31_record(
            radial_status=0,
            azimuth_angle=0.0,
            elevation_angle=0.5,
            block_payloads=[
                _build_generic_block(b"DREF", [68, 70, 72]),
            ],
        ),
        _build_msg31_record(
            radial_status=2,
            azimuth_angle=90.0,
            elevation_angle=0.5,
            block_payloads=[
                _build_generic_block(b"DREF", [74, 76, 78]),
            ],
        ),
        _build_msg31_record(
            radial_status=0,
            azimuth_angle=0.0,
            elevation_angle=0.5,
            block_payloads=[
                _build_generic_block(b"DVEL", [132, 134, 136]),
                _build_generic_block(b"DSW ", [68, 70, 72]),
            ],
        ),
        _build_msg31_record(
            radial_status=2,
            azimuth_angle=90.0,
            elevation_angle=0.5,
            block_payloads=[
                _build_generic_block(b"DVEL", [138, 140, 142]),
                _build_generic_block(b"DSW ", [74, 76, 78]),
            ],
        ),
    ]
    _write_grouped_ar2v(artifact_path, records)

    artifact = ElevationArtifact(
        site="KTLH",
        volume_id="999",
        volume_timestamp="20260507-150000",
        scan_timestamp="20260507-150000",
        elevation="0.5",
        elevation_timestamp="20260507-150001",
        first_sweep_index=0,
        last_sweep_index=1,
        first_sweep_timestamp=None,
        last_sweep_timestamp=None,
        member_group_names=["/sweep_1", "/sweep_2"],
        member_sweeps=[
            {"group_name": "/sweep_1", "sweep_index": 0, "waveform": "contiguous_surveillance"},
            {"group_name": "/sweep_2", "sweep_index": 1, "waveform": "contiguous_doppler"},
        ],
        waveforms_present={"contiguous_surveillance", "contiguous_doppler"},
        supplemental=False,
        ar2v_path=str(artifact_path),
    )

    manifest_path = serialize_nexrad_elevation_artifacts("KTLH", "999", "20260507-150000", [artifact])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert [layer["variable_name"] for layer in manifest["layers"]] == ["DBZH", "VRADH", "WRADH"]
    assert [layer["sweep_group"] for layer in manifest["layers"]] == ["/sweep_0", "/sweep_1", "/sweep_1"]
