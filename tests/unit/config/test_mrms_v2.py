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
from edgewarn_cli.migrate_mrms import convert_documents, legacy_contract, plan_migration, execute_migration

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((ROOT / 'tests/fixtures/config/mrms_v2_validation.json').read_text())


def v1_documents():
    """Frozen release input, independent of the shipped v2 catalog."""
    from common.ingest.mrms.core_contract import LEGACY_ALIASES
    docs = deepcopy(FIXTURE['documents'])
    docs['ingest']['schema_version'] = 1
    docs['ingest']['mrms'] = deepcopy(legacy_contract()['mrms'])
    aliases = {identity: alias for alias, identity in LEGACY_ALIASES.items()}
    for name, key in [('integration', 'stats_datasets'), ('ewmrs_render', 'mrms_layers')]:
        for item in docs[name][key]:
            item['filepath'] = aliases[item.pop('product')]
    docs['runtime']['run'].pop('disable_stormprob', None)
    return docs


def v1_tree(root):
    shutil.copytree(ROOT / 'config', root)
    for name, doc in v1_documents().items():
        (root / f'{name}.yaml').write_text(yaml.safe_dump(doc))
    return root


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
    v1_tree(root)
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
    docs = v1_documents()
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
    report = plan_migration(v1_tree(tmp_path / 'config'), base)
    assert any('Target conflict' in c for c in report['conflicts'])
    assert any('escapes' in c for c in report['conflicts'])
    assert str(record) in report['runtime_records_requiring_backlog_pin_review']
    assert snapshot(base) == before


def test_v2_schema_rejects_v1_with_migration_hint(tmp_path):
    root = tmp_path / 'config'
    v1_tree(root)
    shutil.copyfile(root / 'schema/ingest.v2.schema.json', root / 'schema/ingest.schema.json')
    old = yaml.safe_load((root / 'ingest.yaml').read_text())
    with pytest.raises(ConfigError, match='migrate-mrms'):
        validate_document('ingest', old, config_dir=root)


def test_conversion_matches_shared_node_fixture_and_restores_reserved():
    docs = v1_documents()
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
    assert len(baseline['mrms']['products']) == 21
    assert len(baseline['paths']) == 21


def test_apply_resume_and_inverse_rollback(tmp_path):
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    source = base / 'data/MRMS_EchoTop18'
    source.mkdir(parents=True)
    (source / 'input').write_bytes(b'weather')
    original = {p: (root / p).read_bytes() for p in ['ingest.yaml', 'integration.yaml', 'ewmrs_render.yaml']}
    assert execute_migration(root, base)['status'] == 'complete'
    assert (base / 'data/MRMS_EchoTop_18/input').read_bytes() == b'weather'
    assert execute_migration(root, base, resume=True)['status'] == 'complete'
    assert execute_migration(root, base, rollback=True)['status'] == 'rolled-back'
    assert (source / 'input').read_bytes() == b'weather'
    assert all((root / p).read_bytes() == value for p, value in original.items())


