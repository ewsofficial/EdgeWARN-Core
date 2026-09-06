"""Argument-resolution tests for the standalone NEXRAD service."""

from pathlib import Path

import run_nexrad


def test_parser_resolves_environment_base_directory(monkeypatch, tmp_path):
    configured = tmp_path / "runtime"
    monkeypatch.setenv("EDGEWARN_BASE_DIR", str(configured))

    args = run_nexrad._parse_args([])

    assert args.base_dir == str(configured.resolve())


def test_parser_cli_base_directory_wins_and_is_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EDGEWARN_BASE_DIR", str(tmp_path / "environment"))

    args = run_nexrad._parse_args(["--base-dir", "cli-runtime"])

    assert args.base_dir == str((tmp_path / "cli-runtime").resolve())


def test_parser_resolves_yaml_base_directory(monkeypatch):
    monkeypatch.delenv("EDGEWARN_BASE_DIR", raising=False)
    monkeypatch.delenv("BASE_DIR", raising=False)

    args = run_nexrad._parse_args([])

    assert args.base_dir == str((Path.home() / "EdgeWARN_input").resolve())
