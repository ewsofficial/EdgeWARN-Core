"""Configuration-root selection shared by package commands."""

from __future__ import annotations

import os
from pathlib import Path


def registered_config_root() -> Path:
    """Return the installation's stable default, independent of the caller CWD."""
    from common.config.loader import config_root

    return config_root()


def resolve_config_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve package CLI precedence: explicit path, environment, installation."""
    if explicit is None:
        return registered_config_root()

    root = Path(explicit).expanduser().resolve()
    if not root.is_dir() or not (root / "runtime.yaml").is_file():
        raise ValueError(f"--config-path does not contain runtime.yaml: {root}")
    return root
