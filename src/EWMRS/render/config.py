import util.file as fs
from common.config.loader import ConfigError, load_config

_CONFIG_NAME = "ewmrs_render"


def _render_config():
    return load_config(_CONFIG_NAME, config_dir=fs.MRMS_CONFIG_DIR)


def _resolve_dir(attribute_name):
    """Map a catalog directory attribute name onto the live ``util.file`` path.

    Resolved per call so ``initialize_filesystem`` rebinds are picked up, and
    raised as ``ConfigError`` so a typo in the catalog names the offending key.
    """
    try:
        return getattr(fs, attribute_name)
    except AttributeError:
        raise ConfigError(
            f"{_CONFIG_NAME}.yaml",
            f"outdir/filepath: {attribute_name}",
            "not an attribute of util.file",
        ) from None


def goes_transform_resampling():
    """The ``rasterio`` resampling method for the GOES ABI reprojection.

    Returns the enum member rather than its name so no caller has to repeat the
    lookup, and read per call so ``--config-dir`` can reach it -- ``run.py``
    imports the render package before ``get_args()`` exports
    ``EDGEWARN_CONFIG_DIR``.

    Owns the GOES ABI path only. ``EWMRS/pipeline.py`` reprojects the non-GOES
    layers with ``nearest`` on purpose, to keep radar edges crisp; that is a
    different policy and deliberately not this key.
    """
    from rasterio.enums import Resampling
    from rasterio.warp import SUPPORTED_RESAMPLING

    name = _render_config()["goes_transform"]["resampling"]
    try:
        method = Resampling[name]
    except KeyError:
        raise ConfigError(
            f"{_CONFIG_NAME}.yaml",
            f"goes_transform.resampling: {name}",
            "not a rasterio.enums.Resampling member",
        ) from None

    # The schema enum already excludes `gauss`, but rasterio decides what warp
    # accepts and the two lists are maintained by different projects.
    if method not in SUPPORTED_RESAMPLING:
        raise ConfigError(
            f"{_CONFIG_NAME}.yaml",
            f"goes_transform.resampling: {name}",
            "not supported for reprojection by this rasterio build",
        )
    return method


def tile_size() -> int:
    """Chunk edge length, resolved after entry points select a config root."""
    return _render_config()["tiles"]["tile_size"]


def chunk_schema_version() -> int:
    """Schema version written into both EWMRS chunk index levels."""
    return _render_config()["chunk_format"]["wire_version"]


def __getattr__(name):
    """Resolve legacy constant imports only when the attribute is requested."""
    if name == "TILE_SIZE":
        return tile_size()
    if name == "CHUNK_SCHEMA_VERSION":
        return chunk_schema_version()
    chunk_keys = {
        "CHUNK_FORMAT_VERSION": "format_version",
        "CHUNK_ENCODING": "encoding",
        "CHUNK_MEDIA_TYPE": "media_type",
        "CHUNK_FILE_SUFFIX": "file_suffix",
        "CHUNK_COMPRESSION": "compression",
        "CHUNK_BYTES_PER_COMPONENT": "bytes_per_component",
        "CHUNK_PIXEL_ROW_ORDER": "pixel_row_order",
        "CHUNK_GRID_ORIGIN": "grid_origin",
    }
    try:
        return _render_config()["chunk_format"][chunk_keys[name]]
    except KeyError:
        raise AttributeError(name) from None


def chunk_format_descriptor(*, include_media_type: bool = False) -> dict:
    """Return the JSON-serializable float16 value-chunk contract.

    EWMRS serves raw single-channel science values; derived color products
    (for example GOES RGB composites) are a client-side concern.
    """
    chunk = _render_config()["chunk_format"]
    value = {
        "version": chunk["format_version"],
        "encoding": chunk["encoding"],
        "file_suffix": chunk["file_suffix"],
        "compression": chunk["compression"],
        "data_type": chunk["data_type"],
        "channels": chunk["channels"],
        "value_kind": chunk["value_kind"],
        "no_data": chunk["no_data"],
        "bytes_per_component": chunk["bytes_per_component"],
        "pixel_row_order": chunk["pixel_row_order"],
        "grid_origin": chunk["grid_origin"],
    }
    if include_media_type:
        value["media_type"] = chunk["media_type"]
    return value

def nexrad_variable_colormaps() -> dict:
    """Map each served radar moment to the colormap the GUI draws it with.

    Resolved per call rather than bound at module scope: this package is imported
    before ``get_args()`` exports ``EDGEWARN_CONFIG_DIR``, so an import-time read
    would freeze the repo-default config directory and ``--config-dir`` could
    never reach it.

    Hoist the result out of per-sweep and per-moment loops. ``load_config`` stats
    the catalog on every call, cache hit included, so calling this once per
    rendered moment is a measurable cost rather than a style question.

    A moment absent from this mapping is still served by the API; it simply has no
    colormap, so the GUI does not draw it. ``CCORH`` is exactly that case.
    """
    return dict(_render_config()["nexrad_gui"]["variable_colormaps"])


def get_mrms_file_list(*, include_inactive=False):
    """Resolve eligible layers; diagnostic entries never resolve disabled paths."""
    from common.ingest.mrms.config import get_registry
    from common.ingest.mrms.core_contract import LEGACY_ALIASES

    registry = get_registry()
    result = []
    for layer in _render_config()["mrms_layers"]:
        product = layer.get("product") or LEGACY_ALIASES.get(layer.get("filepath"))
        if registry is not None and product is None:
            raise ConfigError(f"{_CONFIG_NAME}.yaml", f"mrms_layers: {layer['name']}",
                              "requires a product identity or supported legacy filepath")
        active = registry is None or registry.is_enabled(product)
        if not active and not include_inactive:
            continue
        entry = {
            "name": layer["name"], "colormap_key": layer["colormap_key"],
            "filepath": (registry.path_for(product) if registry is not None
                         else _resolve_dir(layer["filepath"])) if active else None,
            "outdir": _resolve_dir(layer["outdir"]) if active else None,
            "required": bool(layer.get("required", True)),
        }
        if registry is not None:
            entry.update(product=product, active=active,
                         reason=None if active else "ingestion-disabled")
        result.append(entry)
    return result


def get_goes_file_list():
    """Return the GOES-backed render configuration list.

    GOES ABI layers default to ``required: false``: the GOES renderer reports
    success ratios without gating on individual channels.
    """
    goes = _render_config()["goes_layers"]
    common = goes["common"]
    return [
        {
            "name": layer["name"],
            "colormap_key": layer["colormap_key"],
            "filepath": _resolve_dir(layer["filepath"]),
            "outdir": _resolve_dir(layer["outdir"]),
            "source_type": common["source_type"],
            "variable_name": common["variable_name"],
            "fallback_variable_names": list(common["fallback_variable_names"]),
            "channel_id": layer["channel_id"],
            "value_transform": layer["value_transform"],
            "mask_min": dict(layer["mask_min"]),
            "mask_max": dict(layer["mask_max"]),
            "required": bool(common.get("required", False)),
        }
        for layer in goes["layers"]
    ]


def get_file_list():
    """Return the combined render configuration list."""
    return get_mrms_file_list() + get_goes_file_list()
