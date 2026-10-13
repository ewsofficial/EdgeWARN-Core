"""Entrypoint import-safety contract (decomposition plan, test plan).

No entrypoint may parse arguments, initialize runtime paths, wrap the standard
streams, or spawn processes as an import side effect. Module scope of an
entry point runs before ``main()``, and re-executes in every spawned child, so
the split into per-service modules must not carry the old monolith's
module-level behavior forward.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import sys
sys.path.insert(0, {src!r})
import run

wrapped = (
    type(sys.stdout).__name__ == "TimestampedOutput"
    or type(sys.stderr).__name__ == "TimestampedOutput"
)
if wrapped:
    raise SystemExit("FAIL: run.py wrapped sys.stdout/sys.stderr at import time")
print("IMPORT_SAFE")
"""


def test_importing_run_py_has_no_side_effects(tmp_path):
    env = {
        **os.environ,
        # If a regression reintroduces import-time runtime initialization, keep
        # its writes inside the test's temporary base directory.
        "EDGEWARN_BASE_DIR": str(tmp_path / "accidental_base"),
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(src=str(REPO_ROOT / "src"))],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, (
        f"run.py import raised or reported side effects:\n{result.stderr}"
    )
    # Exactly one marker line: argument parsing or runtime initialization at
    # module scope would emit scheduler output (or fail validation) instead.
    assert result.stdout.strip() == "IMPORT_SAFE"


def test_run_py_module_scope_is_free_of_runtime_calls():
    source = (REPO_ROOT / "src" / "run.py").read_text(encoding="utf-8")
    module_body = source.split("\ndef ", 1)[0]
    for banned in (
        "io_manager.get_args(",
        "initialize_runtime(",
        "multiprocessing.Event()",
        "multiprocessing.Manager()",
        "sys.stdout =",
        "sys.stderr =",
    ):
        assert banned not in module_body, (
            f"run.py module scope still performs runtime work: {banned!r}"
        )


def test_run_py_refuses_the_silent_partial_topology():
    result = subprocess.run(
        [sys.executable, "src/run.py"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 2
    assert "retired" in result.stderr
    assert "python src/run_all.py" in result.stderr
    assert "python src/run_nexrad.py" in result.stderr
