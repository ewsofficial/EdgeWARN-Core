"""Cross-catalog characterization for configurable MRMS ingestion Phase 1."""

import json
from pathlib import Path

import yaml

from common.config.loader import load_config
import util.file as fs


ROOT = Path(__file__).resolve().parents[2]
BASELINE = json.loads((ROOT / "tests/fixtures/config/mrms_ingestion_contract.json").read_text())["v1_baseline"]


def test_source_and_retention_settings_remain_v1_operator_owned():
    mrms = yaml.safe_load((ROOT / "config/ingest.yaml").read_text())["mrms"]
    assert {key: mrms[key] for key in BASELINE["source"]} == BASELINE["source"]
    assert {key: mrms[key] for key in BASELINE["retention"]} == BASELINE["retention"]
    assert load_config("filesystem")["cleanup_defaults"]["max_files"] == BASELINE["cleanup_max_files"]


def test_fourteen_optional_statistic_products_and_twenty_five_statistics():
    expected = [stat for item in BASELINE["products"] for stat in item["stats"]]
    actual = [dict(stat) for stat in load_config("integration")["stats_datasets"]]
    assert sorted(actual, key=lambda item: item["key"]) == sorted(expected, key=lambda item: item["key"])
    assert len(actual) == 25
    assert len({item["filepath"] for item in actual}) == 14
    assert not {item["filepath"] for item in actual} & {
        "MRMS_COMPOSITE_DIR", "MRMS_PRECIPTYP_DIR", "MRMS_PROBSEVERE_DIR",
    }


def test_render_api_identities_and_wire_format_are_independent_of_raw_renames():
    layers = load_config("ewmrs_render")["mrms_layers"]
    expected = [layer for item in BASELINE["products"] for layer in item["render_layers"]]
    assert len(layers) == len(expected) == 16
    by_name = {layer["name"]: dict(layer) for layer in layers}
    api_products = json.loads((ROOT / "src/api/config/product-catalog.json").read_text())
    api_mrms = {item["id"]: item for item in api_products if item["id"].startswith("MRMS_")}
    assert set(api_mrms) == set(by_name)
    for layer in expected:
        assert by_name[layer["name"]] == {key: value for key, value in layer.items() if key != "gui_basename"}
        assert getattr(fs, layer["outdir"]) == fs.BASE_DIR / "gui" / layer["gui_basename"]
        assert api_mrms[layer["name"]]["storageDirectory"] == layer["gui_basename"]
        assert api_mrms[layer["name"]]["legacyFilePrefix"] == layer["name"]
    assert dict(load_config("ewmrs_render")["chunk_format"]) == BASELINE["chunk_format"]


def test_stormprob_mesh_is_probsevere_derived_and_not_the_raw_mesh_product():
    from EdgeWARN.stormprob.features import IMPORTANT_SCALAR_PROPERTY_FEATURES
    integration = load_config("integration")
    assert "MESH" in IMPORTANT_SCALAR_PROPERTY_FEATURES
    assert integration["probsevere_field_map"]["MESH"] == "MESH"
    assert not any(stat["filepath"] == "MRMS_MESH_DIR" for stat in integration["stats_datasets"])
    # Lightning enrichment is supported but is not a trained property feature.
    assert "maxCGFlashDensity" not in IMPORTANT_SCALAR_PROPERTY_FEATURES
