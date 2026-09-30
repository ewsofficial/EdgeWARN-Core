"""Offline MRMS conversion with durable apply, resume and inverse rollback."""
import base64
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

from common.config.loader import CONFIG_NAMES, ConfigError, release_config_root, validate_document
from common.config.mrms_products import normalize_products, parse_product_id
from common.ingest.mrms.core_contract import CORE_PRODUCTS, LEGACY_ALIASES


def legacy_contract():
    from importlib.resources import files
    return json.loads(files('common.config').joinpath('mrms-v1.json').read_text())


def convert_documents(documents, legacy):
    """Return a candidate tree and diagnostics without mutating inputs or doing I/O.

    Custom source/path settings are reported as conflicts, never guessed away.
    The candidate is review material only when conflicts are present.
    """
    converted = deepcopy(documents)
    ingest = converted['ingest']
    if ingest.get('schema_version') != 1:
        raise ValueError('Migration requires an ingest schema_version: 1 tree')
    old = ingest['mrms']
    conflicts = []
    protected = [p.configured_id for p in CORE_PRODUCTS]
    products = []
    aliases = dict(LEGACY_ALIASES)
    for item in old['products']:
        extra = set(item) - {'region', 'product', 'outdir'}
        if extra:
            conflicts.append(f'Unsupported product overrides: {sorted(extra)}; manual conversion required')
        identity = 'ProbSevere' if item['region'] == 'ProbSevere' and item['product'] is None else item['product']
        parsed = parse_product_id('MRMS_' + str(identity))
        expected_region = 'ProbSevere' if identity == 'ProbSevere' else 'CONUS'
        if item['region'] != expected_region:
            conflicts.append(f'Unsupported region for {identity}: {item["region"]}')
        if aliases.get(item['outdir']) != identity:
            conflicts.append(f'Custom directory mapping {item["outdir"]} -> {identity}; an explicit reviewed mapping is required')
        products.append(parsed.configured_id)
    normalize_products(products, protected_ids=protected)
    for key in ('bucket', 'path_patterns', 'check_products', 'membership_lists'):
        if old.get(key) != legacy['mrms'].get(key):
            conflicts.append(f'Custom mrms.{key} will be replaced by the protected source/readiness contract; review required')
    https = old['ncep_https']
    retained_https = ('sync_timeout_seconds', 'match_window_seconds', 'download_chunk_size_bytes')
    for key in set(https) - set(retained_https):
        if https[key] != legacy['mrms']['ncep_https'].get(key):
            conflicts.append(f'Custom mrms.ncep_https.{key} cannot be converted automatically')
    retained = ('decompress_chunk_size_bytes', 'remove_old_files', 'cleanup_max_age_minutes')
    known = set(retained) | {'bucket', 'path_patterns', 'check_products', 'membership_lists', 'products', 'ncep_https'}
    for key in set(old) - known:
        conflicts.append(f'Unknown mrms.{key}; manual conversion required')
    ingest['schema_version'] = 2
    ingest['mrms'] = {key: old[key] for key in retained}
    ingest['mrms'].update(products=[p for p in products if p not in protected],
                          ncep_https={key: https[key] for key in retained_https},
                          downloads={'max_concurrency': 8, 'optional_timeout_seconds': 30})
    for name, key in [('integration', 'stats_datasets'), ('ewmrs_render', 'mrms_layers')]:
        for item in converted[name][key]:
            alias = item.get('filepath')
            if alias not in aliases:
                conflicts.append(f'{name}.{key}: unknown filepath {alias!r}; explicit product mapping required')
                continue
            item['product'] = aliases[alias]
            del item['filepath']
    converted['runtime']['run'].setdefault('disable_stormprob', False)
    return converted, sorted(conflicts), [p for p in protected if p not in products]


def plan_migration(config_path, base_dir):
    """Read an old tree directly, without invoking startup or runtime imports."""
    root, base = Path(config_path).resolve(), Path(base_dir).expanduser().resolve()
    documents = {name: yaml.safe_load((root / f'{name}.yaml').read_text()) for name in CONFIG_NAMES}
    converted, conflicts, restored = convert_documents(documents, legacy_contract())
    # Validate against release schemas, not possibly outdated operator schemas.
    for name, document in converted.items():
        try:
            validate_document(name, document, config_dir=release_config_root())
        except ConfigError as exc:
            conflicts.append(str(exc))
    legacy = legacy_contract()
    paths = []
    data = base / 'data'
    for alias, identity in LEGACY_ALIASES.items():
        source = data / legacy['paths'][alias]
        target = data / parse_product_id('MRMS_' + identity).path_name
        action = 'unchanged' if source == target else 'rename'
        paths.append({'product': identity, 'source': str(source), 'target': str(target),
                      'action': action, 'source_exists': source.exists(), 'target_exists': target.exists()})
        if not source.resolve().is_relative_to(data) or not target.resolve().is_relative_to(data):
            conflicts.append(f'Path escapes runtime data directory: {source} -> {target}')
        if source != target and (source.exists() or source.is_symlink()) and (target.exists() or target.is_symlink()):
            conflicts.append(f'Target conflict: {source} -> {target}; directories must not be merged')
        for candidate in (source, target):
            if candidate.exists() and not candidate.is_dir():
                conflicts.append(f'Expected directory: {candidate}')
    state = base / 'state/realtime'
    # A planner cannot prove liveness or interpret future pin schemas. Report
    # existing durable records conservatively; phase 8 adds the apply interlock.
    records = sorted(str(p) for area in ('cycles', 'consumers', 'leases', 'services')
                     for p in (state / area).rglob('*.json'))
    return {'dry_run': True, 'apply_supported': True, 'config_path': str(root),
            'base_dir': str(base), 'restored_protected_products': restored,
            'config_changes': [name for name in CONFIG_NAMES if converted[name] != documents[name]],
            'paths': paths, 'conflicts': sorted(set(conflicts)),
            'runtime_records_requiring_backlog_pin_review': records,
            'converted_documents': converted}


