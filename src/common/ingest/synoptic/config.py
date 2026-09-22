"""RAP ingest settings read from ``config/synoptic_rap.yaml``.

Accessors rather than module constants so the catalog is read per call. A
``--config-dir`` may be resolved after this module is imported -- spawned
accessories receive no argv and re-resolve the root themselves -- and a
module-level read would have frozen the repo default at import time.
"""

from common.config.loader import load_config
from common.config.overlay import resolve

_CONFIG_NAME = "synoptic_rap"

RAP_MAX_AGE_ENV = "EDGEWARN_RAP_MAX_AGE_MINUTES"


def _rap():
    """The ``rap`` section. ``load_config`` is memoized, so this is cheap."""
    return load_config(_CONFIG_NAME)["rap"]


def rap_bucket() -> str:
    return _rap()["bucket"]


def rap_file_pattern() -> str:
    """Remote object name, formatted with ``hour``."""
    return _rap()["file_pattern"]


def rap_dir_pattern() -> str:
    """Remote parent directory, formatted with ``date``."""
    return _rap()["dir_pattern"]


def rap_local_file_pattern() -> str:
    """Local name, formatted with ``date`` and ``hour``.

    Not derivable from :func:`rap_file_pattern`: it is uppercased and carries the
    date, which the remote name does not.
    """
    return _rap()["local_file_pattern"]


def rap_date_format() -> str:
    """``strftime`` format for the ``date`` field of both patterns."""
    return _rap()["date_format"]


def rap_filename_regex() -> str:
    """Parses :func:`rap_local_file_pattern` back into a timestamp."""
    return _rap()["filename_regex"]


def rap_lookback_step_hours() -> int:
    return _rap()["lookback_step_hours"]


def rap_max_files() -> int:
    return _rap()["max_files"]


def rap_nomads_base_url() -> str:
    return _rap()["nomads_base_url"]


def rap_nomads_timeout_seconds() -> int:
    return _rap()["nomads_timeout_seconds"]


def rap_nomads_chunk_size_bytes() -> int:
    return _rap()["nomads_chunk_size_bytes"]


def get_rap_max_age_minutes() -> int:
    """Return the configured maximum RAP analysis age.

    The environment variable outranks the catalog, per the project's
    CLI > env > YAML precedence. ``minimum=0`` is what keeps a malformed or
    negative override an outright error here; it was a hand-written parse until
    ``overlay.resolve`` learned to enforce a bound, and the rejection -- not the
    hand-written form -- was the part worth keeping.
    """
    return resolve(
        None,
        env_names=(RAP_MAX_AGE_ENV,),
        yaml_value=_rap()["max_age_minutes"],
        key="synoptic_rap.rap.max_age_minutes",
        minimum=0,
    )
