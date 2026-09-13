"""Ties ``EdgeWARN.ctam.limits`` to the artifacts it copies its numbers from.

``docs/ctam/internal-api-limits.md`` is where a limit is decided: it carries the
grounding citation and the "On excess" behavior. ``src/EdgeWARN/ctam/limits.py``
is a convenience copy so manifest validation, discovery, readiness, and the
runner do not import each other for a number. Two copies drift, and the drift is
silent because each side stays internally consistent -- the document keeps
explaining 8 modules while the code admits 12, and nothing fails.

So the table is parsed and every constant is compared against its row. The
docstring in ``limits.py`` promises this file by name; changing a number there
without changing the document is meant to fail here.

The identifier constants have no table row -- they are protocol identity, not
bounds -- so they are tied to the frozen Phase 0 schemas under
``docs/ctam/schema/`` instead. No schema file is added: the schema directory is
asserted elsewhere to be an exact 7-item set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from EdgeWARN.ctam import limits as limits_module
from EdgeWARN.ctam.limits import (
    API_VERSION,
    DEFAULT_HISTORY_WINDOW,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_EXTERNAL_MODULES,
    MAX_HISTORY_WINDOW,
    MAX_MODULE_ID_LENGTH,
    MAX_STREAMED_FILE_BYTES,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    RESERVED_MODULE_IDS,
    STATUS_SCHEMA_VERSION,
    SUPPORTED_API_VERSIONS,
)

pytestmark = pytest.mark.ctam

REPO_ROOT = Path(__file__).resolve().parents[4]
LIMITS_PATH = REPO_ROOT / "docs" / "ctam" / "internal-api-limits.md"
SCHEMA_DIR = REPO_ROOT / "docs" / "ctam" / "schema"

STATUS_RECORD_SCHEMA = "status-record.schema.json"
MODULE_ID_SCHEMA = "response-envelope.schema.json"

# Constant name to the verbatim row label in the Values table. The labels are
# already asserted to be present verbatim by test_limits_contract.py, so a rename
# there fails loudly rather than quietly detaching a tie here.
TIES = {
    "MAX_EXTERNAL_MODULES": "Maximum external module count",
    "MIN_TIMEOUT_SECONDS": "Minimum manifest `timeout_seconds`",
    "MAX_TIMEOUT_SECONDS": "Maximum manifest `timeout_seconds`",
    "DEFAULT_TIMEOUT_SECONDS": "Default `timeout_seconds` when omitted",
    "MAX_MODULE_ID_LENGTH": "Maximum module ID length",
    "DEFAULT_HISTORY_WINDOW": "Default history read window",
    "MAX_HISTORY_WINDOW": "Maximum history read window",
    "MAX_STREAMED_FILE_BYTES": "Maximum streamed file size",
}


def documented_limits() -> dict[str, int]:
    """Row label to integer value from the 5-column Values table.

    Rows whose value is not a bare integer (``Terminate-to-kill escalation`` is
    "5 then 1") have no single number to tie a constant to and are dropped.
    """
    table: dict[str, int] = {}
    for line in LIMITS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 5:
            continue
        label, value = cells[0], cells[1]
        if not value.isdigit():
            continue
        assert label not in table, f"duplicate limit row {label!r}"
        table[label] = int(value)
    return table


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


def test_every_tied_row_label_is_present_in_the_table():
    """Without this the whole file passes vacuously once a row is renamed.

    Every parametrized comparison below looks its row up by label; a missing label
    would skip the comparison rather than fail it, which is the failure mode
    contract tests are most prone to.
    """
    table = documented_limits()
    missing = sorted(label for label in TIES.values() if label not in table)
    assert not missing, f"limit rows renamed or removed: {missing}"


def test_every_tied_constant_exists_in_the_limits_module():
    """A constant rename must fail here, not disappear from the comparison set."""
    missing = sorted(name for name in TIES if not hasattr(limits_module, name))
    assert not missing, f"constants renamed or removed from limits.py: {missing}"


# --------------------------------------------------------------------------
# Document to constant
# --------------------------------------------------------------------------


@pytest.mark.parametrize("constant_name,row_label", sorted(TIES.items()))
def test_constant_equals_its_documented_row(constant_name, row_label):
    """The document is the decision record; a disagreeing constant is the bug.

    Editing the number in Python is the easy path when a limit feels wrong, and
    it is exactly the edit that leaves the reasoning and the grounding citation
    behind describing a limit the host no longer applies.
    """
    documented = documented_limits()[row_label]
    actual = getattr(limits_module, constant_name)
    assert actual == documented, (
        f"{constant_name} is {actual} but {row_label!r} documents {documented}; "
        f"change the document first, then this constant"
    )


def test_the_timeout_bounds_bracket_the_default():
    """A default outside its own bounds would reject every manifest that omits it.

    The three numbers are tied to three independent rows, so nothing above would
    notice if the document itself drifted into an impossible combination.
    """
    assert MIN_TIMEOUT_SECONDS <= DEFAULT_TIMEOUT_SECONDS <= MAX_TIMEOUT_SECONDS


def test_the_default_history_window_fits_inside_the_maximum():
    """A default above the clamp would make every unqualified read silently short.

    The document says over-limit reads are clamped rather than rejected, so a
    default larger than the maximum would be applied and then quietly reduced.
    """
    assert DEFAULT_HISTORY_WINDOW <= MAX_HISTORY_WINDOW


# --------------------------------------------------------------------------
# Schema to constant, for the identifiers that have no table row
# --------------------------------------------------------------------------


def test_api_version_matches_the_status_record_schema_const():
    """Modules read the version off the status record, so the two must agree.

    A host advertising "1" while the published record is pinned to a different
    const would make every conforming module reject the host's own output.
    """
    schema = load_schema(STATUS_RECORD_SCHEMA)
    assert API_VERSION == schema["properties"]["api_version"]["const"]
    assert API_VERSION in SUPPORTED_API_VERSIONS


def test_supported_api_versions_contains_only_versions_the_schema_admits():
    """Accepting a version the schema cannot express would admit unservable calls."""
    schema = load_schema(STATUS_RECORD_SCHEMA)
    declared = schema["properties"]["api_version"]["const"]
    assert set(SUPPORTED_API_VERSIONS) == {declared}


def test_initial_protocol_has_no_previous_supported_version():
    """A v2 rollout must extend this contract to cover its retained v1 path."""
    assert API_VERSION == "1"
    assert SUPPORTED_API_VERSIONS == (API_VERSION,)


def test_status_schema_version_matches_the_status_record_schema_const():
    """The writer's version stamp and the schema's own const are one number.

    They are written by different code paths -- the record producer and the
    validator -- and only this test makes a bump to one require the other.
    """
    schema = load_schema(STATUS_RECORD_SCHEMA)
    assert STATUS_SCHEMA_VERSION == schema["properties"]["schema_version"]["const"]


def test_max_module_id_length_matches_the_frozen_pattern_quantifier():
    """The pattern expresses the limit indirectly, one smaller than the constant.

    ``^[a-z0-9][a-z0-9_-]{0,127}\\Z`` matches the first character with a separate
    class, so the quantifier bound is 127 for a 128-character limit. The number is
    extracted from the schema rather than restated, and the boundary is exercised
    against the pattern so dropping the leading class also fails.
    """
    pattern = load_schema(MODULE_ID_SCHEMA)["properties"]["module_id"]["pattern"]
    match = re.search(r"\{0,(\d+)\}", pattern)
    assert match, f"module_id pattern {pattern!r} declares no quantifier bound"
    assert MAX_MODULE_ID_LENGTH == int(match.group(1)) + 1

    assert re.search(pattern, "a" * MAX_MODULE_ID_LENGTH)
    assert not re.search(pattern, "a" * (MAX_MODULE_ID_LENGTH + 1))


def test_reserved_module_ids_is_exactly_the_builtin_adapter():
    """Reserving more ids than the plan states would silently forbid legal names.

    ``stormprob`` is the built-in; ``stormcast`` stays reserved during migration
    so an external module cannot impersonate the retired producer.
    """
    assert RESERVED_MODULE_IDS == frozenset({"stormprob", "stormcast"})


def test_every_reserved_module_id_is_a_legal_module_id():
    """A reserved id no module could have typed anyway would be a dead rule.

    If a reserved name failed the ``module_id`` pattern, the reservation would
    never fire and the real collision it guards against would go unprotected.
    """
    pattern = load_schema(MODULE_ID_SCHEMA)["properties"]["module_id"]["pattern"]
    for module_id in sorted(RESERVED_MODULE_IDS):
        assert re.search(pattern, module_id), (
            f"reserved id {module_id!r} does not match the frozen module_id "
            f"pattern, so no external module could ever have claimed it"
        )
