from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import util.file as fs
from common.config.loader import ConfigError, load_config
from common.config.mrms_products import parse_product_id
from common.ingest.mrms import source
from common.ingest.mrms.registry import build_registry

_CONFIG_NAME = "ingest"


def _catalog():
    return load_config(_CONFIG_NAME, config_dir=fs.MRMS_CONFIG_DIR)


def get_registry():
    """Resolve v2 explicitly; v1 remains available until the coordinated release."""
    catalog = _catalog()
    if catalog["schema_version"] != 2:
        return None
    registry = build_registry(catalog["mrms"], fs.BASE_DIR.expanduser().resolve())
    if fs.MRMS_REGISTRY is None or fs.MRMS_REGISTRY.fingerprint != registry.fingerprint:
        fs.bind_mrms_paths(registry)
    return registry


def _source(modifier):
    return source.source_for(parse_product_id("MRMS_" + (modifier or "ProbSevere")))


def _resolve_outdir(attribute_name):
    """Map a catalog ``outdir`` attribute name onto the live ``util.file`` path.

    Resolved per call rather than at import so ``initialize_filesystem`` rebinds
    are picked up, and raised as ``ConfigError`` so a typo in the catalog fails
    with the offending file and key instead of a bare ``AttributeError``.
    """
    try:
        return getattr(fs, attribute_name)
    except AttributeError:
        raise ConfigError(
            f"{_CONFIG_NAME}.yaml",
            f"outdir: {attribute_name}",
            "not an attribute of util.file",
        ) from None


def mrms_bucket() -> str:
    """The S3 bucket MRMS products are read from."""
    if _catalog()["schema_version"] == 2:
        return source.MRMS_BUCKET
    return _catalog()["mrms"]["bucket"]


def mrms_decompress_chunk_size_bytes() -> int:
    """Copy length used when a gzipped MRMS grib is expanded to disk.

    Distinct from ``mrms.ncep_https.download_chunk_size_bytes``: that one sizes
    reads off the network, this one sizes a local ``copyfileobj``.
    """
    return _catalog()["mrms"]["decompress_chunk_size_bytes"]


def mrms_cleanup_max_age_minutes() -> int:
    """Retention window applied to MRMS output directories before a download."""
    return _catalog()["mrms"]["cleanup_max_age_minutes"]


def mrms_remove_old_files() -> bool:
    """Whether an ingest run prunes its output directories at all."""
    return _catalog()["mrms"]["remove_old_files"]


def _format_path_pattern(pattern: str, dt, **fields) -> str:
    """Substitute the timestamp placeholders the S3 key grammars use.

    One formatter for every pattern in the catalog, so ``{YYYYMMDD}`` cannot come
    to mean two different things in two files. ``DDD`` is the zero-padded day of
    year GOES keys use, not the day of month.
    """
    return pattern.format(
        YYYY=dt.strftime("%Y"),
        YYYYMMDD=dt.strftime("%Y%m%d"),
        DDD=dt.strftime("%j"),
        HH=dt.strftime("%H"),
        HHMM=dt.strftime("%H%M"),
        **fields,
    )


def _path_patterns():
    return _catalog()["mrms"]["path_patterns"]


def mrms_s3_prefix(dt, region, modifier) -> str:
    """The S3 key prefix for one MRMS product on one day.

    ProbSevere has no modifier segment, and the pattern for that case is a
    separate key rather than the same pattern with an empty ``{modifier}``:
    substituting empty would yield a doubled slash, which S3 treats as a
    different prefix and would silently match nothing.
    """
    if _catalog()["schema_version"] == 2:
        return _source(modifier).s3_prefix(dt)
    patterns = _path_patterns()
    if modifier is None:
        return _format_path_pattern(patterns["s3_prefix_no_modifier"], dt, region=region)
    return _format_path_pattern(patterns["s3_prefix"], dt, region=region, modifier=modifier)


def mrms_filename_prefix(dt, modifier) -> str:
    """Filename prefix appended to the day prefix to narrow a listing to one hour.

    This is the whole reason the standard products need no ``StartAfter`` marker:
    S3 prefix filtering already excludes every other hour.
    """
    if _catalog()["schema_version"] == 2:
        return _source(modifier).filename_prefix(dt)
    return _format_path_pattern(_path_patterns()["filename_prefix"], dt, modifier=modifier)


