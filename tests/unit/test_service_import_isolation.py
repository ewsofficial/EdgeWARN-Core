"""Import-isolation contracts between decomposed services (decomposition Phase 1).

"Verify imports of each service do not load the other scientific stacks":
importing primary runtime code must not pull the
NEXRAD ingest pipeline, and shared utility modules must stay free of every
scientific stack. The probes run in subprocesses so ``sys.modules`` starts
clean.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def probe(statement):
    result = subprocess.run(
        [sys.executable, "-c", f"import sys\nsys.path.insert(0, 'src')\n{statement}\n"
         "print('NEXRAD:', 'common.ingest.nexrad' in sys.modules)\n"
         "print('EDGEWARN:', 'EdgeWARN' in sys.modules)"],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
    flags = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(": ")
        flags[key] = value == "True"
    return flags


def test_util_io_imports_no_scientific_stack():
    flags = probe("import util.io")
    assert not flags["NEXRAD"]
    assert not flags["EDGEWARN"]


def test_bare_runtime_package_imports_no_scientific_stack():
    """Lazy re-exports mean importing the package loads nothing heavy."""
    flags = probe("import util.runtime")
    assert not flags["NEXRAD"]
    assert not flags["EDGEWARN"]


def test_cycle_module_no_longer_imports_nexrad():
    """The primary path keeps its own stack only; NEXRAD stay out."""
    flags = probe("import util.runtime.cycle")
    assert not flags["NEXRAD"]
    # The detection worker is genuinely part of the primary cycle today.
    assert flags["EDGEWARN"]


def test_primary_service_module_avoids_nexrad():
    flags = probe("import util.runtime.primary_service")
    assert not flags["NEXRAD"]
    assert flags["EDGEWARN"]


def test_primary_cycle_module_avoids_nexrad_after_phase_4_cut():
    """Phase 5: the primary cycle imports neither NEXRAD at all."""
    flags = probe("import util.runtime.cycle")
    assert not flags["NEXRAD"]


def test_run_edgewarn_entrypoint_loads_no_nexrad_stack():
    flags = probe("import run_edgewarn")
    assert not flags["NEXRAD"]
    assert flags["EDGEWARN"]


def test_lazy_reexports_still_resolve():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys\nsys.path.insert(0, 'src')\n"
         "from util.runtime import CycleStateStore, drain_log_queue, stop_process\n"
         "from util.runtime.services import heartbeat_path\n"
         "print('RESOLVED')"],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "RESOLVED" in result.stdout
