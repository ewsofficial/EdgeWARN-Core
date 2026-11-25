"""PNG (template 5.41) fast path in ``load_grib_fast`` matches ecCodes exactly.

ecCodes in the test environment cannot encode PNG fields, so messages are
assembled from the ecCodes GRIB2 sample's sections 1-4 plus hand-written
sections 5-7 whose payload is a PNG from OpenCV. ecCodes then decodes them,
which makes its own ``values`` the reference.
"""
import struct
from unittest.mock import patch

import eccodes
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

import util.grib_loader as grib_loader  # noqa: E402


def _sections(message):
    offset = 16
    while message[offset:offset + 4] != b"7777":
        length = int.from_bytes(message[offset:offset + 4], "big")
        yield message[offset + 4], message[offset:offset + length]
        offset += length


def _sign_magnitude(value):
    return struct.pack(">H", (0x8000 | -value) if value < 0 else value)


def _png_message(codes, bits, reference, binary_scale, decimal_scale, *, bitmap=False):
    nj, ni = codes.shape
    sample = eccodes.codes_grib_new_from_samples("GRIB2")
    try:
        eccodes.codes_set_long(sample, "Ni", ni)
        eccodes.codes_set_long(sample, "Nj", nj)
        base = eccodes.codes_get_message(sample)
    finally:
        eccodes.codes_release(sample)
    head = b"".join(raw for number, raw in _sections(base) if number in (1, 2, 3, 4))
    ok, png = cv2.imencode(".png", codes.astype(np.uint8 if bits == 8 else np.uint16))
    assert ok
    png = png.tobytes()
    body5 = (struct.pack(">IH", ni * nj, 41) + struct.pack(">f", reference)
             + _sign_magnitude(binary_scale) + _sign_magnitude(decimal_scale) + bytes([bits, 0]))
    section5 = struct.pack(">IB", 5 + len(body5), 5) + body5
    if bitmap:
        mask = np.packbits(np.ones(ni * nj, dtype=bool)).tobytes()
        section6 = struct.pack(">IBB", 6 + len(mask), 6, 0) + mask
    else:
        section6 = struct.pack(">IBB", 6, 6, 255)
    section7 = struct.pack(">IB", 5 + len(png), 7) + png
    rest = head + section5 + section6 + section7 + b"7777"
    return b"GRIB\x00\x00\x00\x02" + struct.pack(">Q", 16 + len(rest)) + rest


def _ecc_reference(gid, ni, nj):
    return eccodes.codes_get_double_array(gid, "values").reshape(nj, ni).astype(np.float32)


def _write(tmp_path, message):
    path = tmp_path / "field.grib2"
    path.write_bytes(message)
    return str(path)


@pytest.mark.parametrize(
    "bits, reference, binary_scale, decimal_scale",
    [
        (8, -30.0, 0, 1),        # MESH / VIL / RQI
        (8, -25.0, 0, 0),        # AzShear
        (8, -3.0, 0, 0),         # PrecipFlag
        (16, -9990.0, 0, 1),     # reflectivity
        (16, -3000.0, 0, 3),     # echo tops
        (16, -1000.0, 0, 3),     # NLDN density
        (16, 1.5, -1, 2),
        (8, 0.25, 2, -1),
    ],
)
def test_lut_matches_eccodes_for_every_code(bits, reference, binary_scale, decimal_scale):
    codes = np.arange(2 ** bits).reshape(-1, 256)
    gid = eccodes.codes_new_from_message(
        _png_message(codes, bits, reference, binary_scale, decimal_scale))
    try:
        nj, ni = codes.shape
        expected = _ecc_reference(gid, ni, nj)
        fast = grib_loader._decode_png_values(gid, ni, nj)
    finally:
        eccodes.codes_release(gid)
    assert fast is not None
    assert fast.dtype == np.float32 and fast.shape == expected.shape
    assert np.array_equal(fast.view(np.uint32), expected.view(np.uint32))
    lut = grib_loader._png_lut(bits, float(np.float32(reference)), binary_scale, decimal_scale)
    assert np.array_equal(lut.view(np.uint32), expected.ravel().view(np.uint32))


