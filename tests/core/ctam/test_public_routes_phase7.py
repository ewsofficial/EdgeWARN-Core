"""Phase 7 end-to-end coverage for CTAM public module routes.

Covers the declaration, transaction, loopback, SDK, and runner edges listed
in plans/ctam-public-module-routes-plan.md section 7 that are not already
pinned by the focused test files.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from EdgeWARN.ctam.api import CTAMReadService, LoopbackCTAMServer
from EdgeWARN.ctam.discovery import discover_modules
from EdgeWARN.ctam.limits import (
    MAX_PUBLIC_ROUTES_PER_MODULE,
    MAX_PUBLIC_ROUTE_DEPTH,
    MAX_PUBLIC_ROUTE_PAYLOAD_BYTES,
    MAX_PUBLIC_ROUTE_TOTAL_BYTES,
)
from EdgeWARN.ctam.manifest import (
    ManifestError,
    ModuleManifest,
    ModuleRequirement,
    ModuleWrite,
    PublicRoute,
    Selector,
    parse_manifest,
)
from EdgeWARN.ctam.api.models import APIError
from EdgeWARN.ctam.readiness import CatalogFile, CTAMCycleCatalog, READY
from EdgeWARN.ctam.runner import ExternalModuleRunner
from EdgeWARN.ctam.sdk import CTAMClient
from EdgeWARN.ctam.transaction import CTAMTransactionService

pytestmark = pytest.mark.ctam

MINIMAL = """\
schema_version = 1
id = "cellstats"
name = "CellStats"
version = "1.0.0"
api_version = "1"
entrypoint = ["{python}", "main.py"]

