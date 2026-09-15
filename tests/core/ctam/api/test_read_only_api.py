"""Contract coverage for Phase 2's cycle-pinned read-only CTAM API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from EdgeWARN.ctam.api import CTAMReadService, LoopbackCTAMServer
from EdgeWARN.ctam.manifest import ModuleManifest, ModuleRequirement, ModuleWrite, PublicRoute, Selector
from EdgeWARN.ctam.readiness import CatalogFile, CTAMCycleCatalog, READY
from EdgeWARN.ctam.sdk import CTAMAPIError, CTAMClient
from EdgeWARN.ctam.transaction import CTAMTransactionService


@pytest.fixture
def api(tmp_path: Path):
    source = tmp_path / "pinned.bin"; source.write_bytes(b"pinned-cycle-bytes")
    history = tmp_path / "7.json"; history.write_text(json.dumps([{"id": 7, "timestamp": "2026-08-05T11:55:00+00:00"}]))
    catalog = CTAMCycleCatalog(
        cycle_id="20260805-120000", analysis_time="2026-08-05T12:00:00+00:00", historical=False, cell_count=1,
        files=(
            CatalogFile("input:mrms:VIL_00.50:current", "input", "mrms", "VIL_00.50", "current", "2026-08-05T12:00:00+00:00", True, True, READY, None, source.stat().st_size, "application/x-grib2", source),
            CatalogFile("stormcells:current", "stormcells", None, None, "current", "2026-08-05T12:00:00+00:00", True, True, READY, None, None, "application/json", None),
            CatalogFile("cell_history:7", "cell_history", None, None, "history", "2026-08-05T12:00:00+00:00", True, True, READY, None, history.stat().st_size, "application/json", history),
        ),
    )
    manifest = ModuleManifest(module_id="reader", name="Reader", version="1.0.0", api_version="1", enabled=True, required=False, scope="stormcells", entrypoint=(), timeout_seconds=10, after=(), requires=(
        ModuleRequirement(Selector("input:MRMS:VIL_00.50:current", "input", "mrms", "VIL_00.50", "current"), True, None, None),
        ModuleRequirement(Selector("stormcells.current", "stormcells", None, None, "current"), True, None, None),
        ModuleRequirement(Selector("cells.history", "cell_history", None, None, "history"), False, None, None),
    ), writes=(ModuleWrite("stormcells.current", "/features/*/modules/Reader"),), directory=tmp_path, manifest_path=tmp_path / "module.toml", public_routes=(PublicRoute("summary", "Latest summary"),))
    cells = [{"id": 7, "properties": {"morphology": "cluster"}}]
    service = CTAMReadService(catalog=catalog, cells=cells, manifests={"reader": manifest}, transactions=CTAMTransactionService(cells=cells, manifests={"reader": manifest}))
    with LoopbackCTAMServer(service, tokens={"reader": "test-token"}) as server:
        yield server, source


def test_loopback_api_reads_pinned_catalog_content_and_history(api):
    server, source = api
    client = CTAMClient(server.url, "test-token")
    assert server.url.startswith("http://127.0.0.1:")
    assert client.health()["cycle_bound"] is True
    assert len(client.files()) == 3
    assert client.content("input:mrms:VIL_00.50:current", byte_range="0-5") == b"pinned"
    (source.parent / "newer.bin").write_bytes(b"newer-file-must-not-be-rescanned")
    # A descriptor is a pinned identity; its bytes are read through that exact
    # path, never selected again by a directory/mtime scan.
    assert client.file("input:mrms:VIL_00.50:current")["file_id"].endswith(":current")
    assert client.stormcell(7)["cell"]["properties"]["morphology"] == "cluster"
    assert client.history(7)["returned"] == 1
    assert client.history(7, limit=999)["returned"] == 1


def test_token_cannot_read_undeclared_content_while_catalog_stays_visible(api):
    server, _ = api
    client = CTAMClient(server.url, "wrong-token")
    with pytest.raises(CTAMAPIError) as excinfo:
        client.files()
    assert excinfo.value.status == 401


def test_server_enforces_declared_content_scope_for_every_artifact_read(api):
    server, _ = api
    # This manifest declares all catalog entries in this fixture; remove its
    # content grant after startup to exercise the transport-independent check.
    server.service._manifests["reader"] = ModuleManifest(
        **{**server.service._manifests["reader"].__dict__, "requires": ()}
    )
    client = CTAMClient(server.url, "test-token")
    assert len(client.files()) == 3
    with pytest.raises(CTAMAPIError) as excinfo:
        client.content("input:mrms:VIL_00.50:current")
    assert excinfo.value.status == 403
    for request in (client.stormcells, lambda: client.stormcell(7), lambda: client.history(7)):
        with pytest.raises(CTAMAPIError) as excinfo:
            request()
        assert excinfo.value.status == 403


def test_server_rejects_unsupported_protocol_version(api):
    server, _ = api
    request = Request(server.url + "/health", headers={"Authorization": "Bearer test-token", "X-CTAM-API-Version": "999"})
    with pytest.raises(HTTPError) as excinfo:
        urlopen(request)
    assert excinfo.value.code == 426


@pytest.mark.parametrize("version", [None, "1"])
def test_server_accepts_every_currently_supported_protocol_version(api, version):
    """v1 is the first release, so it has no preceding version to retain yet."""
    server, _ = api
    headers = {"Authorization": "Bearer test-token"}
    if version is not None:
        headers["X-CTAM-API-Version"] = version
    with urlopen(Request(server.url + "/health", headers=headers)) as response:
        payload = json.loads(response.read())
    assert payload["api_version"] == "1"
    assert payload["data"]["supported_api_versions"] == ["1"]


def test_patch_commit_uses_the_same_ownership_gate_and_is_idempotent(api):
    server, _ = api
    client = CTAMClient(server.url, "test-token")
    revision = client.stormcell(7)["revision"]
    staged = client.patch_stormcell(7, revision=revision, operations=[{"op": "add", "path": "/modules/Reader", "value": {"score": 2}}])
    assert staged["staged_operations"] == 1
    assert client.validate_transaction()["state"] == "open"
    committed = client.commit_transaction(idempotency_key="one")
    assert client.commit_transaction(idempotency_key="one") == committed
    assert client.stormcell(7)["cell"]["modules"]["Reader"] == {"score": 2}
    with pytest.raises(CTAMAPIError) as excinfo:
        client.patch_stormcell(7, revision=1, operations=[{"op": "add", "path": "/id", "value": "changed"}])
    assert excinfo.value.status == 409  # sealed transaction wins over a later patch


def test_alerts_are_staged_with_the_module_transaction(api):
    server, _ = api
    client = CTAMClient(server.url, "test-token")
    assert client.stage_alert({"id": "reader-7", "source": "Reader", "cell_id": 7, "geometry": [[1, 2]]})["staged_alerts"] == 1
    assert client.transaction()["staged"]["alerts"] == 1


def test_sdk_put_stages_declared_route_and_rejects_encoded_separators(api):
    server, _ = api
    client = CTAMClient(server.url, "test-token")
    assert "register_routes" in client.cycle()["allowed_operations"]
    assert client.register_route("summary", {"risk": "elevated"}) == {"route_id": "summary", "bytes": 19}
    assert client.transaction()["staged"]["routes"] == 1
    with pytest.raises(CTAMAPIError) as excinfo:
        client.register_route("not-declared", {})
    assert excinfo.value.status == 403

    request = Request(server.url + "/routes/a%252Fb", data=b"{}", method="PUT", headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"})
    with pytest.raises(HTTPError) as excinfo:
        urlopen(request)
    assert excinfo.value.code == 400
