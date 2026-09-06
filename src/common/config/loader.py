"""Load, validate, and freeze the YAML files under ``config/``.

Importable with only the standard library plus ``yaml`` -- no ``util.file``,
no domain modules -- so it can run before the rest of the application
(including the filesystem layer) is initialized.

Schema validation is a small hand-rolled walker (see ``_walk``) rather than a
full JSON Schema implementation. It supports exactly the keywords used by
``config/schema/*.schema.json`` today: ``type``, ``properties``,
``required``, ``additionalProperties``, ``items``, ``minItems``,
``maxItems``, ``uniqueItems``, ``minimum``, ``maximum``, ``exclusiveMinimum``,
``exclusiveMaximum``, ``const``, ``enum``, and ``pattern``. Any other keyword
in a schema is a startup error (``_KNOWN_SCHEMA_KEYWORDS`` guard) rather than
a silently-unenforced constraint, so a schema author who reaches for
``oneOf``/``$ref``/``format`` finds out immediately instead of shipping a
schema that looks stricter than it is.

``jsonschema`` is deliberately not used, which is a departure from
``plans/source-configuration-extraction-plan.md`` as originally written; that
document has been corrected. Three reasons, in order of weight. ``src/config/
loader.js`` implements the same walker over the same keyword set, and
cross-language parity is a requirement -- adopting a full validator here alone
would mean Python quietly accepting ``$ref``/``oneOf``/``format`` schemas that
Node still rejects. The unknown-keyword guard is a property a real validator
would remove rather than add: ``jsonschema`` ignores keywords it does not
recognize, so a misspelled ``requred`` would silently enforce nothing. And
``environment.yml`` is the repository's only dependency manifest, and it does
not list ``jsonschema``; it being importable from some ambient interpreter is
not the same as it being declared. Revisit if a schema genuinely needs
composition keywords -- at that point the JS walker has to grow too, and taking
the dependency on both sides is the cheaper answer.

Precedence for locating the config root, highest first:
1. An explicit ``config_dir`` argument (typically sourced from a ``--config-dir``
   CLI flag).
2. The ``EDGEWARN_CONFIG_DIR`` environment variable.
3. A ``config/`` directory found by walking up from this file's location.

Path values inside a catalog are expanded by :func:`expand_path` against an
explicit token allowlist, never against the working directory.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

CONFIG_NAMES: tuple[str, ...] = (
    "runtime",
    "historical",
    "filesystem",
    "detection",
    "lineage",
    "integration",
    "scheduler",
    "api_index",
    "ingest",
    "nexrad",
    "synoptic_rap",
    "wpc",
    "metar",
    "nws",
    "ewmrs_render",
    "ewmrs_pipeline",
    "api",
    "kalman",
)

_ENV_CONFIG_DIR = "EDGEWARN_CONFIG_DIR"

_config_cache: dict[tuple[str, str], Any] = {}
_provenance_cache: dict[tuple[str, str], dict[str, Any]] = {}

# Resolving the root costs a ``Path.resolve()`` plus an ``is_file()`` stat --
# ~260us on Windows -- and every accessor pays it on every read, some of them
# per-polygon. Memoized keyed by the *input*, never unconditionally: a bare
# cache over ``config_root()`` would freeze the first answer and put a later
# ``EDGEWARN_CONFIG_DIR`` out of reach, which is the same freeze the per-call
# accessor convention exists to avoid.
#
# Relative inputs are not memoized, because they resolve against the CWD and so
# are not a function of the key alone. ``export_config_root`` publishes an
# absolute string, so the production path is always the memoized one. The
# ``None`` key is the walk-up result, which depends only on ``__file__``.
_root_cache: dict[str | None, Path] = {}


class ConfigError(Exception):
    """Raised for a missing config file, missing key, or schema violation."""

    def __init__(self, filename: str, dotted_path: str | None, message: str):
        self.filename = filename
        self.dotted_path = dotted_path
        self.message = message
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.dotted_path:
            return f"{self.filename}: {self.dotted_path}: {self.message}"
        return f"{self.filename}: {self.message}"


def _find_config_root_by_walking_up() -> Path:
    if None in _root_cache:
        return _root_cache[None]

    here = Path(__file__).resolve()
    for candidate_dir in here.parents:
        config_dir = candidate_dir / "config"
        if (config_dir / "runtime.yaml").is_file():
            _root_cache[None] = config_dir
            return config_dir
    installed_config = Path(sys.prefix).resolve() / "share" / "edgewarn" / "config"
    if (installed_config / "runtime.yaml").is_file():
        _root_cache[None] = installed_config
        return installed_config
    raise ConfigError(
        "config/",
        None,
        "could not locate a config/ directory containing runtime.yaml by "
        f"walking up from {here} or at {installed_config}",
    )


def _resolve_given_root(raw: str | os.PathLike[str], invalid_message: str) -> Path:
    """Resolve and validate an explicitly named config root.

    ``invalid_message`` names whichever channel supplied ``raw``, so a bad value
    reports the flag or variable the operator actually set. Only successes are
    memoized; a failure re-raises on every call with its own message.
    """
    key = str(raw)
    cached = _root_cache.get(key) if Path(key).is_absolute() else None
    if cached is not None:
        return cached

    resolved = Path(raw).resolve()
    if not (resolved / "runtime.yaml").is_file():
        raise ConfigError(str(resolved), None, invalid_message)

    if Path(key).is_absolute():
        _root_cache[key] = resolved
    return resolved


def config_root(cli_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the config root directory using CLI > env > repo-root precedence."""
    if cli_dir is not None:
        return _resolve_given_root(cli_dir, "--config-dir does not contain runtime.yaml")

    env_dir = os.environ.get(_ENV_CONFIG_DIR)
    if env_dir is not None:
        return _resolve_given_root(
            env_dir, f"{_ENV_CONFIG_DIR} does not contain runtime.yaml"
        )

    return _find_config_root_by_walking_up()