def test_load_grib_fast_uses_png_path_and_matches_fallback(tmp_path):
    codes = np.random.default_rng(7).integers(0, 2 ** 16, (31, 47))
    path = _write(tmp_path, _png_message(codes, 16, -9990.0, 0, 1))

    with patch.object(grib_loader, "_decode_png_values", wraps=grib_loader._decode_png_values) as spy, \
         patch.object(grib_loader.eccodes, "codes_get_double_array",
                      wraps=grib_loader.eccodes.codes_get_double_array) as ecc_values:
        fast = grib_loader.load_grib_fast(path)
    spy.assert_called_once()
    ecc_values.assert_not_called()  # the PNG path produced the values

    with patch.object(grib_loader, "_decode_png_values", return_value=None):
        old = grib_loader.load_grib_fast(path)

    assert fast.equals(old)
    (name,) = fast.data_vars
    assert fast[name].dtype == np.float32
    assert np.array_equal(fast[name].values.view(np.uint32), old[name].values.view(np.uint32))


def test_guard_rejects_non_png_template():
    gid = eccodes.codes_grib_new_from_samples("GRIB2")
    try:
        ni, nj = eccodes.codes_get_long(gid, "Ni"), eccodes.codes_get_long(gid, "Nj")
        assert eccodes.codes_get_long(gid, "dataRepresentationTemplateNumber") != 41
        assert grib_loader._decode_png_values(gid, ni, nj) is None
    finally:
        eccodes.codes_release(gid)


def test_guard_rejects_bitmap(tmp_path):
    codes = np.arange(256).reshape(16, 16)
    gid = eccodes.codes_new_from_message(_png_message(codes, 8, 0.0, 0, 0, bitmap=True))
    try:
        assert eccodes.codes_get_long(gid, "bitmapPresent") == 1
        assert grib_loader._decode_png_values(gid, 16, 16) is None
    finally:
        eccodes.codes_release(gid)


def test_guard_rejects_unsupported_bit_depth():
    codes = np.arange(256).reshape(16, 16)
    gid = eccodes.codes_new_from_message(_png_message(codes, 8, 0.0, 0, 0))
    real_get_long = eccodes.codes_get_long
    try:
        with patch.object(grib_loader.eccodes, "codes_get_long",
                          side_effect=lambda g, k: 24 if k == "bitsPerValue" else real_get_long(g, k)):
            assert grib_loader._decode_png_values(gid, 16, 16) is None
    finally:
        eccodes.codes_release(gid)


def test_guard_rejects_shape_mismatch():
    codes = np.arange(256).reshape(16, 16)
    gid = eccodes.codes_new_from_message(_png_message(codes, 8, 0.0, 0, 0))
    try:
        assert grib_loader._decode_png_values(gid, 16, 16) is not None
        assert grib_loader._decode_png_values(gid, 17, 16) is None
    finally:
        eccodes.codes_release(gid)


def test_guard_returns_none_without_cv2():
    codes = np.arange(256).reshape(16, 16)
    gid = eccodes.codes_new_from_message(_png_message(codes, 8, 0.0, 0, 0))
    try:
        with patch.object(grib_loader, "_cv2", return_value=None):
            assert grib_loader._decode_png_values(gid, 16, 16) is None
    finally:
        eccodes.codes_release(gid)


def test_cv2_import_failure_yields_none():
    grib_loader._cv2.cache_clear()
    try:
        with patch.dict("sys.modules", {"cv2": None}):
            assert grib_loader._cv2() is None
    finally:
        grib_loader._cv2.cache_clear()


def test_fallback_taken_when_fast_path_declines(tmp_path):
    codes = np.arange(256).reshape(16, 16)
    path = _write(tmp_path, _png_message(codes, 8, -30.0, 0, 1))
    with patch.object(grib_loader, "_decode_png_values", return_value=None) as declined, \
         patch.object(grib_loader.eccodes, "codes_get_double_array",
                      wraps=grib_loader.eccodes.codes_get_double_array) as ecc_values:
        ds = grib_loader.load_grib_fast(path)
    declined.assert_called_once()
    ecc_values.assert_called_once()
    (name,) = ds.data_vars
    assert ds[name].dtype == np.float32
    assert ds[name].shape == (16, 16)


def test_section_walk_requires_single_data_section():
    codes = np.arange(256).reshape(16, 16)
    message = _png_message(codes, 8, 0.0, 0, 0)
    assert grib_loader._section7_payload(message) is not None
    assert grib_loader._section7_payload(message[:-4]) is None
    assert grib_loader._section7_payload(b"GRIB\x00\x00\x00\x01" + message[8:]) is None
    sections = list(_sections(message))
    section7 = next(raw for number, raw in sections if number == 7)
    doubled = message[:-4] + section7 + b"7777"
    assert grib_loader._section7_payload(doubled) is None
