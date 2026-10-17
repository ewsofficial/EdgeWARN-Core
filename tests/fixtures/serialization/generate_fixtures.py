"""Regenerate deterministic NEXRAD serialization fixtures with the production encoder.

Run with PYTHONPATH=src python tests/fixtures/serialization/generate_fixtures.py.
"""
from pathlib import Path
import numpy as np
from NEXRAD import render as nexrad_render
FIXTURES_ROOT = Path(__file__).resolve().parent
TIMESTAMP = "20260317-200000"

def build_nexrad_field(out_root: Path) -> None:
    """One DBZH field (2 azimuths x 3 ranges) in the GUI variable-bin layout."""
    data = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, np.nan]], dtype=np.float32)
    azimuths = np.array([0.5, 1.5], dtype=np.float32)
    ranges = np.array([125.0, 250.0, 375.0], dtype=np.float32)
    field_path = (
        out_root / "nexrad" / "KTLH" / "0.5"
        / nexrad_render.nexrad_render_variable_bin_name(
            "KTLH", TIMESTAMP, "0.5", "DBZH"
        )
    )
    field_path.parent.mkdir(parents=True, exist_ok=True)
    nexrad_render._write_nexrad_variable_bin(field_path, data, azimuths, ranges)

BUILDERS = {"nexrad_field": build_nexrad_field}

def build_all(out_root):
    out_root = Path(out_root)
    build_nexrad_field(out_root)
    return out_root

if __name__ == "__main__":
    build_all(FIXTURES_ROOT)
