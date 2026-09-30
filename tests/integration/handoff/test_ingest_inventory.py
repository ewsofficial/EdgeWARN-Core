"""Independent ingest contracts across source commit, restart and consumers."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from common.ingest.inventory import InputInventory
from common.ingest.manifest import StagedInput
from common.ingest.mrms.core_contract import IngestDependencies
from common.ingest.objects import CommittedInput
from common.ingest.replay import input_lock
from common.pipeline.readiness import evaluate_scan
from util.runtime.handoff import canonical_cycle_id
from util.runtime.ingest_handoff import IngestHandoff, IngestRecordError, render_job_id

T = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
RENDER = 'a' * 64


@pytest.fixture
def dependencies():
    return IngestDependencies(('A', 'B', 'ProbSevere'), ('A', 'ProbSevere'), ('I',),
        ('Late',), (), ('A', 'ProbSevere'), ('Late',), False, False, 30, 'b' * 64, auxiliary_settings_json='{' + '"rap":{"max_age_minutes":180}' + '}')


@pytest.fixture
def inventory(tmp_path, dependencies):
    return InputInventory(tmp_path, fingerprint=dependencies.fingerprint, run_id='first')


def completion(root, product='A', at=T, *, body=b'validated payload', family='mrms', suffix='grib2'):
    path = root / 'data' / product / f'MRMS_{product}_{at:%Y%m%d-%H%M%S}.{suffix}'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    staged = StagedInput(product, str(path), at, 's3', family)
    return CommittedInput(staged, hashlib.sha256(body).hexdigest(), 's3://bucket/key')


def add(inventory, product='A', at=T):
    committed = completion(inventory.base_dir, product, at)
    inventory.commit_input(committed)
    return committed


def checks(inventory):
    return [add(inventory, p) for p in ('A', 'B', 'ProbSevere')]


def no_mapping(inventory, input_id, fingerprint=RENDER):
    inventory.handoff.publish_render_ready(input_id)
    inventory.handoff.plan_render(input_id, [], fingerprint)
    inventory.handoff.acknowledge_input(input_id, fingerprint)


def test_recover_file_inventory_outbox_and_ack_crash_windows(inventory, dependencies):
    committed = completion(inventory.base_dir)
    with pytest.raises(IngestRecordError, match='validator'):
        inventory.reconcile([committed.record.path])
    adopted = inventory.reconcile([committed.record.path], revalidate=lambda path: committed)
    assert adopted['adopted'] == adopted['published'] == adopted['pending'] == (committed.input_id,)
    # Restart has no source acquisition and uses the same committed bytes/identity.
    restarted = InputInventory(inventory.base_dir, fingerprint=dependencies.fingerprint, run_id='restart')
    assert restarted.commit_input(replace(committed, source_locator='https://mirror', reused=True)).run_id == 'first'
    assert restarted.reconcile()['published'] == ()
    assert restarted.reconcile()['pending'] == (committed.input_id,)
    no_mapping(restarted, committed.input_id)
    assert restarted.reconcile()['pending'] == ()


def test_inventory_notification_failure_does_not_redownload(inventory, monkeypatch):
    committed = add(inventory)
    original = inventory.handoff._write
    def fail(kind, *args, **kwargs):
        if kind == 'render-ready':
            raise OSError('publication interrupted')
        return original(kind, *args, **kwargs)
    monkeypatch.setattr(inventory.handoff, '_write', fail)
    with pytest.raises(OSError):
        inventory.handoff.publish_render_ready(committed.input_id)
    assert inventory.handoff.read('input', committed.input_id)
    monkeypatch.setattr(inventory.handoff, '_write', original)
    assert inventory.reconcile()['published'] == (committed.input_id,)


@pytest.mark.parametrize('missing', ['A', 'B', 'ProbSevere'])
def test_each_missing_check_blocks_start(inventory, dependencies, missing):
    for product in ('A', 'B', 'ProbSevere', 'I', 'Late'):
        if product != missing:
            add(inventory, product)
    result = inventory.publish_scan(T, dependencies, at=T)
    assert result.start is None and result.integration is None
    assert result.missing_check == (missing,)
    assert inventory.handoff.read('core-start-ready', canonical_cycle_id(T)) is None


def test_previous_and_other_scan_inputs_do_not_satisfy_current_checks(inventory, dependencies):
    for product in dependencies.check:
        add(inventory, product, T - timedelta(minutes=2))
    assert evaluate_scan(T, inventory.valid_inputs(), dependencies).start is None
    # Check B cannot borrow the next scan within the manifest tolerance.
    add(inventory, 'A'); add(inventory, 'ProbSevere'); add(inventory, 'B', T + timedelta(minutes=2))
    assert evaluate_scan(T, inventory.valid_inputs(), dependencies).missing_check == ('B',)


def test_start_integration_final_pin_identical_detection_and_history(inventory, dependencies):
    previous = add(inventory, 'A', T - timedelta(minutes=2))
    current = checks(inventory)
    first = inventory.publish_scan(T, dependencies, at=T)
    assert first.start is not None and first.integration is None
    assert first.missing_integration == ('I',)
    key = canonical_cycle_id(T)
    start = inventory.handoff.read('core-start-ready', key)
    assert previous.input_id in start.data['input_ids']
    assert start.to_manifest().records_for_product('A')[0].role == 'previous'
    # New history cannot change an already published start selection.
    add(inventory, 'A', T - timedelta(seconds=30))
    add(inventory, 'I')
    second = inventory.publish_scan(T, dependencies, at=T + timedelta(seconds=10))
    assert second.integration is not None and second.final is None
    integration = inventory.handoff.read('core-integration-ready', key)
    assert integration.data['input_ids'][:len(start.data['input_ids'])] == start.data['input_ids']
    restarted = InputInventory(inventory.base_dir, fingerprint=dependencies.fingerprint, run_id='restart')
    third = restarted.publish_scan(T, dependencies, at=T + timedelta(seconds=30))
    assert third.final is not None and third.optional_dispositions == {'Late': 'expired'}
    frozen = restarted.handoff.read('core-final-ready', key)
    late = add(restarted, 'Late')
    restarted.publish_scan(T, dependencies, at=T + timedelta(seconds=40))
    assert restarted.handoff.read('core-final-ready', key).data == frozen.data
    restarted.handoff.publish_render_ready(late.input_id)
    assert late.input_id in restarted.reconcile()['pending']
    assert current[0].input_id in frozen.data['input_ids']


def test_two_late_same_scan_inputs_are_independent(inventory):
    first = add(inventory, 'Late')
    second = add(inventory, 'Other')
    for item in (first, second):
        inventory.handoff.publish_render_ready(item.input_id)
    assert set(inventory.reconcile()['pending']) == {first.input_id, second.input_id}
    no_mapping(inventory, first.input_id)
    assert inventory.reconcile()['pending'] == (second.input_id,)


def test_optional_success_and_failure_snapshot(inventory, dependencies):
    checks(inventory); add(inventory, 'I')
    failed = inventory.publish_scan(T, dependencies, at=T, optional_failures=['Late'])
    assert failed.final and failed.optional_dispositions == {'Late': 'failed'}


def test_auxiliary_gates_and_rap_reuse(tmp_path, dependencies):
    deps = replace(dependencies, rap_enabled=True, glm_enabled=True)
    inventory = InputInventory(tmp_path, fingerprint=deps.fingerprint, run_id='run')
    checks(inventory); add(inventory, 'I'); add(inventory, 'Late')
    assert inventory.publish_scan(T, deps, at=T).missing_integration == ('RAP', 'GLM')
    # Use the source's established filename parser.
    from common.ingest.synoptic.main import parse_rap_analysis_time
    path = tmp_path / 'data/rap/RAP.20260930-12z.awp130pgrbf00.grib2'
    path.parent.mkdir(parents=True); path.write_bytes(b'rap')
    assert parse_rap_analysis_time(path) == T
    rap = CommittedInput(StagedInput('RAP', str(path), T, 'synoptic', 'rap'),
                         hashlib.sha256(b'rap').hexdigest(), str(path))
    inventory.commit_input(rap)
    glm = tmp_path / 'data/glm/OR_GLM-L2-LCFA_G16_s20262731200000.nc'
    glm.parent.mkdir(parents=True); glm.write_bytes(b'glm')
    inventory.commit_input(CommittedInput(StagedInput('GLM-L2-LCFA', str(glm), T, 's3', 'goes'),
        hashlib.sha256(b'glm').hexdigest(), str(glm)))
    assert inventory.publish_scan(T, deps, at=T).integration is not None
    inventory.commit_input(replace(rap, reused=True))
    inventory.reconcile()
    assert len([r for r in inventory.handoff.records('render-ready') if r.data['input']['family'] == 'rap']) == 1


@pytest.mark.parametrize('failure', ['timestamp', 'digest', 'outside', 'unvalidated'])
def test_invalid_completion_never_enters_inventory(inventory, tmp_path, failure):
    item = completion(inventory.base_dir)
    if failure == 'timestamp':
        item = replace(item, record=replace(item.record, analysis_time=T + timedelta(minutes=2)))
    elif failure == 'digest':
        item = replace(item, sha256='0' * 64)
    elif failure == 'outside':
        item = replace(item, record=replace(item.record, path=str(tmp_path.parent / 'escape')))
    else:
        item = replace(item, record=replace(item.record, validated=False))
    with pytest.raises(IngestRecordError):
        inventory.commit_input(item)
    assert inventory.handoff.records('input') == ()


def test_corrected_content_does_not_overwrite_inventory(inventory):
    first = add(inventory)
    # A corrected mirror has another local path; it must not revise product/time.
    second = completion(inventory.base_dir, body=b'corrected')
    with pytest.raises(IngestRecordError, match='Conflicting'):
        inventory.commit_input(second)
    assert inventory.handoff.read('input', first.input_id).data['sha256'] == first.sha256
    with pytest.raises(IngestRecordError, match='changed'):
        inventory.valid_inputs()


@pytest.mark.parametrize('mutation', ['schema', 'fingerprint', 'key', 'path', 'evidence', 'boolean', 'time', 'extra'])
def test_strict_reader_rejects_incompatible_records(inventory, mutation):
    item = add(inventory)
    path = inventory.handoff.path('input', item.input_id)
    raw = json.loads(path.read_text())
    if mutation == 'schema': raw['schema_version'] = 2
    elif mutation == 'fingerprint': raw['fingerprint'] = 'f' * 64
    elif mutation == 'key': raw['key'] = '0' * 64
    elif mutation == 'path': raw['data']['input']['path'] = '/tmp/escape'
    elif mutation == 'evidence': raw['data']['validation'] = {}
    elif mutation == 'boolean': raw['data']['input']['validated'] = 'true'
    elif mutation == 'time': raw['data']['input']['analysis_time'] = '2026-09-30T12:00:00'
    else: raw['unknown'] = 'field'
    path.write_text(json.dumps(raw))
    with pytest.raises(IngestRecordError):
        inventory.handoff.read('input', item.input_id)
    with pytest.raises(IngestRecordError):
        inventory.commit_input(item)


def test_symlink_state_escape_is_rejected(inventory, tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    # A namespace symlink must stay inside the resolved runtime root.
    inventory.handoff.root.parent.mkdir(parents=True)
    inventory.handoff.root.symlink_to('/tmp')
    with pytest.raises(IngestRecordError, match='escapes'):
        inventory.handoff.read('input', '0' * 64)


def test_phase_republication_refuses_changed_selection(inventory, dependencies):
    checks(inventory)
    evaluation = inventory.publish_scan(T, dependencies, at=T)
    extra = add(inventory, 'Extra')
    manifest = evaluation.start.with_inputs([extra.record])
    with pytest.raises(IngestRecordError, match='Incompatible'):
        inventory.handoff.publish_phase('core-start-ready', manifest, (*evaluation.start_ids, extra.input_id))
    with pytest.raises(IngestRecordError, match='identities'):
        inventory.handoff.publish_phase('core-start-ready', evaluation.start, [])


def test_render_layers_retry_and_ack_loss(inventory, dependencies):
    item = add(inventory)
    h = inventory.handoff
    h.publish_render_ready(item.input_id)
    h.plan_render(item.input_id, ['reflectivity', 'quality'], RENDER)
    h.disposition('render-ack', '', input_id=item.input_id, layer_id='reflectivity',
                  render_fingerprint=RENDER, status='success')
    retry = h.disposition('render-ack', '', input_id=item.input_id, layer_id='quality',
                  render_fingerprint=RENDER, status='retry', attempts=1, retry_at=T + timedelta(seconds=5))
    assert not retry.retry_eligible(T) and retry.retry_eligible(T + timedelta(seconds=5))
    with pytest.raises(IngestRecordError, match='pending'):
        h.acknowledge_input(item.input_id, RENDER)
    assert inventory.reconcile()['pending'] == (item.input_id,)
    restart = IngestHandoff(inventory.base_dir, fingerprint=dependencies.fingerprint, run_id='restart')
    restart.disposition('render-ack', '', input_id=item.input_id, layer_id='quality',
                        render_fingerprint=RENDER, status='success', attempts=2)
    restart.acknowledge_input(item.input_id, RENDER)
    assert inventory.reconcile()['pending'] == ()
    # A new render configuration owns another reference even after old success.
    restart.plan_render(item.input_id, ['quality'], 'f' * 64)
    assert inventory.reconcile()['pending'] == (item.input_id,)


def test_expiry_terminal_and_core_consumption_are_durable(inventory, dependencies):
    checks(inventory)
    inventory.publish_scan(T, dependencies, at=T)
    key = canonical_cycle_id(T)
    with pytest.raises(IngestRecordError, match='reason'):
        inventory.handoff.disposition('terminal', key, status='expired')
    inventory.handoff.disposition('terminal', key, status='expired', reason='Missing I')
    inventory.handoff.disposition('core-state', key, status='abandoned', reason='Missing I')
    with pytest.raises(IngestRecordError, match='terminal'):
        inventory.handoff.disposition('core-state', key, status='success')
    add(inventory, 'I')
    with pytest.raises(IngestRecordError, match='terminal'):
        inventory.publish_scan(T, dependencies, at=T + timedelta(minutes=2))
    assert inventory.reconcile()['pending']  # Late inputs still create render work.


def test_retention_protects_pins_and_shared_replay_lock(inventory):
    item = add(inventory)
    no_mapping(inventory, item.input_id)
    h = inventory.handoff
    h.pin('history', [item.input_id])
    assert inventory.cleanup(before=T + timedelta(days=1)) == ()
    h.release_pin('history')
    # Cleanup and selection use the same OS lock. Contention defers the operation.
    with input_lock(inventory.base_dir):
        with pytest.raises(OSError):
            inventory.cleanup(before=T + timedelta(days=1))
        with pytest.raises(OSError):
            h.pin('new-selection', [item.input_id])
    assert inventory.cleanup(before=T + timedelta(days=1)) == (item.input_id,)
    assert not item.record.local_path.exists()
    assert h.read('retired-input', item.input_id)
    assert inventory.reconcile()['pending'] == ()


def test_pending_core_phase_and_unpublished_outbox_protect_inputs(inventory, dependencies):
    items = checks(inventory)
    assert inventory.cleanup(before=T + timedelta(days=1)) == ()
    inventory.publish_scan(T, dependencies, at=T)
    for item in items:
        no_mapping(inventory, item.input_id)
    assert inventory.cleanup(before=T + timedelta(days=1)) == ()
    inventory.handoff.disposition('core-state', canonical_cycle_id(T), status='success')
    assert inventory.cleanup(before=T + timedelta(days=1)) == (items[1].input_id,)
    # Detection history remains pinned between sequential scans.
    assert items[0].record.local_path.exists()
    inventory.handoff.release_pin('core-history')
    assert set(inventory.cleanup(before=T + timedelta(days=1))) == {items[0].input_id, items[2].input_id}


def test_restart_finishes_interrupted_retention(inventory, monkeypatch):
    item = add(inventory); no_mapping(inventory, item.input_id)
    original = type(item.record.local_path).unlink
    def crash(path, *args, **kwargs):
        if path == item.record.local_path:
            raise OSError('crash after tombstone')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(item.record.local_path), 'unlink', crash)
    with pytest.raises(OSError):
        inventory.cleanup(before=T + timedelta(days=1))
    monkeypatch.setattr(type(item.record.local_path), 'unlink', original)
    assert inventory.reconcile()['pending'] == ()
    assert not item.record.local_path.exists()


def test_poll_status_and_corruption_are_visible(inventory):
    inventory.handoff.publish_poll_status({'pending': 2}, ['missing B'])
    assert inventory.handoff.read('poll-status', 'poll-status').data['counts']['pending'] == 2
    inventory.handoff.path('poll-status', 'poll-status').write_text('{partial')
    with pytest.raises(IngestRecordError, match='Cannot read'):
        inventory.handoff.read('poll-status', 'poll-status')


def test_evaluation_is_pure_after_loading_inventory(inventory, dependencies, monkeypatch):
    from pathlib import Path
    checks(inventory); add(inventory, 'I'); add(inventory, 'Late')
    records = inventory.valid_inputs()
    def forbidden(*args, **kwargs):
        raise AssertionError('Pure evaluation touched the filesystem')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(Path, 'is_file', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    assert evaluate_scan(T, records, dependencies, at=T, optional_started_at=T).final


def test_pinned_phase_missing_check_cannot_pass_consumer_preflight(inventory, dependencies):
    from common.pipeline.readiness import validate_phase_dependencies
    item = add(inventory, 'A')
    from common.ingest.manifest import CycleInputManifest
    record = inventory.handoff.publish_phase('core-start-ready', CycleInputManifest(T, (item.record,)), [item.input_id])
    with pytest.raises(IngestRecordError, match='check'):
        validate_phase_dependencies(record, dependencies)
    with pytest.raises(IngestRecordError, match='check'):
        evaluate_scan(T, inventory.valid_inputs(), dependencies, start_record=record)


def test_history_is_strictly_earlier_than_pinned_actual_observation(inventory, dependencies):
    previous = add(inventory, 'A', T + timedelta(seconds=10))
    current = add(inventory, 'A', T + timedelta(seconds=40))
    add(inventory, 'B'); add(inventory, 'ProbSevere')
    result = inventory.publish_scan(T, dependencies, at=T)
    assert current.input_id in result.start_ids and previous.input_id in result.start_ids
    assert result.start.records_for_product('A')[0].role == 'previous'


def test_fingerprint_mismatch_never_consumes_inventory(inventory, dependencies):
    checks(inventory)
    incompatible = replace(dependencies, glm_enabled=True)
    with pytest.raises(IngestRecordError, match='fingerprint'):
        evaluate_scan(T, inventory.valid_inputs(), incompatible)
    with pytest.raises(IngestRecordError, match='agreement'):
        inventory.publish_scan(T, incompatible, at=T)


@pytest.mark.parametrize('change', ['timestamp', 'validated', 'tolerance', 'schema'])
def test_phase_strict_reader_rejects_corruption(inventory, dependencies, change):
    checks(inventory)
    inventory.publish_scan(T, dependencies, at=T)
    key = canonical_cycle_id(T)
    path = inventory.handoff.path('core-start-ready', key)
    raw = json.loads(path.read_text())
    if change == 'timestamp': raw['data']['manifest']['cycle_time'] = (T + timedelta(minutes=2)).isoformat()
    elif change == 'validated': raw['data']['manifest']['inputs'][0]['validated'] = 'true'
    elif change == 'tolerance': raw['data']['manifest']['tolerances']['rap_max_age_seconds'] = float('nan')
    else: raw['data']['manifest']['extra'] = 1
    path.write_text(json.dumps(raw))
    with pytest.raises(IngestRecordError):
        inventory.handoff.read('core-start-ready', key)


def test_failed_payload_adoption_never_publishes(inventory):
    item = completion(inventory.base_dir, body=b'partial')
    def reject(path):
        raise ValueError('Corrupt payload')
    with pytest.raises(ValueError, match='Corrupt'):
        inventory.reconcile([item.record.path], revalidate=reject)
    assert inventory.handoff.records('input') == inventory.handoff.records('render-ready') == ()


def test_cleanup_racing_pin_defers_then_observes_reference(inventory, monkeypatch):
    import threading
    item = add(inventory); no_mapping(inventory, item.input_id)
    acquired = threading.Event()
    release = threading.Event()
    failures = []
    write = inventory.handoff._write
    def pause(kind, *args, **kwargs):
        if kind == 'pin':
            acquired.set()
            if not release.wait(2):
                raise AssertionError('Pin publication timed out')
        return write(kind, *args, **kwargs)
    monkeypatch.setattr(inventory.handoff, '_write', pause)
    def pin():
        try:
            inventory.handoff.pin('active-worker', [item.input_id])
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=pin)
    worker.start()
    try:
        assert acquired.wait(2)
        with pytest.raises(OSError):
            inventory.cleanup(before=T + timedelta(days=1))
    finally:
        release.set()
        worker.join(2)
    assert not failures and not worker.is_alive()
    assert inventory.cleanup(before=T + timedelta(days=1)) == ()
    assert item.record.local_path.exists()


def test_expired_layer_releases_only_after_input_acknowledgment(inventory):
    item = add(inventory)
    h = inventory.handoff
    h.publish_render_ready(item.input_id)
    h.plan_render(item.input_id, ['one', 'two'], RENDER)
    h.disposition('render-ack', '', status='success', input_id=item.input_id,
                  layer_id='one', render_fingerprint=RENDER)
    h.disposition('render-ack', '', status='expired', reason='Retry budget exhausted',
                  input_id=item.input_id, layer_id='two', render_fingerprint=RENDER)
    assert inventory.cleanup(before=T + timedelta(days=1)) == ()
    assert h.acknowledge_input(item.input_id, RENDER).data['status'] == 'expired'
    assert inventory.cleanup(before=T + timedelta(days=1)) == (item.input_id,)


def test_disabled_auxiliary_sources_do_not_create_missing_requirements(inventory, dependencies):
    checks(inventory); add(inventory, 'I'); add(inventory, 'Late')
    result = inventory.publish_scan(T, dependencies, at=T)
    assert result.integration and result.final and result.missing_integration == ()
    assert all(r.family == 'mrms' for r in result.final.inputs)


def test_protected_history_and_rap_pin_yield_to_active_references(inventory):
    older = add(inventory, 'A', T - timedelta(minutes=2))
    newer = add(inventory, 'A')
    for item in (older, newer):
        no_mapping(inventory, item.input_id)
    assert inventory.cleanup(before=T + timedelta(days=1), protected_products=['A']) == (older.input_id,)
    assert newer.record.local_path.exists()


def test_file_verification_cache_invalidates_on_same_size_replacement(inventory):
    item = add(inventory)
    inventory.valid_inputs()
    item.record.local_path.write_bytes(b'x' * len(b'validated payload'))
    with pytest.raises(IngestRecordError, match='changed'):
        inventory.valid_inputs()


def test_active_core_keeps_phase_inputs_after_scan_expiry(inventory, dependencies):
    items = checks(inventory)
    inventory.publish_scan(T, dependencies, at=T)
    key = canonical_cycle_id(T)
    active = inventory.handoff.disposition('core-state', key, status='active')
    assert set(active.data['input_ids']) == {item.input_id for item in items}
    inventory.handoff.disposition('terminal', key, status='expired', reason='Missing I')
    for item in items:
        no_mapping(inventory, item.input_id)
    inventory.handoff.release_pin('core-history')
    assert inventory.cleanup(before=T + timedelta(days=1)) == ()
    inventory.handoff.disposition('core-state', key, status='abandoned', reason='Worker stopped')
    assert set(inventory.cleanup(before=T + timedelta(days=1))) == {item.input_id for item in items}


def test_consumer_revalidates_phase_identity_and_bytes(inventory, dependencies):
    items = checks(inventory)
    inventory.publish_scan(T, dependencies, at=T)
    record = inventory.handoff.read('core-start-ready', canonical_cycle_id(T))
    assert inventory.handoff.validate_phase_inputs(record) is record
    items[0].record.local_path.write_bytes(b'changed')
    with pytest.raises(IngestRecordError, match='changed'):
        inventory.handoff.validate_phase_inputs(record)


def test_conflicting_integration_selection_is_not_overwritten(inventory, dependencies):
    checks(inventory); add(inventory, 'I')
    result = inventory.publish_scan(T, dependencies, at=T)
    ids = list(result.integration_ids)
    records = list(result.integration.inputs)
    other = add(inventory, 'A', T + timedelta(seconds=40))
    index = next(i for i, r in enumerate(records) if r.product == 'A' and r.role == 'current')
    ids[index], records[index] = other.input_id, other.record
    manifest = replace(result.integration, inputs=tuple(records))
    with pytest.raises(IngestRecordError, match='pinned'):
        inventory.handoff.publish_phase('core-integration-ready', manifest, ids)


def test_reused_rap_is_pinned_by_two_active_scans(tmp_path, dependencies):
    deps = replace(dependencies, rap_enabled=True)
    inventory = InputInventory(tmp_path, fingerprint=deps.fingerprint, run_id='run')
    path = tmp_path / 'data/rap/RAP.20260930-12z.awp130pgrbf00.grib2'
    path.parent.mkdir(parents=True); path.write_bytes(b'rap')
    rap = CommittedInput(StagedInput('RAP', str(path), T, 'synoptic', 'rap'),
                         hashlib.sha256(b'rap').hexdigest(), str(path))
    inventory.commit_input(rap)
    for scan in (T, T + timedelta(minutes=2)):
        for product in (*deps.check, 'I'):
            add(inventory, product, scan)
        result = inventory.publish_scan(scan, deps, at=scan)
        assert rap.input_id in result.integration_ids
        inventory.handoff.disposition('core-state', canonical_cycle_id(scan), status='active')
    for record in inventory.handoff.records('input'):
        no_mapping(inventory, record.key)
    inventory.handoff.disposition('core-state', canonical_cycle_id(T), status='success')
    inventory.cleanup(before=T + timedelta(days=1))
    assert path.exists()
    inventory.handoff.disposition('core-state', canonical_cycle_id(T + timedelta(minutes=2)), status='success')
    assert rap.input_id in inventory.cleanup(before=T + timedelta(days=1))
    assert not path.exists()
