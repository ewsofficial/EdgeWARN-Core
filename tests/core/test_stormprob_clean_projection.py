"""The inlined clean_projection must match the original isinstance walk."""

import json
import math
import random
from collections import OrderedDict, namedtuple

import numpy as np
import pytest

from EdgeWARN.stormprob.database import clean_projection


def _reference(value):
    """The original recursive walk, kept verbatim."""
    if isinstance(value, dict):
        return {str(key): _reference(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reference(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class Name(str):
    pass


class Count(int):
    pass


class Ratio(float):
    pass


Point = namedtuple("Point", "lat lon")


def _same(left, right):
    """Equal, with the same types and key order all the way down."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return list(left) == list(right) and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_same, left, right))
    if isinstance(left, (float, np.floating)) and math.isnan(left):
        return math.isnan(right)
    return left == right


LEAVES = [None, True, False, 0, -7, 2 ** 70, 1.5, -0.0, float("nan"), float("inf"), -float("inf"),
          "text", "", Name("n"), Count(3), Ratio(2.5), Ratio("nan"), np.float64(1.25),
          np.float64("nan"), np.float32(2.0), np.float32("inf"), np.int64(4), np.bool_(True)]
KEYS = ["a", 1, 2.5, True, None, Name("k"), Count(9), (1, 2)]


def _random_value(rng, depth=0):
    if depth > 4 or rng.random() < 0.4:
        return rng.choice(LEAVES)
    shape = rng.randrange(5)
    items = [_random_value(rng, depth + 1) for _ in range(rng.randrange(5))]
    if shape == 0:
        return items
    if shape == 1:
        return tuple(items)
    if shape == 2:
        return Point(*(items + [None, None])[:2])
    pairs = [(rng.choice(KEYS), item) for item in items]
    return OrderedDict(pairs) if shape == 3 else dict(pairs)


@pytest.mark.parametrize("seed", range(30))
def test_random_structures_match_reference(seed):
    rng = random.Random(seed)
    for _ in range(200):
        value = _random_value(rng)
        assert _same(clean_projection(value), _reference(value)), value


def test_cell_projection_serializes_identically():
    rng = random.Random(4)
    cells = [{"id": i, "bbox": [[35 + rng.random(), -97 + rng.random()] for _ in range(50)],
              "centroid": (35.0, -97.0),
              "properties": {f"p{k}": rng.uniform(-1, 1) for k in range(30)} | {"bad": float("nan")}}
             for i in range(10)]
    assert json.dumps(clean_projection(cells)) == json.dumps(_reference(cells))


def test_leaves_are_returned_as_is():
    for leaf in LEAVES:
        assert _same(clean_projection(leaf), _reference(leaf))
