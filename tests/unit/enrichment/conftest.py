"""Fixtures for proving integration config keys are live rather than inert.

Asserting the shipped value reaches the call site is not enough: a hardcoded
literal that happens to equal the YAML passes that check. These fixtures run the
integrator against a *different* value, so an inert key fails.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from common.config import loader as config_loader
from EdgeWARN.process.integrate import config as integrate_config

REPO_ROOT = Path(__file__).resolve().parents[3]


def _reset_caches():
    config_loader.reset_cache()
    integrate_config.reset_cache()


@pytest.fixture
def override_integration_config(tmp_path, monkeypatch):
    """Point the loader at a copy of ``config/`` with one key changed."""

    root = tmp_path / "config"
    source = REPO_ROOT / "config" / "integration.yaml"

    def _override(section_name, key, value):
        # Always patched from the pristine file, so a test may call this
        # repeatedly to compare one key's effect across several values.
        if not root.exists():
            shutil.copytree(REPO_ROOT / "config", root)

        document = yaml.safe_load(source.read_text(encoding="utf-8"))
        document[section_name][key] = value
        (root / "integration.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

        monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(root))
        _reset_caches()
        return root

    yield _override
    _reset_caches()
