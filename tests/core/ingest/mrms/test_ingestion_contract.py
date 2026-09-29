"""Phase 1 baseline. Target v2 membership is data, not a runtime assertion."""

from datetime import datetime, timezone
import importlib
import json
from pathlib import Path

import pytest

from common.config.loader import load_config
from common.ingest.mrms import config
from common.ingest.mrms.downloader import _mrms_modifier_label, _narrow_mrms_lookup
from common.ingest.mrms.https_client import HttpsFileFinder
from common.ingest.mrms.main import get_detection_modifiers, get_integration_modifiers
import util.file as fs


CONTRACT = json.loads((Path(__file__).resolve().parents[3] /
                      "fixtures/config/mrms_ingestion_contract.json").read_text())
BASELINE = CONTRACT["v1_baseline"]
TARGET = CONTRACT["v2_target"]
PRODUCTS = BASELINE["products"]


def test_current_catalog_and_discovery_are_exact_v1_baseline():
    assert load_config("ingest")["schema_version"] == BASELINE["ingest_schema_version"]
    assert config.get_mrms_modifiers() == [
        (item["region"], item["source_modifier"], getattr(fs, item["legacy_alias"]))
        for item in PRODUCTS
    ]
    assert [_mrms_modifier_label(mod) for _, mod, _ in config.get_check_modifiers()] == BASELINE["discovery_ids"]
    assert len(PRODUCTS) == 21
    assert len(BASELINE["discovery_ids"]) == 10


def test_target_contract_is_three_reserved_plus_eighteen_additions():
    """Pin design intent without pretending runtime protection exists yet."""
    reserved = {"MergedReflectivityQCComposite_00.50", "PrecipFlag_00.00", "ProbSevere"}
    assert set(TARGET["reserved_ids"]) == reserved
    assert TARGET["discovery_ids"] == TARGET["reserved_ids"]
    additions = TARGET["default_additions"]
    assert len(additions) == len(set(additions)) == 18
    assert all(product.startswith("MRMS_") for product in additions)
    optional_ids = {product.removeprefix("MRMS_") for product in additions}
    assert not reserved & optional_ids
    assert reserved | optional_ids == {item["product_id"] for item in PRODUCTS}
    assert {_mrms_modifier_label(mod) for mod in get_detection_modifiers()} == reserved
    # Current integration gating includes even non-statistic products. Phase 5
    # deliberately replaces this baseline with phase-specific required inputs.
    assert {_mrms_modifier_label(mod) for mod in get_integration_modifiers()} == optional_ids
    assert set(BASELINE["discovery_ids"]) - reserved == {
        "EchoTop_18_00.50", "EchoTop_30_00.50", "EchoTop_50_00.50",
        "MergedAzShear_0-2kmAGL_00.50", "MergedAzShear_3-6kmAGL_00.50",
        "VIL_Density_00.50", "VII_00.50",
    }


@pytest.mark.parametrize("item", PRODUCTS, ids=lambda item: item["product_id"])
def test_legacy_alias_paths_and_source_grammars(item):
    """No network calls or operational directory creation are needed."""
    assert getattr(fs, item["legacy_alias"]) == fs.BASE_DIR / "data" / item["raw_basename_v1"]
    dt = datetime(2026, 9, 26, 0, 6, tzinfo=timezone.utc)
    modifier = item["source_modifier"]
    if modifier is None:
        assert item["product_id"] == "ProbSevere"
        assert item["adapter"] == "probsevere_json"
        assert _narrow_mrms_lookup(dt, item["region"], modifier) == (
            "ProbSevere/20260926/", "ProbSevere/20260926/MRMS_PROBSEVERE_20260925_23",
        )
        expected_url = "https://mrms.ncep.noaa.gov/data/ProbSevere"
        assert config.mrms_probsevere_start_after_minute(dt) == "MRMS_PROBSEVERE_20260926_0006"
    else:
        assert item["adapter"] == "conus_grib2"
        assert _narrow_mrms_lookup(dt, item["region"], modifier) == (
            f"CONUS/{modifier}/20260926/MRMS_{modifier}_20260926-00", None,
        )
        expected_url = "https://mrms.ncep.noaa.gov/data/2D/" + modifier.rsplit("_", 1)[0]
        assert config.mrms_filename_start_after(dt, modifier) == f"MRMS_{modifier}_20260926-0006"
    assert HttpsFileFinder(dt).construct_url(item["region"], modifier) == expected_url
    assert _mrms_modifier_label(modifier) == item["product_id"]


def test_target_raw_path_renames_are_exactly_the_ten_planned_changes():
    renames = {item["raw_basename_v1"]: item["raw_basename_v2"] for item in PRODUCTS
               if item["raw_basename_v1"] != item["raw_basename_v2"]}
    assert renames == {
        "MRMS_EchoTop18": "MRMS_EchoTop_18",
        "MRMS_EchoTop30": "MRMS_EchoTop_30",
        "MRMS_EchoTop50": "MRMS_EchoTop_50",
        "MRMS_QPE": "MRMS_RadarOnly_QPE_01H",
        "MRMS_VILDensity": "MRMS_VIL_Density",
        "MRMS_MergedReflectivityQC": "MRMS_MergedReflectivityQCComposite",
        "MRMS_ReflectivityAtLowestAltitude": "MRMS_MergedReflectivityAtLowestAltitude",
        "MRMS_ReflectivityAt0C": "MRMS_Reflectivity_0C",
        "MRMS_ReflectivityAtM5C": "MRMS_Reflectivity_-5C",
        "MRMS_ReflectivityAtM15C": "MRMS_Reflectivity_-15C",
    }


@pytest.mark.parametrize("module", [
    "config", "main", "downloader", "parse", "https_client", "s3_sync", "s3_async",
    "utils", "timestamp_utils",
])
def test_legacy_ingest_imports_share_the_implementation(module):
    assert importlib.import_module(f"EdgeWARN.ingest.mrms.{module}") is importlib.import_module(
        f"common.ingest.mrms.{module}"
    )