def mrms_probsevere_start_after(dt, *, lookback_hours=None) -> str:
    """The ``StartAfter`` marker filename for a ProbSevere listing.

    ProbSevere cannot use :func:`mrms_filename_prefix` -- its filenames separate
    the date and hour with an underscore where the standard products use a hyphen,
    so it needs its own grammar and an explicit marker instead.

    Returns the filename only. The caller prepends the bucket path, because the
    marker has to be a key in the same prefix it is narrowing.

    The default lookback is applied here rather than by the caller so the two
    download call sites cannot disagree about how far back the marker sits. The
    scheduler overrides it because its window is its own tunable, and passes 0
    when it has already shifted ``dt`` itself.
    """
    if _catalog()["schema_version"] == 2:
        return _source(None).filename_start_after(dt, lookback_hours=1 if lookback_hours is None else lookback_hours, minute=False)
    patterns = _path_patterns()
    if lookback_hours is None:
        lookback_hours = patterns["probsevere_start_after_lookback_hours"]
    shifted = dt - timedelta(hours=lookback_hours)
    return _format_path_pattern(patterns["probsevere_start_after"], shifted)


def mrms_filename_start_after(dt, modifier) -> str:
    """Minute-precision ``StartAfter`` marker for a standard MRMS listing.

    Distinct from :func:`mrms_filename_prefix` in precision, not in grammar: a
    prefix that narrows a listing to one hour stops at ``HH``, while a marker that
    resumes from a known file has to carry the minute or it would re-list the
    whole hour.
    """
    if _catalog()["schema_version"] == 2:
        return _source(modifier).filename_start_after(dt)
    return _format_path_pattern(
        _path_patterns()["filename_start_after_minute"], dt, modifier=modifier
    )


def mrms_probsevere_start_after_minute(dt) -> str:
    """Minute-precision ``StartAfter`` marker for a ProbSevere listing.

    The minute-precision counterpart of :func:`mrms_probsevere_start_after`, and
    like :func:`mrms_filename_start_after` it applies no lookback -- the caller
    already has the exact timestamp it wants to resume after.
    """
    if _catalog()["schema_version"] == 2:
        return _source(None).filename_start_after(dt)
    return _format_path_pattern(_path_patterns()["probsevere_start_after_minute"], dt)


def goes_bucket_path(dt, product, hour_offset: int = 0) -> str:
    """The S3 key prefix for one GOES product at one hour."""
    shifted = dt - timedelta(hours=hour_offset)
    pattern = _catalog()["goes"]["bucket_path_pattern"]
    return _format_path_pattern(pattern, shifted, product=product)


def _ncep_https():
    return _catalog()["mrms"]["ncep_https"]


def ncep_base_url() -> str:
    """Index root for the NCEP 2D HTTPS fallback.

    A function rather than the module constant it replaces: ``run.py`` imports the
    MRMS package before ``get_args()`` exports ``EDGEWARN_CONFIG_DIR``, so a
    module-scope binding put this out of reach of ``--config-dir``.
    """
    if _catalog()["schema_version"] == 2:
        return source.NCEP_BASE_URL
    return _ncep_https()["base_url"]


def ncep_probsevere_url() -> str:
    """Index root for ProbSevere, which is not under :func:`ncep_base_url`.

    ProbSevere lives at ``/data/ProbSevere``, a sibling of ``/data/2D`` rather than
    a product directory inside it, so this is a separate key and not a suffix
    appended to the base URL.
    """
    if _catalog()["schema_version"] == 2:
        return source.NCEP_PROBSEVERE_URL
    return _ncep_https()["probsevere_url"]


def ncep_sync_timeout_seconds() -> float:
    """Request timeout for the sync index scrape used by the scheduler."""
    return _ncep_https()["sync_timeout_seconds"]


def ncep_match_window_seconds() -> float:
    """How far from the requested minute a filename may sit and still be used.

    Applied only after an exact-minute match fails.
    """
    return _ncep_https()["match_window_seconds"]


def ncep_download_chunk_size_bytes() -> int:
    """Read size for the streamed HTTPS download.

    Sizes reads off the network; :func:`mrms_decompress_chunk_size_bytes` sizes a
    local gzip expansion. Two owners because they bound different resources.
    """
    return _ncep_https()["download_chunk_size_bytes"]


def ncep_directory_split_token() -> str:
    """Token the product name is split on when it is absent from the map."""
    return _ncep_https()["directory_split_token"]


def ncep_directory_map():
    """S3 modifier to NCEP directory name, for the names that do not derive."""
    if _catalog()["schema_version"] == 2:
        return source.HTTPS_DIRECTORY_EXCEPTIONS
    return _ncep_https()["directory_map"]


def goes_bucket() -> str:
    """The S3 bucket GOES products are read from."""
    return _catalog()["goes"]["bucket"]