def test_all_ten_renames_preserve_records_and_unrelated_files(tmp_path):
    from datetime import datetime, timezone
    from common.ingest.manifest import CycleInputManifest, StagedInput
    from util.runtime.handoff import ConsumerCheckpointStore, PhaseRecordPublisher, canonical_cycle_id

    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    legacy = legacy_contract()
    for alias, directory in legacy['paths'].items():
        path = base / 'data' / directory / 'input.grib2'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(alias.encode())
    untouched = base / 'data/unrelated/operator.dat'
    untouched.parent.mkdir()
    untouched.write_bytes(b'unrelated')
    gui = base / 'gui/MRMS_EchoTop18/render.bin'
    gui.parent.mkdir(parents=True)
    gui.write_bytes(b'gui bytes')

    # A drained record keeps its exact original input paths after migration.
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = StagedInput(product='EchoTop_18_00.50',
                         path=str(base / 'data/MRMS_EchoTop18/input.grib2'),
                         analysis_time=dt, source='fixture', family='mrms')
    PhaseRecordPublisher(base).publish('mrms-ready', CycleInputManifest(dt, inputs=(record,)))
    ConsumerCheckpointStore(base, 'ewmrs-mrms-ready').record(canonical_cycle_id(dt))
    original_config = snapshot(root)
    original_runtime = snapshot(base)
    state_bytes = snapshot(base / 'state/realtime')
    report = plan_migration(root, base)

    assert execute_migration(root, base)['status'] == 'complete'
    assert sum(item['action'] == 'rename' for item in report['paths']) == 10
    for item in report['paths']:
        target = Path(item['target']) / 'input.grib2'
        assert target.read_bytes() == original_runtime[str((Path(item['source']) / 'input.grib2').relative_to(base))]
        if item['action'] == 'rename':
            assert not Path(item['source']).exists()
    assert {name: content for name, content in snapshot(base / 'state/realtime').items()
            if not name.endswith('.lock')} == state_bytes
    assert untouched.read_bytes() == b'unrelated'
    assert gui.read_bytes() == b'gui bytes'
    for name in original_config:
        if name.endswith('.yaml') and name[:-5] not in report['config_changes']:
            assert (root / name).read_bytes() == original_config[name]

    assert execute_migration(root, base, rollback=True)['status'] == 'rolled-back'
    assert all((root / name).read_bytes() == content for name, content in original_config.items())
    assert all((base / name).read_bytes() == content for name, content in original_runtime.items())
    assert execute_migration(root, base, rollback=True)['status'] == 'rolled-back'


def test_interrupted_step_reconciles_resume(tmp_path, monkeypatch):
    import edgewarn_cli.migrate_mrms as migration
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    source = base / 'data/MRMS_EchoTop18'
    source.mkdir(parents=True)
    real_save = migration._save
    def crash_after_mutation(path, journal):
        if any(step['kind'] == 'rename' and step['status'] == 'done' for step in journal['steps']):
            raise OSError('simulated power loss')
        real_save(path, journal)
    monkeypatch.setattr(migration, '_save', crash_after_mutation)
    with pytest.raises(OSError, match='power loss'):
        execute_migration(root, base)
    monkeypatch.setattr(migration, '_save', real_save)
    assert execute_migration(root, base, resume=True)['status'] == 'complete'
    assert execute_migration(root, base, rollback=True)['status'] == 'rolled-back'
    assert source.is_dir()


def test_active_input_pins_refuse_apply(tmp_path):
    from common.ingest.replay import input_lock
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    before = snapshot(root)
    with input_lock(base):
        with pytest.raises(ValueError, match='pins'):
            execute_migration(root, base)
    assert snapshot(root) == before


def test_stopped_service_and_drained_backlog_interlocks(tmp_path):
    from util.runtime.handoff import _AdvisoryFileLock
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    with _AdvisoryFileLock(base / 'state/realtime/services/edgewarn.lock'):
        with pytest.raises(ValueError, match='stopped'):
            execute_migration(root, base)
    pending = base / 'state/realtime/cycles/20260101T000000Z/mrms-ready.json'
    pending.parent.mkdir(parents=True)
    pending.write_text('{}')
    with pytest.raises(ValueError, match='backlog'):
        execute_migration(root, base)


def test_nested_symlink_escape_refused(tmp_path):
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    raw = base / 'data/MRMS_EchoTop18'
    raw.mkdir(parents=True)
    (raw / 'escape').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='escapes'):
        execute_migration(root, base)
    assert not (root / '.mrms-migration/journal.json').exists()


def test_failed_node_validation_keeps_journal_incomplete(tmp_path, monkeypatch):
    import edgewarn_cli.migrate_mrms as migration
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    real_validate = migration.validate_node_catalog
    def unavailable(_):
        raise ValueError('Node validation failed')
    monkeypatch.setattr(migration, 'validate_node_catalog', unavailable)
    with pytest.raises(ValueError, match='Node validation'):
        execute_migration(root, base)
    journal = json.loads((root / '.mrms-migration/journal.json').read_text())
    assert journal['status'] == 'applying'
    monkeypatch.setattr(migration, 'validate_node_catalog', real_validate)
    assert execute_migration(root, base, resume=True)['status'] == 'complete'


