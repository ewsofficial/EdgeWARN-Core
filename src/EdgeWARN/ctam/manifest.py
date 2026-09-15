"""Strict TOML manifest parsing for external CTAM modules.

A manifest is the only thing Phase 1 reads from an installed module. No module
code is imported and no process is launched here -- validation must be decidable
from the file and its containing directory alone, so a hostile or broken module
cannot influence its own admission.

Every rejection raises :class:`ManifestError` naming the offending key and the
fix. Discovery turns that message into the module's ``reason`` and the status
record publishes it, so an operator has to be able to act on the text without
reading this file.

The frozen patterns below are copied byte-for-byte from ``docs/ctam/schema/``.
They are duplicated rather than derived because the schemas are JSON documents
validated by the repository's own restricted walker, and pulling them in at
import time would make manifest parsing depend on the documentation tree.
``tests/core/ctam/test_manifest.py`` pins the boundaries the quantifiers imply.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .limits import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_HISTORY_WINDOW,
    MAX_PUBLIC_ROUTES_PER_MODULE,
    MAX_PUBLIC_ROUTE_DESCRIPTION_LENGTH,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    RESERVED_MODULE_IDS,
    RESERVED_OUTPUT_KEYS,
    SUPPORTED_API_VERSIONS,
    SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
)

# docs/ctam/schema/requirements-evaluation.schema.json, module_id. Also in
# response-envelope and transaction. 128 characters maximum: see
# limits.MAX_MODULE_ID_LENGTH.
MODULE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,127}\Z"
# docs/ctam/schema/status-record.schema.json, modules.*.version.
VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+\Z"
# docs/ctam/schema/status-record.schema.json, modules.*.api_version.
API_VERSION_PATTERN = r"^[0-9]+\Z"
# docs/ctam/schema/file-descriptor.schema.json, product.
PRODUCT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
# docs/ctam/schema/requirements-evaluation.schema.json, requirements.*.selector.
SELECTOR_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:*-]{0,191}\Z"

# The display name becomes the module's `modules.<name>` output key, so it is
# bounded like one and must not collide with a reserved container key.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z"
PUBLIC_ROUTE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z"

VALID_SCOPES = ("stormcells", "cycle")
VALID_WRITE_RESOURCES = ("stormcells.current", "cells.history")
INPUT_FAMILIES = ("mrms", "goes", "rap")
INPUT_ROLES = ("current", "previous")

# An argument vector is handed to the runner without a shell, but a metacharacter
# in an element is still a sign the author expected shell semantics, and Phase 4
# should never have to guess. Reject at discovery instead.
SHELL_METACHARACTERS = (";", "|", "&", "<", ">", "`", "$", "\n", "\r")

PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")
PYTHON_PLACEHOLDER = "{python}"

# Manifest pointers are document-relative and use one `*` for the element; the
# runtime PATCH pointer of Phase 3 is entry-relative. The prefix is what bridges
# the two, so it is fixed per resource rather than free-form.
POINTER_PREFIXES = {
    "stormcells.current": "/features/*",
    "cells.history": "/*",
}

OWNED_CONTAINERS = ("modules", "properties")


class ManifestError(Exception):
    """A manifest is unusable. The message is shown to an operator verbatim."""


@dataclass(frozen=True)
class Selector:
    """One parsed requirement selector.

    ``raw`` is preserved exactly as the manifest wrote it because
    ``docs/ctam/schema/requirements-evaluation.schema.json`` requires the
    selector to be echoed back verbatim. ``family`` is folded to lowercase: the
    Phase 0 OpenAPI examples spell the selector family in uppercase
    (``input:MRMS:...``) while the catalog ``file_id`` uses lowercase
    (``input:mrms:...``), so the parsed form is the one the catalog can match.
    """

    raw: str
    kind: str
    family: str | None
    product: str | None
    role: str


@dataclass(frozen=True)
class ModuleRequirement:
    """A declared input, with the conditions that make it usable.

    ``required`` defaults to true in the manifest, so an author who forgot to
    mark an input optional gets a blocked module rather than a silent skip.
    """

    selector: Selector
    required: bool
    max_age_seconds: float | None
    min_history_entries: int | None


@dataclass(frozen=True)
class ModuleWrite:
    """A declared output location, already validated for container and ownership."""

    resource: str
    json_pointer: str


@dataclass(frozen=True)
class PublicRoute:
    """One host-owned public JSON route declared by a module."""

    route_id: str
    description: str


@dataclass(frozen=True)
class ModuleManifest:
    """The whole manifest, frozen.

    ``entrypoint`` is kept verbatim, ``{python}`` unexpanded: Phase 4 owns
    launching, and the manifest stays the record of what the author declared
    rather than of what one host happened to resolve.
    """

    module_id: str
    name: str
    version: str
    api_version: str
    enabled: bool
    required: bool
    scope: str
    entrypoint: tuple[str, ...]
    timeout_seconds: int
    after: tuple[str, ...]
    requires: tuple[ModuleRequirement, ...]
    writes: tuple[ModuleWrite, ...]
    directory: Path
    manifest_path: Path
    public_routes: tuple[PublicRoute, ...] = ()


def parse_selector(raw: str) -> Selector:
    """Parse one selector, or raise :class:`ManifestError` naming the accepted set.

    Unknown selectors fail here rather than becoming requirements that can never
    be satisfied. ``alerts.current`` is deliberately not admitted in Phase 1
    even though the catalog has an ``alerts`` kind.
    """
    if not isinstance(raw, str):
        raise ManifestError(
            f"requires.selector must be a string, got {type(raw).__name__}; "
            f"write it as a quoted selector such as \"stormcells.current\""
        )
    if not re.search(SELECTOR_PATTERN, raw):
        raise ManifestError(
            f"requires.selector {raw!r} is not a legal selector string: it must "
            f"start with a letter or digit, use only letters, digits and "
            f"'. _ : * -', and be at most 192 characters"
        )

    if raw == "stormcells.current":
        return Selector(raw=raw, kind="stormcells", family=None, product=None, role="current")
    if raw == "cells.history":
        return Selector(raw=raw, kind="cell_history", family=None, product=None, role="history")

    if not raw.startswith("input:"):
        raise ManifestError(_unknown_selector_message(raw))

    parts = raw.split(":")
    if len(parts) != 4:
        raise ManifestError(
            f"requires.selector {raw!r} is not a valid input selector; the form is "
            f"'input:<FAMILY>:<PRODUCT>:<role>' with exactly three colons, for "
            f"example 'input:MRMS:VIL_00.50:current'"
        )
    _, raw_family, product, role = parts

    family = raw_family.lower()
    if family not in INPUT_FAMILIES:
        raise ManifestError(
            f"requires.selector {raw!r} names input family {raw_family!r}; "
            f"accepted families are {', '.join(INPUT_FAMILIES)} (case-insensitive)"
        )
    if role not in INPUT_ROLES:
        raise ManifestError(
            f"requires.selector {raw!r} names role {role!r}; an input selector "
            f"accepts only {' or '.join(repr(r) for r in INPUT_ROLES)}. Use "
            f"'cells.history' for history."
        )
    if not re.search(PRODUCT_PATTERN, product):
        raise ManifestError(
            f"requires.selector {raw!r} names product {product!r}, which is not a "
            f"legal product id: it must start with a letter or digit, use only "
            f"letters, digits and '. _ -', and be at most 128 characters"
        )
    _validate_product_against_catalog(raw, family, product)

    return Selector(raw=raw, kind="input", family=family, product=product, role=role)


def _unknown_selector_message(raw: str) -> str:
    extra = ""
    if raw.startswith("alerts."):
        extra = (
            " Alerts are not an admitted Phase 1 module input; the host owns "
            "alert generation."
        )
    return (
        f"requires.selector {raw!r} is not a known selector. Accepted selectors "
        f"are 'stormcells.current', 'cells.history', and "
        f"'input:<FAMILY>:<PRODUCT>:<role>' with role 'current' or 'previous'."
        f"{extra}"
    )


def _validate_product_against_catalog(raw: str, family: str, product: str) -> None:
    """Reject a product typo at discovery, using the host's existing catalogs.

    The catalog accessors are imported lazily: ``parse_manifest`` must not drag
    the ingest and integration configuration stack into every importer of this
    module. If a catalog cannot be loaded -- a config error, a stripped test
    environment -- the product is treated as unvalidated rather than blamed on
    the module author, but the family and role checks still stand.
    """
    try:
        known = _catalog_products(family)
    except Exception:  # pragma: no cover - depends on host config health
        return
    if not known:
        return
    if product not in known:
        raise ManifestError(
            f"requires.selector {raw!r} names product {product!r}, which is not in "
            f"the host's {family} catalog. Known {family} products include: "
            f"{', '.join(sorted(known)[:8])}"
            f"{' ...' if len(known) > 8 else ''}"
        )


def _catalog_products(family: str) -> frozenset[str]:
    if family == "mrms":
        from common.ingest.mrms.config import get_mrms_modifiers

        # (region, product, outdir) triples. ProbSevere carries product None,
        # which is not addressable by a product selector.
        return frozenset(
            product for _region, product, _outdir in get_mrms_modifiers() if product
        )
    if family == "goes":
        from common.ingest.mrms.config import get_goes_modifiers

        # GoesIngestSpec objects. A caller may name the S3 product
        # ('ABI-L1b-RadC'), the channel id ('C01'), or the channel name
        # ('visible_blue'); all three appear in the pinned cycle manifest.
        names: set[str] = set()
        for spec in get_goes_modifiers():
            for value in (
                getattr(spec, "product", None),
                getattr(spec, "channel_id", None),
                getattr(spec, "channel_name", None),
            ):
                if value:
                    names.add(str(value))
        return frozenset(names)
    if family == "rap":
        from EdgeWARN.process.integrate.config import get_rap_products

        catalog = get_rap_products()
        # {"products": [{"var": ...}], "derived": [{"key": ...}]}
        names = {
            str(entry["var"]) for entry in catalog.get("products", ()) if entry.get("var")
        }
        names |= {
            str(entry["key"]) for entry in catalog.get("derived", ()) if entry.get("key")
        }
        return frozenset(names)
    return frozenset()  # pragma: no cover - families are checked before this


def parse_manifest(manifest_path: Path) -> ModuleManifest:
    """Read and validate one ``module.toml``, or raise :class:`ManifestError`."""
    manifest_path = Path(manifest_path)
    directory = manifest_path.parent

    try:
        with open(manifest_path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ManifestError(
            f"no manifest at {manifest_path}; every module directory needs a "
            f"'module.toml'"
        ) from None
    except OSError as exc:
        raise ManifestError(f"cannot read {manifest_path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{manifest_path} is not valid TOML: {exc}") from None

    if not isinstance(raw, dict):  # pragma: no cover - tomllib always returns a dict
        raise ManifestError(f"{manifest_path} must be a TOML table")

    _require_schema_version(raw)
    api_version = _require_api_version(raw)
    module_id = _require_id(raw, directory)
    name = _require_name(raw)
    version = _require_version(raw)
    enabled = _require_bool(raw, "enabled", default=True)
    required = _require_bool(raw, "required", default=False)
    scope = _require_scope(raw)
    timeout_seconds = _require_timeout(raw)
    entrypoint = _require_entrypoint(raw, directory)
    after = _require_after(raw, module_id)
    requires = _require_requires(raw)
    writes = _require_writes(raw, module_id, name)
    public_routes = _require_public_routes(raw)

    return ModuleManifest(
        module_id=module_id,
        name=name,
        version=version,
        api_version=api_version,
        enabled=enabled,
        required=required,
        scope=scope,
        entrypoint=entrypoint,
        timeout_seconds=timeout_seconds,
        after=after,
        requires=requires,
        writes=writes,
        directory=directory,
        manifest_path=manifest_path,
        public_routes=public_routes,
    )


def _require_public_routes(raw: dict) -> tuple[PublicRoute, ...]:
    value = raw.get("public_routes", [])
    if not isinstance(value, list):
        raise ManifestError("'public_routes' must be an array of TOML tables written as [[public_routes]]")
    if len(value) > MAX_PUBLIC_ROUTES_PER_MODULE:
        raise ManifestError(
            f"'public_routes' declares {len(value)} routes; at most "
            f"{MAX_PUBLIC_ROUTES_PER_MODULE} are allowed per module"
        )
    routes: list[PublicRoute] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        key = f"public_routes[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{key} must be a TOML table")
        unknown = set(item) - {"id", "description"}
        if unknown:
            raise ManifestError(f"{key} has unknown field(s): {', '.join(sorted(unknown))}")
        route_id = item.get("id")
        if not isinstance(route_id, str) or not re.fullmatch(PUBLIC_ROUTE_ID_PATTERN, route_id):
            raise ManifestError(
                f"{key}.id must be 1-128 characters, start with a letter or digit, "
                "and contain only letters, digits, '.', '_' and '-'"
            )
        if route_id in {".", ".."} or route_id in seen:
            reason = "is duplicated" if route_id in seen else "is reserved"
            raise ManifestError(f"{key}.id {route_id!r} {reason}; route ids must be unique safe segments")
        description = item.get("description")
        if not isinstance(description, str) or not description or len(description) > MAX_PUBLIC_ROUTE_DESCRIPTION_LENGTH:
            raise ManifestError(
                f"{key}.description must be non-empty plain text of at most "
                f"{MAX_PUBLIC_ROUTE_DESCRIPTION_LENGTH} characters"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in description):
            raise ManifestError(f"{key}.description must not contain control characters")
        seen.add(route_id)
        routes.append(PublicRoute(route_id=route_id, description=description))
    return tuple(routes)


def _is_int(value) -> bool:
    """True for a real integer. A TOML ``true`` is an ``int`` subclass in Python."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_schema_version(raw: dict) -> int:
    if "schema_version" not in raw:
        raise ManifestError(
            f"missing 'schema_version'; add schema_version = "
            f"{SUPPORTED_MANIFEST_SCHEMA_VERSIONS[0]}"
        )
    value = raw["schema_version"]
    if not _is_int(value) or value not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        raise ManifestError(
            f"'schema_version' is {value!r}; this host supports manifest schema "
            f"versions {', '.join(str(v) for v in SUPPORTED_MANIFEST_SCHEMA_VERSIONS)}"
        )
    return value


