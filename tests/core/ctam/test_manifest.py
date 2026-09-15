"""Manifest validation, one rejection rule per test.

Every assertion here checks the *message*, not just that an exception was raised.
The message is what discovery stores as a module's ``reason`` and what the status
record publishes, so an operator has to be able to act on it without reading
source. A rule that rejects with an unhelpful message is only half implemented.

Manifests are built in ``tmp_path`` rather than checked in, because most cases
are one deliberately wrong key and a folder per case would bury the valid
fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from EdgeWARN.ctam.limits import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_HISTORY_WINDOW,
    MAX_MODULE_ID_LENGTH,
    MAX_PUBLIC_ROUTES_PER_MODULE,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
)
from EdgeWARN.ctam.manifest import (
    ManifestError,
    ModuleManifest,
    parse_manifest,
    parse_selector,
)

pytestmark = pytest.mark.ctam


MINIMAL = """\
schema_version = 1
id = "cellstats"
name = "CellStats"
version = "1.0.0"
api_version = "1"
entrypoint = ["{python}", "main.py"]

[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/modules/CellStats"
"""


def write_module(root: Path, name: str, body: str) -> Path:
    """Create ``<root>/<name>/module.toml`` plus the payload the manifest names."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "main.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    manifest_path = directory / "module.toml"
    manifest_path.write_text(body, encoding="utf-8")
    return manifest_path


def replace(body: str, old: str, new: str) -> str:
    assert old in body, f"fixture no longer contains {old!r}"
    return body.replace(old, new)


# --------------------------------------------------------------------------
# The valid case
# --------------------------------------------------------------------------


FULL = """\
schema_version = 1
id = "cellstats"
name = "CellStats"
version = "2.3.4"
api_version = "1"
enabled = true
required = true
scope = "cycle"
entrypoint = ["{python}", "main.py", "--verbose"]
timeout_seconds = 12
after = ["stormprob", "other"]

[[requires]]
selector = "stormcells.current"

[[requires]]
selector = "cells.history"
required = true
min_history_entries = 2

[[requires]]
selector = "input:MRMS:VIL_00.50:current"
required = false
max_age_seconds = 180

[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/modules/CellStats"

[[writes]]
resource = "cells.history"
json_pointer = "/*/modules/CellStats"

[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/properties/cellstats_severity"
"""


def test_fully_populated_manifest_round_trips(tmp_path):
    """The one test that pins every field, so a silently dropped key fails here."""
    manifest_path = write_module(tmp_path, "cellstats", FULL)
    manifest = parse_manifest(manifest_path)

    assert isinstance(manifest, ModuleManifest)
    assert manifest.module_id == "cellstats"
    assert manifest.name == "CellStats"
    assert manifest.version == "2.3.4"
    assert manifest.api_version == "1"
    assert manifest.enabled is True
    assert manifest.required is True
    assert manifest.scope == "cycle"
    assert manifest.entrypoint == ("{python}", "main.py", "--verbose")
    assert manifest.timeout_seconds == 12
    assert manifest.after == ("stormprob", "other")
    assert manifest.directory == tmp_path / "cellstats"
    assert manifest.manifest_path == manifest_path

    assert [r.selector.raw for r in manifest.requires] == [
        "stormcells.current",
        "cells.history",
        "input:MRMS:VIL_00.50:current",
    ]
    # An input the author did not explicitly mark optional is needed, so a silent
    # skip is never the default.
    assert manifest.requires[0].required is True
    assert manifest.requires[1].min_history_entries == 2
    assert manifest.requires[2].required is False
    assert manifest.requires[2].max_age_seconds == 180.0
    assert manifest.requires[0].max_age_seconds is None
    assert manifest.requires[0].min_history_entries is None

    assert [(w.resource, w.json_pointer) for w in manifest.writes] == [
        ("stormcells.current", "/features/*/modules/CellStats"),
        ("cells.history", "/*/modules/CellStats"),
        ("stormcells.current", "/features/*/properties/cellstats_severity"),
    ]


def test_entrypoint_placeholder_is_not_expanded(tmp_path):
    """Phase 4 owns launching; the manifest stays the record of what was declared.

    Substituting ``sys.executable`` here would make the parsed manifest describe
    one host rather than the author's intent, and would make a status record
    leak an absolute interpreter path.
    """
    manifest = parse_manifest(write_module(tmp_path, "cellstats", FULL))
    assert manifest.entrypoint[0] == "{python}"


def test_omitted_optional_keys_take_their_documented_defaults(tmp_path):
    manifest = parse_manifest(write_module(tmp_path, "cellstats", MINIMAL))
    assert manifest.enabled is True
    assert manifest.required is False
    assert manifest.scope == "stormcells"
    assert manifest.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert manifest.after == ()
    assert manifest.requires == ()


def test_checked_in_fixture_parses(tmp_path):
    """The fixture an operator would copy must be a manifest the host accepts."""
    fixture = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "ctam_modules"
    manifest = parse_manifest(fixture / "cellstats" / "module.toml")
    assert manifest.module_id == "cellstats"
    assert manifest.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


# --------------------------------------------------------------------------
# File-level failures
# --------------------------------------------------------------------------


def test_missing_manifest_names_the_expected_filename(tmp_path):
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(tmp_path / "cellstats" / "module.toml")
    assert "module.toml" in str(excinfo.value)


def test_unparsable_toml_is_reported_as_toml_not_as_a_missing_key(tmp_path):
    """A syntax error must not be misreported as a semantic problem."""
    manifest_path = write_module(tmp_path, "cellstats", "id = \n")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(manifest_path)
    assert "not valid TOML" in str(excinfo.value)


# --------------------------------------------------------------------------
# schema_version and api_version
# --------------------------------------------------------------------------


def test_missing_schema_version_is_rejected(tmp_path):
    body = replace(MINIMAL, "schema_version = 1\n", "")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "schema_version" in str(excinfo.value)
    assert "schema_version = 1" in str(excinfo.value)


def test_unsupported_schema_version_is_rejected(tmp_path):
    body = replace(MINIMAL, "schema_version = 1", "schema_version = 2")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "schema_version" in str(excinfo.value)
    assert "1" in str(excinfo.value)


def test_missing_api_version_is_rejected(tmp_path):
    body = replace(MINIMAL, 'api_version = "1"\n', "")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "api_version" in str(excinfo.value)


def test_numeric_api_version_is_rejected_because_the_status_record_stores_a_string(tmp_path):
    body = replace(MINIMAL, 'api_version = "1"', "api_version = 1")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "api_version" in str(excinfo.value)
    assert "string" in str(excinfo.value)


def test_non_digit_api_version_is_rejected(tmp_path):
    """``^[0-9]+\\Z`` is the frozen status-record pattern; "1.0" cannot be recorded."""
    body = replace(MINIMAL, 'api_version = "1"', 'api_version = "1.0"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "digits" in str(excinfo.value)


def test_unsupported_api_version_is_rejected(tmp_path):
    body = replace(MINIMAL, 'api_version = "1"', 'api_version = "2"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "not supported" in str(excinfo.value)


# --------------------------------------------------------------------------
# id
# --------------------------------------------------------------------------


def test_missing_id_suggests_the_directory_name(tmp_path):
    body = replace(MINIMAL, 'id = "cellstats"\n', "")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert 'id = "cellstats"' in str(excinfo.value)


def test_uppercase_id_is_rejected(tmp_path):
    body = replace(MINIMAL, 'id = "cellstats"', 'id = "CellStats"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "CellStats", body))
    assert "lowercase" in str(excinfo.value)


def test_id_not_matching_its_directory_is_rejected(tmp_path):
    """Equality with the directory name is what makes duplicates decidable.

    Without it, two directories could both claim ``cellstats`` and the filesystem
    alone would not say which one an operator meant.
    """
    body = replace(MINIMAL, 'id = "cellstats"', 'id = "otherstats"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert "'otherstats'" in message
    assert "'cellstats'" in message
    assert "directory" in message


def test_reserved_id_cannot_be_shadowed_by_an_installation(tmp_path):
    body = replace(MINIMAL, 'id = "cellstats"', 'id = "stormprob"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "stormprob", body))
    assert "reserved" in str(excinfo.value)


def test_id_of_maximum_length_is_accepted(tmp_path):
    """128 is the quantifier boundary of the frozen module_id pattern."""
    longest = "a" * MAX_MODULE_ID_LENGTH
    body = replace(MINIMAL, 'id = "cellstats"', f'id = "{longest}"')
    manifest = parse_manifest(write_module(tmp_path, longest, body))
    assert manifest.module_id == longest


def test_id_one_character_over_the_maximum_is_rejected(tmp_path):
    over = "a" * (MAX_MODULE_ID_LENGTH + 1)
    body = replace(MINIMAL, 'id = "cellstats"', f'id = "{over}"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, over, body))
    assert str(MAX_MODULE_ID_LENGTH) in str(excinfo.value)


# --------------------------------------------------------------------------
# name, version, enabled, required, scope
# --------------------------------------------------------------------------


def test_missing_name_explains_that_it_becomes_the_output_key(tmp_path):
    body = replace(MINIMAL, 'name = "CellStats"\n', "")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "name" in str(excinfo.value)
    assert "output key" in str(excinfo.value)


def test_name_with_illegal_characters_is_rejected(tmp_path):
    body = replace(MINIMAL, 'name = "CellStats"', 'name = "Cell Stats!"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "output key" in str(excinfo.value)


@pytest.mark.parametrize("candidate", ["StormProb", "stormprob", "STORMPROB", "_grid_outputs"])
def test_name_colliding_with_a_reserved_output_key_is_rejected(tmp_path, candidate):
    """Case-insensitive, because a case-only difference is not a distinct key.

    Two sibling keys in ``modules`` that differ only by case are
    indistinguishable to an operator reading a snapshot and to anything that
    lower-cases a lookup.
    """
    body = replace(MINIMAL, 'name = "CellStats"', f'name = "{candidate}"')
    body = replace(body, "/features/*/modules/CellStats", f"/features/*/modules/{candidate}")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "reserved" in str(excinfo.value)


def test_missing_version_is_rejected(tmp_path):
    body = replace(MINIMAL, 'version = "1.0.0"\n', "")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "version" in str(excinfo.value)


@pytest.mark.parametrize("candidate", ['"1.0"', '"one"', '"1.0.0-rc1"', "1"])
def test_non_three_part_version_is_rejected(tmp_path, candidate):
    """The status record records ``version`` under ``^[0-9]+\\.[0-9]+\\.[0-9]+\\Z``."""
    body = replace(MINIMAL, 'version = "1.0.0"', f"version = {candidate}")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert '"1.0.0"' in str(excinfo.value)


@pytest.mark.parametrize("key", ["enabled", "required"])
def test_non_boolean_flag_is_rejected(tmp_path, key):
    body = f'{key} = "yes"\n' + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert key in str(excinfo.value)
    assert "true or false" in str(excinfo.value)


def test_unknown_scope_is_rejected(tmp_path):
    body = 'scope = "grid"\n' + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert "'stormcells'" in message and "'cycle'" in message


# --------------------------------------------------------------------------
# timeout_seconds
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [MIN_TIMEOUT_SECONDS, 10, MAX_TIMEOUT_SECONDS])
def test_timeout_inside_the_documented_bounds_is_accepted(tmp_path, value):
    body = f"timeout_seconds = {value}\n" + MINIMAL
    assert parse_manifest(write_module(tmp_path, "cellstats", body)).timeout_seconds == value


@pytest.mark.parametrize("value", [0, -1, MAX_TIMEOUT_SECONDS + 1, 300])
def test_timeout_outside_the_documented_bounds_is_rejected(tmp_path, value):
    body = f"timeout_seconds = {value}\n" + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert str(MIN_TIMEOUT_SECONDS) in message and str(MAX_TIMEOUT_SECONDS) in message


def test_boolean_timeout_is_rejected_despite_bool_being_an_int(tmp_path):
    """``isinstance(True, int)`` is True, so a naive range check accepts ``true``.

    ``timeout_seconds = true`` would then pass as 1 second and silently give the
    module the minimum budget instead of failing the manifest.
    """
    body = "timeout_seconds = true\n" + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "whole number" in str(excinfo.value)


def test_fractional_timeout_is_rejected(tmp_path):
    body = "timeout_seconds = 10.5\n" + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "whole number" in str(excinfo.value)


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------


def test_missing_entrypoint_is_rejected(tmp_path):
    body = replace(MINIMAL, 'entrypoint = ["{python}", "main.py"]\n', "")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "entrypoint" in str(excinfo.value)


def test_shell_string_entrypoint_is_rejected(tmp_path):
    """An argument vector, never a shell string: the launcher uses no shell."""
    body = replace(MINIMAL, 'entrypoint = ["{python}", "main.py"]', 'entrypoint = "python main.py"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "array of strings" in str(excinfo.value)


def test_empty_entrypoint_is_rejected(tmp_path):
    body = replace(MINIMAL, 'entrypoint = ["{python}", "main.py"]', "entrypoint = []")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "empty" in str(excinfo.value)


def test_non_string_entrypoint_element_is_rejected(tmp_path):
    body = replace(MINIMAL, 'entrypoint = ["{python}", "main.py"]', 'entrypoint = ["{python}", 7]')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "must be a string" in str(excinfo.value)


@pytest.mark.parametrize("metacharacter", [";", "|", "&", "<", ">", "`", "$"])
def test_shell_metacharacter_in_an_argument_is_rejected(tmp_path, metacharacter):
    """Rejected even though no shell is used, because it signals author intent.

    Phase 4 should never have to decide whether ``main.py; rm -rf /`` was meant
    as one filename or two commands.
    """
    body = replace(
        MINIMAL,
        'entrypoint = ["{python}", "main.py"]',
        f'entrypoint = ["{{python}}", "main.py {metacharacter} echo"]',
    )
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "metacharacter" in str(excinfo.value)


def test_unknown_placeholder_is_rejected(tmp_path):
    body = replace(MINIMAL, '"{python}", "main.py"', '"{venv_python}", "main.py"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert "{venv_python}" in message
    assert "{python}" in message


def test_absolute_entrypoint_path_is_rejected(tmp_path):
    body = replace(MINIMAL, '"main.py"', '"/usr/bin/evil.py"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "absolute path" in str(excinfo.value)


def test_relative_entrypoint_path_escaping_the_module_directory_is_rejected(tmp_path):
    body = replace(MINIMAL, '"main.py"', '"../outside/evil.py"')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "outside the module directory" in str(excinfo.value)


def test_symlinked_entrypoint_pointing_outside_the_module_directory_is_rejected(tmp_path):
    """``resolve()`` is what makes containment survive a symlink, not string checks.

    ``payload.py`` looks contained and is not, which is exactly the case a
    prefix comparison on the unresolved path would admit.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "evil.py"
    target.write_text("", encoding="utf-8")
    directory = tmp_path / "cellstats"
    directory.mkdir()
    try:
        (directory / "payload.py").symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform refuses to create a symlink (needs privilege): {exc}")
    body = replace(MINIMAL, '"main.py"', '"payload.py"')
    (directory / "module.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(directory / "module.toml")
    assert "outside the module directory" in str(excinfo.value)


def test_flag_arguments_are_not_treated_as_paths(tmp_path):
    """A flag is not a payload, so containment must not reject ``--config``."""
    body = replace(MINIMAL, '"main.py"', '"main.py", "--strict"')
    manifest = parse_manifest(write_module(tmp_path, "cellstats", body))
    assert manifest.entrypoint == ("{python}", "main.py", "--strict")


# --------------------------------------------------------------------------
# after
# --------------------------------------------------------------------------


def test_after_must_be_an_array(tmp_path):
    body = 'after = "stormprob"\n' + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "after" in str(excinfo.value)


def test_after_element_with_an_illegal_id_is_rejected(tmp_path):
    body = 'after = ["StormProb"]\n' + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "legal module id" in str(excinfo.value)


def test_self_reference_in_after_is_rejected(tmp_path):
    """A self-dependency is a cycle of length one and would deadlock ordering."""
    body = 'after = ["cellstats"]\n' + MINIMAL
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "cannot depend on itself" in str(excinfo.value)


def test_after_is_deduplicated_but_order_is_preserved(tmp_path):
    body = 'after = ["stormprob", "other", "stormprob"]\n' + MINIMAL
    assert parse_manifest(write_module(tmp_path, "cellstats", body)).after == (
        "stormprob",
        "other",
    )


# --------------------------------------------------------------------------
# Selector grammar
# --------------------------------------------------------------------------


def test_stormcells_selector_parses():
    selector = parse_selector("stormcells.current")
    assert (selector.kind, selector.family, selector.product, selector.role) == (
        "stormcells",
        None,
        None,
        "current",
    )
    assert selector.raw == "stormcells.current"


def test_cell_history_selector_parses():
    selector = parse_selector("cells.history")
    assert (selector.kind, selector.family, selector.product, selector.role) == (
        "cell_history",
        None,
        None,
        "history",
    )


@pytest.mark.parametrize("role", ["current", "previous"])
def test_input_selector_parses(role):
    selector = parse_selector(f"input:MRMS:VIL_00.50:{role}")
    assert selector.kind == "input"
    assert selector.family == "mrms"
    assert selector.product == "VIL_00.50"
    assert selector.role == role


def test_family_is_folded_to_lowercase_while_raw_is_preserved():
    """The case asymmetry is deliberate and comes from the Phase 0 examples.

    The selector spells the family in the plan's uppercase form
    (``input:MRMS:...``) while a catalog ``file_id`` uses lowercase
    (``input:mrms:...``), so ``family`` is folded for matching. ``raw`` stays
    verbatim because ``requirements-evaluation.schema.json`` requires the
    selector to be echoed back exactly as the manifest wrote it.
    """
    selector = parse_selector("input:MRMS:VIL_00.50:current")
    assert selector.family == "mrms"
    assert selector.raw == "input:MRMS:VIL_00.50:current"
    assert parse_selector("input:mrms:VIL_00.50:current").family == "mrms"


def test_product_case_is_not_folded():
    """Product ids are mixed case in the catalog, so folding would break matching."""
    selector = parse_selector("input:MRMS:MergedReflectivityQCComposite_00.50:current")
    assert selector.product == "MergedReflectivityQCComposite_00.50"


def test_alerts_selector_is_rejected_with_an_explanation():
    """Alerts are deliberately not an admitted Phase 1 input, not an oversight."""
    with pytest.raises(ManifestError) as excinfo:
        parse_selector("alerts.current")
    message = str(excinfo.value)
    assert "stormcells.current" in message and "cells.history" in message
    assert "Alerts are not an admitted Phase 1 module input" in message


@pytest.mark.parametrize(
    "raw",
    [
        "stormcells.previous",
        "cells.current",
        "stormcells",
        "input:MRMS:VIL_00.50",
        "input:MRMS:VIL_00.50:current:extra",
    ],
)
def test_unknown_selector_shapes_are_rejected(raw):
    with pytest.raises(ManifestError):
        parse_selector(raw)


def test_unknown_input_family_names_the_accepted_families():
    with pytest.raises(ManifestError) as excinfo:
        parse_selector("input:NEXRAD:VIL_00.50:current")
    message = str(excinfo.value)
    assert "mrms" in message and "goes" in message and "rap" in message


def test_unknown_input_role_points_at_cells_history_for_history():
    with pytest.raises(ManifestError) as excinfo:
        parse_selector("input:MRMS:VIL_00.50:history")
    assert "cells.history" in str(excinfo.value)


def test_product_typo_fails_at_discovery_against_the_host_catalog():
    """A typo must not become a requirement that is perpetually unsatisfied."""
    with pytest.raises(ManifestError) as excinfo:
        parse_selector("input:MRMS:VIL_00.51:current")
    assert "not in the host's mrms catalog" in str(excinfo.value)


def test_selector_violating_the_frozen_pattern_is_rejected():
    """The frozen selector pattern admits no space, so a spaced product fails."""
    with pytest.raises(ManifestError) as excinfo:
        parse_selector("input:MRMS:VIL 00.50:current")
    assert "not a legal selector string" in str(excinfo.value)


def test_selector_with_a_trailing_newline_is_rejected():
    r"""``\Z`` rather than ``$``: Python's ``$`` also matches before a newline."""
    with pytest.raises(ManifestError):
        parse_selector("stormcells.current\n")


def test_non_string_selector_is_rejected():
    with pytest.raises(ManifestError) as excinfo:
        parse_selector(7)
    assert "must be a string" in str(excinfo.value)


# --------------------------------------------------------------------------
# [[requires]]
# --------------------------------------------------------------------------


def requires_block(selector: str, extra: str = "") -> str:
    return MINIMAL + f'\n[[requires]]\nselector = "{selector}"\n{extra}'


def test_requires_block_without_a_selector_is_rejected(tmp_path):
    body = MINIMAL + "\n[[requires]]\nrequired = true\n"
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "no 'selector'" in str(excinfo.value)


def test_max_age_on_a_host_owned_selector_is_rejected(tmp_path):
    """Host artifacts are always this cycle's, so freshness there is meaningless.

    Accepting the key would give the author a false sense that staleness was
    being checked on something that has no staleness.
    """
    body = requires_block("stormcells.current", "max_age_seconds = 60\n")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert "max_age_seconds" in message and "input:" in message


@pytest.mark.parametrize("value", ["0", "-5", "true", '"60"'])
def test_non_positive_or_non_numeric_max_age_is_rejected(tmp_path, value):
    body = requires_block("input:MRMS:VIL_00.50:current", f"max_age_seconds = {value}\n")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "max_age_seconds" in str(excinfo.value)


def test_min_history_entries_on_a_non_history_selector_is_rejected(tmp_path):
    body = requires_block("stormcells.current", "min_history_entries = 2\n")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert "min_history_entries" in message and "cells.history" in message


def test_min_history_entries_below_one_is_rejected(tmp_path):
    body = requires_block("cells.history", "min_history_entries = 0\n")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "at least 1" in str(excinfo.value)


def test_min_history_entries_above_the_read_window_is_a_manifest_bug(tmp_path):
    """No read can return more than the maximum window, so this can never pass.

    Recording it as an unmet runtime requirement every cycle would hide an
    authoring mistake behind a recurring operational alarm.
    """
    body = requires_block(
        "cells.history", f"min_history_entries = {MAX_HISTORY_WINDOW + 1}\n"
    )
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert str(MAX_HISTORY_WINDOW) in message
    assert "no read could ever satisfy it" in message


def test_min_history_entries_at_the_read_window_is_accepted(tmp_path):
    body = requires_block("cells.history", f"min_history_entries = {MAX_HISTORY_WINDOW}\n")
    manifest = parse_manifest(write_module(tmp_path, "cellstats", body))
    assert manifest.requires[0].min_history_entries == MAX_HISTORY_WINDOW


def test_boolean_min_history_entries_is_rejected(tmp_path):
    body = requires_block("cells.history", "min_history_entries = true\n")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "whole number" in str(excinfo.value)


def test_non_boolean_requirement_required_flag_is_rejected(tmp_path):
    body = requires_block("cells.history", 'required = "yes"\n')
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "true or false" in str(excinfo.value)


# --------------------------------------------------------------------------
# [[writes]] and the pointer allowlist
# --------------------------------------------------------------------------


NO_WRITES = """\
schema_version = 1
id = "cellstats"
name = "CellStats"
version = "1.0.0"
api_version = "1"
entrypoint = ["{python}", "main.py"]
"""


def writes_block(resource: str, pointer: str) -> str:
    return NO_WRITES + f'\n[[writes]]\nresource = "{resource}"\njson_pointer = "{pointer}"\n'


def test_manifest_with_no_writes_is_rejected(tmp_path):
    """A module that declares no output has nothing to contribute."""
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", NO_WRITES))
    assert "no [[writes]] blocks" in str(excinfo.value)


def test_unknown_write_resource_is_rejected(tmp_path):
    body = writes_block("alerts.current", "/features/*/modules/CellStats")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    message = str(excinfo.value)
    assert "'stormcells.current'" in message and "'cells.history'" in message


def test_duplicate_write_entry_is_rejected(tmp_path):
    body = writes_block("stormcells.current", "/features/*/modules/CellStats")
    body += '\n[[writes]]\nresource = "stormcells.current"\njson_pointer = "/features/*/modules/CellStats"\n'
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert "repeats" in str(excinfo.value)


def test_the_same_pointer_under_two_resources_is_not_a_duplicate(tmp_path):
    """Uniqueness is per ``(resource, pointer)``: the two documents differ."""
    body = writes_block("stormcells.current", "/features/*/modules/CellStats")
    body += '\n[[writes]]\nresource = "cells.history"\njson_pointer = "/*/modules/CellStats"\n'
    assert len(parse_manifest(write_module(tmp_path, "cellstats", body)).writes) == 2


# The caller throughout is module id "cellstats" with display name "CellStats",
# matching the caller in tests/core/ctam/contract/test_pointer_allowlist.py.
ACCEPTED_POINTERS = (
    ("stormcells.current", "/features/*/modules/CellStats", "the caller's own namespace"),
    (
        "stormcells.current",
        "/features/*/modules/CellStats/gate_density",
        "a leaf inside the caller's namespace",
    ),
    (
        "stormcells.current",
        "/features/*/modules/CellStats/nested/deeper",
        "depth is bounded by a limit, not by the allowlist",
    ),
    (
        "stormcells.current",
        "/features/*/properties/cellstats_severity",
        "a module-id-prefixed flat scalar",
    ),
    (
        "stormcells.current",
        "/features/*/properties/cellstats",
        "the bare module id is the owner's key too",
    ),
    (
        "stormcells.current",
        "/features/*/modules/CellStats/key~1with~1slashes",
        "~1 is an escaped slash inside one segment, not a separator",
    ),
    ("cells.history", "/*/modules/CellStats", "the history document uses the /*/ prefix"),
    (
        "cells.history",
        "/*/properties/cellstats_severity",
        "the same ownership rule applies in history entries",
    ),
)

REJECTED_POINTERS = (
    # Prefix: a manifest pointer is document-relative.
    ("stormcells.current", "/modules/CellStats", "entry-relative, missing /features/*"),
    ("cells.history", "/features/*/modules/CellStats", "the history document has no /features"),
    ("stormcells.current", "/features/0/modules/CellStats", "an index, not the '*' element"),
    ("stormcells.current", "features/*/modules/CellStats", "not a pointer: no leading slash"),
    # Containers themselves.
    ("stormcells.current", "/features/*/modules", "cannot replace or clear a container"),
    ("stormcells.current", "/features/*/properties", "cannot replace or clear a container"),
    ("cells.history", "/*/modules", "cannot replace or clear a container"),
    # Trailing and empty segments.
    ("stormcells.current", "/features/*/modules/", "trailing empty segment"),
    ("stormcells.current", "/features/*/modules/CellStats/", "trailing empty segment"),
    ("stormcells.current", "/features/*/modules//CellStats", "empty interior segment"),
    # Outside the two containers.
    ("stormcells.current", "/features/*/id", "core identity is host-owned"),
    ("stormcells.current", "/features/*/centroid", "geometry is host-owned"),
    ("stormcells.current", "/features/*/timestamp", "timestamps are host-owned"),
    ("stormcells.current", "/features/*/Modules/CellStats", "container names are case-sensitive"),
    ("stormcells.current", "/features/*/modulesX/CellStats", "container name must match exactly"),
    # Another module's key, or a reserved one.
    ("stormcells.current", "/features/*/modules/SomeOtherModule", "another module's namespace"),
    ("stormcells.current", "/features/*/modules/StormProb", "reserved built-in namespace"),
    ("stormcells.current", "/features/*/modules/_grid_outputs", "reserved legacy key"),
    (
        "stormcells.current",
        "/features/*/modules/cellstats",
        "the module id, not the manifest display name",
    ),
    (
        "stormcells.current",
        "/features/*/properties/p95VIL",
        "an enrichment value StormProb consumes as if measured",
    ),
    ("stormcells.current", "/features/*/properties/severity", "missing the module-id prefix"),
    (
        "stormcells.current",
        "/features/*/properties/cellstatsX",
        "the prefix must be the id or 'id_', not any string starting with it",
    ),
    # Escaping.
    (
        "stormcells.current",
        "/features/*/modules~1CellStats",
        "an encoded separator is one literal key named 'modules/CellStats'",
    ),
    ("stormcells.current", "/features/*/modules/CellStats/bad~2", "'~2' is not a legal escape"),
    ("stormcells.current", "/features/*/modules/CellStats/trailing~", "a lone trailing '~'"),
)


@pytest.mark.parametrize(
    "resource,pointer,why",
    [pytest.param(r, p, w, id=p) for r, p, w in ACCEPTED_POINTERS],
)
def test_accepted_write_pointer(tmp_path, resource, pointer, why):
    body = writes_block(resource, pointer)
    manifest = parse_manifest(write_module(tmp_path, "cellstats", body))
    assert manifest.writes[0].json_pointer == pointer, why


@pytest.mark.parametrize(
    "resource,pointer,why",
    [pytest.param(r, p, w, id=p) for r, p, w in REJECTED_POINTERS],
)
def test_rejected_write_pointer(tmp_path, resource, pointer, why):
    body = writes_block(resource, pointer)
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    assert pointer in str(excinfo.value), why


def test_control_character_in_a_pointer_segment_is_rejected(tmp_path):
    """A NUL or newline cannot be written into a TOML literal, so build it directly."""
    from EdgeWARN.ctam.manifest import validate_write_pointer

    with pytest.raises(ManifestError) as excinfo:
        validate_write_pointer(
            "stormcells.current",
            "/features/*/modules/CellStats\n",
            module_id="cellstats",
            name="CellStats",
        )
    assert "control character" in str(excinfo.value)


def test_traversal_segment_is_rejected_as_a_literal_key_not_as_a_path(tmp_path):
    """``..`` is a legal JSON Pointer key name, and that is exactly why it fails.

    ``/features/*/modules/CellStats/../id`` must be read as the four keys
    ``modules``, ``CellStats``, ``..``, ``id``. Under that reading it is accepted
    on ownership grounds -- it is a deep key inside the caller's own namespace.
    The dangerous reading is the filesystem one, where the pointer collapses to
    ``/id``. So the case the host must get right is the *sibling* form
    ``/features/*/modules/../id``, which names a key ``..`` under ``modules``
    that the caller does not own. It is rejected for not being the owner's key,
    not for containing dots. See ``plans/modular-ctam-phase0-findings.md``
    section 10.
    """
    from EdgeWARN.ctam.manifest import validate_write_pointer

    with pytest.raises(ManifestError) as excinfo:
        validate_write_pointer(
            "stormcells.current",
            "/features/*/modules/../id",
            module_id="cellstats",
            name="CellStats",
        )
    message = str(excinfo.value)
    assert "'..'" in message
    assert "literal key name, not a parent reference" in message
    assert "owns only 'CellStats'" in message

    # And the segments really are literal: no normalization happened anywhere.
    segments = validate_write_pointer(
        "stormcells.current",
        "/features/*/modules/CellStats/../id",
        module_id="cellstats",
        name="CellStats",
    )
    assert segments == ("modules", "CellStats", "..", "id")


def test_pointer_segment_is_unescaped_exactly_once(tmp_path):
    """``~01`` must decode to the literal ``~1``, never on to ``/``.

    Unescaping ``~0`` before ``~1``, or unescaping twice, would invent a
    separator the author never wrote and split one owned key into two.
    """
    from EdgeWARN.ctam.manifest import validate_write_pointer

    segments = validate_write_pointer(
        "stormcells.current",
        "/features/*/modules/CellStats/a~01b",
        module_id="cellstats",
        name="CellStats",
    )
    assert segments == ("modules", "CellStats", "a~1b")


def test_encoded_separator_cannot_smuggle_a_container_name(tmp_path):
    """``/features/*/modules~1CellStats`` is one key, and it is not a container.

    A string-prefix check on the raw pointer would see ``modules`` and admit it.
    """
    body = writes_block("stormcells.current", "/features/*/modules~1CellStats")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(write_module(tmp_path, "cellstats", body))
    # The reported container is the whole decoded key, which is the proof that
    # the segment was decoded rather than string-matched.
    assert "targets 'modules/CellStats'" in str(excinfo.value)


def test_public_routes_are_optional_and_strictly_parsed(tmp_path):
    manifest = parse_manifest(write_module(tmp_path, "cellstats", MINIMAL))
    assert manifest.public_routes == ()

    body = MINIMAL + '''\n[[public_routes]]\nid = "forecast-summary"\ndescription = "Latest forecast summary"\n'''
    manifest = parse_manifest(write_module(tmp_path, "cellstats", body))
    assert manifest.public_routes[0].route_id == "forecast-summary"
    assert manifest.public_routes[0].description == "Latest forecast summary"


@pytest.mark.parametrize("route_id", ["../secret", "a/b", "a\\\\b", "%2F", ".hidden"])
def test_public_route_id_rejects_non_safe_segments(tmp_path, route_id):
    body = MINIMAL + f'''\n[[public_routes]]\nid = "{route_id}"\ndescription = "Bad route"\n'''
    with pytest.raises(ManifestError, match="public_routes\\[0\\]\\.id"):
        parse_manifest(write_module(tmp_path, "cellstats", body))


def test_public_routes_reject_duplicates_controls_and_overflow(tmp_path):
    duplicate = MINIMAL + '''\n[[public_routes]]\nid = "same"\ndescription = "First"\n[[public_routes]]\nid = "same"\ndescription = "Second"\n'''
    with pytest.raises(ManifestError, match="duplicated"):
        parse_manifest(write_module(tmp_path, "cellstats", duplicate))

    routes = "".join(
        f'\n[[public_routes]]\nid = "route-{index}"\ndescription = "Route {index}"\n'
        for index in range(MAX_PUBLIC_ROUTES_PER_MODULE + 1)
    )
    with pytest.raises(ManifestError, match="at most"):
        parse_manifest(write_module(tmp_path, "cellstats", MINIMAL + routes))
