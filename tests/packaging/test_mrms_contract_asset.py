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
