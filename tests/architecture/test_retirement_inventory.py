"""The retired service must not return through code, configuration, or packaging."""
import subprocess
from pathlib import Path


def test_retired_service_has_no_live_references():
    root = Path(__file__).resolve().parents[2]
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root, text=True).splitlines()
    needle = "ew" + "mrs"
    matches = []
    for name in files:
        if name.startswith("plans/") or name == "CHANGELOG.md":
            continue
        path = root / name
        if path.is_file() and (needle in name.lower() or needle.encode() in path.read_bytes().lower()):
            matches.append(name)
    assert matches == []
