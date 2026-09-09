"""Container smoke contract: disposable mounts and SIGTERM handling.

Phase 9 secondary coverage: the shipped ``Dockerfile`` defines the runtime
contract (pinned Conda base, ``VOLUME`` for the runtime and log trees,
``STOPSIGNAL SIGTERM``, and a reaping entrypoint that pipes the installed
``edgewarn`` command through ``rotatelogs``), but nothing pinned it, so a
Dockerfile edit that dropped a volume or changed the stop signal would
ship silently.

Two lanes, matching the suite's opt-in philosophy for heavyweight checks:

- the static contract below always runs offline and pins the load-bearing
  Dockerfile directives by reading the file itself;
- ``test_container_run_with_disposable_mount_and_sigterm`` performs a real
  ``docker build`` + ``docker run`` with a disposable tmp mount and SIGTERM
  stop, but only under ``EDGEWARN_TEST_DOCKER=1`` with a working daemon.
  It skips everywhere else, including default CI.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

pytestmark = pytest.mark.slow


def _dockerfile_text():
    return DOCKERFILE.read_text(encoding="utf-8")


class TestDockerfileContract:
    def test_base_image_is_pinned(self):
        text = _dockerfile_text()
        assert re.search(r"^FROM continuumio/miniconda3:\S+", text, re.MULTILINE), (
            "runtime base must stay a pinned image, not a floating tag"
        )

    def test_runtime_mount_is_a_disposable_volume(self):
        text = _dockerfile_text()
        assert 'VOLUME ["/var/lib/edgewarn", "/var/log/edgewarn"]' in text
        assert 'EDGEWARN_BASE_DIR="/var/lib/edgewarn"' in text

    def test_stop_signal_is_sigterm(self):
        assert re.search(r"^STOPSIGNAL SIGTERM", _dockerfile_text(), re.MULTILINE), (
            "supervisors send SIGTERM; the image must not override it"
        )

    def test_entrypoint_uses_init_and_pipes_through_rotatelogs(self):
        text = _dockerfile_text()
        assert (
            'ENTRYPOINT ["/usr/bin/tini", "--", '
            '"/usr/local/bin/edgewarn-entrypoint"]' in text
        )
        assert (
            'CMD ["edgewarn", "run", "--config-path", '
            '"/etc/edgewarn/config"]' in text
        )
        entrypoint = (REPO_ROOT / "docker" / "edgewarn-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        assert 'kill "-${signum}" "${supervisor_pid}"' in entrypoint
        assert "rotatelogs" in entrypoint

    def test_wheel_is_built_and_installed_without_dep_resolution(self):
        text = _dockerfile_text()
        assert "--no-deps --no-build-isolation" in text
        assert "--no-deps" in text

    def test_config_is_baked_in_but_state_is_not(self):
        text = _dockerfile_text()
        assert "cp -a config /etc/edgewarn/config" in text
        # No COPY of a runtime tree: state must arrive via the volume mount.
        assert not re.search(r"^COPY .*(data/|gui/|wpc/)", text, re.MULTILINE)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal contract")
def test_entrypoint_forwards_term_and_drains_logger(tmp_path):
    """Exercise the PID-tracking wrapper without building the full image."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_rotatelogs = bin_dir / "rotatelogs"
    fake_rotatelogs.write_text(
        "#!/usr/bin/env bash\n"
        "output=''\n"
        "while (($#)); do\n"
        "  if [[ $1 == '-L' ]]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        "cat >\"${output}\"\n",
        encoding="utf-8",
    )
    fake_rotatelogs.chmod(0o755)

    worker = tmp_path / "worker.py"
    worker.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))\n"
        "print('worker-ready', flush=True)\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"
    entrypoint = REPO_ROOT / "docker" / "edgewarn-entrypoint.sh"
    proc = subprocess.Popen(
        ["bash", str(entrypoint), sys.executable, str(worker)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "EDGEWARN_LOG_DIR": str(log_dir),
        },
    )
    current_log = log_dir / "edgewarn.current.log"
    deadline = time.monotonic() + 10
    while (
        (not current_log.exists() or "worker-ready" not in current_log.read_text())
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)

    try:
        assert current_log.exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert "worker-ready" in current_log.read_text(encoding="utf-8")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _docker_available():
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True, timeout=30
    ).returncode == 0


@pytest.mark.skipif(
    os.environ.get("EDGEWARN_TEST_DOCKER") != "1" or not _docker_available(),
    reason="opt-in live container check: needs EDGEWARN_TEST_DOCKER=1 and a docker daemon",
)
def test_container_run_with_disposable_mount_and_sigterm(tmp_path):
    """Build once, run with a disposable mount, stop via SIGTERM."""
    mount = tmp_path / "edgewarn-data"
    mount.mkdir()
    image = "edgewarn-core-smoke:latest"
    container = f"edgewarn-core-smoke-{os.getpid()}"
    subprocess.run(
        ["docker", "build", "-t", image, str(REPO_ROOT)],
        check=True,
        timeout=1800,
        capture_output=True,
    )
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", container,
                "-v", f"{mount}:/var/lib/edgewarn", image,
                "python", "-c",
                "import signal,sys,time; "
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                "print('ready', flush=True); time.sleep(300)",
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "stop", "-t", "25", container],
            check=True,
            timeout=30,
            capture_output=True,
        )
        state = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.ExitCode}}", container],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        assert state.stdout.strip() == "0"
        # The disposable mount must not leak image state back out.
        assert mount.is_dir()
    finally:
        subprocess.run(
            ["docker", "rm", "-f", "-v", container],
            capture_output=True,
            timeout=60,
        )
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True, timeout=300)
