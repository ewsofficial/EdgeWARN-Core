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
    supplied_wheel = os.environ.get("EDGEWARN_TEST_WHEEL")
    if supplied_wheel:
        wheel = Path(supplied_wheel).resolve()
        assert wheel.is_file(), f"EDGEWARN_TEST_WHEEL does not exist: {wheel}"
    else:
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
    assert version_result.stdout.strip() == "edgewarn 3.0.2"
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


def test_installed_wheel_loads_and_runs_stormprob_models(installed_command):
    """The installed artifact must carry usable StormProb inference assets."""
    _root, python, _edgewarn = installed_command
    probe = _run(
        [
            python,
            "-c",
            (
                "import numpy as np; "
                "from EdgeWARN.stormprob import assets, onnx_runtime; "
                "directory = assets.validate_assets(); "
                "sessions = onnx_runtime.load_sessions(directory, assets.manifest_path()); "
                "outputs = onnx_runtime.infer_pair(*sessions, "
                "radial_history=np.zeros((128,30,64), np.float32), "
                "statistics_history=np.zeros((128,30,1), np.float32), "
                "current_features=np.zeros((128,135), np.float32), "
                "history_mask=np.zeros((128,30), np.bool_), "
                "history_sequence=np.zeros((128,30,135), np.float32), "
                "trajectory_sequence=np.zeros((128,30,16), np.float32), "
                "trajectory_mask=np.zeros((128,30), np.bool_)); "
                "assert outputs['coefficient_mean'].shape == (128,4,33); "
                "assert outputs['residual_motion_mps'].shape == (128,4,2); "
                "print(directory)"
            ),
        ],
        cwd=_root,
    )
    if "onnxruntime is not installed" in probe.stderr:
        pytest.skip("onnxruntime is not installed in this test environment")
    assert probe.returncode == 0, probe.stderr
    assert "models/stormprob" in probe.stdout


def test_installed_mrms_migration_uses_release_schemas(installed_command):
    root, python, edgewarn = installed_command
    config = root / "migration-v1-config"
    shutil.copytree(REPO_ROOT / "config", config)
    # Restore frozen pre-upgrade documents independently of the shipped catalog.
    from tests.unit.config.test_mrms_v2 import v1_documents
    for name, document in v1_documents().items():
        (config / f"{name}.yaml").write_text(yaml.safe_dump(document))
    # Simulate an old operator tree which has no v2 schema.
    (config / "schema/ingest.v2.schema.json").unlink()
    before = {str(p.relative_to(config)): p.read_bytes() for p in config.rglob('*') if p.is_file()}
    result = subprocess.run(
        [str(edgewarn), "migrate-mrms", "--config-path", str(config),
         "--base-dir", str(root / "migration-runtime")],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report['conflicts'] == []
    assert sum(p['action'] == 'rename' for p in report['paths']) == 10
    assert not (root / 'migration-runtime').exists()
    assert before == {str(p.relative_to(config)): p.read_bytes() for p in config.rglob('*') if p.is_file()}


def test_installed_mrms_apply_and_rollback_with_node(installed_command):
    if shutil.which('node') is None:
        pytest.skip('Node runtime unavailable')
    root, python, edgewarn = installed_command
    release_root = python.parent.parent / 'share/edgewarn'
    assert (release_root / 'scripts/validate-config.js').is_file()
    # Reuse installed npm dependencies without downloading or contacting a registry.
    dependencies = REPO_ROOT / 'node_modules'
    if not dependencies.is_dir():
        pytest.skip('Release npm dependencies unavailable')
    (release_root / 'node_modules').symlink_to(dependencies, target_is_directory=True)
    config = root / 'apply-v1-config'
    shutil.copytree(REPO_ROOT / 'config', config)
    from tests.unit.config.test_mrms_v2 import v1_documents
    for name, document in v1_documents().items():
        (config / f'{name}.yaml').write_text(yaml.safe_dump(document))
    before = (config / 'ingest.yaml').read_bytes()
    base = root / 'apply-runtime'
    source = base / 'data/MRMS_EchoTop18'
    source.mkdir(parents=True)
    (source / 'scan.grib2').write_bytes(b'fixture')
    for operation, expected in [('--apply', 'complete'), ('--rollback', 'rolled-back')]:
        result = subprocess.run([str(edgewarn), 'migrate-mrms', '--config-path', str(config),
                                 '--base-dir', str(base), operation], cwd=root,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)['status'] == expected
    assert (config / 'ingest.yaml').read_bytes() == before
    assert (source / 'scan.grib2').read_bytes() == b'fixture'


def test_wheel_includes_ingest_worker_and_mode(installed_command):
    root, python, edgewarn = installed_command
    result = _run([edgewarn, "run", "ingest", "--help"], cwd=root)
    assert result.returncode == 0, result.stderr
    probe = _run([python, "-c", "import importlib.util; assert importlib.util.find_spec('run_ingest') is not None"], cwd=root)
    assert probe.returncode == 0, probe.stderr