def _require_api_version(raw: dict) -> str:
    if "api_version" not in raw:
        raise ManifestError(
            f"missing 'api_version'; add api_version = "
            f"\"{SUPPORTED_API_VERSIONS[0]}\""
        )
    value = raw["api_version"]
    if not isinstance(value, str):
        raise ManifestError(
            f"'api_version' must be a quoted string, got {value!r}; write "
            f"api_version = \"{SUPPORTED_API_VERSIONS[0]}\""
        )
    if not re.search(API_VERSION_PATTERN, value):
        raise ManifestError(
            f"'api_version' {value!r} must be digits only, because the status "
            f"record records it under the pattern {API_VERSION_PATTERN}"
        )
    if value not in SUPPORTED_API_VERSIONS:
        raise ManifestError(
            f"'api_version' {value!r} is not supported by this host; supported "
            f"versions are {', '.join(repr(v) for v in SUPPORTED_API_VERSIONS)}"
        )
    return value


def _require_id(raw: dict, directory: Path) -> str:
    if "id" not in raw:
        raise ManifestError(
            f"missing 'id'; add id = \"{directory.name}\" to match the containing "
            f"directory name"
        )
    value = raw["id"]
    if not isinstance(value, str):
        raise ManifestError(f"'id' must be a quoted string, got {value!r}")
    if not re.search(MODULE_ID_PATTERN, value):
        raise ManifestError(
            f"'id' {value!r} is not a legal module id: it must start with a "
            f"lowercase letter or digit, contain only lowercase letters, digits, "
            f"'_' and '-', and be 1 to 128 characters long"
        )
    if value in RESERVED_MODULE_IDS:
        raise ManifestError(
            f"'id' {value!r} is reserved for a built-in module and cannot be used "
            f"by an installed module; choose a different id"
        )
    if value != directory.name:
        raise ManifestError(
            f"'id' {value!r} does not match its directory name "
            f"{directory.name!r}; rename the directory to {value!r} or change 'id' "
            f"to {directory.name!r} so one module cannot claim another's id"
        )
    return value


