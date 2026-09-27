"""Immutable effective MRMS registry, built explicitly before runtime binding."""
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType

from common.config.mrms_products import normalize_products
from common.ingest.mrms.core_contract import (
    CORE_CONTRACT_VERSION, CORE_PRODUCTS, LEGACY_ALIASES,
)
from common.ingest.mrms.source import SOURCE_CONTRACT_VERSION, source_for


@dataclass(frozen=True)
class MrmsProductSpec:
    configured_id: str
    product_id: str
    source_modifier: str | None
    region: str
    adapter: str
    path_name: str
    directory: Path
    protected: bool
    core_phase: str | None
    discovery: bool
    required: bool


@dataclass(frozen=True)
class MrmsRegistry:
    base_dir: Path
    products: tuple[MrmsProductSpec, ...]
    normalized_config_json: str
    fingerprint: str
    contract_version: int = CORE_CONTRACT_VERSION

    def is_enabled(self, product_id: str) -> bool:
        return any(item.product_id == product_id for item in self.products)

    def require(self, product_id: str) -> MrmsProductSpec:
        """Lookup by full upstream/manifest ID, including elevation."""
        for item in self.products:
            if item.product_id == product_id:
                return item
        raise KeyError(f"MRMS product {product_id!r} is not enabled")

    def path_for(self, product_id: str) -> Path:
        return self.require(product_id).directory

    def for_phase(self, phase: str) -> tuple[MrmsProductSpec, ...]:
        return tuple(item for item in self.products if item.core_phase == phase)

    def paths_by_name(self) -> Mapping[str, Path]:
        return MappingProxyType({item.path_name: item.directory for item in self.products})

    def legacy_paths(self) -> Mapping[str, Path]:
        return MappingProxyType({
            alias: self.path_for(product_id)
            for alias, product_id in LEGACY_ALIASES.items() if self.is_enabled(product_id)
        })

    def get_mrms_modifiers(self):
        """Compatibility triples, derived exclusively from this registry."""
        return tuple((p.region, p.source_modifier, p.directory) for p in self.products)

    def get_check_modifiers(self):
        return tuple((p.region, p.source_modifier, p.directory)
                     for p in self.products if p.discovery)


def _plain(value):
    """Copy frozen catalog mappings/tuples into canonical JSON-compatible data."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("MRMS configuration keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(f"Non-JSON MRMS configuration value: {type(value).__name__}")


def build_registry(mrms_config: Mapping, base_dir: Path) -> MrmsRegistry:
    """Build from an already validated v2 MRMS section and resolved base path.

    Does not load configuration, resolve symlinks, touch directories or query
    upstream. Runtime binding must verify symlink containment in phase 4.
    All MRMS settings participate in the fingerprint; membership order and
    redundant inclusion of a reserved product do not change it.
    """
    base_dir = Path(base_dir)
    if not base_dir.is_absolute():
        raise ValueError("MRMS registry requires an absolute, resolved base directory")
    base_dir = Path(os.path.normpath(base_dir))
    if not isinstance(mrms_config, Mapping) or "products" not in mrms_config:
        raise ValueError("MRMS registry requires a v2 section with products")
    identities = normalize_products(
        mrms_config["products"],
        protected_ids=tuple(item.configured_id for item in CORE_PRODUCTS),
    )
    protected = {item.product_id: item for item in CORE_PRODUCTS}
    specs = []
    for identity in sorted(identities, key=lambda item: item.product_id):
        source = source_for(identity)
        core = protected.get(identity.product_id)
        specs.append(MrmsProductSpec(
            configured_id=identity.configured_id, product_id=identity.product_id,
            source_modifier=core.source_modifier if core else source.source_modifier,
            region=core.region if core else source.region,
            adapter=core.adapter if core else source.adapter,
            path_name=identity.path_name,
            directory=base_dir / "data" / identity.path_name,
            protected=core is not None,
            core_phase=core.core_phase if core else None,
            discovery=core.discovery if core else False,
            required=core.required if core else False,
        ))
    normalized = _plain(mrms_config)
    normalized["products"] = [item.configured_id for item in specs]
    config_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload = {
        "contract_version": CORE_CONTRACT_VERSION,
        "source_contract_version": SOURCE_CONTRACT_VERSION,
        "config": normalized,
        "products": [{**asdict(item), "directory": str(item.directory)} for item in specs],
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return MrmsRegistry(base_dir, tuple(specs), config_json, fingerprint)
