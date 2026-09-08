"""Precedence resolution and the origin registry it writes for diagnostics.

The registry exists so a startup summary can say where an effective value came
from; ``get_provenance`` is file-level and cannot. These tests pin that the
recording is faithful and that it stays invisible to resolution itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config import overlay


@pytest.fixture(autouse=True)
def clean_origins():
    overlay.reset_origins()
    yield
    overlay.reset_origins()


def test_env_override_is_reported_as_env_sourced(monkeypatch):
    monkeypatch.setenv("EDGEWARN_TEST_ATTEMPTS", "9")

    resolved = overlay.resolve(
        None,
        env_names=("EDGEWARN_TEST_ATTEMPTS",),
        yaml_value=3,
        key="cycle.retry.max_attempts",
    )

    assert resolved == 9
    assert overlay.overrides() == {
        "cycle.retry.max_attempts": "env:EDGEWARN_TEST_ATTEMPTS"
    }


def test_the_winning_variable_is_named_not_the_first_candidate(monkeypatch):
    monkeypatch.delenv("EDGEWARN_TEST_PRIMARY", raising=False)
    monkeypatch.setenv("EDGEWARN_TEST_FALLBACK", "4")

    overlay.resolve(
        None,
        env_names=("EDGEWARN_TEST_PRIMARY", "EDGEWARN_TEST_FALLBACK"),
        yaml_value=1,
        key="sample.key",
    )

    assert overlay.overrides() == {"sample.key": "env:EDGEWARN_TEST_FALLBACK"}


def test_cli_override_outranks_env_and_is_reported_as_cli(monkeypatch):
    monkeypatch.setenv("EDGEWARN_TEST_ATTEMPTS", "9")

    resolved = overlay.resolve(
        7,
        env_names=("EDGEWARN_TEST_ATTEMPTS",),
        yaml_value=3,
        key="cycle.retry.max_attempts",
    )

    assert resolved == 7
    assert overlay.overrides() == {"cycle.retry.max_attempts": "cli"}


def test_a_yaml_value_is_recorded_but_not_reported_as_an_override():
    resolved = overlay.resolve(None, yaml_value=3, key="cycle.retry.max_attempts")

    assert resolved == 3
    assert overlay.overrides() == {}
    assert overlay.origins() == {"cycle.retry.max_attempts": "yaml"}


def test_base_dir_resolution_uses_only_platform_defaults(monkeypatch):
    monkeypatch.delenv("EDGEWARN_BASE_DIR", raising=False)
    defaults = {"posix": "~/EdgeWARN_input", "windows": r"C:\\EdgeWARN_input"}

    assert overlay.resolve_base_dir(None, defaults, system="Linux") == Path.home() / "EdgeWARN_input"
    assert overlay.resolve_base_dir(None, defaults, system="Windows") == Path(r"C:\\EdgeWARN_input")


def test_the_registry_holds_no_values(monkeypatch):
    """Labels name the layer, never the value, so a secret cannot leak."""
    monkeypatch.setenv("EDGEWARN_TEST_TOKEN", "s3cret")

    overlay.resolve(
        None, env_names=("EDGEWARN_TEST_TOKEN",), yaml_value="", key="api.security.token"
    )

    assert overlay.overrides() == {"api.security.token": "env:EDGEWARN_TEST_TOKEN"}


def test_an_unparseable_override_records_no_winner(monkeypatch):
    """The env layer raised instead of producing a value, so it did not win."""
    monkeypatch.setenv("EDGEWARN_TEST_ATTEMPTS", "not-a-number")

    with pytest.raises(ValueError):
        overlay.resolve(
            None, env_names=("EDGEWARN_TEST_ATTEMPTS",), yaml_value=3, key="sample.key"
        )

    assert overlay.overrides() == {}


def test_a_rejected_override_names_the_variable_that_carried_it(monkeypatch):
    """`int(raw)` used to surface as "invalid literal for int() with base 10".

    That message names neither the variable nor the file, so an operator with a
    typo in one of the twelve override variables learned only that something,
    somewhere, was not a number.
    """
    monkeypatch.setenv("EDGEWARN_TEST_ATTEMPTS", "abc")

    with pytest.raises(ValueError, match=r"EDGEWARN_TEST_ATTEMPTS must be an integer, got 'abc'"):
        overlay.resolve(None, env_names=("EDGEWARN_TEST_ATTEMPTS",), yaml_value=3)


def test_a_bound_rejects_an_out_of_range_override(monkeypatch):
    """Without `minimum` a negative override parses cleanly and is honored.

    The value then flows into whatever the key means -- an age budget, a pool
    size -- and misbehaves far from the variable that caused it.
    """
    monkeypatch.setenv("EDGEWARN_TEST_AGE", "-1")

    with pytest.raises(ValueError, match=r"must be a non-negative integer, got '-1'"):
        overlay.resolve(
            None, env_names=("EDGEWARN_TEST_AGE",), yaml_value=180, minimum=0, key="sample.key"
        )

    assert overlay.overrides() == {}


def test_a_tristate_key_needs_an_explicit_type_to_be_coerced(monkeypatch):
    """A `null` YAML value carries no type, so inference has nothing to work from.

    Without `value_type` the raw string comes back uncoerced, and every non-empty
    value -- including "0" and "false" -- is truthy.
    """
    monkeypatch.setenv("EDGEWARN_TEST_FLAG", "false")

    assert overlay.resolve(None, env_names=("EDGEWARN_TEST_FLAG",), yaml_value=None) == "false"
    assert (
        overlay.resolve(
            None, env_names=("EDGEWARN_TEST_FLAG",), yaml_value=None, value_type=bool
        )
        is False
    )


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_a_variable_exported_empty_counts_as_unset(monkeypatch, raw):
    """Clearing a setting by exporting it blank must not mean `float("")`.

    A shell wrapper or compose file that writes `EDGEWARN_X=` for an unset
    option is expressing absence, and the numeric sites would otherwise raise on
    a variable the operator believes is switched off.

    `""` is parametrized alongside the whitespace forms because it is the one a
    shell actually produces -- `EDGEWARN_X=` exports the empty string, not spaces
    -- and because it is the case whose behavior the migration changed. See
    `test_a_blank_rap_age_now_defers_instead_of_raising`.
    """
    monkeypatch.setenv("EDGEWARN_TEST_BLANK", raw)

    resolved = overlay.resolve(
        None, env_names=("EDGEWARN_TEST_BLANK",), yaml_value=3.5, key="sample.key"
    )

    assert resolved == 3.5
    assert overlay.overrides() == {}


def test_a_blank_rap_age_now_defers_instead_of_raising(monkeypatch):
    """The one accepted-value change the overlay migration made.

    Before the move `get_rap_max_age_minutes` tested `if raw_value is None`, so
    `EDGEWARN_RAP_MAX_AGE_MINUTES=` reached `int("")` and raised. It now falls
    through to the catalog, matching what `render.py` and `performance.py`
    already did. Pinned at the call site rather than only in the generic test
    because the generic one would still pass if this accessor grew its own
    pre-check and started raising again.
    """
    from common.ingest.synoptic.config import RAP_MAX_AGE_ENV, get_rap_max_age_minutes

    monkeypatch.delenv(RAP_MAX_AGE_ENV, raising=False)
    catalog_value = get_rap_max_age_minutes()

    monkeypatch.setenv(RAP_MAX_AGE_ENV, "")
    assert get_rap_max_age_minutes() == catalog_value

    # The bound and the parse are still enforced for a value that is actually
    # present, so widening "blank" did not widen "malformed".
    monkeypatch.setenv(RAP_MAX_AGE_ENV, "-1")
    with pytest.raises(ValueError, match="non-negative"):
        get_rap_max_age_minutes()

    monkeypatch.setenv(RAP_MAX_AGE_ENV, "abc")
    with pytest.raises(ValueError, match="non-negative"):
        get_rap_max_age_minutes()


def test_an_unnamed_key_resolves_without_being_recorded(monkeypatch):
    monkeypatch.setenv("EDGEWARN_TEST_ATTEMPTS", "9")

    resolved = overlay.resolve(None, env_names=("EDGEWARN_TEST_ATTEMPTS",), yaml_value=3)

    assert resolved == 9
    assert overlay.overrides() == {}


def test_the_last_read_wins_for_a_repeated_key(monkeypatch):
    """Accessors resolve per call, some inside poll loops, so this must not grow."""
    monkeypatch.setenv("EDGEWARN_TEST_ATTEMPTS", "9")
    overlay.resolve(
        None, env_names=("EDGEWARN_TEST_ATTEMPTS",), yaml_value=3, key="sample.key"
    )

    monkeypatch.delenv("EDGEWARN_TEST_ATTEMPTS")
    overlay.resolve(
        None, env_names=("EDGEWARN_TEST_ATTEMPTS",), yaml_value=3, key="sample.key"
    )

    assert overlay.overrides() == {}
