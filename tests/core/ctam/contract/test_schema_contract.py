"""Phase 0 contract tests for the CTAM internal API schemas.

The schemas in ``docs/ctam/schema/`` are validated by this repository's own
hand-rolled walker in ``src/common/config/loader.py``, not by ``jsonschema``.
That constrains which keywords an author may use, and the walker's failure mode
for an unsupported keyword is a startup error rather than a silently unenforced
constraint. These tests hold the schemas inside that keyword set and pin the
consequences of the restriction that are easy to get wrong later.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from common.config.loader import (
    _KNOWN_SCHEMA_KEYWORDS,
    _check_supported_keywords,
    _walk,
)

pytestmark = pytest.mark.ctam

REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_DIR = REPO_ROOT / "docs" / "ctam" / "schema"

# Named so a deleted or renamed schema fails loudly instead of shrinking the
# parametrized test set to nothing.
EXPECTED_SCHEMAS = (
    "cycle-state.schema.json",
    "file-descriptor.schema.json",
    "patch-request.schema.json",
    "requirements-evaluation.schema.json",
    "response-envelope.schema.json",
    "status-record.schema.json",
    "transaction.schema.json",
)


def load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def iter_subschemas(node, path=()):
    """Every dict in a schema document, with its path. Not keyword-aware."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from iter_subschemas(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_subschemas(value, path + (index,))


def test_expected_schema_set_is_present():
    """Guards the parametrization: a silently empty glob would pass everything."""
    found = tuple(sorted(p.name for p in SCHEMA_DIR.glob("*.schema.json")))
    assert found == EXPECTED_SCHEMAS


@pytest.mark.parametrize("name", EXPECTED_SCHEMAS)
def test_schema_parses_and_uses_only_supported_keywords(name):
    """The walker rejects unknown keywords, so an author cannot reach for $ref.

    ``_check_supported_keywords`` is the real gate used at config load time.
    Running it here means a schema that would blow up at startup fails in CI
    instead.
    """
    _check_supported_keywords(SCHEMA_DIR / name, load(name), [])


@pytest.mark.parametrize("name", EXPECTED_SCHEMAS)
def test_no_unsupported_keyword_anywhere(name):
    """Broader than ``_check_supported_keywords``, which only recurses into
    ``properties``, ``additionalProperties``, and ``items``.

    A subschema reached any other way would escape that check, so scan every
    dict that looks like a schema and confirm it too is clean.
    """
    for path, node in iter_subschemas(load(name)):
        if not ({"type", "enum", "const", "pattern"} & set(node)):
            continue
        unknown = set(node) - _KNOWN_SCHEMA_KEYWORDS
        assert not unknown, f"{name} at {path}: unsupported {sorted(unknown)}"


@pytest.mark.parametrize("name", EXPECTED_SCHEMAS)
def test_every_pattern_compiles_and_is_anchored(name):
    r"""Patterns must end in ``\Z``, not ``$``.

    The walker matches with ``re.search`` (loader.py:410), not ``re.fullmatch``,
    so anchoring is the author's job. Python's ``$`` also matches immediately
    before a trailing newline, which means a ``$``-anchored pattern accepts a
    value with a trailing newline. ``"\\S"`` is the sanctioned exception: it is
    a non-emptiness check, not a format check, and must not be anchored.
    """
    for path, node in iter_subschemas(load(name)):
        if "pattern" not in node:
            continue
        source = node["pattern"]
        re.compile(source)
        if source == "\\S":
            continue
        assert source.startswith("^"), f"{name} at {path}: {source!r} is unanchored at the start"
        assert source.endswith("\\Z"), f"{name} at {path}: {source!r} must end in \\Z, not $"