def _require_name(raw: dict) -> str:
    if "name" not in raw:
        raise ManifestError(
            "missing 'name'; it is the display name that becomes this module's "
            "modules.<name> output key"
        )
    value = raw["name"]
    if not isinstance(value, str):
        raise ManifestError(f"'name' must be a quoted string, got {value!r}")
    # Reserved first: `_grid_outputs` also fails the pattern, and "that key is
    # reserved" is the reason an author can act on, where "illegal character" would
    # send them to rename it to `grid_outputs` and collide all over again.
    lowered = value.casefold()
    for reserved in RESERVED_OUTPUT_KEYS:
        if lowered == reserved.casefold():
            raise ManifestError(
                f"'name' {value!r} collides with reserved output key {reserved!r} "
                f"(matching is case-insensitive); choose a different display name"
            )
    if not re.search(NAME_PATTERN, value):
        raise ManifestError(
            f"'name' {value!r} is not a legal output key: it must start with a "
            f"letter or digit, contain only letters, digits, '_' and '-', and be "
            f"1 to 64 characters long"
        )
    return value


def _require_version(raw: dict) -> str:
    if "version" not in raw:
        raise ManifestError("missing 'version'; add a version like version = \"1.0.0\"")
    value = raw["version"]
    if not isinstance(value, str) or not re.search(VERSION_PATTERN, value):
        raise ManifestError(
            f"'version' {value!r} must be a quoted three-part version such as "
            f"\"1.0.0\", because the status record records it under the pattern "
            f"{VERSION_PATTERN}"
        )
    return value


