"""Phase 0 characterization of the product/data-source catalogs.

Each catalog is snapshotted in full, and its length is asserted separately.
The plan identifies a silent entry drop during transcription as the most
likely regression, so a bare length check earns its keep next to the snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import util.file as fs

from tests.architecture.baseline import assert_baseline, requires

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- MRMS / GOES ingest ----------------------------------------------------

def test_mrms_ingest_catalog_baseline():
    from common.ingest.mrms.config import get_mrms_modifiers

    assert_baseline("mrms_ingest_catalog", get_mrms_modifiers())


def test_mrms_ingest_catalog_length():
    from common.ingest.mrms.config import get_mrms_modifiers

    assert len(get_mrms_modifiers()) == 21


def test_mrms_readiness_catalog_baseline():
    from common.ingest.mrms.config import get_check_modifiers

    assert_baseline("mrms_readiness_catalog", get_check_modifiers())


def test_mrms_readiness_catalog_length():
    from common.ingest.mrms.config import get_check_modifiers

    assert len(get_check_modifiers()) == 3


def test_rala_remains_ingested_but_is_not_a_readiness_check():
    from common.ingest.mrms.config import get_check_modifiers, get_mrms_modifiers

    rala = ("CONUS", "MergedReflectivityAtLowestAltitude_00.50", fs.MRMS_RALA_DIR)
    assert rala not in get_check_modifiers()
    assert rala in get_mrms_modifiers()


def test_mrms_readiness_is_subset_of_ingest():
    """The two lists are maintained separately; nothing enforces this today."""
    from common.ingest.mrms.config import get_check_modifiers, get_mrms_modifiers

    ingest = set(get_mrms_modifiers())
    missing = [entry for entry in get_check_modifiers() if entry not in ingest]
    assert missing == []


def test_goes_ingest_catalog_baseline():
    from common.ingest.mrms.config import get_goes_modifiers

    assert_baseline("goes_ingest_catalog", get_goes_modifiers())


def test_mrms_goes_bucket_baseline():
    from common.ingest.mrms import config as mrms_config

    assert_baseline(
        "mrms_goes_buckets",
        {
            "bucket": mrms_config.mrms_bucket(),
            "goes_bucket": mrms_config.goes_bucket(),
        },
    )


# --- Derived MRMS memberships ---------------------------------------------


def test_detection_modifiers_exist_in_ingest_catalog():
    requires("xarray")
    from common.ingest.mrms.config import get_mrms_modifiers
    from common.ingest.mrms.main import get_detection_modifiers

    ingest_modifiers = {modifier for _, modifier, _ in get_mrms_modifiers()}
    missing = [m for m in get_detection_modifiers() if m not in ingest_modifiers]
    assert missing == []


def test_detection_and_integration_partition_the_ingest_catalog():
    requires("xarray")
    from common.ingest.mrms.config import get_mrms_modifiers
    from common.ingest.mrms.main import get_detection_modifiers, get_integration_modifiers

    ingest_modifiers = [modifier for _, modifier, _ in get_mrms_modifiers()]
    detection = set(get_detection_modifiers())
    integration = set(get_integration_modifiers())

    assert detection & integration == set()
    assert detection | integration == set(ingest_modifiers)


# --- MRMS integration statistics ------------------------------------------

def test_integration_datasets_baseline():
    requires("xarray")
    from EdgeWARN.process.integrate.config import get_datasets_config

    assert_baseline("integration_datasets", get_datasets_config())


def test_integration_datasets_length_and_unique_keys():
    requires("xarray")
    from EdgeWARN.process.integrate.config import get_datasets_config

    datasets = get_datasets_config()
    assert len(datasets) == 25

    keys = [entry["key"] for entry in datasets]
    assert len(keys) == len(set(keys))


def test_every_integration_dataset_declares_its_method():
    """RESOLVED (Phase 4): Ref15 no longer inherits an invisible "max".

    It used to omit ``method`` and pick the reduction up from a
    ``conf.get("method", "max")`` default inside ``prepare_stats_specs``, so the
    reduction a dataset used was not visible where the dataset was declared.
    ``integration.yaml`` now requires the key on all 25 entries -- the resolved
    value for Ref15 is unchanged.
    """
    requires("xarray")
    from EdgeWARN.process.integrate.config import get_datasets_config

    datasets = get_datasets_config()
    assert [e["name"] for e in datasets if "method" not in e] == []
    assert next(e for e in datasets if e["name"] == "Ref15")["method"] == "max"

    source = (REPO_ROOT / "src/EdgeWARN/process/integrate/core/stats.py").read_text(encoding="utf-8")
    assert '.get("method"' not in source


# --- RAP integration ------------------------------------------------------

def test_rap_integration_products_baseline():
    requires("xarray")
    from EdgeWARN.process.integrate.config import get_rap_products

    assert_baseline("rap_integration_products", get_rap_products())


def test_rap_integration_products_shape():
    requires("xarray")
    from EdgeWARN.process.integrate.config import get_rap_products

    catalog = get_rap_products()
    assert len(catalog["products"]) == 7
    assert len(catalog["derived"]) == 2

    isobaric = [p for p in catalog["products"] if "levels" in p]
    assert len(isobaric) == 2
    for product in isobaric:
        assert len(product["levels"]) == 37


def test_rap_transform_names_resolve_in_registry():
    """`TRANSFORMS.get(name, identity)` means an unknown name fails silently."""
    requires("xarray")
    from EdgeWARN.process.integrate.config import get_rap_products
    from EdgeWARN.process.integrate.integrate_rap import TRANSFORMS

    used = {p["transform"] for p in get_rap_products()["products"] if "transform" in p}
    assert used <= set(TRANSFORMS)


def test_rap_transform_registry_baseline():
    requires("xarray")
    from EdgeWARN.process.integrate.integrate_rap import TRANSFORMS

    assert_baseline("rap_transform_registry", sorted(TRANSFORMS))


def test_rap_derived_formulas_parse_under_the_safe_grammar():
    """Every catalog formula passes the same check production runs before extraction."""
    requires("xarray")
    from EdgeWARN.process.integrate.config import get_rap_products
    from EdgeWARN.process.integrate.integrate_rap import _compile_derived, _safe_eval_formula

    variables = {"temp_2m": 20.0, "dewpoint_2m": 10.0, "freezing_level_m": 3000.0}
    results = {
        key: _safe_eval_formula(expression, variables)
        for key, expression in (
            _compile_derived(entry) for entry in get_rap_products()["derived"]
        )
    }

    assert results == {"dewpoint_depression": 10.0, "freezing_level_height": 3.0}


# --- NEXRAD ---------------------------------------------------------------

def test_nexrad_config_baseline():
    """Snapshots accessor return values, not module constants.

    Phase 5 replaced the uppercase constants with accessors so `--config-dir`
    could reach the NEXRAD child, which reads its catalog before
    `EDGEWARN_CONFIG_DIR` is exported. Discovery is by zero-argument signature
    rather than an explicit list, so a new accessor joins the snapshot without
    anyone remembering to add it; `format_perf_ms` takes a required argument
    and so drops out on its own.
    """
    import inspect

    from common.ingest.nexrad import config as nexrad_config

    accessors = {}
    for name in sorted(vars(nexrad_config)):
        if name.startswith("_"):
            continue
        value = getattr(nexrad_config, name)
        if not inspect.isfunction(value) or value.__module__ != nexrad_config.__name__:
            continue
        required = [
            parameter
            for parameter in inspect.signature(value).parameters.values()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
        ]
        if required:
            continue
        accessors[name] = value()

    assert accessors, "accessor discovery found nothing -- the module shape changed"
    assert_baseline("nexrad_config", accessors)


@pytest.mark.parametrize(
    ("module_name", "needs"),
    [
        ("common.ingest.mrms.config", ()),
        ("common.ingest.nexrad.config", ()),
        # Pure apart from `util.file`, but gated behind EdgeWARN/__init__.py.
        ("EdgeWARN.process.integrate.config", ("xarray",)),
    ],
)
def test_catalog_modules_import_standalone(module_name, needs):
    """Guards the loader work: these modules must stay importable on their own."""
    import importlib

    requires(*needs)
    assert importlib.import_module(module_name) is not None


# --- WPC surface-analysis styling ------------------------------------------


# --- Node API product catalog ("API mappings") -----------------------------
