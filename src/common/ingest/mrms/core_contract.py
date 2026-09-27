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