def goes_cleanup_max_age_minutes() -> int:
    """Retention window applied to GOES spec output directories.

    A separate owner from :func:`mrms_cleanup_max_age_minutes` even though both
    are 60 today: GOES cleanup also enforces ``max_files`` per spec, so the two
    windows bound different retention policies and can legitimately diverge.
    """
    return _catalog()["goes"]["cleanup_max_age_minutes"]


def goes_hour_lookback() -> int:
    """How many hourly bucket prefixes a GOES lookup walks back through."""
    return _catalog()["goes"]["hour_lookback"]


def abi_radc_product() -> str:
    """The ABI product every RadC channel spec is built against."""
    return _catalog()["goes"]["abi_product"]


def _abi_channel_definitions():
    return tuple(
        (channel["id"], channel["name"], channel["outdir"])
        for channel in _catalog()["goes"]["abi_channels"]
    )


def default_abi_radc_channel_ids() -> tuple:
    """Every ABI channel id in the catalog, in catalog order."""
    return tuple(channel_id for channel_id, _, _ in _abi_channel_definitions())


def goes_max_files_per_spec() -> int:
    """The retained-file count cap applied per GOES spec during cleanup."""
    return _catalog()["goes"]["max_files_per_spec"]


_FROM_CATALOG = object()


@dataclass(frozen=True)
class GoesIngestSpec:
    product: str
    outdir: Path
    channel_id: str | None = None
    channel_name: str | None = None
    filename_matcher: str | None = None
    # Resolved in __post_init__, not as a field default: a default is evaluated at
    # class-definition time, which is before get_args() exports EDGEWARN_CONFIG_DIR,
    # so --config-dir could never reach it. A distinct sentinel rather than None
    # because None is a meaningful value for a count cap -- it means "no cap" to
    # util.file.clean_old_files.
    max_files: int | None = _FROM_CATALOG

    def __post_init__(self):
        if self.max_files is _FROM_CATALOG:
            object.__setattr__(self, "max_files", goes_max_files_per_spec())

    @property
    def label(self) -> str:
        return self.channel_name or self.channel_id or self.product

    @property
    def is_glm(self) -> bool:
        return self.channel_id is None and "GLM" in self.product


def _catalog_triples(key):
    return [
        (entry["region"], entry["product"], _resolve_outdir(entry["outdir"]))
        for entry in _catalog()["mrms"][key]
    ]


def get_mrms_modifiers():
    """The full MRMS ingest catalog as (region, product, outdir) triples."""
    registry = get_registry()
    if registry is not None:
        return list(registry.get_mrms_modifiers())
    return _catalog_triples("products")


def get_check_modifiers():
    """The readiness-check subset of :func:`get_mrms_modifiers`."""
    registry = get_registry()
    if registry is not None:
        return list(registry.get_check_modifiers())
    return _catalog_triples("check_products")


def get_goes_modifiers():
    goes = _catalog()["goes"]
    return [
        GoesIngestSpec(goes["glm_product"], _resolve_outdir(goes["glm_outdir"])),
        *get_abi_radc_channel_specs(),
    ]


def get_goes_max_entries():
    """The S3 listing depth for every GOES lookup."""
    return _catalog()["goes"]["max_entries"]


def get_abi_radc_channel_specs(channel_ids=None):
    """Build a spec per ABI channel, optionally restricted to ``channel_ids``.

    ``None`` means every channel in the catalog. It used to mean that indirectly,
    via a signature default of ``DEFAULT_ABI_RADC_CHANNEL_IDS`` -- which selected
    every catalog channel and so produced the same result, but bound the channel
    list at import time and put it out of reach of ``--config-dir``.
    """
    selected_channel_ids = set(channel_ids) if channel_ids is not None else None
    specs = []
    product = abi_radc_product()
    matcher_template = _catalog()["goes"]["filename_matcher_template"]

    for channel_id, channel_name, outdir_attr in _abi_channel_definitions():
        if selected_channel_ids is not None and channel_id not in selected_channel_ids:
            continue

        specs.append(
            GoesIngestSpec(
                product=product,
                outdir=_resolve_outdir(outdir_attr),
                channel_id=channel_id,
                channel_name=channel_name,
                filename_matcher=matcher_template.format(channel_id=channel_id),
            )
        )

    return specs


def normalize_goes_modifier(spec):
    if isinstance(spec, GoesIngestSpec):
        return spec

    if isinstance(spec, tuple) and len(spec) == 2:
        product, outdir = spec
        return GoesIngestSpec(product=product, outdir=outdir)

    raise TypeError(f"Unsupported GOES modifier specification: {spec!r}")
