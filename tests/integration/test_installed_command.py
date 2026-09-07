"""Exercise a built wheel outside the source checkout.

The fixture inherits the active Conda environment's runtime dependencies but
installs EdgeWARN itself only from the newly built wheel.  CI repeats this in
its own environment and runs the command from ``RUNNER_TEMP``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def installed_command(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-edgewarn")
    wheel_dir = root / "wheel"
    wheel_dir.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            ".",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheel = next(wheel_dir.glob("edgewarn_core-*.whl"))

    environment = root / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    edgewarn = environment / ("Scripts/edgewarn.exe" if os.name == "nt" else "bin/edgewarn")
    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    return root, python, edgewarn


def _run(command, *, cwd, env=None):
    clean_env = {**os.environ, "PYTHONPATH": ""}
    if env:
        clean_env.update(env)
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_wheel_help_version_and_import_are_independent_of_checkout(installed_command):
    root, python, edgewarn = installed_command

    help_result = _run([edgewarn, "--help"], cwd=root)
    version_result = _run([edgewarn, "--version"], cwd=root)
    probe = _run(
        [
            python,
            "-c",
            (
                "import json, pathlib, sys; import edgewarn_cli; "
                "print(json.dumps({'file': edgewarn_cli.__file__, "
                "'scientific': [name for name in sys.modules if "
                "name.split('.')[0] in {'EdgeWARN', 'EWMRS', 'NEXRAD'}]}))"
            ),
        ],
        cwd=root,
    )

    assert help_result.returncode == 0, help_result.stderr
    assert "run" in help_result.stdout and "configure" in help_result.stdout
    assert version_result.returncode == 0, version_result.stderr
    assert version_result.stdout.strip() == "edgewarn 3.0.0"
    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout)
    assert not Path(payload["file"]).resolve().is_relative_to(REPO_ROOT)
    assert payload["scientific"] == []


def test_installed_command_validates_and_edits_deployed_config(installed_command):
    root, _python, edgewarn = installed_command
    config = root / "deployed-config"
    shutil.copytree(REPO_ROOT / "config", config)

    result = _run(
        [
            edgewarn,
            "configure",
            "--config-path",
            config,
            "runtime.run.disable_nexrad",
            "true",
        ],
        cwd=root,
    )

    assert result.returncode == 0, result.stderr
    assert "validation: passed" in result.stdout
    document = yaml.safe_load((config / "runtime.yaml").read_text(encoding="utf-8"))
    assert document["run"]["disable_nexrad"] is True
