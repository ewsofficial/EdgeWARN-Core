"""Resolver precedence for the CTAM module install root.

``src/util/ctam_config.py`` centralizes the source and anchoring of the
directory installed CTAM module manifests are discovered from. It must obey the
same CLI > environment > YAML contract as every other setting -- and the
repo-root anchoring must be unconditional, because accessory children are
spawned without argv or a predictable working directory, so a relative value
has to name one fixed place.

``tests/architecture/test_surface_baseline.py`` scans ``src/`` for environment
variables, and ``EDGEWARN_CTAM_MODULE_DIR`` is picked up from the native
``env_names`` tuple in ``resolve_ctam_module_dir`` in that inventory. These
tests cover the resolver's actual precedence and anchoring behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from common.config import loader as config_loader
from common.config import overlay
from util.ctam_config import (
    CTAM_MODULE_DIR_ENV,
    export_ctam_module_dir,
    resolve_ctam_module_dir,
)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv(CTAM_MODULE_DIR_ENV, raising=False)
    overlay.reset_origins()
    config_loader.reset_cache()
    yield
    overlay.reset_origins()


def test_yaml_default_anchors_to_the_repo_root():
    """No CLI value, no environment: `run.ctam_module_dir` wins and resolves
    against the repository root rather than the working directory."""
    resolved = resolve_ctam_module_dir()

    assert resolved == config_loader.repo_root() / "ctam_modules"
    assert overlay.overrides() == {}


def test_relative_env_value_anchors_to_the_repo_root(monkeypatch):
    """An operator exporting a bare relative name must land in the same tree a
    child spawned with no argv would scan -- the repo root, not the CWD."""
    monkeypatch.setenv(CTAM_MODULE_DIR_ENV, "operator_modules")

    resolved = resolve_ctam_module_dir()

    assert resolved == config_loader.repo_root() / "operator_modules"
    assert overlay.overrides() == {"run.ctam_module_dir": f"env:{CTAM_MODULE_DIR_ENV}"}


def test_absolute_env_value_passes_through(monkeypatch):
    monkeypatch.setenv(CTAM_MODULE_DIR_ENV, "/srv/ctam/modules")

    assert resolve_ctam_module_dir() == Path("/srv/ctam/modules")


def test_cli_value_outranks_env(monkeypatch):
    """CLI is the top of the precedence chain, exactly as with every setting."""
    monkeypatch.setenv(CTAM_MODULE_DIR_ENV, "/env/path")

    resolved = resolve_ctam_module_dir("/cli/path")

    assert resolved == Path("/cli/path")
    assert overlay.overrides() == {"run.ctam_module_dir": "cli"}


def test_relative_cli_value_anchors_to_the_repo_root():
    resolved = resolve_ctam_module_dir("subdir/modules")

    assert resolved == config_loader.repo_root() / "subdir/modules"


def test_blank_env_value_counts_as_unset(monkeypatch):
    """Exporting an empty value clears a setting; it must not mask the YAML
    default (matching the shared resolver's blank-is-unset contract)."""
    monkeypatch.setenv(CTAM_MODULE_DIR_ENV, "")

    assert resolve_ctam_module_dir() == config_loader.repo_root() / "ctam_modules"
    assert overlay.overrides() == {}


def test_alternate_config_dir_supplies_the_yaml_value(tmp_path):
    """A copied config tree with its own `run.ctam_module_dir` is honoured, so
    a packaged deployment can point modules elsewhere without repo edits."""
    destination = tmp_path / "alt_config"
    import shutil

    shutil.copytree(config_loader.config_root(), destination)

    import yaml

    document = yaml.safe_load((destination / "runtime.yaml").read_text(encoding="utf-8"))
    document["run"]["ctam_module_dir"] = "/alt/site/modules"
    (destination / "runtime.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    resolved = resolve_ctam_module_dir(config_dir=str(destination))

    assert resolved == Path("/alt/site/modules")


def test_export_round_trip_via_monkeypatch_setenv(monkeypatch):
    """`export_ctam_module_dir` publishes the resolved root so accessory
    children, spawned with no argv, discover modules from the same tree."""
    monkeypatch.delenv(CTAM_MODULE_DIR_ENV, raising=False)
    target = Path("/exported/root")

    export_ctam_module_dir(target)

    assert CTAM_MODULE_DIR_ENV in os.environ
    assert resolve_ctam_module_dir() == target


def test_exported_path_is_what_env_resolution_reads(monkeypatch):
    """Repeated exports overwrite: the last published root wins, never the
    first, so a tight CLI loop cannot leave a stale root in the environment."""
    export_ctam_module_dir("/first/root")
    export_ctam_module_dir("/second/root")

    assert resolve_ctam_module_dir() == Path("/second/root")
