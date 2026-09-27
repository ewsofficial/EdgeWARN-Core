"""Pure MRMS identifier rules shared by configuration and ingest.

No catalog loading or filesystem access belongs here. The configured prefix is
removed exactly once; only the local path drops the final elevation token.
"""
from dataclasses import dataclass
import re

MAX_PRODUCT_ID_LENGTH = 128
_PRODUCT = re.compile(r"MRMS_([A-Za-z0-9][A-Za-z0-9_-]*)_([0-9]{2}\.[0-9]{2})", re.ASCII)


@dataclass(frozen=True)
class MrmsProductIdentity:
    configured_id: str
    product_id: str
    base_product: str
    elevation: str | None
    path_name: str


def parse_product_id(configured_id: str) -> MrmsProductIdentity:
    """Validate a configured ID without checking upstream availability."""
    if not isinstance(configured_id, str):
        raise ValueError("MRMS products must be strings, not product/source/path overrides")
    if configured_id == "MRMS_ProbSevere":
        return MrmsProductIdentity(configured_id, "ProbSevere", "ProbSevere", None,
                                   "MRMS_ProbSevere")
    match = _PRODUCT.fullmatch(configured_id)
    if (match is None or match[1].startswith("MRMS_")
            or len(configured_id) > MAX_PRODUCT_ID_LENGTH + len("MRMS_")):
        raise ValueError(
            f"Invalid MRMS product {configured_id!r}: expected one MRMS_ prefix, "
            "an alphanumeric base with underscores/hyphens, and _DD.DD elevation "
            "(or MRMS_ProbSevere); normalized identity must fit 128 characters"
        )
    base, elevation = match.groups()
    return MrmsProductIdentity(configured_id, configured_id[5:], base, elevation,
                               f"MRMS_{base}")


def normalize_products(configured_ids, *, protected_ids=()) -> tuple[MrmsProductIdentity, ...]:
    """Merge reserved IDs and additions, rejecting duplicates and path collisions.

    Explicitly listing a reserved ID once is valid. Duplicate operator entries
    remain errors. Callers supply the protected contract; this module has no
    dependency on ingest definitions.
    """
    if not isinstance(configured_ids, (list, tuple)):
        raise ValueError("MRMS products must be a list or tuple of configured IDs")
    additions = tuple(parse_product_id(value) for value in configured_ids)
    seen = set()
    for item in additions:
        if item.configured_id in seen:
            raise ValueError(f"Duplicate MRMS product {item.configured_id!r}")
        seen.add(item.configured_id)
    identities = {}
    paths = {}
    for item in (*tuple(parse_product_id(value) for value in protected_ids), *additions):
        if item.configured_id in identities:
            continue
        key = item.path_name.casefold()
        if key in paths:
            raise ValueError(
                f"MRMS path collision: {paths[key]!r} and {item.configured_id!r} "
                f"both use {item.path_name!r} (case-insensitive)"
            )
        identities[item.configured_id] = item
        paths[key] = item.configured_id
    return tuple(identities.values())
