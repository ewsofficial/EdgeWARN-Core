"""Registry path binding is explicit, replaceable and side-effect free."""
import json
from pathlib import Path
import shutil

import pytest
import yaml

import util.file as fs
from common.config.loader import reset_cache
from common.ingest.mrms.registry import build_registry

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def restore_paths():
    base = fs.BASE_DIR
    yield
    fs.initialize_filesystem(base)
    reset_cache()


def test_binding_exact_aliases_and_disabled_removal(tmp_path):
    registry = build_registry({'products': ['MRMS_MergedAzShear_3-6kmAGL_00.50']}, tmp_path)
    fs.bind_mrms_paths(registry)
    expected = tmp_path / 'data/MRMS_MergedAzShear_3-6kmAGL'
    assert fs.MRMS_PATHS['MRMS_MergedAzShear_3-6kmAGL'] == expected
    assert getattr(fs, 'MRMS_MergedAzShear_3-6kmAGL') == expected
    assert fs.MRMS_AZSHEARMID_DIR == expected
    assert not (tmp_path / 'data').exists()
    fs.bind_mrms_paths(build_registry({'products': []}, tmp_path / 'new'))
    assert not hasattr(fs, 'MRMS_MergedAzShear_3-6kmAGL')
    assert not hasattr(fs, 'MRMS_AZSHEARMID_DIR')
    assert fs.MRMS_COMPOSITE_DIR == tmp_path / 'new/data/MRMS_MergedReflectivityQCComposite'


def test_explicit_creation_and_symlink_containment(tmp_path):
    registry = build_registry({'products': []}, tmp_path / 'base')
    fs.bind_mrms_paths(registry)
    fs.ensure_mrms_directories(registry)
    assert all(p.directory.is_dir() for p in registry.products)
    target = registry.products[0].directory
    target.rmdir()
    target.symlink_to(tmp_path)
    with pytest.raises(ValueError, match='escapes'):
        fs.bind_mrms_paths(registry)
    with pytest.raises(ValueError, match='escapes'):
        fs.ensure_mrms_directories(registry)


def test_spawn_rebuild_checks_fingerprint_before_creation(tmp_path):
    registry = build_registry({'products': []}, tmp_path)
    rebuilt = build_registry(json.loads(registry.normalized_config_json), tmp_path)
    fs.bind_mrms_paths(rebuilt, expected_fingerprint=registry.fingerprint)
    with pytest.raises(ValueError, match='fingerprint'):
        fs.bind_mrms_paths(rebuilt, expected_fingerprint='wrong')
    assert not (tmp_path / 'data').exists()


def test_config_and_base_rebinding(tmp_path):
    config = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', config)
    fixture = json.loads((ROOT / 'tests/fixtures/config/mrms_v2_validation.json').read_text())
    doc = fixture['documents']['ingest']
    doc['mrms']['products'] = ['MRMS_NewProduct_01.25']
    (config / 'ingest.yaml').write_text(yaml.safe_dump(doc))
    fs.initialize_filesystem(tmp_path / 'runtime', config_dir=config)
    from common.ingest.mrms.config import get_mrms_modifiers
    assert any(mod == 'NewProduct_01.25' for _, mod, _ in get_mrms_modifiers())
    assert getattr(fs, 'MRMS_NewProduct') == tmp_path / 'runtime/data/MRMS_NewProduct'
    assert not (tmp_path / 'runtime').exists()
    fs.initialize_filesystem(tmp_path / 'legacy', config_dir=ROOT / 'config')
    assert not hasattr(fs, 'MRMS_NewProduct')
    assert fs.MRMS_COMPOSITE_DIR == tmp_path / 'legacy/data/MRMS_MergedReflectivityQCComposite'


def test_fresh_process_rebuilds_and_rejects_changed_config(tmp_path):
    import os
    import subprocess
    import sys
    config = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', config)
    fixture = json.loads((ROOT / 'tests/fixtures/config/mrms_v2_validation.json').read_text())
    doc = fixture['documents']['ingest']
    (config / 'ingest.yaml').write_text(yaml.safe_dump(doc))
    base = tmp_path / 'runtime'
    fs.initialize_filesystem(base, config_dir=config)
    fingerprint = fs.MRMS_REGISTRY.fingerprint
    code = '''import sys
import util.file as fs
fs.initialize_filesystem(sys.argv[1], config_dir=sys.argv[2], expected_mrms_fingerprint=sys.argv[3])
assert fs.MRMS_REGISTRY.fingerprint == sys.argv[3]
'''
    command = [sys.executable, '-c', code, str(base), str(config), fingerprint]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(filter(None, [str(ROOT / 'src'), os.environ.get('PYTHONPATH')]))}
    first = subprocess.run(command, capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stderr
    doc['mrms']['products'] = []
    (config / 'ingest.yaml').write_text(yaml.safe_dump(doc))
    changed = subprocess.run(command, capture_output=True, text=True, env=env)
    assert changed.returncode != 0
    assert 'fingerprint' in changed.stderr
    assert not base.exists()


def test_v2_source_and_phase_accessors(tmp_path):
    config = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', config)
    fixture = json.loads((ROOT / 'tests/fixtures/config/mrms_v2_validation.json').read_text())
    doc = fixture['documents']['ingest']
    doc['mrms']['products'] = ['MRMS_NewProduct_01.25']
    (config / 'ingest.yaml').write_text(yaml.safe_dump(doc))
    fs.initialize_filesystem(tmp_path / 'runtime', config_dir=config)
    from common.ingest.mrms.config import get_check_modifiers, mrms_bucket
    from common.ingest.mrms.main import get_detection_modifiers, get_other_modifiers
    from common.ingest.mrms.https_client import HttpsFileFinder
    from datetime import datetime, timezone
    assert len(get_check_modifiers()) == 3
    assert set(get_detection_modifiers()) == {'MergedReflectivityQCComposite_00.50', 'PrecipFlag_00.00', None}
    assert get_other_modifiers() == ['NewProduct_01.25']
    assert mrms_bucket() == 'noaa-mrms-pds'
    finder = HttpsFileFinder(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert finder.construct_url('CONUS', 'NewProduct_01.25').endswith('/2D/NewProduct')
    assert finder.construct_url('ProbSevere', None).endswith('/data/ProbSevere')
