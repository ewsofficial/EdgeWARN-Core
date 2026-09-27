"""Shared parity cases and read-only migration acceptance tests."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest
import yaml

from common.config.loader import ConfigError, validate_document, validate_all_configs, reset_cache
from edgewarn_cli.configure import edit_configuration
from edgewarn_cli.main import main
from edgewarn_cli.migrate_mrms import convert_documents, legacy_contract, plan_migration

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((ROOT / 'tests/fixtures/config/mrms_v2_validation.json').read_text())


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda c: c['label'])
def test_shared_validation(case):
    document = deepcopy(FIXTURE['documents'][case['name']])
    parent = document
    for part in case['path'][:-1]:
        parent = parent[part]
    parent[case['path'][-1]] = case['value']
    if case['valid']:
        validate_document(case['name'], document, config_dir=ROOT / 'config')
    else:
        with pytest.raises(ConfigError):
            validate_document(case['name'], document, config_dir=ROOT / 'config')


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_dry_run_and_converted_tree(tmp_path, capsys):
    root = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', root)
    base = tmp_path / 'runtime'
    raw = base / 'data/MRMS_EchoTop18'
    raw.mkdir(parents=True)
    (raw / 'input.grib2').write_bytes(b'fixture')
    before = snapshot(tmp_path)
    assert main(['migrate-mrms', '--config-path', str(root), '--base-dir', str(base)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert snapshot(tmp_path) == before
    assert len([p for p in report['paths'] if p['action'] == 'rename']) == 10
    assert len(report['converted_documents']['ingest']['mrms']['products']) == 18
    for name, doc in report['converted_documents'].items():
        (root / f'{name}.yaml').write_text(yaml.safe_dump(doc))
    validate_all_configs(config_dir=root)
    reset_cache()
    assert main(['configure', '--config-path', str(root), 'runtime.run.disable_stormprob', 'true']) == 0
    before = (root / 'ingest.yaml').read_bytes()
    with pytest.raises(ConfigError):
        edit_configuration(root, 'ingest.mrms.products.0', 'MRMS_PrecipFlag_01.00')
    assert (root / 'ingest.yaml').read_bytes() == before
    reset_cache()


def test_pure_conversion_preserves_settings_and_reports_customizations():
    docs = {name: yaml.safe_load((ROOT / 'config' / f'{name}.yaml').read_text()) for name in FIXTURE['documents']}
    docs['ingest']['mrms']['cleanup_max_age_minutes'] = 90
    docs['ingest']['mrms']['ncep_https']['sync_timeout_seconds'] = 17
    docs['ingest']['mrms']['products'][0]['outdir'] = 'CUSTOM_DIR'
    docs['ingest']['mrms']['bucket'] = 'custom-bucket'
    original = deepcopy(docs)
    converted, conflicts, restored = convert_documents(docs, legacy_contract())
    assert docs == original
    assert converted['ingest']['mrms']['cleanup_max_age_minutes'] == 90
    assert converted['ingest']['mrms']['ncep_https']['sync_timeout_seconds'] == 17
    assert converted['ingest']['goes'] == docs['ingest']['goes']
    assert converted['ewmrs_render']['goes_layers'] == docs['ewmrs_render']['goes_layers']
    assert any('CUSTOM_DIR' in c for c in conflicts)
    assert any('bucket' in c for c in conflicts)
    assert restored == []


def test_conflicts_and_runtime_records(tmp_path):
    base = tmp_path / 'runtime'
    for name in ('MRMS_EchoTop18', 'MRMS_EchoTop_18'):
        (base / 'data' / name).mkdir(parents=True)
    (base / 'data/MRMS_QPE').symlink_to(tmp_path)
    record = base / 'state/realtime/cycles/pending.json'
    record.parent.mkdir(parents=True)
    record.write_text('{}')
    before = snapshot(base)
    report = plan_migration(ROOT / 'config', base)
    assert any('Target conflict' in c for c in report['conflicts'])
    assert any('escapes' in c for c in report['conflicts'])
    assert str(record) in report['runtime_records_requiring_backlog_pin_review']
    assert snapshot(base) == before


def test_v2_schema_rejects_v1_with_migration_hint(tmp_path):
    root = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', root)
    shutil.copyfile(root / 'schema/ingest.v2.schema.json', root / 'schema/ingest.schema.json')
    old = yaml.safe_load((root / 'ingest.yaml').read_text())
    with pytest.raises(ConfigError, match='migrate-mrms'):
        validate_document('ingest', old, config_dir=root)


def test_conversion_matches_shared_node_fixture_and_restores_reserved():
    docs = {name: yaml.safe_load((ROOT / 'config' / f'{name}.yaml').read_text()) for name in FIXTURE['documents']}
    converted, conflicts, restored = convert_documents(docs, legacy_contract())
    assert converted == FIXTURE['documents']
    assert conflicts == restored == []
    docs['ingest']['mrms']['products'] = [p for p in docs['ingest']['mrms']['products'] if p['region'] != 'ProbSevere']
    _, _, restored = convert_documents(docs, legacy_contract())
    assert restored == ['MRMS_ProbSevere']


def test_migration_asset_packaging_and_baseline():
    import tomllib
    metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert 'mrms-v1.json' in metadata['tool']['setuptools']['package-data']['common.config']
    baseline = legacy_contract()
    assert baseline['mrms'] == yaml.safe_load((ROOT / 'config/ingest.yaml').read_text())['mrms']
    assert len(baseline['paths']) == 21


def test_apply_is_not_available(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(['migrate-mrms', '--config-path', str(ROOT / 'config'), '--base-dir', str(tmp_path), '--apply'])
    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []
