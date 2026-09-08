"""Phase 0 characterization of the filesystem, CLI, and environment surface.

The catalog snapshots cover *what* the pipeline processes. This file covers the
three surfaces that decide *where* it writes, *how* it is invoked, and *which
ambient state* can override a value -- the three that a config loader has to
subsume without changing anything.
"""

from __future__ import annotations

import ast
import functools
import inspect
import re
from pathlib import Path, PurePath

import pytest

from tests.architecture.baseline import assert_baseline
from tests.architecture.source_inspect import SRC, argparse_defaults

REPO_ROOT = SRC.parent

CLI_MODULES = [
    # Package command: top-level version, Phase 2 run, and Phase 3 configuration.
    "edgewarn_cli/main.py",
    "edgewarn_cli/run.py",
    "edgewarn_cli/configure.py",
    "edgewarn_cli/nws_zones.py",
    "util/cli.py",
    "util/io.py",
    "common/ingest/nexrad/main.py",
    "common/ingest/nexrad/pipeline/__init__.py",
    "common/ingest/nws/zone_sync.py",
    # Decomposition Phase 6: the optional all-services supervisor owns its
    # routing parser (no scientific imports).
    "run_all.py",
]


# --- Filesystem layout ----------------------------------------------------

def _path_attributes() -> dict[str, PurePath]:
    import util.file as fs

    return {
        name: value
        for name, value in vars(fs).items()
        if not name.startswith("_") and isinstance(value, PurePath)
    }


def test_derived_directory_names_baseline():
    """Every artifact directory, as a base-relative name.

    The migration proposes moving these into `paths.yaml`. Snapshotting the
    relative names rather than the absolute paths means the baseline holds on
    any machine, and a renamed directory shows up as a diff.
    """
    import util.file as fs

    base = Path(fs.BASE_DIR)
    relative = {}
    for name, value in sorted(_path_attributes().items()):
        try:
            relative[name] = Path(value).relative_to(base).as_posix()
        except ValueError:
            relative[name] = f"abs:{Path(value).name}"

    assert_baseline("filesystem_path_names", relative)


def test_path_attribute_count_and_uniqueness():
    """113 names, no two pointing at the same directory.

    Uniqueness is what makes the snapshot harness able to render a path back to
    its attribute name unambiguously; a duplicate would silently alias two
    catalog entries onto one baseline token.
    """
    attributes = _path_attributes()
    assert len(attributes) == 113

    values = [str(value) for value in attributes.values()]
    assert len(values) == len(set(values))


def test_base_dir_is_bound_at_import_time_from_the_platform():
    """DECISION OWED: `paths.yaml` cannot win against an import-time binding.

    `_define_paths()` runs at module scope, so all 113 paths exist before
    argparse or any config file is read. Phase 1 has to defer this.
    """
    import util.file as fs

    assert isinstance(fs.BASE_DIR, PurePath)

    source = (SRC / "util/file.py").read_text(encoding="utf-8")
    tail = source.split("def initialize_filesystem")[1]
    assert "_define_paths(" in tail


def test_cleanup_retention_defaults_resolve_from_the_catalog():
    """No cleaner restates the retention numbers; every signature defers.

    `max_files` uses a sentinel rather than `None` because `None` is already a
    value there -- it means "age only, no count cap", which the RAP pre-download
    sweep relies on. Collapsing the two would silently cap that directory.
    """
    import inspect

    import util.file as fs
    from util.file_config import cleanup_max_age_minutes, cleanup_max_files

    defaults = {}
    for name in (
        "clean_old_files",
        "async_clean_old_files",
        "clean_files_by_age",
        "async_clean_files_by_age",
    ):
        signature = inspect.signature(getattr(fs, name))
        defaults[name] = {
            key: parameter.default
            for key, parameter in signature.parameters.items()
            if parameter.default is not inspect.Parameter.empty
        }

    sentinel = fs._FROM_CATALOG
    assert sentinel is not None
    assert defaults == {
        "clean_old_files": {"max_age_minutes": None, "max_files": sentinel},
        "async_clean_old_files": {"max_age_minutes": None, "max_files": sentinel},
        "clean_files_by_age": {"max_age_minutes": None},
        "async_clean_files_by_age": {"max_age_minutes": None},
    }

    assert cleanup_max_age_minutes() == 60
    assert cleanup_max_files() == 10


def test_cleanup_skip_rules_are_hardcoded():
    """`.idx` is always spared and `.gz` is spared only if unzipped nearby."""
    source = (SRC / "util/file.py").read_text(encoding="utf-8")
    assert ".idx" in source
    assert ".gz" in source


def test_nexrad_manifest_staleness_comes_from_the_catalog():
    """RESOLVED (Phase 5): the retention window was a `writer.py` constant.

    The parameter had to stop defaulting to it, rather than defaulting to the
    accessor: a default expression is evaluated at import time, and this module
    is imported before `--config-dir` is resolved, so the signature would have
    frozen the repo-default value and no override could reach it.
    """
    from common.config import loader
    from common.ingest.nexrad import writer

    assert not hasattr(writer, "STALE_MANIFEST_MAX_AGE_HOURS")

    signature = inspect.signature(writer.prune_stale_site_manifests)
    assert signature.parameters["max_age_hours"].default is None

    catalog = loader.load_config("nexrad")["retention"]
    assert catalog["stale_manifest_max_age_hours"] == 12


# --- Process supervision timers --------------------------------------------

def test_supervisor_restart_policy_baseline():
    """`AccessorySupervisor`'s crash-loop and backoff timers.

    Named by the plan's "timers" category; verified during the plan's
    corrections-table audit but never previously pinned by a test.

    The timers now reach the dataclass from `runtime.yaml` through
    `default_factory`, so this reads them off a constructed instance. A snapshot
    of `field.default` would be blank for exactly the fields worth pinning.
    """
    import dataclasses

    from util.runtime.processes import AccessorySupervisor

    supervisor = AccessorySupervisor()
    defaults = {
        field.name: getattr(supervisor, field.name)
        for field in dataclasses.fields(AccessorySupervisor)
        if not field.name.startswith("_")
    }

    assert_baseline("supervisor_restart_policy", defaults)


def test_supervisor_stop_process_join_timeouts_come_from_runtime_yaml():
    """`stop_process` gives a process 5s to exit, then 1s after a kill signal.

    Both timeouts moved to `runtime.yaml`, so the parameter default is now `None`
    and the values are resolved inside the call. The numbers themselves must not
    have changed in the move.
    """
    from tests.architecture.source_inspect import param_default
    from util.runtime.config import section

    assert param_default("util/runtime/processes.py", "stop_process", "join_timeout") is None

    supervisor_settings = section("supervisor")
    assert supervisor_settings["stop_join_timeout_seconds"] == 5
    assert supervisor_settings["stop_kill_join_timeout_seconds"] == 1


# --- CLI defaults ---------------------------------------------------------

def test_cli_default_baseline():
    """Every `add_argument` default across all four argparse surfaces.

    Phase 3 makes CLI the top of the precedence chain, which only works if a
    flag can express "unset". A flag whose default is a real value cannot, so
    this snapshot is the list Phase 3 has to convert to `default=None`.
    """
    assert_baseline(
        "cli_defaults",
        {module: argparse_defaults(module) for module in CLI_MODULES},
    )


def test_cli_modules_are_enumerated():
    found = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "add_argument" in path.read_text(encoding="utf-8", errors="ignore")
    )
    assert found == sorted(CLI_MODULES)


@pytest.mark.parametrize("module", CLI_MODULES)
def test_most_flags_cannot_express_unset(module):
    """DECISION OWED: which of these defaults belong in YAML instead.

    A flag with a non-``None`` default always sends a value, so YAML can never
    take effect for it. Counting the two groups per module makes the Phase 3
    conversion measurable rather than a matter of opinion.
    """
    defaults = argparse_defaults(module)
    expressive = sorted(
        flag
        for flag, spec in defaults.items()
        if spec["default"] is None or spec["action"] == "store_true"
    )
    shadowing = sorted(set(defaults) - set(expressive))

    assert_baseline(
        f"cli_shadowing_{module.replace('/', '_').removesuffix('.py')}",
        {"shadows_yaml": shadowing, "can_be_unset": expressive},
    )


# --- Environment variables ------------------------------------------------

_ENV_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,}")


