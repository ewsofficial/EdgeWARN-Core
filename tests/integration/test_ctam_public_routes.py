"""Phase 7 integration coverage for CTAM public route publication.

Proves the plan's publication boundary: route files and registry appear only
with the storm snapshot/index commit, recover after an interrupted replace,
retain last-known-good data after a later module failure, and stop exposing a
removed declaration.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import util.file as fs
from EdgeWARN.ctam.manifest import ModuleManifest, ModuleWrite, PublicRoute
from EdgeWARN.ctam.publication import CTAMPublicationCoordinator
from EdgeWARN.ctam.run import CTAMRunResult
from EdgeWARN.process.integrate import pipeline as integrate_pipeline

pytestmark = pytest.mark.integration


def _manifest(module_id="cellstats", routes=(PublicRoute("summary", "Latest summary"),), version="1.0.0"):
    return ModuleManifest(
        module_id=module_id, name="CellStats", version=version, api_version="1",
        enabled=True, required=False, scope="stormcells",
        entrypoint=("{python}", "main.py"), timeout_seconds=10, after=(),
        requires=(), writes=(ModuleWrite("stormcells.current", "/features/*/modules/CellStats"),),
        directory=Path("/tmp"), manifest_path=Path("/tmp/module.toml"),
        public_routes=routes,
    )


def _result(manifests, routes, cycle_id="20260915-120000"):
    return CTAMRunResult(cells=[], manifests=tuple(manifests), module_results=(), committed_routes=routes, cycle_id=cycle_id)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "base" / "data"
    root.mkdir(parents=True)
    monkeypatch.setattr(fs, "DATA_DIR", root)
    return root


def test_public_payloads_build_registry_and_wrapped_artifacts(data_root):
    result = _result([_manifest()], {"cellstats": {"summary": {"risk": "elevated"}}})
    payloads = integrate_pipeline._public_route_payloads("2026-09-15T12:00:00+00:00", result)
    public_root = data_root / "ctam" / "public"
    registry = payloads[public_root / "registry.json"]
    assert registry["schema_version"] == 1
    assert registry["modules"][0]["id"] == "cellstats"
    assert registry["modules"][0]["href"] == "/api/v3/modules/cellstats"
    route = registry["modules"][0]["routes"][0]
    assert route == {"id": "summary", "description": "Latest summary",
                     "href": "/api/v3/modules/cellstats/summary", "available": True}
    wrapper = payloads[public_root / "modules" / "cellstats" / "summary.json"]
    assert wrapper["module_id"] == "cellstats"
    assert wrapper["module_version"] == "1.0.0"
    assert wrapper["route_id"] == "summary"
    assert wrapper["cycle_id"] == "20260915-120000"
    assert wrapper["data"] == {"risk": "elevated"}
    assert wrapper["published_at"].endswith("Z")


def test_unavailable_route_is_declared_but_has_no_artifact(data_root):
    result = _result([_manifest()], {})
    payloads = integrate_pipeline._public_route_payloads("2026-09-15T12:00:00+00:00", result)
    public_root = data_root / "ctam" / "public"
    assert payloads[public_root / "registry.json"]["modules"][0]["routes"][0]["available"] is False
    assert public_root / "modules" / "cellstats" / "summary.json" not in payloads


def test_last_known_good_survives_later_module_failure(data_root):
    public_root = data_root / "ctam" / "public"
    prior_module_dir = public_root / "modules" / "cellstats"
    prior_module_dir.mkdir(parents=True)
    (prior_module_dir / "summary.json").write_text(json.dumps({
        "schema_version": 1, "module_id": "cellstats", "module_version": "1.0.0",
        "route_id": "summary", "cycle_id": "20260915-110000",
        "published_at": "2026-09-15T11:00:05Z", "data": {"risk": "elevated"},
    }))
    (public_root / "registry.json").write_text(json.dumps({
        "schema_version": 1, "modules": [{
            "id": "cellstats", "name": "CellStats", "version": "1.0.0",
            "href": "/api/v3/modules/cellstats",
            "routes": [{"id": "summary", "description": "Latest summary",
                        "href": "/api/v3/modules/cellstats/summary", "available": True}],
        }],
    }))
    # Later cycle commits nothing for the route (module failed/skipped).
    result = _result([_manifest()], {}, cycle_id="20260915-120000")
    payloads = integrate_pipeline._public_route_payloads("2026-09-15T12:00:00+00:00", result)
    assert payloads[public_root / "registry.json"]["modules"][0]["routes"][0]["available"] is True
    # No replacement artifact is staged; the prior file stays on disk until publish.
    assert public_root / "modules" / "cellstats" / "summary.json" not in payloads


def test_removed_declaration_is_no_longer_addressable(data_root):
    public_root = data_root / "ctam" / "public"
    stale_dir = public_root / "modules" / "cellstats"
    stale_dir.mkdir(parents=True)
    (stale_dir / "old.json").write_text(json.dumps({"stale": True}))
    (public_root / "registry.json").write_text(json.dumps({
        "schema_version": 1, "modules": [{
            "id": "cellstats", "name": "CellStats", "version": "1.0.0",
            "href": "/api/v3/modules/cellstats",
            "routes": [{"id": "old", "description": "Old", "href": "/api/v3/modules/cellstats/old", "available": True}],
        }],
    }))
    manifest = _manifest(routes=(PublicRoute("summary", "Latest summary"),))
    result = _result([manifest], {"cellstats": {"summary": {"v": 1}}})
    payloads = integrate_pipeline._public_route_payloads("2026-09-15T12:00:00+00:00", result)
    registry = payloads[public_root / "registry.json"]
    assert [r["id"] for r in registry["modules"][0]["routes"]] == ["summary"]
    assert public_root / "modules" / "cellstats" / "old.json" not in payloads


def test_routes_publish_atomically_with_snapshot_and_recover(tmp_path, data_root, monkeypatch):
    # Route payloads join the same coordinator payload map as the snapshot, so a
    # crash mid-replace rolls every prepared target forward together.
    journal_dir = tmp_path / "journals"
    coordinator = CTAMPublicationCoordinator(journal_dir)
    snapshot_path = data_root / "stormcells" / "stormcells_20260915-120000.json"
    result = _result([_manifest()], {"cellstats": {"summary": {"risk": "elevated"}}})
    route_payloads = integrate_pipeline._public_route_payloads("2026-09-15T12:00:00+00:00", result)
    payloads = {snapshot_path: {"features": []}, **route_payloads}

    calls = {"replaced": 0}
    real_replace = coordinator.replace

    def flaky_replace(src, dst):
        calls["replaced"] += 1
        if calls["replaced"] == 2:
            raise OSError("simulated crash mid publication")
        return real_replace(src, dst)

    coordinator.replace = flaky_replace
    with pytest.raises(OSError):
        coordinator.publish(payloads, transaction_id="cycle-1")
    # Nothing is half-visible under a real name check: recovery rolls forward.
    recovered = CTAMPublicationCoordinator(journal_dir).recover()
    assert len(recovered) == 1
    assert json.loads(snapshot_path.read_text()) == {"features": []}
    public_root = data_root / "ctam" / "public"
    assert json.loads((public_root / "registry.json").read_text())["schema_version"] == 1
    assert json.loads((public_root / "modules" / "cellstats" / "summary.json").read_text())["data"] == {"risk": "elevated"}


def test_no_committed_routes_still_writes_empty_registry(data_root):
    result = _result([_manifest()], {})
    payloads = integrate_pipeline._public_route_payloads("2026-09-15T12:00:00+00:00", result)
    public_root = data_root / "ctam" / "public"
    assert payloads[public_root / "registry.json"]["modules"][0]["routes"][0]["available"] is False
