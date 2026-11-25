"""JSON parsing that returns exactly what ``json.loads`` returns, faster.

``orjson`` parses several times faster than the standard library, but it is
not a drop-in replacement on every input.  It rejects ``NaN``/``Infinity``
literals, out-of-range numbers and lone surrogates that ``json.loads``
accepts, and it silently turns integers outside its integer range (below -2**63 or above
2**64 - 1) into floats.

``loads`` therefore only hands ``orjson`` text that cannot hit any of those
differences: anything ``orjson`` refuses is re-parsed by ``json.loads``, and
text containing a run of 19 or more digits (every integer outside that range
has at least 19) goes straight to ``json.loads``.  Without
``orjson`` installed this module is ``json.loads``.

Serialization stays on the standard library everywhere: ``orjson`` writes
floats, non-ASCII text and non-finite values differently, and several
callers hash the exact bytes.
"""
from __future__ import annotations

import json
from typing import Any

try:  # pragma: no cover - exercised by whichever environment runs the tests
    import orjson as _orjson
except ImportError:  # pragma: no cover
    _orjson = None

# Map every ASCII digit to b"0" and every other byte to a space, so a run of
# 19 digits becomes a substring search (much cheaper than a regex scan).
_DIGIT_RUNS = bytes(48 if 48 <= byte <= 57 else 32 for byte in range(256))
_WIDE_RUN = b"0" * 19


def loads(text: str | bytes | bytearray) -> Any:
    """Parse JSON text with results identical to ``json.loads``."""
    if _orjson is None or not isinstance(text, (str, bytes, bytearray)):
        return json.loads(text)
    raw = text.encode("utf-8", "surrogatepass") if isinstance(text, str) else text
    if _WIDE_RUN not in raw.translate(_DIGIT_RUNS):
        try:
            return _orjson.loads(text)
        except _orjson.JSONDecodeError:
            pass
    return json.loads(text)


def available() -> bool:
    """True when the accelerated parser is installed."""
    return _orjson is not None
