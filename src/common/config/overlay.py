"""Generic CLI > environment > YAML precedence resolution.

Works uniformly for booleans too: ``argparse.BooleanOptionalAction`` with
``default=None`` produces ``True``/``False``/``None`` (unset), so the same
``is not None`` check used for other types applies.

This module is the only place that parses an environment variable into a
configuration *value*. The one other environment read in the config layer is
``loader.config_root``'s ``EDGEWARN_CONFIG_DIR`` (``loader.py:155``), which selects
*which catalog to load* and so cannot come from a catalog; it is resolved before any
key exists and stays outside this precedence chain by necessity.
The retired ``plans/source-configuration-extraction-plan.md`` named
``synoptic/config.py``'s ``get_rap_max_age_minutes`` as the reference shape for
an override -- a named env-var constant, an unset test, and a ``ValueError``
that re-quotes the raw value -- while also requiring that no domain module parse
the environment itself. Those two only reconcile one way: the reference is a
description of the *semantics*, not of the location, so the semantics live here
and domain modules pass ``env_names=`` instead of reading ``os.environ``.
Concretely that means ``value_type`` for a key whose YAML value is ``null`` (so
there is no reference value to infer a type from), ``minimum`` for a bound that
was previously enforced by hand, and a message naming the variable rather than
the bare ``invalid literal for int()`` that ``int(raw)`` used to raise.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any, Iterable

_TRUE_STRINGS = {"1", "true", "yes", "on"}

# Which layer supplied each key, keyed by dotted catalog path. Write-only from
# :func:`resolve`, which never reads it, so a stale or missing entry cannot
# affect resolution. Labels are ``"cli"``, ``"env:<VAR>"``, or ``"yaml"`` -- the
# layer and, for the environment, the variable that won; never the value, so a
# key holding a secret cannot leak into a diagnostic.
#
# Accessors are called per read, some inside poll loops, so this is last-write-
# wins on a key rather than an append log. Each process records its own view:
# children spawned by the supervisor get no argv and re-resolve independently.
_origins: dict[str, str] = {}


def _target_type(value_type: type | None, yaml_value: Any) -> type | None:
    """The type an environment string should be parsed into.

    Inferred from the YAML value, because the catalog is authoritative about the
    shape of every key. ``value_type`` is the explicit override for the case the
    inference cannot cover: a tri-state key whose YAML value is ``null``, which
    carries no type at all and would otherwise leave the raw string uncoerced.
    """
    if value_type is not None:
        return value_type
    # bool is checked first because it is a subclass of int.
    for candidate in (bool, int, float):
        if isinstance(yaml_value, candidate):
            return candidate
    return None


def _expected(target: type, minimum: Any) -> str:
    noun = "integer" if target is int else "number"
    article = "an" if target is int else "a"
    if minimum == 0:
        # Spelled this way rather than ">= 0" because it is the wording the RAP
        # age override has always used, and its error message is asserted on.
        return f"a non-negative {noun}"
    if minimum is not None:
        return f"{article} {noun} >= {minimum}"
    return f"{article} {noun}"


def _coerce(raw: str, target: type | None, env_name: str, minimum: Any) -> Any:
    if target is bool:
        # An unrecognized value reads false rather than falling through as a
        # (truthy) string. This is the pre-existing semantics of the boolean
        # environment variables being routed through here: the opt-in set is
        # tested after strip().lower(), so "2" and "enabled" mean off.
        return raw.strip().lower() in _TRUE_STRINGS
    if target is int or target is float:
        expected = _expected(target, minimum)
        try:
            value = target(raw)
        except ValueError as exc:
            raise ValueError(f"{env_name} must be {expected}, got {raw!r}") from exc
        if minimum is not None and value < minimum:
            raise ValueError(f"{env_name} must be {expected}, got {raw!r}")
        return value
    return raw


def resolve(
    cli_value: Any,
    *,
    env_names: Iterable[str] = (),
    yaml_value: Any = None,
    key: str | None = None,
    value_type: type | None = None,
    minimum: Any = None,
) -> Any:
    """Return the highest-precedence value among CLI, environment, and YAML.

    ``cli_value is None`` is treated as "the CLI flag was not supplied" for
    both non-boolean flags (``default=None``) and boolean flags using
    ``argparse.BooleanOptionalAction`` (also ``default=None``).

    ``key`` is the dotted catalog path this value came from. When supplied, the
    winning layer is recorded for :func:`overrides`; a value resolved without it
    is simply absent from that report.

    ``value_type`` names the parse target when ``yaml_value`` is ``None`` and so
    cannot supply one. ``minimum`` rejects an out-of-range override instead of
    letting it through; both raise ``ValueError`` naming the variable.

    An environment variable that is set but blank counts as unset and the search
    continues. Exporting an empty value is how a shell or a compose file clears a
    setting, so a variable the operator believes is switched off must not make the
    process raise.

    Two of the three migrated sites already resolved a blank to the same answer
    they resolve it to now, by different routes: ``render.py`` tested
    ``if env_value:``, so a blank fell through to the catalog, and
    ``performance.py`` tested ``raw in {"1", "true", ...}`` after ``strip()``, so a
    blank was simply not truthy and the tracker stayed off -- which is still what
    happens, since the catalog's ``perf_tracker`` is ``null`` and the call site
    collapses that through ``bool()``. The RAP age override is the one that
    differed: it tested ``if raw_value is None``, so a blank reached ``int("")``
    and raised.
    Widening that one to "blank is unset" is an intended behavior change, not a
    side effect of the move; it is the only case where a value the old code
    rejected is now accepted, and ``test_overlay.py`` pins it at both the generic
    layer and the RAP site so a future tightening has to be deliberate.
    """
    if cli_value is not None:
        if key is not None:
            _origins[key] = "cli"
        return cli_value

    target = _target_type(value_type, yaml_value)
    for env_name in env_names:
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            continue
        # Coerce before recording: an unparseable override raises, and a
        # layer that never produced a value must not be named the winner.
        coerced = _coerce(raw, target, env_name, minimum)
        if key is not None:
            _origins[key] = f"env:{env_name}"
        return coerced

    if key is not None:
        _origins[key] = "yaml"
    return yaml_value


def overrides() -> dict[str, str]:
    """Keys this process resolved from somewhere other than YAML, path -> layer."""
    return {key: layer for key, layer in _origins.items() if layer != "yaml"}


def origins() -> dict[str, str]:
    """Every tracked key origin, including values resolved from YAML."""
    return dict(_origins)


def reset_origins() -> None:
    """Forget every recorded origin. Intended for tests."""
    _origins.clear()


def resolve_base_dir(
    cli_value: Any,
    filesystem: Any,
    *,
    env_names: Iterable[str] = ("EDGEWARN_BASE_DIR", "BASE_DIR"),
    system: str | None = None,
) -> Path:
    """Resolve the shared runtime base directory and record its provenance.

    ``filesystem`` is the validated ``filesystem.yaml`` document (or its
    ``base_dir`` section).  Keeping the platform defaults there prevents Python
    and Node from quietly maintaining separate catalogs.
    """
    defaults = filesystem["base_dir"] if "base_dir" in filesystem else filesystem
    platform_name = system or platform.system()
    yaml_value = defaults["windows"] if platform_name == "Windows" else defaults["posix"]
    selected = resolve(
        cli_value,
        env_names=env_names,
        yaml_value=yaml_value,
        key="filesystem.base_dir",
    )
    if platform_name != "Windows" and str(selected).startswith("~"):
        return Path(selected).expanduser()
    return Path(selected)


def inherited_process_path() -> str:
    """The ``PATH`` inherited from the launching process, for a child spawn.

    A supervisor launching a subprocess passes this along so the child can
    locate interpreters it shells out to, without inheriting the rest of the
    host environment. It is not configuration policy, but it is read from the
    environment, so it lives here to keep the environment-read boundary
    confined to the configuration infrastructure.
    """
    return os.environ.get("PATH", "")
