"""The release-owned Node validation asset also travels in Python wheels."""
from importlib.resources import files
import json
from pathlib import Path
import tomllib

from common.ingest.mrms.core_contract import contract_document


def test_mrms_core_contract_is_declared_package_data():
    root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    assert "core-contract.json" in metadata["tool"]["setuptools"]["package-data"]["common.ingest.mrms"]
    asset = files("common.ingest.mrms").joinpath("core-contract.json")
    assert json.loads(asset.read_text()) == contract_document()
    # Node loads the same packaged source-tree asset; no second maintained list.
    assert root.joinpath("src/common/ingest/mrms/core-contract.json").is_file()


def test_installed_migration_includes_node_validator_and_dependency_authorities():
    root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    data = metadata["tool"]["setuptools"]["data-files"]
    expected = {
        "share/edgewarn": ["package.json", "package-lock.json"],
        "share/edgewarn/scripts": ["scripts/validate-config.js"],
        "share/edgewarn/src/config": ["src/config/loader.js", "src/config/mrms-products.js"],
        "share/edgewarn/src/common/ingest/mrms": ["src/common/ingest/mrms/core-contract.json"],
    }
    for destination, sources in expected.items():
        assert data[destination] == sources
        assert all((root / source).is_file() for source in sources)
