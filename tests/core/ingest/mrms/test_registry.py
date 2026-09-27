"""Phase 2 pure definitions: no acquisition, live configuration or path binding."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pickle
import subprocess
import sys
from types import MappingProxyType

import pytest

from common.config.mrms_products import parse_product_id
from common.ingest.mrms.core_contract import (
    CORE_PRODUCTS, DISCOVERY_IDS, LEGACY_ALIASES, PROTECTED_IDS, contract_json,
)
from common.ingest.mrms.registry import build_registry
from common.ingest.mrms.source import source_for

ROOT = Path(__file__).resolve().parents[4]
CASES = json.loads((ROOT / "tests/fixtures/config/mrms_products_v2.json").read_text())
BASELINE = json.loads((ROOT / "tests/fixtures/config/mrms_ingestion_contract.json").read_text())


@pytest.mark.parametrize("case", CASES["valid"])
def test_identity(case):
    product = parse_product_id(case["configured_id"])
    assert product.product_id == case["product_id"]
    assert product.path_name == case["path_name"]


@pytest.mark.parametrize("value", CASES["invalid"])
def test_invalid_identity(value):
    with pytest.raises(ValueError):
        parse_product_id(value)


def test_identity_length_boundary():
    assert len(parse_product_id("MRMS_" + "A" * 122 + "_00.50").product_id) == 128
    with pytest.raises(ValueError):
        parse_product_id("MRMS_" + "A" * 123 + "_00.50")


@pytest.mark.parametrize("products", CASES["collisions"])
def test_collisions_include_protected_products(products, tmp_path):
    with pytest.raises(ValueError, match="collision"):
        build_registry({"products": products}, tmp_path)


def test_duplicate_operator_entries_and_reserved_deduplication(tmp_path):
    product = "MRMS_PrecipFlag_00.00"
    registry = build_registry({"products": [product]}, tmp_path)
    assert len(registry.products) == 3
    with pytest.raises(ValueError, match="Duplicate"):
        build_registry({"products": [product, product]}, tmp_path)


def test_empty_additions_are_exact_core_contract(tmp_path):
    registry = build_registry({"products": ()}, tmp_path)
    assert {p.product_id for p in registry.products} == PROTECTED_IDS
    assert set(DISCOVERY_IDS) == PROTECTED_IDS
    assert registry.for_phase("detection") == registry.products
    assert registry.for_phase("integration") == ()
    assert all(p.required and p.protected and p.discovery for p in registry.products)
    assert len(registry.get_check_modifiers()) == 3
    assert len(registry.legacy_paths()) == 3
    prob = registry.require("ProbSevere")
    assert (prob.region, prob.adapter, prob.source_modifier) == (
        "ProbSevere", "probsevere_json", None,
    )


def test_default_membership_aliases_and_sources(tmp_path):
    registry = build_registry({"products": BASELINE["v2_target"]["default_additions"]}, tmp_path)
    assert len(registry.products) == 21
    assert sum(not p.protected for p in registry.products) == 18
    assert len(registry.for_phase("detection")) == 3
    for old in BASELINE["v1_baseline"]["products"]:
        spec = registry.require(old["product_id"])
        assert spec.path_name == old["raw_basename_v2"]
        assert LEGACY_ALIASES[old["legacy_alias"]] == spec.product_id
        assert registry.legacy_paths()[old["legacy_alias"]] == spec.directory
        source = source_for(parse_product_id(spec.configured_id))
        assert (source.region, source.adapter, source.source_modifier) == (
            spec.region, spec.adapter, spec.source_modifier,
        )
        assert source.bucket == BASELINE["v1_baseline"]["source"]["bucket"]
        dt = datetime(2026, 9, 26, 0, 6, tzinfo=timezone.utc)
        if spec.product_id != "ProbSevere":
            assert source.https_url == (
                "https://mrms.ncep.noaa.gov/data/2D/" + spec.product_id.rsplit("_", 1)[0]
            )
            assert source.listing_bounds(dt) == (
                f"CONUS/{spec.product_id}/20260926/MRMS_{spec.product_id}_20260926-00",
                None,
            )


def test_new_product_and_disabled_lookup(tmp_path):
    registry = build_registry({"products": ["MRMS_NewProduct_01.25"]}, tmp_path)
    spec = registry.require("NewProduct_01.25")
    assert not spec.required and not spec.discovery and spec.core_phase is None
    assert registry.paths_by_name()["MRMS_NewProduct"] == tmp_path / "data/MRMS_NewProduct"
    assert registry.path_for(spec.product_id) == spec.directory
    assert not registry.is_enabled("MESH_00.50")
    assert "MRMS_MESH_DIR" not in registry.legacy_paths()
    with pytest.raises(KeyError, match="not enabled"):
        registry.path_for("MESH_00.50")
    source = source_for(parse_product_id(spec.configured_id))
    dt = datetime(2026, 9, 26, 0, 6, tzinfo=timezone.utc)
    assert source.listing_bounds(dt) == (
        "CONUS/NewProduct_01.25/20260926/MRMS_NewProduct_01.25_20260926-00", None,
    )
    assert source.https_url.endswith("/2D/NewProduct")
    assert source.filename_start_after(dt) == "MRMS_NewProduct_01.25_20260926-0006"


def test_probsevere_midnight_and_utc_conversion():
    source = source_for(parse_product_id("MRMS_ProbSevere"))
    dt = datetime(2026, 9, 25, 20, 6, tzinfo=timezone(timedelta(hours=-4)))
    assert source.listing_bounds(dt) == (
        "ProbSevere/20260926/", "ProbSevere/20260926/MRMS_PROBSEVERE_20260925_23",
    )
    assert source.filename_start_after(dt) == "MRMS_PROBSEVERE_20260926_0006"
    assert source.https_url == "https://mrms.ncep.noaa.gov/data/ProbSevere"
    with pytest.raises(ValueError, match="timezone-aware"):
        source.s3_prefix(dt.replace(tzinfo=None))


def test_registry_is_immutable_and_serializable(tmp_path):
    config = {"products": ["MRMS_MESH_00.50"], "downloads": {"max_concurrency": 8}}
    registry = build_registry(config, tmp_path)
    config["products"].clear()
    config["downloads"]["max_concurrency"] = 1
    assert registry.is_enabled("MESH_00.50")
    assert json.loads(registry.normalized_config_json)["downloads"]["max_concurrency"] == 8
    assert pickle.loads(pickle.dumps(registry)) == registry
    with pytest.raises(FrozenInstanceError):
        registry.products[0].required = False
    with pytest.raises(FrozenInstanceError):
        registry.products = ()
    with pytest.raises(TypeError):
        registry.paths_by_name()["anything"] = tmp_path


def test_fingerprint_normalizes_membership_and_frozen_config(tmp_path):
    products = ["MRMS_MESH_00.50", "MRMS_VIL_00.50"]
    first = build_registry({"products": products, "downloads": {"max_concurrency": 8}}, tmp_path)
    frozen = MappingProxyType({
        "downloads": MappingProxyType({"max_concurrency": 8}),
        "products": tuple(reversed(products)) + ("MRMS_ProbSevere",),
    })
    assert build_registry(frozen, tmp_path).fingerprint == first.fingerprint
    assert build_registry(frozen, tmp_path / "other").fingerprint != first.fingerprint
    assert build_registry({"products": products}, tmp_path).fingerprint != first.fingerprint
    assert build_registry({"products": []}, tmp_path).fingerprint != first.fingerprint


def test_build_does_not_probe_filesystem(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem access in pure registry")
    for name in ("stat", "resolve", "mkdir", "open"):
        monkeypatch.setattr(Path, name, forbidden)
    registry = build_registry({"products": ["MRMS_MESH_00.50"]}, tmp_path / "absent")
    assert len(registry.products) == 4


def test_fresh_import_does_not_load_runtime_or_configuration():
    code = """
import sys
from common.ingest.mrms.registry import build_registry
assert not any(name in sys.modules for name in (
    'util.file', 'common.config.loader', 'EdgeWARN', 'EWMRS', 'aiohttp', 'boto3'))
from pathlib import Path
registry = build_registry({'products': []}, Path('/nonexistent/mrms-test'))
assert len(registry.products) == 3
"""
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT / "src")


def test_release_asset_is_generated_from_core_contract():
    asset = ROOT / "src/common/ingest/mrms/core-contract.json"
    assert asset.read_text() == contract_json()
    assert {p.product_id for p in CORE_PRODUCTS} == set(BASELINE["v2_target"]["reserved_ids"])
