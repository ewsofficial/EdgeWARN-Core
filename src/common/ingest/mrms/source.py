"""Code-owned source grammars. Formatting only; no acquisition or config I/O."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from common.config.mrms_products import MrmsProductIdentity

SOURCE_CONTRACT_VERSION = 1
MRMS_BUCKET = "noaa-mrms-pds"
NCEP_BASE_URL = "https://mrms.ncep.noaa.gov/data/2D"
NCEP_PROBSEVERE_URL = "https://mrms.ncep.noaa.gov/data/ProbSevere"
# All existing standard mappings derive from the parsed base. Future exceptional
# remote directories belong here, never in operator config or local path logic.
HTTPS_DIRECTORY_EXCEPTIONS = MappingProxyType({})


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("MRMS source timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class MrmsSource:
    region: str
    adapter: str
    source_modifier: str | None
    https_directory: str | None

    @property
    def bucket(self):
        return MRMS_BUCKET

    @property
    def https_url(self):
        if self.adapter == "probsevere_json":
            return NCEP_PROBSEVERE_URL
        return f"{NCEP_BASE_URL}/{self.https_directory}"

    def s3_prefix(self, dt):
        segment = f"{self.region}/"
        if self.source_modifier is not None:
            segment += f"{self.source_modifier}/"
        return segment + _utc(dt).strftime("%Y%m%d/")

    def filename_prefix(self, dt):
        dt = _utc(dt)
        if self.adapter == "probsevere_json":
            return dt.strftime("MRMS_PROBSEVERE_%Y%m%d_%H")
        return f"MRMS_{self.source_modifier}_" + dt.strftime("%Y%m%d-%H")

    def filename_start_after(self, dt, *, lookback_hours=0, minute=True):
        dt = _utc(dt) - timedelta(hours=lookback_hours)
        if self.adapter == "probsevere_json":
            return dt.strftime("MRMS_PROBSEVERE_%Y%m%d_" + ("%H%M" if minute else "%H"))
        return f"MRMS_{self.source_modifier}_" + dt.strftime(
            "%Y%m%d-" + ("%H%M" if minute else "%H")
        )

    def listing_bounds(self, dt):
        """Return the existing hourly prefix / previous-hour ProbSevere marker."""
        prefix = self.s3_prefix(dt)
        if self.adapter == "probsevere_json":
            return prefix, prefix + self.filename_start_after(
                dt, lookback_hours=1, minute=False
            )
        return prefix + self.filename_prefix(dt), None


def source_for(identity: MrmsProductIdentity) -> MrmsSource:
    if identity.product_id == "ProbSevere":
        return MrmsSource("ProbSevere", "probsevere_json", None, None)
    return MrmsSource(
        "CONUS", "conus_grib2", identity.product_id,
        HTTPS_DIRECTORY_EXCEPTIONS.get(identity.product_id, identity.base_product),
    )
