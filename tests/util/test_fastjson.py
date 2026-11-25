"""``util.fastjson.loads`` must return exactly what ``json.loads`` returns."""

import json
import math
import random

import pytest

from util import fastjson


def _same(left, right):
    """Equality that also tells NaN, -0.0 and int/float apart."""
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        return (math.isnan(left) and math.isnan(right)) or (
            left == right and math.copysign(1, left) == math.copysign(1, right))
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_same, left, right))
    if isinstance(left, dict):
        return list(left) == list(right) and all(_same(left[k], right[k]) for k in left)
    return left == right


def _outcome(function, text):
    try:
        return ("ok", function(text))
    except Exception as exc:  # noqa: BLE001 - the exception type is the result
        return ("error", type(exc))


EDGE_CASES = [
    '{"a":1,"a":2}', '﻿{"a":1}', "[1e400]", "[-1e400]", "[-0]", "[-0.0]",
    '["\\ud800"]', '["\\udc00x"]', "[NaN]", "[Infinity]", "[-Infinity]",
    "[123456789012345678901234567890]", "[-123456789012345678901234567890]",
    "[18446744073709551615]", "[18446744073709551616]", "[-9223372036854775808]",
    "[-9223372036854775809]", "[-9999999999999999999]", "[-10000000000000000000]",
    "[9999999999999999999]", "[0.0012345678901234567]", "[1.0000000000000002]", "[5e-324]", "[1e-400]",
    " [1] ", "[1,]", '"\\u00e9"', '"é"', "[1E5]", "[0.1e1]", "01", "", "  ",
    '{"k": [1, 2, {"c": null, "d": true, "e": false}]}', '"' + "x" * 5000 + '"',
    "[" * 500 + "]" * 500, '{"12345678901234567890":1}', '"id-123456789012345678901"',
    "[0.12345678901234567890123]", "[1e19]", "[9.999999999999999e18]",
]


@pytest.mark.parametrize("text", EDGE_CASES)
def test_edge_cases_match_stdlib(text):
    for value in (text, text.encode("utf-8", "surrogatepass")):
        expected, actual = _outcome(json.loads, value), _outcome(fastjson.loads, value)
        assert expected[0] == actual[0]
        if expected[0] == "ok":
            assert _same(actual[1], expected[1])
        else:
            assert actual[1] is expected[1]


def _random_value(rng, depth=0):
    kind = rng.randrange(9 if depth < 4 else 6)
    if kind == 0:
        return rng.choice([None, True, False])
    if kind == 1:
        return rng.randrange(-(2 ** 70), 2 ** 70) if rng.random() < 0.05 else rng.randrange(-10 ** 6, 10 ** 6)
    if kind in (2, 3):
        special = [0.0, -0.0, 1e-310, 5e-324, 1.7976931348623157e308, 1e16, 1e-5, 0.1, 1 / 3]
        if rng.random() < 0.2:
            return rng.choice(special)
        return rng.uniform(-1, 1) * 10 ** rng.randrange(-30, 30)
    if kind == 4:
        alphabet = "abc XYZ 0123456789 é漢\"\\/\n\t "
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
    if kind == 5:
        return rng.choice(["", "2026-05-01T12:00:00Z", "cell-42"])
    if kind in (6, 7):
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(6))]
    return {f"k{rng.randrange(20)}": _random_value(rng, depth + 1) for _ in range(rng.randrange(6))}


@pytest.mark.parametrize("seed", range(5))
def test_random_documents_match_stdlib(seed):
    rng = random.Random(seed)
    for _ in range(400):
        value = _random_value(rng)
        for text in (json.dumps(value), json.dumps(value, ensure_ascii=False),
                     json.dumps(value, separators=(",", ":"), sort_keys=True)):
            assert _same(fastjson.loads(text), json.loads(text)), text


def test_storm_cell_shapes_round_trip():
    rng = random.Random(7)
    cell = {"id": 12345, "timestamp": "2026-05-01T12:00:00Z", "centroid": [35.1234567, -97.7654321],
            "bbox": [[35 + rng.random(), -97 + rng.random()] for _ in range(120)],
            "properties": {f"p{i}": rng.uniform(-100, 100) for i in range(60)}}
    text = json.dumps(cell, allow_nan=False, separators=(",", ":"), sort_keys=True, default=str)
    assert _same(fastjson.loads(text), json.loads(text))


@pytest.mark.parametrize("depth", [1023, 1024, 1025, 1500])
def test_deep_nesting_matches_stdlib(depth):
    text = "[" * depth + "]" * depth

    def depth_of(value):
        count = 0
        while isinstance(value, list) and value:
            value, count = value[0], count + 1
        return count

    assert depth_of(fastjson.loads(text)) == depth_of(json.loads(text)) == depth - 1


def test_non_text_inputs_behave_like_stdlib():
    for value in (None, 5, 1.5, ["[]"]):
        assert _outcome(fastjson.loads, value) == _outcome(json.loads, value)


def test_without_orjson_it_is_stdlib(monkeypatch):
    monkeypatch.setattr(fastjson, "_orjson", None)
    assert not fastjson.available()
    assert _same(fastjson.loads("[NaN, 1, 123456789012345678901234567890]"),
                 json.loads("[NaN, 1, 123456789012345678901234567890]"))
