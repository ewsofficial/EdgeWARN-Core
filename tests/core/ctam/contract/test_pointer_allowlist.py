"""The pointer allowlist table, in its Phase 0 form.

The plan's test matrix asks for pointer allowlist enforcement as a dedicated
table-driven test run against both the HTTP handlers and the StormProb
in-process adapter. Neither exists yet, so this module holds the table itself
and runs it against the only gate Phase 0 ships: the ``patch-request`` schema
pattern.

That gate is deliberately partial. It decides *shape* -- which container a path
begins with, and that a key is named below it -- and cannot decide *ownership*,
which needs the caller's manifest. Each row therefore records which layer is
expected to reject it. When Phase 3 adds the segment-parsing validator, it should
import ``TABLE`` and assert that every row marked ``HOST`` is rejected there, so
the rows that are currently only aspirational become enforced without anyone
having to rediscover them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.ctam

REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = REPO_ROOT / "docs" / "ctam" / "schema" / "patch-request.schema.json"

ALLOWED = "ALLOWED"
SCHEMA = "SCHEMA"  # rejected by the checked-in pattern
HOST = "HOST"      # accepted by the pattern; must be rejected by host code

# (pointer, verdict, why). The caller is the module whose manifest declares
# display name "CellStats" and module id "cellstats".
TABLE = (
    # Accepted: the caller's own keys.
    ("/modules/CellStats", ALLOWED, "the caller's own namespace"),
    ("/modules/CellStats/gate_density", ALLOWED, "a leaf inside the caller's namespace"),
    ("/modules/CellStats/nested/deeper", ALLOWED, "depth is bounded by a limit, not by the allowlist"),
    ("/properties/cellstats_severity", ALLOWED, "a declared, module-id-prefixed scalar"),
    ("/modules/CellStats/key~1with~1slashes", ALLOWED, "~1 is an escaped slash inside one segment"),

    # Rejected by the pattern: the container itself.
    ("/modules", SCHEMA, "cannot replace or clear a container"),
    ("/properties", SCHEMA, "cannot replace or clear a container"),
    ("/modules/", SCHEMA, "trailing empty segment"),
    ("/properties/", SCHEMA, "trailing empty segment"),
    ("/modules/CellStats/", SCHEMA, "trailing empty segment"),

    # Rejected by the pattern: core identity, geometry, and tracking state.
    ("/id", SCHEMA, "core identity"),
    ("/centroid", SCHEMA, "geometry"),
    ("/bbox", SCHEMA, "geometry, and a polygon ring rather than four scalars"),
    ("/hail_core", SCHEMA, "geometry"),
    ("/max_refl", SCHEMA, "measured value"),
    ("/num_gates", SCHEMA, "measured value"),
    ("/event_type", SCHEMA, "tracking state"),
    ("/parent_ids", SCHEMA, "tracking state"),
    ("/split_from", SCHEMA, "tracking state"),
    ("/timestamp", SCHEMA, "timestamps are host-owned"),

    # Rejected by the pattern: not addressable by a module at all.
    ("/", SCHEMA, "the document root"),
    ("", SCHEMA, "the whole document"),
    ("/features", SCHEMA, "the features array is not part of a cell entry"),
    ("/features/0/modules/CellStats", SCHEMA, "a patch addresses one resolved entry, not the snapshot"),
    ("/0/modules/CellStats", SCHEMA, "array index"),
    ("modules/CellStats", SCHEMA, "not a pointer: no leading slash"),
    ("/Modules/CellStats", SCHEMA, "container names are case-sensitive"),
    ("/modulesX/CellStats", SCHEMA, "container name must match exactly"),
    ("/modules/CellStats\n", SCHEMA, "trailing newline, which a $-anchored pattern would admit"),
    ("/modules/CellStats\x00", SCHEMA, "embedded NUL"),

    # Accepted by the pattern, and must be rejected by the host: ownership.
    ("/modules/StormProb", HOST, "reserved built-in namespace, never grantable"),
    ("/modules/StormProb/u", HOST, "reserved built-in namespace"),
    ("/modules/_grid_outputs", HOST, "reserved legacy key, never grantable"),
    ("/modules/SomeOtherModule", HOST, "another module's namespace"),
    ("/modules/cellstats", HOST, "module id, not the manifest display name"),
    ("/properties/morphology", HOST, "written by detection, and read by downstream physics"),
    ("/properties/p100EchoTop30", HOST, "enrichment value StormProb consumes as if measured"),
    ("/properties/EchoTop50", HOST, "enrichment value StormProb consumes as if measured"),
    ("/properties/undeclared_key", HOST, "not declared in the caller's manifest"),
    ("/properties/severity", HOST, "declared keys must carry the module-id prefix"),
    ("/modules/CellStats/../id", HOST, "'..' is a literal key name here; it becomes a write to /id only if the host normalizes it as a path, which it must not"),
    ("/modules/CellStats/~0/x", HOST, "'~0' unescapes to '~' once; double unescaping is a host-side bug"),
)


def pointer_pattern() -> str:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["properties"]["operations"]["items"]["properties"]["path"]["pattern"]


def test_table_covers_every_verdict():
    """A table that lost all its HOST rows would still pass every row below."""
    verdicts = {verdict for _pointer, verdict, _why in TABLE}
    assert verdicts == {ALLOWED, SCHEMA, HOST}
    assert sum(1 for _p, v, _w in TABLE if v == HOST) >= 10


def test_table_has_no_duplicate_pointers():
    pointers = [pointer for pointer, _verdict, _why in TABLE]
    assert len(pointers) == len(set(pointers))


@pytest.mark.parametrize(
    "pointer,verdict,why",
    [pytest.param(p, v, w, id=f"{v}:{p!r}") for p, v, w in TABLE],
)
def test_schema_pattern_verdict(pointer, verdict, why):
    """The pattern rejects exactly the SCHEMA rows and admits the rest.

    A HOST row passing here is the expected, documented state, not a gap that
    slipped through: the pattern has no way to know what the caller owns.
    """
    matched = re.search(pointer_pattern(), pointer) is not None
    if verdict == SCHEMA:
        assert not matched, f"pattern should reject {pointer!r} ({why})"
    else:
        assert matched, f"pattern should admit {pointer!r} ({why})"


def test_host_rows_are_all_inside_the_two_containers():
    """Ownership is the only thing left to decide for a HOST row.

    If a HOST row were outside ``modules``/``properties``, it would mean the
    pattern had a hole in container enforcement, which is the one thing it is
    responsible for.
    """
    for pointer, verdict, why in TABLE:
        if verdict != HOST:
            continue
        assert pointer.startswith(("/modules/", "/properties/")), f"{pointer!r} ({why})"


def test_traversal_segment_is_a_literal_key():
    """Pins the reasoning behind the ``..`` row so it is not 'fixed' by mistake.

    Rejecting ``..`` in the pattern would be treating a JSON Pointer as a
    filesystem path, which is the confusion the plan warns about. The segment is
    a legal key name; the requirement is that the host never resolves it
    positionally.
    """
    pointer = "/modules/CellStats/../id"
    assert re.search(pointer_pattern(), pointer)
    segments = pointer.split("/")[1:]
    assert segments == ["modules", "CellStats", "..", "id"]
    assert segments[2] == "..", "a literal key named '..', not a parent reference"
