"""Offline, read-only v1 conversion planning. Apply/resume belongs to phase 8."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import yaml

from common.config.loader import CONFIG_NAMES, ConfigError, release_config_root, validate_document
from common.config.mrms_products import normalize_products, parse_product_id
from common.ingest.mrms.core_contract import CORE_PRODUCTS, LEGACY_ALIASES


def legacy_contract():
    import common.config
    return json.loads((Path(common.config.__file__).parent / 'mrms-v1.json').read_text())


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
    return {'dry_run': True, 'apply_supported': False, 'config_path': str(root),
            'base_dir': str(base), 'restored_protected_products': restored,
            'config_changes': [name for name in CONFIG_NAMES if converted[name] != documents[name]],
            'paths': paths, 'conflicts': sorted(set(conflicts)),
            'runtime_records_requiring_backlog_pin_review': records,
            'converted_documents': converted}


def add_migrate_mrms_parser(subparsers):
    parser = subparsers.add_parser('migrate-mrms', help='report an offline MRMS v1-to-v2 migration (read-only)')
    parser.add_argument('--config-path', type=Path, required=True)
    parser.add_argument('--base-dir', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true', help='default; never writes files')
    parser.set_defaults(handler=migrate_from_namespace)


def migrate_from_namespace(args):
    try:
        report = plan_migration(args.config_path, args.base_dir)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, ConfigError) as exc:
        print(f'MRMS migration: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if report['conflicts'] else 0
