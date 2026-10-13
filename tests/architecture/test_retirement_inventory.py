"""Temporary retirement inventory guard."""
import subprocess
from pathlib import Path

def test_retirement_inventory_does_not_grow():
    root = Path(__file__).resolve().parents[2]
    files = subprocess.check_output(["git", "ls-files", "src", "config", "docs", "tests", "scripts", "benchmarks"], cwd=root, text=True).splitlines()
    count = sum((root / f).read_text(errors="ignore").lower().count("ew" + "mrs") for f in files if (root / f).is_file())
    assert count <= 985
