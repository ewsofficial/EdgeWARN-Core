"""Release-owned Core requirements, independent of operator configuration."""
from dataclasses import asdict, dataclass
import json
from types import MappingProxyType

from common.config.mrms_products import parse_product_id

CORE_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class CoreProduct:
    configured_id: str
    region: str
    adapter: str
    source_modifier: str | None
    core_phase: str = "detection"
    discovery: bool = True
    required: bool = True

    @property
    def product_id(self):
        return parse_product_id(self.configured_id).product_id

    @property
    def path_name(self):
        return parse_product_id(self.configured_id).path_name


CORE_PRODUCTS = (
    CoreProduct("MRMS_MergedReflectivityQCComposite_00.50", "CONUS", "conus_grib2",
                "MergedReflectivityQCComposite_00.50"),
    CoreProduct("MRMS_PrecipFlag_00.00", "CONUS", "conus_grib2", "PrecipFlag_00.00"),
    CoreProduct("MRMS_ProbSevere", "ProbSevere", "probsevere_json", None),
)
PROTECTED_IDS = frozenset(item.product_id for item in CORE_PRODUCTS)
DISCOVERY_IDS = tuple(item.product_id for item in CORE_PRODUCTS)

# Compatibility names only, never an enabled-product catalog. Supported through
# 3.x; earliest removal is 4.0 with a deprecation notice.
LEGACY_ALIASES = MappingProxyType({
    "MRMS_COMPOSITE_DIR": "MergedReflectivityQCComposite_00.50",
    "MRMS_PRECIPTYP_DIR": "PrecipFlag_00.00",
    "MRMS_PROBSEVERE_DIR": "ProbSevere",
    "MRMS_ECHOTOP18_DIR": "EchoTop_18_00.50",
    "MRMS_ECHOTOP30_DIR": "EchoTop_30_00.50",
    "MRMS_ECHOTOP50_DIR": "EchoTop_50_00.50",
    "MRMS_RQI_DIR": "RadarQualityIndex_00.00",
    "MRMS_MESH_DIR": "MESH_00.50",
    "MRMS_NLDN_DIR": "NLDN_CG_001min_AvgDensity_00.00",
    "MRMS_PRECIPRATE_DIR": "PrecipRate_00.00",
    "MRMS_QPE_DIR": "RadarOnly_QPE_01H_00.00",
    "MRMS_AZSHEARLOW_DIR": "MergedAzShear_0-2kmAGL_00.50",
    "MRMS_AZSHEARMID_DIR": "MergedAzShear_3-6kmAGL_00.50",
    "MRMS_DVIL_DIR": "VIL_Density_00.50",
    "MRMS_RHOHV_DIR": "MergedRhoHV_00.50",
    "MRMS_RALA_DIR": "MergedReflectivityAtLowestAltitude_00.50",
    "MRMS_VII_DIR": "VII_00.50",
    "MRMS_VIL_DIR": "VIL_00.50",
    "MRMS_REF_0C_DIR": "Reflectivity_0C_00.50",
    "MRMS_REFM5C_DIR": "Reflectivity_-5C_00.50",
    "MRMS_REFM15C_DIR": "Reflectivity_-15C_00.50",
})


def contract_document():
    """Serializable release asset consumed by Node's future v2 validator."""
    return {
        "contract_version": CORE_CONTRACT_VERSION,
        "products": [
            {**asdict(item), "product_id": item.product_id, "path_name": item.path_name}
            for item in CORE_PRODUCTS
        ],
    }


def contract_json():
    return json.dumps(contract_document(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    # Explicit generation only: redirect stdout to core-contract.json.
    print(contract_json(), end="")


@dataclass(frozen=True)
class IngestDependencies:
    """Frozen producer/consumer agreement; IDs are full manifest identities.

    Optional completion is a terminal acquisition boundary, not an all-success
    gate. The final CTAM snapshot extends the pinned integration snapshot with
    aligned optional current inputs and validated previous history. It never
    reselects detection. StormProb's fatal input checks still run on that snapshot.
    """
    check: tuple[str, ...]
    detection: tuple[str, ...]
    mandatory_integration: tuple[str, ...]
    optional: tuple[str, ...]
    enrichment: tuple[str, ...]
    previous_detection: tuple[str, ...]
    previous_optional: tuple[str, ...]
    rap_enabled: bool
    glm_enabled: bool
    optional_timeout_seconds: float
    registry_fingerprint: str
    auxiliary_settings_json: str = "{}"
    final_snapshot: str = "pinned-integration-plus-terminal-optional-and-history"
    history_policy: str = "latest-valid-strictly-earlier-if-available"
    stormprob_missing_inputs: str = "fatal-when-ctam-and-stormprob-enabled"

    @property
    def fingerprint(self):
        import hashlib
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


def resolve_dependencies(registry, *, check=None, detection=None,
                         mandatory_integration=None, enrichment=(),
                         include_rap=True, include_glm=True, mrms_core_only=False,
                         disable_goes=False, auxiliary_settings=None):
    """Pure preflight, also usable with explicit future dependency selections."""
    enabled = {p.product_id for p in registry.products}
    def canonical(values):
        # Only the legacy null ProbSevere modifier is normalized. Short product
        # aliases must not accidentally match a different elevation.
        return tuple(sorted({"ProbSevere" if p is None else p for p in values}))
    check = canonical(check if check is not None else
                      (p.product_id for p in registry.products if p.discovery))
    detection = canonical(detection if detection is not None else
                          (p.product_id for p in registry.for_phase("detection")))
    mandatory = canonical(mandatory_integration if mandatory_integration is not None else
                          (p.product_id for p in registry.for_phase("integration") if p.required))
    enrichment = canonical(enrichment)
    validate_dependency_sets(enabled, check, detection, mandatory, enrichment)
    optional = tuple(sorted(enabled - set(detection) - set(mandatory)))
    settings = json.loads(registry.normalized_config_json)
    return IngestDependencies(check, detection, mandatory, optional, enrichment,
                              detection, optional,
                              bool(include_rap and not mrms_core_only),
                              bool(include_glm and not mrms_core_only and not disable_goes),
                              settings['downloads']['optional_timeout_seconds'],
                              registry.fingerprint,
                              json.dumps(auxiliary_settings or {}, sort_keys=True,
                                         separators=(",", ":")))


def validate_dependency_sets(enabled, check, detection, mandatory=(), enrichment=()):
    """Validate membership without requiring resource configuration or I/O."""
    if not check:
        raise ValueError("Ingest check set must not be empty")
    missing = (set(check) | set(detection) | set(mandatory) | set(enrichment)) - enabled
    if missing:
        raise ValueError(f"Ingest dependencies are disabled: {sorted(missing)}")
    if not set(detection) <= set(check):
        raise ValueError("Detection products must be included in the ingest check set")
