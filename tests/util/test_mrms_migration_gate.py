"""A crashed migration cannot be mistaken for a startup-ready catalog."""

import json

import pytest

from util.runtime.mrms_migration import IncompleteMrmsMigration, require_completed_migration


def test_absent_journal_is_read_only(tmp_path):
    require_completed_migration(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('status', ['complete', 'rolled-back'])
def test_terminal_journal_permits_startup(tmp_path, status):
    journal = tmp_path / '.mrms-migration/journal.json'
    journal.parent.mkdir()
    journal.write_text(json.dumps(dict(schema_version=1, status=status)))
    require_completed_migration(tmp_path)


@pytest.mark.parametrize('payload', [
    'not JSON', '[]', 'null', '{}',
    '{"schema_version": 2, "status": "complete"}',
    '{"schema_version": true, "status": "complete"}',
    '{"schema_version": 1, "status": "applying"}',
    '{"schema_version": 1, "status": "rolling-back"}',
])
def test_unverifiable_journal_refuses_startup(tmp_path, payload):
    journal = tmp_path / '.mrms-migration/journal.json'
    journal.parent.mkdir()
    journal.write_text(payload)
    with pytest.raises(IncompleteMrmsMigration, match='--resume or --rollback'):
        require_completed_migration(tmp_path)
    assert journal.read_text() == payload


def test_escaping_or_dangling_symlink_refuses_startup(tmp_path):
    root = tmp_path / 'config'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / '.mrms-migration').symlink_to(outside, target_is_directory=True)
    with pytest.raises(IncompleteMrmsMigration):
        require_completed_migration(root)
    (outside / 'journal.json').write_text('{"schema_version": 1, "status": "complete"}')
    with pytest.raises(IncompleteMrmsMigration):
        require_completed_migration(root)
