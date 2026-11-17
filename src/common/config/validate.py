"""Validate every known config/*.yaml file against its JSON Schema.

Usage: python -m common.config.validate
Override the config/ directory via the EDGEWARN_CONFIG_DIR environment variable.
"""
from __future__ import annotations

import sys

from common.config import loader as config_loader


def main() -> int:
    failures = 0
    for name in config_loader.CONFIG_NAMES:
        try:
            config_loader.load_config(name)
        except config_loader.ConfigError as exc:
            print(f"FAIL {name} -> {exc}")
            failures += 1
        else:
            print(f"OK   {name}")
    if not failures:
        try:
            config_loader.validate_catalog_invariants()
        except config_loader.ConfigError as exc:
            print(f"FAIL catalog invariants -> {exc}")
            failures += 1
        else:
            print("OK   catalog invariants")
    if failures:
        print(f"{failures} config validation check(s) failed")
        return 1
    print(f"All {len(config_loader.CONFIG_NAMES)} config files passed validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