[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/modules/CellStats"
"""


def write_module(root: Path, name: str, body: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "main.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    manifest_path = directory / "module.toml"
    manifest_path.write_text(body, encoding="utf-8")
    return manifest_path


def _manifest(tmp_path: Path, module_id="cellstats", name="CellStats", routes=(PublicRoute("summary", "Latest summary"),)) -> ModuleManifest:
    return ModuleManifest(
        module_id=module_id, name=name, version="1.0.0", api_version="1",
        enabled=True, required=False, scope="stormcells",
        entrypoint=("{python}", "main.py"), timeout_seconds=10, after=(),
        requires=(), writes=(ModuleWrite("stormcells.current", "/features/*/modules/CellStats"),),
        directory=tmp_path, manifest_path=tmp_path / "module.toml",
        public_routes=routes,
    )


def _service(tmp_path, manifests=None):
    manifests = manifests or {"cellstats": _manifest(tmp_path)}
    return CTAMTransactionService(cells=[{"id": "7"}], manifests=manifests)


# --- Manifest declaration constraints ---------------------------------------

def test_public_route_description_length_and_controls_rejected(tmp_path):
    long_desc = "x" * 257
    with pytest.raises(ManifestError, match="description"):
        parse_manifest(write_module(tmp_path, "cellstats", MINIMAL + f'\n[[public_routes]]\nid = "ok"\ndescription = "{long_desc}"\n'))
    with pytest.raises(ManifestError, match="control"):
        parse_manifest(write_module(tmp_path, "cellstats", MINIMAL + '\n[[public_routes]]\nid = "ok"\ndescription = "bad\\u0001desc"\n'))
    with pytest.raises(ManifestError, match="description"):
        parse_manifest(write_module(tmp_path, "cellstats", MINIMAL + '\n[[public_routes]]\nid = "ok"\ndescription = ""\n'))


def test_public_routes_reject_unknown_fields_non_list_and_reserved_ids(tmp_path):
    with pytest.raises(ManifestError, match="unknown field"):
        parse_manifest(write_module(tmp_path, "cellstats", MINIMAL + '\n[[public_routes]]\nid = "ok"\ndescription = "fine"\nextra = "nope"\n'))
    for reserved in (".", ".."):
        with pytest.raises(ManifestError, match="public_routes\\[0\\]\\.id"):
            parse_manifest(write_module(tmp_path, "cellstats", MINIMAL + f'\n[[public_routes]]\nid = "{reserved}"\ndescription = "reserved"\n'))
    # Non-list table form is a manifest bug, not an empty surface.
    bad = write_module(tmp_path, "cellstats", MINIMAL + '\n[public_routes]\nid = "ok"\ndescription = "fine"\n')
    with pytest.raises(ManifestError, match="public_routes"):
        parse_manifest(bad)


def test_public_route_count_overflow_message_names_limit(tmp_path):
    routes = "".join(f'\n[[public_routes]]\nid = "r{i}"\ndescription = "Route {i}"\n' for i in range(MAX_PUBLIC_ROUTES_PER_MODULE + 1))
    with pytest.raises(ManifestError, match="at most"):
        parse_manifest(write_module(tmp_path, "cellstats", MINIMAL + routes))


# --- Discovery failure isolation ---------------------------------------------

def _discovery_manifest(module_id: str, route_id: str | None = "summary") -> str:
    lines = [
        "schema_version = 1",
        f'id = "{module_id}"',
        f'name = "{module_id}"',
        'version = "1.0.0"',
        'api_version = "1"',
        'entrypoint = ["{python}", "main.py"]',
        "",
        "[[writes]]",
        'resource = "stormcells.current"',
        f'json_pointer = "/features/*/modules/{module_id}"',
    ]
    if route_id is not None:
        lines += ["", "[[public_routes]]", f'id = "{route_id}"', 'description = "Latest summary"']
    return "\n".join(lines) + "\n"


def test_invalid_route_declaration_isolates_only_that_module(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "main.py").write_text("x=1\n")
    (tmp_path / "good" / "module.toml").write_text(_discovery_manifest("good"))
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "main.py").write_text("x=1\n")
    (tmp_path / "bad" / "module.toml").write_text(
        _discovery_manifest("bad") + '[[public_routes]]\nid = "bad"\ndescription = "Second"\n[[public_routes]]\nid = "bad"\ndescription = "Duplicate"\n'
    )
    result = discover_modules(tmp_path)
    by_id = {m.module_id: m for m in result.modules}
    assert by_id["good"].state == "discovered"
    assert by_id["good"].manifest.public_routes[0].route_id == "summary"
    assert by_id["bad"].state == "invalid"
    assert "duplicated" in (by_id["bad"].reason or "")
    assert "good" in {m.module_id for m in result.runnable}


# --- Transaction: cross-module, limits, snapshots -----------------------------

def test_cross_module_route_stage_is_forbidden(tmp_path):
    manifests = {
        "moda": _manifest(tmp_path, module_id="moda", name="CellStats", routes=(PublicRoute("alpha", "Alpha"),)),
        "modb": _manifest(tmp_path, module_id="modb", name="CellStats", routes=(PublicRoute("beta", "Beta"),)),
    }
    # Fix owned namespace for writes validation (not needed for routes, but keeps manifests valid).
    service = CTAMTransactionService(cells=[{"id": "7"}], manifests=manifests)
    with pytest.raises(APIError) as excinfo:
        service.stage_route("moda", "beta", {"x": 1})
    assert excinfo.value.code == "route_not_declared"
    with pytest.raises(APIError) as excinfo:
        service.stage_route("ghost", "alpha", {})
    assert excinfo.value.code == "authentication_failed"


def test_route_payload_size_and_depth_limits(tmp_path):
    service = _service(tmp_path)
    oversized = "x" * (MAX_PUBLIC_ROUTE_PAYLOAD_BYTES + 1)
    with pytest.raises(APIError) as excinfo:
        service.stage_route("cellstats", "summary", {"blob": oversized})
    assert excinfo.value.code == "request_too_large"
    assert service.transaction("cellstats")["staged"]["routes"] == 0
    # Nesting deeper than the documented max is an invalid patch, staging nothing.
    deep = current = {}
    for _ in range(MAX_PUBLIC_ROUTE_DEPTH + 1):
        nxt: dict = {}
        current["child"] = nxt
        current = nxt
    # The helper above builds depth+1; wrap so depth() counts container levels.
    with pytest.raises(APIError) as excinfo:
        service.stage_route("cellstats", "summary", deep)
    assert excinfo.value.code == "invalid_patch"


def test_route_aggregate_limit_keeps_prior_staged_routes(tmp_path):
    routes = tuple(PublicRoute(f"r{i}", f"Route {i}") for i in range(5))
    manifest = _manifest(tmp_path, routes=routes)
    service = CTAMTransactionService(cells=[{"id": "7"}], manifests={"cellstats": manifest})
    chunk = "x" * (900 * 1024)
    for index in range(4):
        service.stage_route("cellstats", f"r{index}", {"blob": chunk})
    staged_before = dict(service.transactions["cellstats"].staged_routes)
    with pytest.raises(APIError) as excinfo:
        service.stage_route("cellstats", "r4", {"blob": chunk})
    assert excinfo.value.code == "request_too_large"
    assert service.transactions["cellstats"].staged_routes == staged_before
    assert service.transaction("cellstats")["staged"]["routes"] == 4
    assert service.transaction("cellstats")["staged"]["route_bytes"] > 0


def test_commit_seals_cells_and_routes_together_abandon_discards_both(tmp_path):
    service = _service(tmp_path)
    service.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": {"score": 1}}])
    service.stage_route("cellstats", "summary", {"risk": "elevated"})
    assert service.committed_routes() == {}
    service.commit("cellstats")
    assert service.committed_routes() == {"cellstats": {"summary": {"risk": "elevated"}}}
    assert service.cells["7"]["modules"]["CellStats"] == {"score": 1}

    service2 = _service(tmp_path)
    service2.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": {"score": 2}}])
    service2.stage_route("cellstats", "summary", {"risk": "low"})
    service2.abandon("cellstats")
    assert service2.committed_routes() == {}
    assert "modules" not in service2.cells["7"]


# --- Loopback API + SDK edges -------------------------------------------------

def _loopback(tmp_path, manifests=None):
    manifests = manifests or {"reader": ModuleManifest(
        module_id="reader", name="Reader", version="1.0.0", api_version="1",
        enabled=True, required=False, scope="stormcells", entrypoint=(), timeout_seconds=10,
        after=(), requires=(
            ModuleRequirement(Selector("stormcells.current", "stormcells", None, None, "current"), True, None, None),
        ),
        writes=(ModuleWrite("stormcells.current", "/features/*/modules/Reader"),),
        directory=tmp_path, manifest_path=tmp_path / "module.toml",
        public_routes=(PublicRoute("summary", "Latest summary"),),
    )}
    cells = [{"id": "7", "properties": {}}]
    catalog = CTAMCycleCatalog(
        cycle_id="20260805-120000", analysis_time="2026-08-05T12:00:00+00:00",
        historical=False, cell_count=1,
        files=(CatalogFile("stormcells:current", "stormcells", None, None, "current",
                           "2026-08-05T12:00:00+00:00", True, True, READY, None, None, "application/json", None),),
    )
    service = CTAMReadService(catalog=catalog, cells=cells, manifests=manifests,
                              transactions=CTAMTransactionService(cells=cells, manifests=manifests))
    server = LoopbackCTAMServer(service, tokens={mid: f"token-{mid}" for mid in manifests})
    return server.start(), service


def test_loopback_route_envelope_shape_and_allowed_operations(tmp_path):
    server, _ = _loopback(tmp_path)
    try:
        client = CTAMClient(server.url, "token-reader")
        assert "register_routes" in client.cycle()["allowed_operations"]
        raw = urlopen(Request(server.url + "/cycle", headers={"Authorization": "Bearer token-reader"}))
        envelope = json.loads(raw.read())
        assert envelope["api_version"] == "1"
        assert envelope["cycle_id"] == "20260805-120000"
        assert envelope["module_id"] == "reader"
        assert envelope["request_id"]
        assert envelope["errors"] == []
    finally:
        server.close()


def test_loopback_rejects_encoded_separators_extra_segments_and_methods(tmp_path):
    server, _ = _loopback(tmp_path)
    try:
        headers = {"Authorization": "Bearer token-reader", "Content-Type": "application/json"}
        # Encoded slash, NUL, and backslash all fail as unsafe single segments.
        for suffix in ("a%2Fb", "a%00b", "..", ".", "a%5Cb"):
            req = Request(server.url + f"/routes/{suffix}", data=b"{}", method="PUT", headers=headers)
            with pytest.raises(HTTPError) as excinfo:
                urlopen(req)
            assert excinfo.value.code in (400, 403, 404)
        # Extra path segments are not a route.
        req = Request(server.url + "/routes/summary/extra", data=b"{}", method="PUT", headers=headers)
        with pytest.raises(HTTPError) as excinfo:
            urlopen(req)
        assert excinfo.value.code == 404
        # Double-encoding tricks must not decode to a valid declaration.
        req = Request(server.url + "/routes/a%252Fb", data=b"{}", method="PUT", headers=headers)
        with pytest.raises(HTTPError) as excinfo:
            urlopen(req)
        assert excinfo.value.code == 400
        # Only PUT stages a route; other methods on the same path are unknown.
        for method in ("GET", "POST", "DELETE", "PATCH"):
            req = Request(server.url + "/routes/summary", method=method, headers={"Authorization": "Bearer token-reader"})
            with pytest.raises(HTTPError) as excinfo:
                urlopen(req)
            assert excinfo.value.code == 404
    finally:
        server.close()


def test_loopback_cross_token_route_is_not_declared_for_caller(tmp_path):
    manifests = {
        "moda": _manifest(tmp_path, module_id="moda", name="CellStats", routes=(PublicRoute("alpha", "Alpha"),)),
        "modb": _manifest(tmp_path, module_id="modb", name="CellStats", routes=(PublicRoute("beta", "Beta"),)),
    }
    cells = [{"id": "7"}]
    catalog = CTAMCycleCatalog("20260805-120000", "2026-08-05T12:00:00+00:00", False, 1, ())
    service = CTAMReadService(catalog=catalog, cells=cells, manifests=manifests,
                              transactions=CTAMTransactionService(cells=cells, manifests=manifests))
    server = LoopbackCTAMServer(service, tokens={"moda": "token-a", "modb": "token-b"}).start()
    try:
        client_a = CTAMClient(server.url, "token-a")
        with pytest.raises(Exception) as excinfo:
            client_a.register_route("beta", {"x": 1})
        assert getattr(excinfo.value, "status", None) == 403
        assert client_a.register_route("alpha", {"x": 1})["route_id"] == "alpha"
    finally:
        server.close()


# --- Runner: timeout and uncommitted exit discard routes ----------------------

def _runner_manifest(tmp_path, module_id, program, *, timeout=10, routes=(PublicRoute("summary", "Latest summary"),)):
    folder = tmp_path / module_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(program, encoding="utf-8")
    return ModuleManifest(
        module_id=module_id, name="Runner" + module_id, version="1.0.0", api_version="1",
        enabled=True, required=False, scope="stormcells", entrypoint=("{python}", "main.py"),
        timeout_seconds=timeout, after=(),
        requires=(ModuleRequirement(Selector("stormcells.current", "stormcells", None, None, "current"), True, None, None),),
        writes=(ModuleWrite("stormcells.current", "/features/*/modules/Runner" + module_id),),
        directory=folder, manifest_path=folder / "module.toml",
        public_routes=routes,
    )


def _catalog():
    return CTAMCycleCatalog("20260805-120000", "2026-08-05T12:00:00+00:00", False, 1,
                            (CatalogFile("stormcells:current", "stormcells", None, None, "current", "", True, True, READY, None, None, "application/json", None),))


def test_runner_timeout_discards_staged_routes(tmp_path):
    program = (
        "import json, os, time\n"
        "from urllib.request import Request, urlopen\n"
        "base=os.environ['CTAM_API_URL']; tok=os.environ['CTAM_API_TOKEN']\n"
        "h={'Authorization':'Bearer '+tok,'X-CTAM-API-Version':'1','Content-Type':'application/json'}\n"
        "urlopen(Request(base+'/routes/summary',data=json.dumps({'risk':'stale'}).encode(),method='PUT',headers=h)).read()\n"
        "time.sleep(5)\n"
    )
    manifest = _runner_manifest(tmp_path, "slow", program, timeout=1)
    runner = ExternalModuleRunner(catalog=_catalog(), cells=[{"id": "7"}], manifests={"slow": manifest})
    results = runner.run()
    assert results[0].state == "timed_out"
    assert runner.transactions.committed_routes() == {}


def test_runner_uncommitted_exit_discards_staged_routes(tmp_path):
    program = (
        "import json, os\n"
        "from urllib.request import Request, urlopen\n"
        "base=os.environ['CTAM_API_URL']; tok=os.environ['CTAM_API_TOKEN']\n"
        "h={'Authorization':'Bearer '+tok,'X-CTAM-API-Version':'1','Content-Type':'application/json'}\n"
        "urlopen(Request(base+'/routes/summary',data=json.dumps({'risk':'draft'}).encode(),method='PUT',headers=h)).read()\n"
    )
    manifest = _runner_manifest(tmp_path, "draft", program)
    runner = ExternalModuleRunner(catalog=_catalog(), cells=[{"id": "7"}], manifests={"draft": manifest})
    results = runner.run()
    assert results[0].state == "failed"
    assert results[0].reason == "module exited without committing"
    assert runner.transactions.committed_routes() == {}