def test_dollar_anchor_admits_a_trailing_newline():
    r"""Freezes the reason the previous test exists.

    Uses the shipped pointer pattern with only the anchor swapped, so this
    demonstrates the actual hole rather than a contrived one. The segment class
    already excludes control characters; ``$`` lets the newline through anyway by
    matching in front of it. If a future Python made ``$`` behave like ``\Z``,
    this test fails and the anchoring rule can be relaxed deliberately rather
    than by accident.
    """
    shipped = load("patch-request.schema.json")["properties"]["operations"]["items"]["properties"]["path"]["pattern"]
    assert shipped.endswith("\\Z")
    with_dollar = shipped[: -len("\\Z")] + "$"

    assert re.search(with_dollar, "/modules/Foo\n") is not None
    assert re.search(shipped, "/modules/Foo\n") is None


def test_repeated_patterns_are_identical_across_schemas():
    """No ``$ref`` means shared shapes are copied, so drift is the failure mode.

    Group every pattern by the field name it constrains and require one distinct
    source per name. Without this, ``analysis_time`` could tighten in one schema
    and stay loose in another with nothing to notice.
    """
    by_field: dict[str, set[str]] = {}
    for name in EXPECTED_SCHEMAS:
        for path, node in iter_subschemas(load(name)):
            if "pattern" not in node or not path:
                continue
            field = str(path[-1])
            if field in {"items", "additionalProperties"}:
                continue
            by_field.setdefault(field, set()).add(node["pattern"])

    drifted = {field: sorted(sources) for field, sources in by_field.items() if len(sources) > 1}
    assert not drifted, f"the same field is constrained differently in different schemas: {drifted}"


def test_timestamp_pattern_rejects_a_naive_timestamp():
    """An explicit offset is required everywhere a time appears.

    This is load-bearing rather than cosmetic: StormProb currently falls back to
    a naive ``datetime.now()``, so a naive value is a real thing that can reach
    the boundary and it must be rejected there.
    """
    pattern = load("file-descriptor.schema.json")["properties"]["analysis_time"]["pattern"]
    assert re.search(pattern, "2026-08-05T12:00:00+00:00")
    assert re.search(pattern, "2026-08-05T12:00:00Z")
    assert re.search(pattern, "2026-08-05T12:00:00.500+00:00")
    assert not re.search(pattern, "2026-08-05T12:00:00")
    assert not re.search(pattern, "20260805-120000")


def test_string_length_cannot_be_bounded_by_keyword():
    """Records a real limitation of the restricted set.

    ``minLength``/``maxLength`` are absent, so length bounds are folded into
    patterns as quantifiers. If a future author adds ``minLength`` expecting it
    to work, ``_check_supported_keywords`` raises, and this test explains why.
    """
    assert "minLength" not in _KNOWN_SCHEMA_KEYWORDS
    assert "maxLength" not in _KNOWN_SCHEMA_KEYWORDS
    module_id = load("response-envelope.schema.json")["properties"]["module_id"]["pattern"]
    assert re.search(r"\{\d+,\d+\}", module_id), f"{module_id!r} bounds no length"


def test_number_type_rejects_non_finite_values():
    """``type: number`` gets finiteness for free (loader.py:335-340).

    The plan requires finite JSON values in patches. This confirms where that is
    already enforced, and ``test_patch_value_finiteness_is_not_enforced`` marks
    where it is not.
    """
    schema = {"type": "number"}
    errors: list = []
    _walk(schema, float("nan"), [], errors)
    assert errors
    errors.clear()
    _walk(schema, float("inf"), [], errors)
    assert errors
    errors.clear()
    _walk(schema, 1.5, [], errors)
    assert not errors


def test_patch_value_finiteness_is_not_enforced_by_schema():
    """A non-finite number nested in a patch value passes the schema.

    ``value`` is arbitrary JSON, so the walker has no subschema to recurse with.
    The plan's finite-value rule must therefore be enforced in host code. This
    test exists so that requirement is not quietly assumed to be covered.
    """
    schema = load("patch-request.schema.json")
    request = {
        "api_version": "1",
        "revision": 0,
        "operations": [{"op": "add", "path": "/modules/CellStats", "value": {"x": float("inf")}}],
    }
    errors: list = []
    _walk(schema, request, [], errors)
    assert not errors, "schema unexpectedly rejected it; the host-code rule may now be redundant"
