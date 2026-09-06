"""Configuration-root selection for installed package commands."""

from __future__ import annotations

from edgewarn_cli import config_path


def _root(path):
    path.mkdir()
    (path / "runtime.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return path


def test_explicit_root_wins_over_environment(tmp_path, monkeypatch):
    explicit = _root(tmp_path / "explicit")
    environment = _root(tmp_path / "environment")
    monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(environment))

    assert config_path.resolve_config_root(explicit) == explicit


def test_environment_root_wins_over_registered_default(tmp_path, monkeypatch):
    environment = _root(tmp_path / "environment")
    monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(environment))

    assert config_path.resolve_config_root() == environment


def test_registered_default_is_independent_of_working_directory(tmp_path, monkeypatch):
    registered = _root(tmp_path / "registered")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("EDGEWARN_CONFIG_DIR", raising=False)
    monkeypatch.setattr(config_path, "registered_config_root", lambda: registered)
    monkeypatch.chdir(elsewhere)

    assert config_path.resolve_config_root() == registered
