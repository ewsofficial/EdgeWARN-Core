from __future__ import annotations

from pathlib import Path

from EdgeWARN.ctam.manifest import ModuleManifest, ModuleRequirement, ModuleWrite, Selector
from EdgeWARN.ctam.readiness import CatalogFile, CTAMCycleCatalog, READY
from EdgeWARN.ctam.runner import ExternalModuleRunner


def _manifest(tmp_path: Path, module_id: str, program: str, *, timeout=1):
    folder = tmp_path / module_id; folder.mkdir(); (folder / "main.py").write_text(program)
    return ModuleManifest(
        module_id=module_id, name="Runner" + module_id, version="1.0.0", api_version="1",
        enabled=True, required=False, scope="stormcells", entrypoint=("{python}", "main.py"),
        timeout_seconds=timeout, after=(),
        requires=(ModuleRequirement(Selector("stormcells.current", "stormcells", None, None, "current"), True, None, None),),
        writes=(ModuleWrite("stormcells.current", "/features/*/modules/Runner" + module_id),),
        directory=folder, manifest_path=folder / "module.toml",
    )


def _catalog():
    return CTAMCycleCatalog("20260805-120000", "2026-08-05T12:00:00+00:00", False, 1, (CatalogFile("stormcells:current", "stormcells", None, None, "current", "", True, True, READY, None, None, "application/json", None),))


def test_runner_isolates_noncommitting_crashing_and_timed_out_modules(tmp_path):
    manifests = {
        "no_commit": _manifest(tmp_path, "no_commit", "print('hello')"),
        "crash": _manifest(tmp_path, "crash", "raise RuntimeError('boom')"),
        "hang": _manifest(tmp_path, "hang", "import time; time.sleep(5)", timeout=1),
    }
    results = ExternalModuleRunner(catalog=_catalog(), cells=[{"id": "7"}], manifests=manifests).run()
    assert [item.state for item in results] == ["failed", "failed", "timed_out"]
    assert results[0].reason == "module exited without committing"


def test_runner_exposes_predecessor_working_revision_to_committing_module(tmp_path):
    program = '''import json, os
from urllib.request import Request, urlopen
base=os.environ["CTAM_API_URL"]; headers={"Authorization":"Bearer "+os.environ["CTAM_API_TOKEN"],"X-CTAM-API-Version":"1","Content-Type":"application/json"}
cell=json.loads(urlopen(Request(base+"/stormcells/7",headers=headers)).read())["data"]
body=json.dumps({"revision":cell["revision"],"operations":[{"op":"add","path":"/modules/Runnerok","value":{"ran":True}}]}).encode()
urlopen(Request(base+"/stormcells/7",data=body,method="PATCH",headers=headers))
urlopen(Request(base+"/transaction/commit",data=b"{}",method="POST",headers=headers))
'''
    manifest = _manifest(tmp_path, "ok", program)
    runner = ExternalModuleRunner(catalog=_catalog(), cells=[{"id": "7"}], manifests={"ok": manifest})
    assert runner.run()[0].state == "completed"
    assert runner.transactions.cells["7"]["modules"]["Runnerok"] == {"ran": True}


def test_runner_drains_and_bounds_noisy_child_output(tmp_path):
    manifest = _manifest(tmp_path, "noisy", "print('x' * 200000)")
    result = ExternalModuleRunner(catalog=_catalog(), cells=[{"id": "7"}], manifests={"noisy": manifest}).run()[0]
    assert result.state == "failed"
    assert result.stdout.endswith("[output truncated]")
    assert len(result.stdout.encode()) <= 65_536 + len("\n[output truncated]")
