"""Integration catalogs and policy read from ``config/integration.yaml``.

``section()`` is memoized because ``load_config`` re-resolves the config root on
every call and ``output.decimals`` is read from inside per-cell loops. Directory
names are deliberately *not* memoized: they are resolved through ``getattr`` per
call so ``initialize_filesystem`` rebinds are picked up.
"""
from collections.abc import Mapping
from functools import lru_cache

import util.file as fs
from common.config.loader import ConfigError, load_config
from common.ingest.mrms.core_contract import LEGACY_ALIASES

_CONFIG_NAME = "integration"
_DISABLED_DIAGNOSTICS = set()


@lru_cache(maxsize=None)
def section(name, config_dir=None):
    """Frozen view of one top-level section of ``integration.yaml``."""
    return load_config(_CONFIG_NAME, config_dir=config_dir or fs.MRMS_CONFIG_DIR)[name]


def reset_cache():
    """Clear memoized sections. Intended for tests, alongside loader.reset_cache."""
    section.cache_clear()
    _DISABLED_DIAGNOSTICS.clear()


def output_decimals(config_dir=None):
    """Decimal places every integrated property value is rounded to."""
    return section("output", config_dir)["decimals"]


def probsevere_field_map(config_dir=None):
    """output_property -> ProbSevere source field.

    Neither the casing nor the abbreviation is mechanical, so the mapping cannot
    be generated from either side. Read per call rather than bound at module
    scope: ``EdgeWARN/pipeline.py`` imports the integrator transitively from
    ``src/run.py:14``, before ``get_args()`` exports ``EDGEWARN_CONFIG_DIR``.
    """
    return section("probsevere_field_map", config_dir)


def _resolve_dir(attribute_name):
    try:
        return getattr(fs, attribute_name)
    except AttributeError:
        raise ConfigError(
            f"{_CONFIG_NAME}.yaml",
            f"stats_datasets.filepath: {attribute_name}",
            "not an attribute of util.file",
        ) from None


def get_datasets_config(*, include_inactive=False):
    datasets = []
    ingest = load_config("ingest", config_dir=fs.MRMS_CONFIG_DIR)
    registry = None
    if ingest["schema_version"] == 2:
        from common.ingest.mrms.config import get_registry
        registry = fs.MRMS_REGISTRY or get_registry()
    for entry in section("stats_datasets"):
        if registry is not None:
            product = entry.get("product") or LEGACY_ALIASES.get(entry.get("filepath"))
            if product is None:
                raise ConfigError(
                    f"{_CONFIG_NAME}.yaml", f"stats_datasets: {entry['name']}",
                    "requires an MRMS product identity or a supported legacy filepath",
                )
            active = registry.is_enabled(product)
            if not active:
                diagnostic = (registry.fingerprint, entry["name"], product)
                if diagnostic not in _DISABLED_DIAGNOSTICS:
                    from util.io import IOManager
                    IOManager("[CellIntegration]").write_warning(
                        f"Statistic {entry['name']} inactive: {product} ingestion disabled; "
                        f"enable MRMS_{product} in ingest.yaml mrms.products to use it")
                    _DISABLED_DIAGNOSTICS.add(diagnostic)
                if not include_inactive:
                    continue
            filepath = registry.path_for(product) if active else None
        else:
            filepath = _resolve_dir(entry["filepath"])
        dataset = {
            "name": entry["name"],
            "filepath": filepath,
            "key": entry["key"],
            "method": entry["method"],
        }
        if "percentile" in entry:
            dataset["percentile"] = entry["percentile"]
        if registry is not None:
            dataset.update(product=product, active=active, reason=None if active else "ingestion-disabled")
        datasets.append(dataset)
    return datasets


def _thaw(value):
    """Deep-copy a frozen config value back into plain dicts and lists.

    ``RAPPointExtractor`` and the apply loop treat these entries as ordinary
    data, and ``copy.deepcopy`` cannot copy a ``MappingProxyType``.
    """
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def get_rap_products():
    """Configuration for RAP GRIB2 extraction.

    ``isobaric_levels_mb`` is an anchor the u/v products expand at parse time,
    so it is not part of the returned catalog.
    """
    rap = section("rap_products")
    return {
        "products": _thaw(rap["products"]),
        "derived": _thaw(rap["derived"]),
    }
