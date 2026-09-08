"""Measured regression budgets recorded in ``docs/ctam/performance-baseline.md``."""
from __future__ import annotations

import time
import tracemalloc
from unittest.mock import patch


def _cell():
    return {
        "id": 1,
        "timestamp": "2026-08-05T12:00:00+00:00",
        "centroid": [35.25, 262.75],
        "dx": 500.0,
        "dy": 250.0,
        "dt": 300.0,
        "properties": {"wind_field": {
            "u850": 12.0, "v850": 4.0,
            "u700": 14.0, "v700": 5.0,
            "u500": 18.0, "v500": 7.0,
            "u250": 22.0, "v250": 9.0,
        }},
        "modules": {},
    }


def test_stormcast_only_cycle_stays_within_phase7_latency_budget():
    from EdgeWARN.ctam.run import run_ctam

    started = time.monotonic()
    result = run_ctam([_cell()])
    elapsed = time.monotonic() - started

    assert result[0]["modules"]["StormCast"]["status"] in {"success", "skipped"}
    assert elapsed < 1.0


def test_disabled_ctam_starts_no_runner_or_loopback_server():
    from EdgeWARN.process.integrate.pipeline import _run_ctam_if_enabled

    cells = [_cell()]
    with patch("EdgeWARN.ctam.run.run_ctam") as run_ctam:
        assert _run_ctam_if_enabled(cells, "20260805-120000", True) is cells
    run_ctam.assert_not_called()


def test_one_external_sdk_module_stays_within_phase7_budget(tmp_path):
    """Exercise subprocess, loopback serialization, and one sealed patch."""
    from EdgeWARN.ctam.manifest import ModuleManifest, ModuleRequirement, ModuleWrite, Selector
    from EdgeWARN.ctam.readiness import CatalogFile, CTAMCycleCatalog, READY
    from EdgeWARN.ctam.runner import ExternalModuleRunner

    program = '''import json, os
from urllib.request import Request, urlopen
base=os.environ["CTAM_API_URL"]
headers={"Authorization":"Bearer "+os.environ["CTAM_API_TOKEN"],"X-CTAM-API-Version":"1","Content-Type":"application/json"}
cell=json.loads(urlopen(Request(base+"/stormcells/7",headers=headers)).read())["data"]
body=json.dumps({"revision":cell["revision"],"operations":[{"op":"add","path":"/modules/Bench","value":{"status":"ok"}}]}).encode()
urlopen(Request(base+"/stormcells/7",data=body,method="PATCH",headers=headers))
urlopen(Request(base+"/transaction/commit",data=b"{}",method="POST",headers=headers))
'''
    (tmp_path / "main.py").write_text(program)
    manifest = ModuleManifest(
        "bench", "Bench", "1.0.0", "1", True, False, "stormcells",
        ("{python}", "main.py"), 10, (),
        (ModuleRequirement(Selector("stormcells.current", "stormcells", None, None, "current"), True, None, None),),
        (ModuleWrite("stormcells.current", "/features/*/modules/Bench"),),
        tmp_path, tmp_path / "module.toml",
    )
    catalog = CTAMCycleCatalog(
        "20260805-120000", "2026-08-05T12:00:00+00:00", False, 1,
        (CatalogFile("stormcells:current", "stormcells", None, None, "current", "", True, True, READY, None, None, "application/json", None),),
    )
    tracemalloc.start()
    started = time.monotonic()
    runner = ExternalModuleRunner(catalog=catalog, cells=[{"id": "7"}], manifests={"bench": manifest})
    outcome = runner.run()[0]
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert outcome.state == "completed"
    assert runner.transactions.cells["7"]["modules"]["Bench"] == {"status": "ok"}
    assert time.monotonic() - started < 5.0
    assert peak < 128 * 1024 * 1024
