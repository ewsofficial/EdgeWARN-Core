"""Container smoke contract: disposable mounts and SIGTERM handling.

Phase 9 secondary coverage: the shipped ``Dockerfile`` defines the runtime
contract (pinned Conda base, ``VOLUME /var/lib/edgewarn``, ``STOPSIGNAL
SIGTERM``, ``ENTRYPOINT ["edgewarn"]``), but nothing pinned it, so a
Dockerfile edit that dropped the volume or changed the stop signal would
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
import subprocess
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
        assert 'VOLUME ["/var/lib/edgewarn"]' in text
        assert 'EDGEWARN_BASE_DIR="/var/lib/edgewarn"' in text

    def test_stop_signal_is_sigterm(self):
        assert re.search(r"^STOPSIGNAL SIGTERM", _dockerfile_text(), re.MULTILINE), (
            "supervisors send SIGTERM; the image must not override it"
        )

    def test_entrypoint_is_the_installed_command(self):
        assert re.search(r'^ENTRYPOINT \["edgewarn"\]', _dockerfile_text(), re.MULTILINE)

    def test_wheel_is_built_and_installed_without_dep_resolution(self):
        text = _dockerfile_text()
        assert "--no-deps --no-build-isolation" in text
        assert "--no-deps" in text

    def test_config_is_baked_in_but_state_is_not(self):
        text = _dockerfile_text()
        assert "cp -a config /etc/edgewarn/config" in text
        # No COPY of a runtime tree: state must arrive via the volume mount.
        assert not re.search(r"^COPY .*(data/|gui/|wpc/)", text, re.MULTILINE)


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
    subprocess.run(
        ["docker", "build", "-t", image, str(REPO_ROOT)],
        check=True,
        timeout=1800,
        capture_output=True,
    )
    try:
        proc = subprocess.Popen(
            ["docker", "run", "--rm", "-v", f"{mount}:/var/lib/edgewarn", image,
             "run", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            out, _ = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.terminate()
            out, _ = proc.communicate(timeout=60)
            pytest.fail(f"container did not exit after SIGTERM: {out!r}")
        assert proc.returncode == 0, out
        # The disposable mount must not leak image state back out.
        assert mount.is_dir()
    finally:
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True, timeout=300)