def _contained(path, root):
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'Path escapes migration root: {path}')
    if path.is_symlink():
        raise ValueError(f'Migration refuses symlink: {path}')


def _interlocks(base, stack):
    from util.runtime.handoff import _AdvisoryFileLock, select_pending_records, ConsumerCheckpointStore, phase_record_path
    from common.ingest.replay import input_lock
    from util.runtime.services import CANONICAL_SERVICE_NAMES
    for name in CANONICAL_SERVICE_NAMES:
        path = base / 'state/realtime/services' / f'{name}.lock'
        _contained(path, base)
        try:
            stack.enter_context(_AdvisoryFileLock(path))
        except OSError as exc:
            raise ValueError(f'Service {name} must be stopped before migration') from exc
    try:
        stack.enter_context(input_lock(base))
    except OSError as exc:
        raise ValueError('Active input pins prevent migration') from exc
    for area in ('cycles', 'consumers', 'leases', 'services'):
        _contained(base / 'state/realtime' / area, base)
    for phase in ('mrms-ready', 'rap-ready'):
        checkpoint = ConsumerCheckpointStore(base, f'ewmrs-{phase}').load()
        pending = select_pending_records(base, phase, checkpoint=checkpoint, max_backlog=1000000)
        # The consumer selector also reports cycle directories where this
        # phase was never published (for example, RAP disabled). Stopped
        # producers cannot add those phases; only existing records are backlog.
        if any(status != 'already-processed' and
               (phase_record_path(base, cycle_id, phase).exists() or
                phase_record_path(base, cycle_id, phase).is_symlink())
               for cycle_id, _, status in pending):
            raise ValueError(f'Unconsumed {phase} backlog; drain before migration')


def _write(path, data):
    from util.atomic import atomic_write_bytes
    atomic_write_bytes(path, data)


def _save(path, journal):
    from util.atomic import atomic_write_json
    atomic_write_json(path, journal)


def _run_steps(journal, journal_path, rollback=False):
    """Write intent before mutation, reconcile a crash after mutation on resume."""
    root, base = Path(journal['config_path']), Path(journal['base_dir'])
    journal['status'] = 'rolling-back' if rollback else 'applying'
    _save(journal_path, journal)
    steps = list(reversed(journal['steps'])) if rollback else journal['steps']
    for step in steps:
        if rollback and step['status'] == 'pending':
            continue
        finished = 'rolled-back' if rollback else 'done'
        # Reconcile even completed steps: another invocation or filesystem
        # edit must not make a recorded commit hide a new conflict.
        step['status'] = 'undoing' if rollback else 'applying'
        _save(journal_path, journal)
        if step['kind'] == 'config':
            path = root / step['path']
            _contained(path, root)
            old = base64.b64decode(step['before'])
            new = base64.b64decode(step['after'])
            current = path.read_bytes()
            desired, expected = (old, new) if rollback else (new, old)
            if current != desired:
                if current != expected:
                    raise ValueError(f'Configuration changed since migration: {path}')
                _write(path, desired)
        else:
            source, target = Path(step['source']), Path(step['target'])
            if rollback:
                source, target = target, source
            for path in (source, target):
                _contained(path, base / 'data')
            if source.exists() and target.exists():
                raise ValueError(f'Target conflict: {source} -> {target}')
            existing = source if source.exists() else target
            if not existing.is_dir() or (existing.stat().st_dev, existing.stat().st_ino) != tuple(step['identity']):
                raise ValueError(f'Migration directory identity changed: {existing}')
            if source.exists():
                if source.stat().st_dev != target.parent.stat().st_dev:
                    raise ValueError(f'Cross-device rename: {source} -> {target}')
                os.rename(source, target)
                from util.atomic import _fsync_directory
                _fsync_directory(source.parent)
            elif not target.is_dir():
                raise ValueError(f'Missing both migration paths: {source}, {target}')
        step['status'] = finished
        _save(journal_path, journal)
    if not rollback:
        from common.config.loader import validate_all_configs, reset_cache
        validate_all_configs(config_dir=root)
        reset_cache()
        validate_node_catalog(root)
    journal['status'] = 'rolled-back' if rollback else 'complete'
    _save(journal_path, journal)
    return {'journal': str(journal_path), 'status': journal['status'], 'steps': len(steps)}



