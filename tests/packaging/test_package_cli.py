"""Phase 1 contracts for the installed ``edgewarn`` command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from argparse import Namespace
from pathlib import Path

import pytest

from edgewarn_cli import main as cli


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_python_and_node_package_versions_match():
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_json = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))

    assert metadata["project"]["name"] == "edgewarn-core"
    assert metadata["project"]["version"] == package_json["version"]
    assert metadata["project"]["scripts"]["edgewarn"] == "edgewarn_cli.main:main"


def test_main_without_arguments_prints_help(capsys):
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "run" in output
    assert "configure" in output
    assert "sync-nws-zones" in output


def test_version_uses_installed_distribution_metadata(monkeypatch, capsys):
    monkeypatch.setattr(cli, "version", lambda name: "9.8.7")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "edgewarn 9.8.7"


def test_command_package_import_has_no_runtime_side_effects(tmp_path):
    probe = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import edgewarn_cli\n"
        "loaded = set(sys.modules) - before\n"
        "blocked = sorted(name for name in loaded if "
        "name == 'EdgeWARN' or name.startswith('EdgeWARN.') or "
        "name == 'EWMRS' or name.startswith('EWMRS.') or "
        "name == 'NEXRAD' or name.startswith('NEXRAD.'))\n"
        "print(json.dumps(blocked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert list(tmp_path.iterdir()) == []


def test_configure_without_assignment_requires_a_tty(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["configure"])
    assert excinfo.value.code == 2

    assert (
        "interactive configuration requires TTY stdin and stdout"
        in capsys.readouterr().err
    )


def test_installed_nws_zone_sync_dispatches_apply(monkeypatch, tmp_path, capsys):
    from common.ingest.nws import zone_sync
    from edgewarn_cli import config_path

    calls = []

    class Syncer:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def sync(self, *, dry_run):
            calls.append(("sync", dry_run))
            return Namespace(to_dict=lambda: {"updated": 1})

    monkeypatch.setattr(config_path, "resolve_config_root", lambda _path: tmp_path)
    monkeypatch.setattr(zone_sync, "_resolve_zone_sync_args", lambda args: args)
    monkeypatch.setattr(zone_sync, "NWSZoneSync", Syncer)

    assert cli.main([
        "sync-nws-zones",
        "--apply",
        "--config-path", str(tmp_path),
        "--assets-dir", str(tmp_path / "zones"),
        "--zone-types", "forecast",
        "--timeout-seconds", "30",
        "--max-retries", "3",
        "--max-workers", "2",
        "--pause-seconds", "0.05",
        "--no-progress",
    ]) == 0

    assert calls[-1] == ("sync", False)
    assert json.loads(capsys.readouterr().out) == {"updated": 1}