def export_config_root(cli_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the config root and publish it so child processes inherit it.

    Any entry point that accepts ``--config-dir`` and later spawns a process must
    call this. A child re-resolves the root on its own -- an accessory process is
    spawned with no argv, and a NEXRAD parse worker is a ProcessPoolExecutor
    child that receives no config in its payload -- so the environment variable
    is the only channel that reaches it. Without the export the parent honours
    ``--config-dir`` while its children silently walk up to the repo default,
    which is a split-brain configuration rather than a visible error.
    """
    resolved = config_root(cli_dir)
    os.environ[_ENV_CONFIG_DIR] = str(resolved)
    return resolved


def repo_root(cli_dir: str | os.PathLike[str] | None = None) -> Path:
    """The repository root, derived as the parent of the config root."""
    return config_root(cli_dir).parent


# The complete set of tokens a catalog path value may begin with. An allowlist
# rather than a scan for bare `<`/`>`: six comment lines and one regex value
# (`synoptic_rap.yaml`'s named capture groups) would false-positive. Runtime format
# fields are spelled `{}` instead (`ingest.yaml:42-56`, `metar.yaml:13`,
# `integration.yaml:149-150`), so the two classes stay visually distinct and only
# these three are ever expanded at load time. Mirrored by `PATH_TOKENS` in
# `src/config/loader.js`.
PATH_TOKENS: tuple[str, ...] = ("base_dir", "gui_dir", "src_dir")


def expand_path(
    template: str,
    roots: Mapping[str, str | os.PathLike[str]],
    *,
    filename: str,
    dotted_path: str,
) -> Path:
    """Expand a leading ``<token>/`` in a catalog path value against ``roots``.

    Tokens exist so a catalog can name a location whose absolute path it cannot
    know: the runtime base directory is chosen per machine, and ``<src_dir>``
    points into the installed tree. The token is mandatory and must lead --
    a bare relative path would silently resolve against the working directory,
    which is the defect this replaces.

    ``roots`` supplies only the tokens meaningful in the calling context, so
    naming one that is not (``<base_dir>`` where no base directory exists) is an
    error rather than an empty expansion.

    Traversal is rejected twice over, because the two checks fail on different
    inputs. The textual check catches ``..`` and a leading ``/`` before any
    filesystem access, so a malicious catalog cannot even probe. The containment
    check afterwards catches what text cannot: a symlink inside the root that
    points out of it.
    """
    if not isinstance(template, str):
        raise ConfigError(filename, dotted_path, f"expected a path string, got {template!r}")

    # Checked before the prefix match so a Windows-style template is reported as
    # the separator problem it is, rather than as a malformed remainder.
    if "\\" in template:
        raise ConfigError(filename, dotted_path, f"{template!r} must use '/' separators")

    # A NUL is never valid in a path and does not survive to a useful error on its
    # own: Windows silently renders it as a space, so `a\0b.json` would resolve to
    # a real but different file rather than failing.
    if "\x00" in template:
        raise ConfigError(filename, dotted_path, "path contains a NUL byte")

    match = re.match(r"<([a-z_]+)>/", template)
    if match is None:
        expected = ", ".join(f"<{token}>/" for token in PATH_TOKENS)
        raise ConfigError(filename, dotted_path, f"{template!r} must begin with one of {expected}")

    token = match.group(1)
    if token not in PATH_TOKENS:
        raise ConfigError(filename, dotted_path, f"<{token}> is not an expandable path token")
    if token not in roots:
        raise ConfigError(filename, dotted_path, f"<{token}> has no value in this context")

    remainder = template[match.end():]
    parts = PurePosixPath(remainder).parts if remainder else ()
    if not parts or PurePosixPath(remainder).is_absolute() or ".." in parts:
        raise ConfigError(
            filename, dotted_path, f"{remainder!r} is not a relative path below <{token}>"
        )

    # An empty or non-absolute root would make `Path.resolve()` fall back to the
    # working directory, reintroducing exactly the defect the mandatory token
    # exists to prevent -- and silently, since the result is a plausible path.
    # Checked here rather than trusted from the caller because the roots come from
    # `util.file`'s import-time bind, which is the earliest and least observable
    # point in the process.
    given_root = roots[token]
    if not given_root or not Path(given_root).is_absolute():
        raise ConfigError(
            filename, dotted_path, f"<{token}> must be an absolute directory, got {given_root!r}"
        )

    root = Path(given_root).resolve()
    resolved = (root / remainder).resolve()
    if not resolved.is_relative_to(root):
        raise ConfigError(filename, dotted_path, f"{template!r} resolves outside <{token}>")
    return resolved


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _dotted_path(path_parts: list[Any]) -> str:
    parts: list[str] = []
    for part in path_parts:
        if isinstance(part, int):
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(str(part))
    return ".".join(parts)


_KNOWN_SCHEMA_KEYWORDS = frozenset({
    "$schema", "title", "description",
    "type", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "uniqueItems",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "const", "enum", "pattern",
})


def _check_supported_keywords(schema_path: Path, node: Any, path: list[Any]) -> None:
    """Reject any schema keyword the hand-rolled walker doesn't implement.

    Without this, an author reaching for e.g. ``oneOf`` or ``$ref`` would get
    a schema that silently enforces nothing for that constraint instead of a
    startup error.
    """
    if not isinstance(node, dict):
        return
    unknown = sorted(set(node) - _KNOWN_SCHEMA_KEYWORDS)
    if unknown:
        raise ConfigError(
            str(schema_path),
            _dotted_path(path) or None,
            f"unsupported schema keyword(s) {unknown}",
        )
    for prop_name, prop_schema in node.get("properties", {}).items():
        _check_supported_keywords(schema_path, prop_schema, path + [prop_name])
    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        _check_supported_keywords(schema_path, additional, path + ["additionalProperties"])
    items = node.get("items")
    if isinstance(items, dict):
        _check_supported_keywords(schema_path, items, path + ["items"])


def _type_matches(value: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if type_name == "null":
        return value is None
    raise ConfigError("<schema>", None, f"unsupported schema type {type_name!r}")


def _has_duplicates(items: list[Any]) -> bool:
    seen: list[Any] = []
    for item in items:
        if item in seen:
            return True
        seen.append(item)
    return False


def _walk(schema: dict[str, Any], value: Any, path: list[Any], errors: list[tuple[list[Any], str]]) -> None:
    type_spec = schema.get("type")
    if type_spec is not None:
        type_names = [type_spec] if isinstance(type_spec, str) else list(type_spec)
        if not any(_type_matches(value, t) for t in type_names):
            errors.append((path, f"{value!r} is not of type {' or '.join(type_names)}"))
            return

    if "const" in schema and value != schema["const"]:
        errors.append((path, f"must equal {schema['const']!r}"))
    if "enum" in schema and value not in schema["enum"]:
        errors.append((path, f"must be one of {schema['enum']!r}"))

    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                errors.append((path + [key], "is a required property"))
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, sub_value in value.items():
            if key in properties:
                _walk(properties[key], sub_value, path + [key], errors)
            elif additional is False:
                errors.append((path + [key], "additional properties are not allowed"))
            elif isinstance(additional, dict):
                _walk(additional, sub_value, path + [key], errors)

    elif isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            errors.append((path, f"must have at least {min_items} item(s)"))
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            errors.append((path, f"must have at most {max_items} item(s)"))
        if schema.get("uniqueItems") and _has_duplicates(value):
            errors.append((path, "items must be unique"))
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _walk(item_schema, item, path + [index], errors)

    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        bound = schema.get("minimum")
        if bound is not None and value < bound:
            errors.append((path, f"must be >= {bound}"))
        bound = schema.get("maximum")
        if bound is not None and value > bound:
            errors.append((path, f"must be <= {bound}"))
        bound = schema.get("exclusiveMinimum")
        if bound is not None and value <= bound:
            errors.append((path, f"must be > {bound}"))
        bound = schema.get("exclusiveMaximum")
        if bound is not None and value >= bound:
            errors.append((path, f"must be < {bound}"))

    elif isinstance(value, str) and "pattern" in schema and re.search(schema["pattern"], value) is None:
        errors.append((path, f"does not match pattern {schema['pattern']!r}"))


def _validate(name: str, document: dict[str, Any], schema_path: Path) -> None:
    if not schema_path.is_file():
        raise ConfigError(str(schema_path), None, "schema file not found")

    with schema_path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)

    _check_supported_keywords(schema_path, schema, [])

    errors: list[tuple[list[Any], str]] = []
    _walk(schema, document, [], errors)
    if errors:
        first_path, first_message = min(errors, key=lambda e: (len(e[0]), [str(p) for p in e[0]]))
        raise ConfigError(f"{name}.yaml", _dotted_path(first_path) or None, first_message)


def validate_document(
    name: str,
    document: Any,
    *,
    config_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Validate one mutable document with the canonical catalog schema.

    This is the supported in-memory counterpart to :func:`load_config`.  It is
    intentionally unfrozen so callers can validate a proposed edit before any
    bytes are written.
    """
    if not isinstance(document, dict):
        raise ConfigError(f"{name}.yaml", None, "top-level document must be a mapping")

    root = config_root(config_dir)
    _validate(name, document, root / "schema" / f"{name}.schema.json")


def reset_cache() -> None:
    """Clear memoized configs, provenance, and resolved roots. Intended for tests."""
    _config_cache.clear()
    _provenance_cache.clear()
    _root_cache.clear()


def validate_all_configs(*, config_dir: str | os.PathLike[str] | None = None) -> tuple[Any, ...]:
    """Validate and cache every catalog before application startup side effects."""
    return tuple(load_config(name, config_dir=config_dir) for name in CONFIG_NAMES)


def load_config(name: str, *, config_dir: str | os.PathLike[str] | None = None) -> Any:
    """Load, schema-validate, and freeze ``config/<name>.yaml``.

    Memoized per resolved config root and name, so repeated calls (including
    across module re-execution under multiprocessing) are cheap and return
    the identical frozen object.
    """
    root = config_root(config_dir)
    cache_key = (str(root), name)
    if cache_key in _config_cache:
        return _config_cache[cache_key]

    yaml_path = root / f"{name}.yaml"
    if not yaml_path.is_file():
        raise ConfigError(f"{name}.yaml", None, f"file not found at {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh)

    if not isinstance(document, dict):
        raise ConfigError(f"{name}.yaml", None, "top-level document must be a mapping")

    validate_document(name, document, config_dir=root)

    frozen = _freeze(document)
    _config_cache[cache_key] = frozen
    _provenance_cache[cache_key] = {
        "path": str(yaml_path),
        "schema_version": document.get("schema_version"),
    }
    return frozen


def get_provenance(name: str, *, config_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Sanitized provenance (path + schema_version, no secrets) for a loaded config."""
    root = config_root(config_dir)
    cache_key = (str(root), name)
    if cache_key not in _provenance_cache:
        load_config(name, config_dir=config_dir)
    return dict(_provenance_cache[cache_key])


def loaded_config_names(*, config_dir: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """Catalogs this process has actually loaded, in load order.

    Lets a diagnostic report what a process read without asking
    :func:`get_provenance` for all of ``CONFIG_NAMES``, which would load and
    validate every catalog just to describe it.
    """
    root = str(config_root(config_dir))
    return tuple(name for cached_root, name in _provenance_cache if cached_root == root)
