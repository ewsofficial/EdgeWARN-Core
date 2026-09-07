import json
from pathlib import Path

import util.release as release


def test_get_release_version_falls_back_to_package_json(monkeypatch):
    package_json_path = Path(__file__).resolve().parents[2] / "package.json"
    expected = json.loads(package_json_path.read_text())["version"]
    release.get_release_version.cache_clear()

    def missing_distribution(_name):
        raise release.PackageNotFoundError

    monkeypatch.setattr(release, "distribution_version", missing_distribution)

    assert release.get_release_version() == expected

    release.get_release_version.cache_clear()


def test_get_release_version_prefers_installed_metadata(monkeypatch):
    release.get_release_version.cache_clear()
    monkeypatch.setattr(release, "distribution_version", lambda name: "9.8.7")

    assert release.get_release_version() == "9.8.7"

    release.get_release_version.cache_clear()