def test_resume_rechecks_completed_directory_steps(tmp_path, monkeypatch):
    import edgewarn_cli.migrate_mrms as migration
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    source = base / 'data/MRMS_EchoTop18'
    source.mkdir(parents=True)
    real_validate = migration.validate_node_catalog
    def unavailable(_):
        raise ValueError('Node validation failed')
    monkeypatch.setattr(migration, 'validate_node_catalog', unavailable)
    with pytest.raises(ValueError, match='Node validation'):
        execute_migration(root, base)
    source.mkdir()
    (source / 'conflict').write_bytes(b'recreated old directory')
    monkeypatch.setattr(migration, 'validate_node_catalog', real_validate)
    with pytest.raises(ValueError, match='Target conflict'):
        execute_migration(root, base, resume=True)
    journal = json.loads((root / '.mrms-migration/journal.json').read_text())
    assert journal['status'] == 'applying'
    assert (source / 'conflict').read_bytes() == b'recreated old directory'


def test_interrupted_rollback_reconciles_its_inverse_rename(tmp_path, monkeypatch):
    import edgewarn_cli.migrate_mrms as migration
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    source = base / 'data/MRMS_EchoTop18'
    source.mkdir(parents=True)
    original = (root / 'ingest.yaml').read_bytes()
    assert execute_migration(root, base)['status'] == 'complete'
    real_rename = migration.os.rename
    def interrupt_after_rename(*args):
        real_rename(*args)
        raise OSError('interrupted inverse rename')
    monkeypatch.setattr(migration.os, 'rename', interrupt_after_rename)
    with pytest.raises(OSError, match='inverse rename'):
        execute_migration(root, base, rollback=True)
    monkeypatch.setattr(migration.os, 'rename', real_rename)
    with pytest.raises(ValueError, match='Interrupted rollback'):
        execute_migration(root, base, resume=True)
    assert execute_migration(root, base, rollback=True)['status'] == 'rolled-back'
    assert source.is_dir()
    assert (root / 'ingest.yaml').read_bytes() == original


def test_interruption_between_configuration_and_rename_rolls_back(tmp_path, monkeypatch):
    import edgewarn_cli.migrate_mrms as migration
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    (base / 'data/MRMS_EchoTop18').mkdir(parents=True)
    original = (root / 'ingest.yaml').read_bytes()
    real_rename = migration.os.rename
    def interrupt(*_):
        raise OSError('interrupted before rename')
    monkeypatch.setattr(migration.os, 'rename', interrupt)
    with pytest.raises(OSError, match='before rename'):
        execute_migration(root, base)
    monkeypatch.setattr(migration.os, 'rename', real_rename)
    assert execute_migration(root, base, rollback=True)['status'] == 'rolled-back'
    assert (root / 'ingest.yaml').read_bytes() == original
    assert (base / 'data/MRMS_EchoTop18').is_dir()


def test_config_commit_before_journal_completion_resumes(tmp_path, monkeypatch):
    import edgewarn_cli.migrate_mrms as migration
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    real_write = migration._write
    def interrupt(path, data):
        real_write(path, data)
        raise OSError('interrupted after config commit')
    monkeypatch.setattr(migration, '_write', interrupt)
    with pytest.raises(OSError, match='config commit'):
        execute_migration(root, base)
    monkeypatch.setattr(migration, '_write', real_write)
    assert execute_migration(root, base, resume=True)['status'] == 'complete'
    assert execute_migration(root, base, rollback=True)['status'] == 'rolled-back'


def test_cross_device_move_refused_before_config_write(tmp_path, monkeypatch):
    import os
    root = v1_tree(tmp_path / 'config')
    base = tmp_path / 'runtime'
    source = base / 'data/MRMS_EchoTop18'
    source.mkdir(parents=True)
    original = (root / 'ingest.yaml').read_bytes()
    real_stat = Path.stat
    def other_device(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == source:
            fields = list(result)
            fields[2] = result.st_dev + 1
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(Path, 'stat', other_device)
    with pytest.raises(ValueError, match='Cross-device'):
        execute_migration(root, base)
    assert (root / 'ingest.yaml').read_bytes() == original
    assert not (root / '.mrms-migration/journal.json').exists()