def _require_bool(raw: dict, key: str, *, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ManifestError(
            f"'{key}' must be true or false, got {value!r}"
        )
    return value


def _require_scope(raw: dict) -> str:
    value = raw.get("scope", "stormcells")
    if value not in VALID_SCOPES:
        raise ManifestError(
            f"'scope' is {value!r}; it must be one of "
            f"{', '.join(repr(s) for s in VALID_SCOPES)}"
        )
    return value


def _require_timeout(raw: dict) -> int:
    if "timeout_seconds" not in raw:
        return DEFAULT_TIMEOUT_SECONDS
    value = raw["timeout_seconds"]
    if not _is_int(value):
        raise ManifestError(
            f"'timeout_seconds' must be a whole number of seconds, got {value!r}; "
            f"a boolean or a fractional timeout is not accepted"
        )
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise ManifestError(
            f"'timeout_seconds' is {value}; it must be between "
            f"{MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds inclusive "
            f"(default {DEFAULT_TIMEOUT_SECONDS})"
        )
    return value


def _require_entrypoint(raw: dict, directory: Path) -> tuple[str, ...]:
    if "entrypoint" not in raw:
        raise ManifestError(
            "missing 'entrypoint'; it is an argument array such as "
            "entrypoint = [\"{python}\", \"main.py\"]"
        )
    value = raw["entrypoint"]
    if not isinstance(value, list):
        raise ManifestError(
            f"'entrypoint' must be an array of strings, got {value!r}; an argument "
            f"vector is never a shell string"
        )
    if not value:
        raise ManifestError(
            "'entrypoint' is empty; it needs at least the program to run, such as "
            "entrypoint = [\"{python}\", \"main.py\"]"
        )
    for index, element in enumerate(value):
        if not isinstance(element, str):
            raise ManifestError(
                f"'entrypoint' element {index} is {element!r}; every element must "
                f"be a string"
            )
        if not element:
            raise ManifestError(f"'entrypoint' element {index} is an empty string")
        for character in SHELL_METACHARACTERS:
            if character in element:
                raise ManifestError(
                    f"'entrypoint' element {index} {element!r} contains shell "
                    f"metacharacter {character!r}; the entrypoint is executed "
                    f"without a shell, so express this as separate arguments"
                )
        for placeholder in PLACEHOLDER_RE.findall(element):
            if placeholder != PYTHON_PLACEHOLDER:
                raise ManifestError(
                    f"'entrypoint' element {index} uses placeholder "
                    f"{placeholder!r}; the only supported placeholder is "
                    f"'{PYTHON_PLACEHOLDER}'"
                )
        if element == PYTHON_PLACEHOLDER:
            continue
        _require_contained_argument(index, element, directory)
    return tuple(value)


def _require_contained_argument(index: int, element: str, directory: Path) -> None:
    """Reject an absolute path or one that escapes the module directory.

    ``resolve()`` is what catches a symlink escape as well as ``..``. A
    non-path-looking flag such as ``--verbose`` is left alone; only an argument
    that could name a payload is checked.
    """
    candidate = Path(element)
    if candidate.is_absolute() or element.startswith(("/", "\\")):
        raise ManifestError(
            f"'entrypoint' element {index} {element!r} is an absolute path; every "
            f"payload path must be relative to the module directory"
        )
    if not _looks_like_path(element):
        return
    resolved_root = directory.resolve()
    resolved = (directory / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ManifestError(
            f"'entrypoint' element {index} {element!r} resolves to {resolved}, "
            f"outside the module directory {resolved_root}; a module may only run "
            f"payloads it ships"
        )


def _looks_like_path(element: str) -> bool:
    if element.startswith("-"):
        return False
    return "/" in element or "\\" in element or "." in element


def _require_after(raw: dict, module_id: str) -> tuple[str, ...]:
    value = raw.get("after", [])
    if not isinstance(value, list):
        raise ManifestError(
            f"'after' must be an array of module ids, got {value!r}"
        )
    seen: list[str] = []
    for index, element in enumerate(value):
        if not isinstance(element, str) or not re.search(MODULE_ID_PATTERN, element):
            raise ManifestError(
                f"'after' element {index} {element!r} is not a legal module id: "
                f"lowercase letters, digits, '_' and '-', 1 to 128 characters"
            )
        if element == module_id:
            raise ManifestError(
                f"'after' lists this module's own id {module_id!r}; a module cannot "
                f"depend on itself"
            )
        if element not in seen:
            seen.append(element)
    return tuple(seen)


def _require_requires(raw: dict) -> tuple[ModuleRequirement, ...]:
    blocks = raw.get("requires", [])
    if not isinstance(blocks, list):
        raise ManifestError(
            "'requires' must be a list of [[requires]] blocks"
        )
    parsed: list[ModuleRequirement] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ManifestError(
                f"requires block {index} is {block!r}; write each requirement as a "
                f"[[requires]] table"
            )
        if "selector" not in block:
            raise ManifestError(
                f"requires block {index} has no 'selector'; every requirement must "
                f"name what it needs"
            )
        selector = parse_selector(block["selector"])
        required = _require_bool(block, "required", default=True)

        max_age = block.get("max_age_seconds")
        if max_age is not None:
            if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
                raise ManifestError(
                    f"requires block {index} 'max_age_seconds' must be a positive "
                    f"number of seconds, got {max_age!r}"
                )
            if max_age <= 0:
                raise ManifestError(
                    f"requires block {index} 'max_age_seconds' is {max_age}; it "
                    f"must be greater than zero"
                )
            if selector.kind != "input":
                raise ManifestError(
                    f"requires block {index} sets 'max_age_seconds' on selector "
                    f"{selector.raw!r}, but freshness only applies to an "
                    f"'input:...' selector; host-owned artifacts are always the "
                    f"current cycle's. Remove the key."
                )
            max_age = float(max_age)

        min_history = block.get("min_history_entries")
        if min_history is not None:
            if not _is_int(min_history):
                raise ManifestError(
                    f"requires block {index} 'min_history_entries' must be a whole "
                    f"number, got {min_history!r}"
                )
            if min_history < 1:
                raise ManifestError(
                    f"requires block {index} 'min_history_entries' is "
                    f"{min_history}; it must be at least 1"
                )
            if min_history > MAX_HISTORY_WINDOW:
                raise ManifestError(
                    f"requires block {index} 'min_history_entries' is "
                    f"{min_history}, above the maximum history read window of "
                    f"{MAX_HISTORY_WINDOW}; no read could ever satisfy it"
                )
            if selector.kind != "cell_history":
                raise ManifestError(
                    f"requires block {index} sets 'min_history_entries' on selector "
                    f"{selector.raw!r}, but it only applies to 'cells.history'. "
                    f"Remove the key."
                )

        parsed.append(
            ModuleRequirement(
                selector=selector,
                required=required,
                max_age_seconds=max_age,
                min_history_entries=min_history,
            )
        )
    return tuple(parsed)


def _require_writes(raw: dict, module_id: str, name: str) -> tuple[ModuleWrite, ...]:
    blocks = raw.get("writes", [])
    if not isinstance(blocks, list):
        raise ManifestError("'writes' must be a list of [[writes]] blocks")
    if not blocks:
        raise ManifestError(
            "no [[writes]] blocks; a module that declares no output has nothing to "
            "contribute, so its manifest is incomplete"
        )
    parsed: list[ModuleWrite] = []
    seen: set[tuple[str, str]] = set()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ManifestError(
                f"writes block {index} is {block!r}; write each output as a "
                f"[[writes]] table"
            )
        resource = block.get("resource")
        if resource not in VALID_WRITE_RESOURCES:
            raise ManifestError(
                f"writes block {index} 'resource' is {resource!r}; it must be one "
                f"of {', '.join(repr(r) for r in VALID_WRITE_RESOURCES)}"
            )
        pointer = block.get("json_pointer")
        if not isinstance(pointer, str):
            raise ManifestError(
                f"writes block {index} needs a 'json_pointer' string, got "
                f"{pointer!r}"
            )
        validate_write_pointer(resource, pointer, module_id=module_id, name=name)
        key = (resource, pointer)
        if key in seen:
            raise ManifestError(
                f"writes block {index} repeats resource {resource!r} pointer "
                f"{pointer!r}; declare each output location once"
            )
        seen.add(key)
        parsed.append(ModuleWrite(resource=resource, json_pointer=pointer))
    return tuple(parsed)


def validate_write_pointer(
    resource: str, pointer: str, *, module_id: str, name: str
) -> tuple[str, ...]:
    """Validate one manifest write pointer, returning its decoded segments.

    The rule this enforces cannot be widened by any manifest or operator grant: a
    module reaches only the ``modules`` and ``properties`` containers of a cell
    entry, and only its own key inside them.

    Validation is segment-wise on the *decoded* segments, unescaping ``~1`` then
    ``~0`` exactly once each. It is deliberately not a string-prefix test and
    deliberately not routed through any filesystem path normalizer: a JSON
    Pointer has no traversal semantics, so a ``..`` segment is a literal key
    name. Normalizing it as a path is what would turn
    ``/modules/Foo/../id`` into a write to ``/id``. See
    ``plans/modular-ctam-phase0-findings.md`` section 10.
    """
    prefix = POINTER_PREFIXES[resource]
    if not pointer.startswith(prefix + "/"):
        raise ManifestError(
            f"json_pointer {pointer!r} for resource {resource!r} must begin "
            f"{prefix + '/'!r}: a manifest pointer is document-relative and uses "
            f"one '*' for the entry"
        )
    remainder = pointer[len(prefix):]

    raw_segments = remainder.split("/")[1:]
    segments: list[str] = []
    for position, raw_segment in enumerate(raw_segments):
        if raw_segment == "":
            raise ManifestError(
                f"json_pointer {pointer!r} has an empty segment at position "
                f"{position}; a bare or trailing '/' addresses no key"
            )
        for character in raw_segment:
            if ord(character) < 0x20:
                raise ManifestError(
                    f"json_pointer {pointer!r} segment {raw_segment!r} contains a "
                    f"control character; remove it"
                )
        segments.append(_unescape_pointer_segment(pointer, raw_segment))

    container = segments[0]
    if container not in OWNED_CONTAINERS:
        raise ManifestError(
            f"json_pointer {pointer!r} targets {container!r}; a module may only "
            f"write inside {' or '.join(repr(c) for c in OWNED_CONTAINERS)} of a "
            f"cell entry. Everything else -- identity, geometry, measured values, "
            f"tracking state, timestamps -- is host-owned."
        )
    if len(segments) < 2:
        raise ManifestError(
            f"json_pointer {pointer!r} addresses the {container!r} container "
            f"itself; name a key inside it, because a module must not be able to "
            f"replace or clear a container shared with other modules"
        )

    owned = segments[1]
    if owned in RESERVED_OUTPUT_KEYS:
        raise ManifestError(
            f"json_pointer {pointer!r} names reserved output key {owned!r}, which "
            f"is never grantable to an installed module"
        )
    if container == "modules":
        if owned != name:
            raise ManifestError(
                f"json_pointer {pointer!r} names {owned!r} under 'modules', but "
                f"this module owns only {name!r} (its manifest display 'name', not "
                f"its 'id'). A '..' or any other segment here is a literal key "
                f"name, not a parent reference, so it is simply not the owner's key."
            )
    else:
        if owned != module_id and not owned.startswith(f"{module_id}_"):
            raise ManifestError(
                f"json_pointer {pointer!r} names {owned!r} under 'properties', but "
                f"a flat scalar must be exactly {module_id!r} or start with "
                f"'{module_id}_'. A '..' or any other segment here is a literal key "
                f"name, not a parent reference, so it is simply not the owner's key."
            )
    return tuple(segments)


def _unescape_pointer_segment(pointer: str, segment: str) -> str:
    """RFC 6901 unescaping, once. ``~1`` before ``~0``, and no lone ``~``.

    Order matters: unescaping ``~0`` first would turn ``~01`` into ``~1`` and
    then into ``/``, inventing a separator the author never wrote.
    """
    index = 0
    while True:
        index = segment.find("~", index)
        if index == -1:
            break
        if index + 1 >= len(segment) or segment[index + 1] not in "01":
            raise ManifestError(
                f"json_pointer {pointer!r} segment {segment!r} contains a '~' that "
                f"is not part of a '~0' or '~1' escape; write '~0' for a literal "
                f"'~' and '~1' for a literal '/'"
            )
        index += 2
    return segment.replace("~1", "/").replace("~0", "~")