def _string_constants(tree: ast.AST) -> dict[str, str]:
    """Map identifier -> value for ``NAME = "literal"`` assignments."""
    return {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


@functools.lru_cache(maxsize=1)
def _src_string_constants() -> dict[str, str]:
    """Every ``NAME = "literal"`` under ``src``, for resolving imported names.

    Accessor modules define their variable names as constants and the reading
    site imports them, so a per-file table cannot resolve the identifier. Scoping
    this repo-wide is imprecise -- two files could define the same name -- but the
    table is only consulted for an identifier already in an ``os.environ`` or
    ``env_names`` position, and the alternative is an inventory that silently
    loses a variable every time one moves into an accessor.
    """
    constants: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        constants.update(_string_constants(ast.parse(source)))
    return constants


def _resolve_env_name(node: ast.AST, local: dict[str, str]) -> str | None:
    """The variable name a literal-or-identifier argument stands for."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value
    elif isinstance(node, ast.Name):
        value = local.get(node.id) or _src_string_constants().get(node.id)
    else:
        return None
    if value is None or not _ENV_PATTERN.fullmatch(value):
        return None
    return value


def _overlay_env_names(tree: ast.AST) -> set[str]:
    """Names passed as ``overlay.resolve(env_names=[...])``.

    A variable routed through the shared resolver never appears as an
    ``os.environ`` read, so the direct scan below cannot see it. Omitting this
    would let the inventory shrink every time a site is migrated to the shared
    parser, reporting variables as dropped while they are still honored.
    """
    local = _string_constants(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "env_names" or not isinstance(keyword.value, (ast.List, ast.Tuple)):
                continue
            for element in keyword.value.elts:
                resolved = _resolve_env_name(element, local)
                if resolved is not None:
                    names.add(resolved)
    return names


def _python_env_names() -> set[str]:
    """Collect every environment variable name read anywhere under ``src``.

    Names are taken from ``os.environ`` subscripts and ``get``/``getenv`` calls,
    including the indirect case where the name is held in a module constant such
    as ``RAP_MAX_AGE_ENV`` -- whether that constant is local to the reading file
    or imported from an accessor module -- plus the ``env_names`` allowlists
    handed to ``common.config.overlay.resolve``.
    """
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "env_names" in source:
            names.update(_overlay_env_names(ast.parse(source)))
        if "environ" not in source and "getenv" not in source:
            continue
        tree = ast.parse(source)
        local = _string_constants(tree)

        for node in ast.walk(tree):
            argument = None
            if isinstance(node, ast.Subscript) and "environ" in ast.dump(node.value):
                argument = node.slice
            elif isinstance(node, ast.Call) and node.args:
                function = node.func
                is_getenv = isinstance(function, ast.Attribute) and function.attr in {"get", "getenv"}
                if is_getenv and "environ" in ast.dump(function.value) or (
                    is_getenv and function.attr == "getenv"
                ):
                    argument = node.args[0]

            if argument is not None:
                resolved = _resolve_env_name(argument, local)
                if resolved is not None:
                    names.add(resolved)
    return names


def test_python_environment_variable_inventory_baseline():
    """DECISION OWED: which of these become documented `env:` aliases.

    The plan's precedence rule is CLI > env > YAML, which requires an explicit
    allowlist. This is that list as it exists today, including the three
    third-party GDAL/PROJ names that are *not* EdgeWARN configuration.
    """
    assert_baseline("environment_variables_python", sorted(_python_env_names()))


def test_environment_variables_are_read_without_a_shared_parser(monkeypatch):
    """How many sites still re-implement their own parse and clamp.

    Recording the two groups together means migrating a site to
    `common.config.overlay` shows up as a move between them rather than as an
    unexplained deletion.

    `synoptic/config.py` was the only ad-hoc reader that rejected a malformed
    value outright, so its migration is the one that could have quietly lost a
    property. It did not: the rejection moved into the overlay, where `minimum=`
    makes it available to every site instead of to one. The assertions below
    follow it there, because a resolve call that dropped the bound would still
    return the right number for well-formed input and would only be visible on
    the malformed input no snapshot covers.

    The overlay half is asserted by calling it, not by searching its source for
    "non-negative". The word appears there once, in the returned message, so a text
    search would in fact notice that line being deleted -- but it cannot tell the
    message being *produced* from the string merely being present, and it says
    nothing about whether `minimum=` still rejects anything. Calling `resolve`
    covers the property the docstring above actually claims.
    """
    synoptic = (SRC / "common/ingest/synoptic/config.py").read_text(encoding="utf-8")
    assert "RAP_MAX_AGE_ENV = \"EDGEWARN_RAP_MAX_AGE_MINUTES\"" in synoptic
    assert "minimum=0" in synoptic
    assert "must be a non-negative integer" not in synoptic, "bound left behind in the caller"

    from common.config.overlay import resolve

    monkeypatch.setenv("EDGEWARN_TEST_BOUND", "-1")
    with pytest.raises(ValueError, match=r"must be a non-negative integer, got '-1'"):
        resolve(None, env_names=("EDGEWARN_TEST_BOUND",), yaml_value=5, minimum=0)

def test_node_reads_only_edgewarn_base_dir():
    """The JS side shares exactly one configuration variable with Python."""
    names: set[str] = set()
    for path in (REPO_ROOT / "src").rglob("*.js"):
        if "node_modules" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        names.update(re.findall(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)", source))

    assert "EDGEWARN_BASE_DIR" in names
    assert names - {"NODE_ENV", "HOME"} == {"EDGEWARN_BASE_DIR"}


def test_base_dir_aliases_are_shared_by_python_and_node():
    """Both runtimes use the resolver's CLI > env > YAML contract."""
    source = (SRC / "common/config/overlay.py").read_text(encoding="utf-8")
    assert '"EDGEWARN_BASE_DIR", "BASE_DIR"' in source
