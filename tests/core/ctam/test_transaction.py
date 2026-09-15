"""Phase 3 ownership and revision tests for the transport-neutral mutation core."""
from __future__ import annotations

from pathlib import Path

import pytest

from EdgeWARN.ctam.api.models import APIError
from EdgeWARN.ctam.manifest import ModuleManifest, ModuleWrite, PublicRoute
from EdgeWARN.ctam.transaction import CTAMTransactionService, validate_patch_path
from tests.core.ctam.contract.test_pointer_allowlist import ALLOWED, HOST, TABLE


def manifest(tmp_path: Path) -> ModuleManifest:
    return ModuleManifest(
        module_id="cellstats", name="CellStats", version="1.0.0", api_version="1",
        enabled=True, required=False, scope="stormcells", entrypoint=(), timeout_seconds=10,
        after=(), requires=(), writes=(
            ModuleWrite("stormcells.current", "/features/*/modules/CellStats"),
            ModuleWrite("stormcells.current", "/features/*/properties/cellstats_severity"),
            ModuleWrite("cells.history", "/*/modules/CellStats"),
        ), directory=tmp_path, manifest_path=tmp_path / "module.toml",
        public_routes=(PublicRoute("summary", "Latest summary"),),
    )


def service(tmp_path):
    return CTAMTransactionService(cells=[{"id": "7", "properties": {"morphology": "cluster"}, "geometry": [1]}], manifests={"cellstats": manifest(tmp_path)})


@pytest.mark.parametrize("pointer,verdict,_why", TABLE)
def test_shared_pointer_gate_enforces_contract_table(tmp_path, pointer, verdict, _why):
    if verdict == ALLOWED:
        assert validate_patch_path(manifest(tmp_path), pointer)
    elif verdict == HOST:
        with pytest.raises(APIError): validate_patch_path(manifest(tmp_path), pointer)


def test_commit_is_revisioned_idempotent_and_preserves_core_fields(tmp_path):
    transactions = service(tmp_path)
    result = transactions.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": {"score": 4}}])
    assert result["staged_operations"] == 1
    committed = transactions.commit("cellstats", idempotency_key="same-request")
    assert committed["state"] == "sealed"
    assert transactions.commit("cellstats", idempotency_key="same-request") == committed
    assert transactions.cells["7"]["modules"]["CellStats"] == {"score": 4}
    assert transactions.cells["7"]["geometry"] == [1]
    with pytest.raises(APIError) as error:
        transactions.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats/next", "value": 1}])
    assert error.value.code == "transaction_sealed"


def test_invalid_or_host_owned_values_never_change_working_set(tmp_path):
    transactions = service(tmp_path)
    before = transactions.cells["7"].copy()
    with pytest.raises(APIError):
        transactions.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/properties/morphology", "value": "bad"}])
    with pytest.raises(APIError):
        transactions.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": float("nan")}])
    assert transactions.cells["7"] == before


def test_owned_property_key_stays_rewritable_across_cycles(tmp_path):
    """A module-owned properties key written by a prior cycle (now pre-existing
    on the cell) must accept a rewrite; a host-owned key must stay frozen."""
    cell = {"id": "7", "properties": {"morphology": "cluster", "cellstats_severity": 2}, "modules": {}}
    transactions = CTAMTransactionService(cells=[cell], manifests={"cellstats": manifest(tmp_path)})
    result = transactions.stage_cell("cellstats", "7", revision=0, operations=[
        {"op": "replace", "path": "/properties/cellstats_severity", "value": 3},
    ])
    assert result["staged_operations"] == 1
    with pytest.raises(APIError) as error:
        transactions.stage_cell("cellstats", "7", revision=0, operations=[
            {"op": "replace", "path": "/modules/CellStats", "value": {}},
            {"op": "replace", "path": "/properties/morphology", "value": "bad"},
        ])
    assert error.value.code == "forbidden_path"


def test_stale_revision_is_rejected_before_staging(tmp_path):
    transactions = service(tmp_path)
    transactions.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": {}}])
    transactions.commit("cellstats")
    second = CTAMTransactionService(cells=[transactions.cells["7"]], manifests={"cellstats": manifest(tmp_path)})
    second.cell_revisions["7"] = 1
    with pytest.raises(APIError) as error:
        second.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats/x", "value": 1}])
    assert error.value.code == "stale_revision"


def test_history_patch_is_limited_to_existing_timestamp_and_own_namespace(tmp_path):
    item = {"id": "7", "timestamp": "2026-08-05T12:00:00+00:00", "properties": {}}
    transactions = CTAMTransactionService(cells=[{"id": "7"}], histories={"7": [item]}, manifests={"cellstats": manifest(tmp_path)})
    transactions.stage_history("cellstats", "7", item["timestamp"], revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": {"historical": True}}])
    transactions.commit("cellstats")
    assert transactions.histories["7"][0]["modules"]["CellStats"]["historical"] is True
    with pytest.raises(APIError):
        transactions.stage_history("cellstats", "7", "not-a-real-entry", revision=1, operations=[{"op": "add", "path": "/modules/CellStats", "value": {}}])


def test_abandon_discards_staged_work_without_changing_the_working_set(tmp_path):
    transactions = service(tmp_path)
    transactions.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": {"discard": True}}])
    assert transactions.abandon("cellstats")["state"] == "abandoned"
    assert transactions.transaction("cellstats")["staged"]["stormcell_operations"] == 0
    assert "modules" not in transactions.cells["7"]
    with pytest.raises(APIError) as error:
        transactions.stage_cell("cellstats", "7", revision=0, operations=[{"op": "add", "path": "/modules/CellStats", "value": {}}])
    assert error.value.code == "transaction_sealed"


def test_only_sealed_transactions_expose_alerts_for_host_publication(tmp_path):
    transactions = service(tmp_path)
    transactions.stage_alert("cellstats", {"id": "a", "source": "CellStats", "cell_id": "7", "geometry": [[1, 2]]})
    assert transactions.committed_alerts() == []
    transactions.commit("cellstats")
    assert transactions.committed_alerts()[0]["id"] == "a"


def test_routes_replace_transactionally_and_only_committed_routes_are_visible(tmp_path):
    transactions = service(tmp_path)
    assert transactions.stage_route("cellstats", "summary", {"risk": "low"})["route_id"] == "summary"
    transactions.stage_route("cellstats", "summary", {"risk": "elevated"})
    assert transactions.transaction("cellstats")["staged"]["routes"] == 1
    assert transactions.committed_routes() == {}
    transactions.commit("cellstats")
    assert transactions.committed_routes() == {"cellstats": {"summary": {"risk": "elevated"}}}


def test_undeclared_nonfinite_and_abandoned_routes_are_not_committed(tmp_path):
    transactions = service(tmp_path)
    with pytest.raises(APIError) as excinfo:
        transactions.stage_route("cellstats", "undeclared", {})
    assert excinfo.value.code == "route_not_declared"
    with pytest.raises(APIError):
        transactions.stage_route("cellstats", "summary", {"bad": float("nan")})
    transactions.stage_route("cellstats", "summary", {"discard": True})
    transactions.abandon("cellstats")
    assert transactions.committed_routes() == {}