def validate_node_catalog(root):
    """Use the same validator as the independently deployed Node API."""
    script = release_config_root().parent / 'scripts/validate-config.js'
    if not script.is_file():
        raise ValueError('Node release validator unavailable; install the complete EdgeWARN release including packaged Node validation assets, then --resume')
    env = dict(os.environ, EDGEWARN_CONFIG_DIR=str(root))
    try:
        result = subprocess.run(['node', str(script)], env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'Node catalog validation unavailable; install Node and run npm install --prefix {script.parent.parent}, then --resume') from exc
    if result.returncode:
        raise ValueError(f'Node catalog validation failed; services remain stopped: {result.stdout.strip()} {result.stderr.strip()}. Install release dependencies with npm install --prefix {script.parent.parent}, fix any reported configuration error, then --resume')

def execute_migration(config_path, base_dir, *, resume=False, rollback=False):
    root, base = Path(config_path).resolve(), Path(base_dir).expanduser().resolve()
    journal_path = root / '.mrms-migration/journal.json'
    _contained(journal_path, root)
    _contained(base / 'data', base)
    _contained(base / 'state', base)
    _contained(root / '.mrms-migration.lock', root)
    report = None if resume or rollback else plan_migration(root, base)
    if report and report['conflicts']:
        raise ValueError('; '.join(report['conflicts']))
    with ExitStack() as stack:
        _interlocks(base, stack)
        from util.runtime.handoff import _AdvisoryFileLock
        stack.enter_context(_AdvisoryFileLock(root / '.mrms-migration.lock'))
        if resume or rollback:
            journal = json.loads(journal_path.read_text())
            if not isinstance(journal, dict):
                raise ValueError('Migration journal must be an object')
            if type(journal.get('schema_version')) is not int or journal['schema_version'] != 1:
                raise ValueError('Unsupported migration journal version')
            if journal['config_path'] != str(root) or journal['base_dir'] != str(base):
                raise ValueError('Journal belongs to different configuration/runtime roots')
            if journal['status'] not in {'applying', 'rolling-back', 'complete', 'rolled-back'}:
                raise ValueError('Invalid migration journal status')
            if journal['status'] == 'rolling-back' and not rollback:
                raise ValueError('Interrupted rollback must be continued with --rollback')
            if journal['status'] == 'rolled-back' and not rollback:
                raise ValueError('Rolled-back migration cannot be resumed')
        else:
            report = plan_migration(root, base)
            if report['conflicts']:
                raise ValueError('; '.join(report['conflicts']))
            if journal_path.exists():
                raise ValueError('Migration journal exists; use --resume or --rollback')
            steps = []
            # Back up documents and deployed schemas before modifying either.
            files = {f'{name}.yaml': yaml.safe_dump(report['converted_documents'][name], sort_keys=False).encode()
                     for name in report['config_changes']}
            for name in CONFIG_NAMES:
                schema = release_config_root() / 'schema' / f'{name}.schema.json'
                files[f'schema/{name}.schema.json'] = schema.read_bytes()
            for relative, after in files.items():
                path = root / relative
                _contained(path, root)
                before = path.read_bytes()
                if before != after:
                    steps.append({'kind': 'config', 'path': relative, 'before': base64.b64encode(before).decode(),
                                  'after': base64.b64encode(after).decode(), 'status': 'pending'})
            for item in report['paths']:
                if item['action'] == 'rename' and item['source_exists']:
                    source, target = Path(item['source']), Path(item['target'])
                    for candidate in (source, target):
                        _contained(candidate, base / 'data')
                    for child in source.rglob('*'):
                        if child.is_symlink() and not child.resolve().is_relative_to(base / 'data'):
                            raise ValueError(f'Path escapes runtime data directory: {child}')
                    if source.stat().st_dev != target.parent.stat().st_dev:
                        raise ValueError(f'Cross-device rename: {source} -> {target}')
                    steps.append({'kind': 'rename', 'source': str(source), 'target': str(target), 'identity': [source.stat().st_dev, source.stat().st_ino], 'status': 'pending'})
            journal = {'schema_version': 1, 'config_path': str(root), 'base_dir': str(base), 'status': 'applying', 'steps': steps}
            # Durable embedded byte backups make restoration independent of the release installation.
            _save(journal_path, journal)
        return _run_steps(journal, journal_path, rollback)


def add_migrate_mrms_parser(subparsers):
    parser = subparsers.add_parser('migrate-mrms', help='offline MRMS v1-to-v2 migration')
    parser.add_argument('--config-path', type=Path, required=True)
    parser.add_argument('--base-dir', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='default; never writes files')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--resume', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    parser.set_defaults(handler=migrate_from_namespace)


def migrate_from_namespace(args):
    try:
        report = (execute_migration(args.config_path, args.base_dir, resume=args.resume, rollback=args.rollback)
                  if args.apply or args.resume or args.rollback else plan_migration(args.config_path, args.base_dir))
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, ConfigError) as exc:
        print(f'MRMS migration: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if report.get('conflicts') else 0
