import sys
from pathlib import Path
import pytest
from unittest.mock import MagicMock

# Add src to pythonpath so we can import EdgeWARN
project_root = Path(__file__).parent.parent
src_path = project_root / "src"
sys.path.insert(0, str(src_path))

_PROCESS_TEST_FILES = {
    "test_worker_pool_recovery.py",
    "test_runtime.py",
    "test_runtime_correctness_reproductions.py",
    "test_run_all_launcher.py",
    "test_package_run.py",
    "test_runner.py",
}
_CONNECTED_CORE_FILES = {
    "test_durable_handoff_wiring.py",
    "test_publication.py",
    "test_read_only_api.py",
    "test_transaction_concurrency.py",
}


def pytest_collection_modifyitems(items):
    """Apply mutually understandable lane markers from stable ownership."""
    for item in items:
        relative = Path(str(item.path)).resolve().relative_to(project_root)
        if "integration" in relative.parts or relative.name in _CONNECTED_CORE_FILES:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)
        if relative.name in _PROCESS_TEST_FILES:
            item.add_marker(pytest.mark.process)
            item.add_marker(pytest.mark.slow)

@pytest.fixture
def mock_io_manager():
    """Mock IOManager to capturing log outputs."""
    mock = MagicMock()
    # Configure common methods
    mock.write_info = MagicMock()
    mock.write_error = MagicMock()
    mock.write_warning = MagicMock()
    mock.write_debug = MagicMock()
    return mock

@pytest.fixture
def mock_fs(tmp_path):
    """
    Mock file system constants.
    Uses tmp_path to create a safe playground for file operations.
    """
    # Create the directory structure in tmp_path
    (tmp_path / "surface").mkdir()
    (tmp_path / "metar").mkdir()
    (tmp_path / "nws").mkdir()
    (tmp_path / "stormcell").mkdir()
    (tmp_path / "cell").mkdir()
    
    # We'll need to patch util.file within the tests usually, 
    # but we can provide the paths here for convenience
    return tmp_path

@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Give every correctness test a disposable, fully restored runtime root."""
    monkeypatch.setenv("EDGEWARN_ENV", "test")
    from common.config import loader as config_loader
    from common.config import overlay
    from util import file as fs

    config_root = project_root / "config"
    monkeypatch.setenv("EDGEWARN_CONFIG_DIR", str(config_root))
    monkeypatch.setenv("EDGEWARN_BASE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("BASE_DIR", raising=False)

    path_state = {
        name: value
        for name, value in vars(fs).items()
        if name.isupper() and isinstance(value, Path)
    }
    config_loader.reset_cache()
    overlay.reset_origins()
    fs.initialize_filesystem(tmp_path / "runtime")

    yield

    for name, value in path_state.items():
        setattr(fs, name, value)
    config_loader.reset_cache()
    overlay.reset_origins()
