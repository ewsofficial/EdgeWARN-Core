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


@dataclass(frozen=True)
class DiscoveredObject:
    """An exact upstream observation, not a request for the latest scan."""
    product_id: str
    observation_time: datetime
    source: str
    locator: str
    remote_version: str | None = None
    remote_version_kind: str = "etag"

    def __post_init__(self):
        from common.config.mrms_products import parse_product_id
        from common.ingest.manifest import parse_file_analysis_time
        from urllib.parse import urlsplit
        if self.remote_version_kind not in {"etag", "version_id"}:
            raise ValueError("Unsupported remote version kind")
        identity = parse_product_id("MRMS_" + self.product_id)
        source = source_for(identity)
        stamp = _utc(self.observation_time)
        name = self.locator.rsplit("/", 1)[-1]
        expected = "MRMS_PROBSEVERE_" if source.adapter == "probsevere_json" else f"MRMS_{self.product_id}_"
        suffixes = (".json", ".json.gz") if source.adapter == "probsevere_json" else (".grib2", ".grib2.gz")
        if (not name.startswith(expected) or not name.endswith(suffixes)
                or "\\" in self.locator or parse_file_analysis_time(name) != stamp):
            raise ValueError("Object name/product/encoded observation time mismatch")
        if self.source == "s3":
            if self.locator != source.s3_prefix(stamp) + name:
                raise ValueError("Unexpected MRMS S3 source locator")
        elif self.source == "https":
            url = urlsplit(self.locator)
            if self.locator != source.https_url + "/" + name or url.query or url.fragment:
                raise ValueError("Unexpected MRMS HTTPS source locator")
        else:
            raise ValueError("Unsupported MRMS source transport")
        object.__setattr__(self, "observation_time", stamp)

    @property
    def logical_identity(self):
        """Mirror-independent candidate key; content is compared after validation."""
        return (self.product_id, self.observation_time.isoformat())

    @property
    def acquisition_identity(self):
        return (*self.logical_identity, self.source, self.locator,
                self.remote_version_kind, self.remote_version)

    def mirror(self, transport):
        from common.config.mrms_products import parse_product_id
        source = source_for(parse_product_id("MRMS_" + self.product_id))
        name = self.locator.rsplit("/", 1)[-1]
        locator = (source.s3_prefix(self.observation_time) if transport == "s3"
                   else source.https_url + "/") + name
        return DiscoveredObject(self.product_id, self.observation_time, transport, locator)
