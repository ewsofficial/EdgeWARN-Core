"""Discovery must account for every candidate directory it looked at.

The registry this replaces (``src/EdgeWARN/ctam/registry.py``) keyed modules by
name in a plain dict, so a second module with the same name silently overwrote
the first: the shadowed module vanished with no error, no log line, and nothing
for an operator to notice. Discovery's contract is the opposite -- a directory
that claims to be a module by carrying a ``module.toml`` always comes back with a
recorded verdict and, when that verdict is not ``discovered``, an actionable
reason. Most of the tests below exist to pin *that*: not that the happy path
works, but that every unhappy path is still visible in the result.

The other half of the file pins order. ``after`` is a partial order, so a
comparison sort over it would leak directory iteration order into the run
sequence, and a capacity cap applied before ordering would make *which* module
gets dropped depend on what an operator named their folders. Both are asserted as
functions of the graph and the ids alone.

Module trees are built in ``tmp_path`` rather than checked in, because most cases
are one deliberately wrong key. ``root=`` is always passed explicitly: the
``root=None`` default resolves through ``util.ctam_config``, which is a separate
integration concern.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from EdgeWARN.ctam.discovery import (
    STATE_DISCOVERED,
    STATE_INVALID,
    STATE_SKIPPED_DISABLED,
    DiscoveredModule,
    DiscoveryResult,
    discover_modules,
)
from EdgeWARN.ctam.limits import MAX_EXTERNAL_MODULES

pytestmark = pytest.mark.ctam


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "ctam_modules"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def manifest_text(
    module_id: str,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    after: tuple[str, ...] | list[str] | None = None,
    extra: tuple[str, ...] = (),
) -> str:
    """A valid manifest for ``module_id``, with individual keys overridable.

    The display ``name`` defaults to the id because a write pointer under
    ``modules`` must name the display name exactly; keeping them equal means a
    test that renames a module does not also have to rewrite its pointer.
    """
    name = module_id if name is None else name
    lines = [
        "schema_version = 1",
        f'id = "{module_id}"',
        f'name = "{name}"',
        'version = "1.0.0"',
        'api_version = "1"',
        'entrypoint = ["{python}", "main.py"]',
    ]
    if enabled is not None:
        lines.append(f"enabled = {'true' if enabled else 'false'}")
    if after is not None:
        lines.append("after = [" + ", ".join(f'"{dep}"' for dep in after) + "]")
    lines.extend(extra)
    lines += [
        "",
        "[[writes]]",
        'resource = "stormcells.current"',
        f'json_pointer = "/features/*/modules/{name}"',
        "",
    ]
    return "\n".join(lines)


def install(
    root: Path,
    module_id: str,
    *,
    directory_name: str | None = None,
    body: str | None = None,
    **kwargs,
) -> Path:
    """Create ``<root>/<module_id>/`` holding a ``module.toml`` and a payload.

    ``directory_name`` exists only for the tests that deliberately mismatch the
    directory and the declared id; ``body`` replaces the manifest wholesale for
    the invalid-manifest cases.
    """
    directory = root / (directory_name or module_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "main.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    text = manifest_text(module_id, **kwargs) if body is None else body
    (directory / "module.toml").write_text(text, encoding="utf-8")
    return directory


def states(result: DiscoveryResult) -> dict[str, str]:
    return {module.module_id: module.state for module in result.modules}


def by_id(result: DiscoveryResult, module_id: str) -> DiscoveredModule:
    matches = [m for m in result.modules if m.module_id == module_id]
    assert matches, f"{module_id!r} not reported at all; states are {states(result)}"
    assert len(matches) == 1, f"{module_id!r} reported {len(matches)} times"
    return matches[0]


def ids(result: DiscoveryResult) -> list[str]:
    return [module.module_id for module in result.modules]


# --------------------------------------------------------------------------
# The acceptance criterion
# --------------------------------------------------------------------------


def test_dropping_a_valid_module_folder_into_the_root_discovers_it(tmp_path):
    """The Phase 1 acceptance criterion: installation is a folder copy.

    No base-code edit, no import list, no registry entry -- if this ever needs a
    source change to pass, modules are not actually pluggable.
    """
    install(tmp_path, "cellstats")

    result = discover_modules(root=tmp_path)

    assert result.root_present is True
    assert len(result.modules) == 1
    module = result.modules[0]
    assert module.module_id == "cellstats"
    assert module.state == STATE_DISCOVERED
    assert module.reason is None
    assert module.directory == tmp_path / "cellstats"
    assert module.manifest is not None
    assert result.runnable == (module,)


def test_the_fixture_root_discovers_the_valid_module_beside_the_broken_one():
    """One unparseable neighbour must not cost an operator their working module.

    ``tests/fixtures/ctam_modules/`` is the stand-in for a real installation, and
    it deliberately ships one good and one broken module for exactly this case.
    """
    result = discover_modules(root=FIXTURE_ROOT)

    assert states(result) == {
        "cellstats": STATE_DISCOVERED,
        "brokenmanifest": STATE_INVALID,
    }
    assert [m.module_id for m in result.runnable] == ["cellstats"]
    assert "version" in by_id(result, "brokenmanifest").reason


# --------------------------------------------------------------------------
# The root itself
# --------------------------------------------------------------------------


def test_a_missing_root_is_an_empty_module_set_not_a_startup_failure(tmp_path):
    """Installing no modules is a supported configuration, so it must not raise.

    Every deployment that has not opted into CTAM modules starts here, and the
    stage has to run with the built-in adapter alone.
    """
    result = discover_modules(root=tmp_path / "never-created")

    assert result.root_present is False
    assert result.modules == ()
    assert result.runnable == ()


def test_a_root_that_is_a_regular_file_raises(tmp_path):
    """A path typo that lands on a file would otherwise disable every module.

    "Empty" and "misconfigured" have to be distinguishable, so this is the one
    root problem that is loud.
    """
    path = tmp_path / "ctam_modules"
    path.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(NotADirectoryError) as excinfo:
        discover_modules(root=path)
    assert "must be a directory" in str(excinfo.value)


def test_an_empty_root_is_present_but_holds_nothing(tmp_path):
    """An operator who created the directory and installed nothing yet.

    ``root_present`` has to distinguish this from the missing-root case so the
    status record can say which one happened.
    """
    result = discover_modules(root=tmp_path)

    assert result.root_present is True
    assert result.modules == ()


# --------------------------------------------------------------------------
# What is skipped without comment
# --------------------------------------------------------------------------


def test_directories_without_a_manifest_are_silently_skipped(tmp_path):
    """Only a directory that claims to be a module is reported on.

    Operators keep virtualenvs, data folders, and notes next to their modules;
    reporting each of those as invalid would bury the real failures.
    """
    (tmp_path / "venv").mkdir()
    (tmp_path / "scratch-data").mkdir()
    install(tmp_path, "cellstats")

    result = discover_modules(root=tmp_path)

    assert ids(result) == ["cellstats"]


@pytest.mark.parametrize("name", (".hidden", ".git", "_staging", "__pycache__"))
def test_dot_and_underscore_prefixed_directories_are_skipped(tmp_path, name):
    """Reserved-looking names are tooling artifacts, not installations.

    A half-unpacked ``_staging`` copy of a module would otherwise collide
    case-insensitively with the real one and invalidate both.
    """
    install(tmp_path, "cellstats", directory_name=name)

    result = discover_modules(root=tmp_path)

    assert result.root_present is True
    assert result.modules == ()


def test_a_loose_file_beside_the_module_directories_is_skipped(tmp_path):
    """A README or a tarball in the module root is not a candidate.

    ``iterdir`` yields it, so the directory check has to be explicit or a stray
    download becomes an invalid module.
    """
    install(tmp_path, "cellstats")
    (tmp_path / "README.md").write_text("modules live here\n", encoding="utf-8")
    (tmp_path / "cellstats.tar.gz").write_bytes(b"\x1f\x8b")

    result = discover_modules(root=tmp_path)

    assert ids(result) == ["cellstats"]


# --------------------------------------------------------------------------
# Verdicts that must stay visible
# --------------------------------------------------------------------------


def test_an_invalid_manifest_is_reported_not_dropped(tmp_path):
    """The regression this whole module replaces: a module disappearing quietly.

    An operator who mistyped a key needs the parse error surfaced against the
    directory they edited, not a shorter module list they have to notice.
    """
    install(tmp_path, "broken", body='id = "broken"\nthis is not toml\n')

    result = discover_modules(root=tmp_path)

    module = by_id(result, "broken")
    assert module.state == STATE_INVALID
    assert module.manifest is None
    assert module.reason and "not valid TOML" in module.reason
    assert result.runnable == ()


def test_an_invalid_manifest_keeps_its_directory_name_as_the_reported_id(tmp_path):
    """With no parsed manifest there is no declared id, so the folder is the id.

    Without this the reason would have nothing to name and an operator could not
    tell which of several broken directories failed.
    """
    install(tmp_path, "unparseable", body="!!!\n")

    module = by_id(discover_modules(root=tmp_path), "unparseable")

    assert module.directory.name == "unparseable"
    assert module.state == STATE_INVALID


def test_a_semantically_invalid_manifest_reports_the_validation_message(tmp_path):
    """The manifest rule text is what discovery publishes, verbatim.

    A generic "invalid manifest" would force the operator to reproduce the parse
    by hand to learn which key was wrong.
    """
    install(tmp_path, "cellstats", extra=("timeout_seconds = 900",))

    module = by_id(discover_modules(root=tmp_path), "cellstats")

    assert module.state == STATE_INVALID
    assert "timeout_seconds" in module.reason


def test_a_disabled_module_keeps_its_manifest(tmp_path):
    """An operator listing modules wants the version of the thing they turned off.

    Discarding the manifest would make a disabled module indistinguishable from a
    broken one in the status record.
    """
    install(tmp_path, "cellstats", enabled=False)

    module = by_id(discover_modules(root=tmp_path), "cellstats")

    assert module.state == STATE_SKIPPED_DISABLED
    assert module.manifest is not None
    assert module.manifest.version == "1.0.0"
    assert module.reason and "enabled = false" in module.reason


def test_a_disabled_module_is_not_runnable(tmp_path):
    """``runnable`` is the launch list, so a disabled module must not appear in it."""
    install(tmp_path, "cellstats", enabled=False)
    install(tmp_path, "other")

    result = discover_modules(root=tmp_path)

    assert [m.module_id for m in result.runnable] == ["other"]


def test_case_insensitive_id_collision_invalidates_both_directories(tmp_path):
    """Naming both is fixable; picking a winner is what silently lost a module.

    Ids are compared case-insensitively downstream (output keys, status records),
    so ``cellstats`` and ``CellStats`` cannot coexist -- and neither of them gets
    to be the one that survives.
    """
    lower = tmp_path / "cellstats"
    lower.mkdir()
    upper = tmp_path / "CellStats"
    try:
        upper.mkdir()
    except FileExistsError:
        pytest.skip(
            "the filesystem under tmp_path is case-insensitive (Windows NTFS is by "
            "default), so 'cellstats' and 'CellStats' cannot exist side by side"
        )
    if len(list(tmp_path.iterdir())) != 2:
        pytest.skip(
            "the filesystem under tmp_path is case-insensitive (Windows NTFS is by "
            "default); the two module directories collapsed into one"
        )

    install(tmp_path, "cellstats")
    install(tmp_path, "CellStats", directory_name="CellStats")

    result = discover_modules(root=tmp_path)

    assert len(result.modules) == 2
    assert {m.state for m in result.modules} == {STATE_INVALID}
    assert result.runnable == ()
    reasons = {m.directory.name: m.reason for m in result.modules}
    assert "'CellStats'" in reasons["cellstats"]
    assert "'cellstats'" in reasons["CellStats"]


# --------------------------------------------------------------------------
# Dependency validation
# --------------------------------------------------------------------------


def test_after_naming_an_uninstalled_module_is_invalid(tmp_path):
    """Launching anyway would run the module without the input it declared.

    "Not installed" is an operator action -- install it or drop the line -- so it
    has to be named in the reason rather than inferred from an ordering failure.
    """
    install(tmp_path, "alpha", after=["missing"])

    module = by_id(discover_modules(root=tmp_path), "alpha")

    assert module.state == STATE_INVALID
    assert "'missing'" in module.reason
    assert "is installed in the module root" in module.reason


def test_after_naming_a_disabled_module_is_invalid_and_says_so(tmp_path):
    """Disabling one module must not silently degrade another that depends on it.

    The reason has to distinguish "disabled" from "missing", because the fix is
    different: flip a flag versus install a package.
    """
    install(tmp_path, "beta", enabled=False)
    install(tmp_path, "alpha", after=["beta"])

    result = discover_modules(root=tmp_path)

    alpha = by_id(result, "alpha")
    assert alpha.state == STATE_INVALID
    assert "disabled" in alpha.reason
    assert by_id(result, "beta").state == STATE_SKIPPED_DISABLED


def test_after_naming_an_invalid_module_is_invalid(tmp_path):
    """A dependent of a broken module cannot run, and should not look like it can.

    The reason points at the module to fix first so the operator does not chase
    the symptom.
    """
    install(tmp_path, "beta", body="not toml at all\n")
    install(tmp_path, "alpha", after=["beta"])

    result = discover_modules(root=tmp_path)

    alpha = by_id(result, "alpha")
    assert alpha.state == STATE_INVALID
    assert "'beta'" in alpha.reason
    assert "invalid" in alpha.reason


def test_invalidity_propagates_transitively(tmp_path):
    """A dependent two hops from the real fault must not be admitted.

    Without the fixed-point loop, ``alpha`` sees a ``beta`` that is still
    ``discovered`` on the first pass, looks runnable, and fails at launch time
    instead of at discovery.
    """
    install(tmp_path, "gamma_missing_dependency", after=["nowhere"])
    install(tmp_path, "beta", after=["gamma_missing_dependency"])
    install(tmp_path, "alpha", after=["beta"])

    result = discover_modules(root=tmp_path)

    assert states(result) == {
        "alpha": STATE_INVALID,
        "beta": STATE_INVALID,
        "gamma_missing_dependency": STATE_INVALID,
    }
    assert result.runnable == ()
    assert "'beta'" in by_id(result, "alpha").reason


def test_a_two_module_cycle_invalidates_both_and_names_the_cycle(tmp_path):
    """A cycle has no correct order, so no member may run and all must be named.

    Reporting only one member would send the operator to edit the module that is
    not necessarily the one with the wrong ``after`` line.
    """
    install(tmp_path, "alpha", after=["beta"])
    install(tmp_path, "beta", after=["alpha"])

    result = discover_modules(root=tmp_path)

    assert states(result) == {"alpha": STATE_INVALID, "beta": STATE_INVALID}
    for module in result.modules:
        assert "cycle" in module.reason
        assert "'alpha'" in module.reason and "'beta'" in module.reason


def test_a_three_module_cycle_invalidates_every_member(tmp_path):
    """Cycle detection must not be a two-node special case.

    A longer cycle is the realistic one -- nobody writes ``a after b, b after a``
    on purpose -- and it is the case a naive pairwise check misses.
    """
    install(tmp_path, "alpha", after=["gamma"])
    install(tmp_path, "beta", after=["alpha"])
    install(tmp_path, "gamma", after=["beta"])

    result = discover_modules(root=tmp_path)

    assert states(result) == {
        "alpha": STATE_INVALID,
        "beta": STATE_INVALID,
        "gamma": STATE_INVALID,
    }
    for module in result.modules:
        for member in ("'alpha'", "'beta'", "'gamma'"):
            assert member in module.reason


def test_depending_on_the_builtin_stormprob_is_legal_and_adds_no_member(tmp_path):
    """Every cell module that consumes forecasts declares ``after = ["stormprob"]``.

    ``stormprob`` is the built-in adapter, so it must satisfy the dependency
    without appearing in the result as an installed module that discovery could
    then report on, order, or count against capacity.
    """
    install(tmp_path, "cellstats", after=["stormprob"])

    result = discover_modules(root=tmp_path)

    assert ids(result) == ["cellstats"]
    assert by_id(result, "cellstats").state == STATE_DISCOVERED
    assert "stormprob" not in ids(result)


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_a_dependency_chain_runs_in_dependency_order(tmp_path):
    """``after`` is the only ordering control a module author has.

    Alphabetically the chain here is exactly backwards, so an id-only sort would
    run each module before the one it declared it must follow.
    """
    install(tmp_path, "aaa", after=["bbb"])
    install(tmp_path, "bbb", after=["ccc"])
    install(tmp_path, "ccc")

    result = discover_modules(root=tmp_path)

    assert ids(result) == ["ccc", "bbb", "aaa"]
    assert [m.state for m in result.modules] == [STATE_DISCOVERED] * 3


def test_independent_modules_come_out_in_ascending_id_order(tmp_path):
    """With no ``after`` edges the id is the only stable tiebreak available.

    Falling back on directory iteration order would make the run sequence, and
    therefore any ordering-sensitive bug, depend on the filesystem.
    """
    for module_id in ("gamma", "alpha", "delta", "beta"):
        install(tmp_path, module_id)

    assert ids(discover_modules(root=tmp_path)) == ["alpha", "beta", "delta", "gamma"]


def test_order_is_a_function_of_the_graph_and_the_ids_alone(tmp_path):
    """Two identical installations built in opposite orders must run identically.

    ``after`` is a partial order, so a comparison sort over it returns whatever
    the input sequence suggested. Pinning the two roots against each other is
    what catches that, because either order looks reasonable in isolation.
    """
    graph = {
        "delta": ["alpha"],
        "alpha": [],
        "charlie": ["alpha"],
        "bravo": ["delta"],
        "echo": [],
    }
    forwards = tmp_path / "forwards"
    backwards = tmp_path / "backwards"
    forwards.mkdir()
    backwards.mkdir()
    for module_id in graph:
        install(forwards, module_id, after=graph[module_id])
    for module_id in reversed(list(graph)):
        install(backwards, module_id, after=graph[module_id])

    first = ids(discover_modules(root=forwards))
    second = ids(discover_modules(root=backwards))

    assert first == second
    assert first == ["alpha", "charlie", "delta", "bravo", "echo"]


# --------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------


def test_modules_past_the_cap_are_invalid_and_still_reported(tmp_path):
    """The cap bounds discovery and status work; it must not hide the overflow.

    An operator who installs one module too many needs to be told which one was
    refused, so the cap is applied by demoting the tail rather than truncating
    the list.
    """
    module_ids = [f"mod{index:02d}" for index in range(1, MAX_EXTERNAL_MODULES + 2)]
    for module_id in module_ids:
        install(tmp_path, module_id)

    result = discover_modules(root=tmp_path)

    assert ids(result) == module_ids
    assert [m.state for m in result.modules] == (
        [STATE_DISCOVERED] * MAX_EXTERNAL_MODULES + [STATE_INVALID]
    )
    overflow = result.modules[-1]
    assert str(MAX_EXTERNAL_MODULES) in overflow.reason
    assert "external modules" in overflow.reason
    assert len(result.runnable) == MAX_EXTERNAL_MODULES


def test_capacity_is_applied_after_ordering_not_in_directory_order(tmp_path):
    """Which module is dropped is a property of the graph, not of folder names.

    ``zzz`` sorts last but every other module depends on it, so the dependency
    order puts it near the front and it is admitted; the module demoted is one
    that sorts *earlier*. Applying the cap over the scan order instead would drop
    ``zzz`` and leave its dependents pointing at a refused module.
    """
    dependents = [f"m{index}" for index in range(1, MAX_EXTERNAL_MODULES)]
    install(tmp_path, "zzz")
    install(tmp_path, "nnn")
    for module_id in dependents:
        install(tmp_path, module_id, after=["zzz"])

    result = discover_modules(root=tmp_path)

    assert ids(result) == ["nnn", "zzz"] + dependents
    assert by_id(result, "zzz").state == STATE_DISCOVERED
    demoted = dependents[-1]
    assert demoted < "zzz"
    assert by_id(result, demoted).state == STATE_INVALID
    assert str(MAX_EXTERNAL_MODULES) in by_id(result, demoted).reason
    assert len(result.runnable) == MAX_EXTERNAL_MODULES


def test_invalid_and_disabled_modules_do_not_consume_capacity(tmp_path):
    """A module that will never launch must not push a working one over the cap.

    Otherwise disabling a module would reduce, not increase, the number of
    modules that actually run.
    """
    runnable_ids = [f"mod{index:02d}" for index in range(1, MAX_EXTERNAL_MODULES + 1)]
    for module_id in runnable_ids:
        install(tmp_path, module_id)
    install(tmp_path, "aa-disabled", enabled=False)
    install(tmp_path, "ab-broken", body="[[[not toml\n")

    result = discover_modules(root=tmp_path)

    assert [m.module_id for m in result.runnable] == runnable_ids
    assert by_id(result, "aa-disabled").state == STATE_SKIPPED_DISABLED
    assert by_id(result, "ab-broken").state == STATE_INVALID


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------


def test_a_symlinked_module_directory_pointing_outside_the_root_is_invalid(tmp_path):
    """The module root is the trust boundary an operator actually administers.

    Following a symlink out of it would let anything writable elsewhere on the
    box install a module, so the escape is recorded rather than traversed.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "escaped"
    outside.mkdir(parents=True)
    (outside / "main.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    (outside / "module.toml").write_text(manifest_text("escaped"), encoding="utf-8")
    try:
        os.symlink(outside, root / "escaped", target_is_directory=True)
    except OSError as exc:
        pytest.skip(
            f"creating a directory symlink requires Developer Mode or "
            f"administrator privilege on this platform: {exc}"
        )

    result = discover_modules(root=root)

    module = by_id(result, "escaped")
    assert module.state == STATE_INVALID
    assert module.manifest is None
    assert "outside the module root" in module.reason
    assert result.runnable == ()


# --------------------------------------------------------------------------
# Result shape
# --------------------------------------------------------------------------


def test_discovered_module_is_frozen(tmp_path):
    """A verdict is a record of what discovery decided, not mutable state.

    Later phases attach runtime state elsewhere; letting them retouch ``state``
    here would make the status record disagree with the discovery decision.
    """
    install(tmp_path, "cellstats")
    module = discover_modules(root=tmp_path).modules[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        module.state = STATE_INVALID


def test_discovery_result_is_frozen(tmp_path):
    """The scan is a snapshot; a caller must re-scan rather than edit the result."""
    install(tmp_path, "cellstats")
    result = discover_modules(root=tmp_path)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.modules = ()


def test_runnable_is_a_filtered_view_in_the_same_order(tmp_path):
    """Callers launch from ``runnable``, so it has to preserve dependency order."""
    install(tmp_path, "aaa", after=["bbb"])
    install(tmp_path, "bbb")
    install(tmp_path, "ccc", enabled=False)

    result = discover_modules(root=tmp_path)

    assert [m.module_id for m in result.runnable] == ["bbb", "aaa"]
    assert all(m.state == STATE_DISCOVERED for m in result.runnable)
