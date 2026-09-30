"""Frozen phase-one dependency and resource contract (no live source I/O)."""
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from common.config.loader import load_config
from common.ingest.mrms.registry import build_registry
from common.ingest.mrms.core_contract import resolve_dependencies
from common.ingest.mrms.config import get_ingest_settings, ingest_auxiliary_settings
from tests.architecture.baseline import assert_baseline


def registry():
    return build_registry(load_config('ingest')['mrms'], Path('/runtime'))


def test_dependency_baseline():
    effective = registry()
    enrichment = [row['product'] for row in load_config('integration')['stats_datasets']
                  if row.get('product') and effective.is_enabled(row['product'])]
    contract = resolve_dependencies(effective, enrichment=enrichment, auxiliary_settings=ingest_auxiliary_settings())
    assert_baseline('independent_ingest_dependencies', asdict(contract))
    assert 'ProbSevere' in contract.check
    assert not contract.mandatory_integration
    assert 'PrecipRate_00.00' in contract.optional
    assert contract.previous_detection == contract.detection
    assert contract.optional_timeout_seconds == 30


@pytest.mark.parametrize('overrides,reason', [
    ({'check': []}, 'empty'),
    ({'check': ['ProbSevere']}, 'Detection'),
    ({'check': ['Missing_00.00']}, 'disabled'),
    ({'mandatory_integration': ['Missing_00.00']}, 'disabled'),
    ({'detection': ['Missing_00.00']}, 'disabled'),
])
def test_invalid_dependency_sets(overrides, reason):
    with pytest.raises(ValueError, match=reason):
        resolve_dependencies(registry(), **overrides)


def test_disable_semantics_and_fingerprint():
    effective = registry()
    normal = resolve_dependencies(effective)
    no_glm = resolve_dependencies(effective, disable_goes=True)
    only = resolve_dependencies(effective, mrms_core_only=True)
    assert no_glm.rap_enabled and not no_glm.glm_enabled
    assert not only.rap_enabled and not only.glm_enabled
    assert normal.fingerprint != no_glm.fingerprint != only.fingerprint
    assert resolve_dependencies(effective, check=[None, 'PrecipFlag_00.00',
        'MergedReflectivityQCComposite_00.50']).fingerprint == normal.fingerprint
    invalid = replace(effective, products=tuple(replace(p, discovery=False) for p in effective.products))
    with pytest.raises(ValueError, match='empty'):
        resolve_dependencies(invalid)


def test_resource_baseline():
    runtime = load_config('runtime')
    assert_baseline('independent_ingest_settings', {
        'scheduler': dict(load_config('scheduler')['scheduler']),
        'ingest': get_ingest_settings(),
        'consumers': dict(runtime['consumers']),
        'input_jobs': dict(load_config('ewmrs_pipeline')['input_jobs']),
    })
    settings = get_ingest_settings()
    assert settings['download_concurrency'] == load_config('ingest')['mrms']['downloads']['max_concurrency']
    assert settings['auxiliary_timeout_seconds'] == 4 * load_config('synoptic_rap')['rap']['nomads_timeout_seconds']


def test_source_arrival_fixture():
    import json
    fixture = json.loads((Path(__file__).parents[1] / 'fixtures/ingest/source_arrivals.json').read_text())
    deps = resolve_dependencies(registry(), disable_goes=fixture['disable_goes'])
    observed = {}
    rap_events = set()
    for arrival in fixture['arrivals']:
        scan = arrival['scan']
        observed.setdefault(scan, set()).add(arrival['product'])
        assert (set(deps.check) <= observed[scan]) == arrival['check_ready']
        if arrival['product'] == 'RAP':
            rap_events.add(arrival['input_id'])
    assert len(rap_events) == 1
    assert not deps.glm_enabled
    assert fixture['optional_completion_seconds'] == deps.optional_timeout_seconds
    assert fixture['arrivals'][-1]['product'] in deps.optional
    for snapshot in fixture['snapshots']:
        products = {arrival['product'] for arrival in fixture['arrivals']
                    if arrival['scan'] == snapshot['scan']
                    and arrival['at_seconds'] <= snapshot['at_seconds']}
        assert (set(deps.check) <= products) == snapshot['check_ready']
        assert products & set(deps.optional) == set(snapshot['optional_inputs'])
    # The completed T snapshot is immutable when its delayed optional layer
    # arrives after the acquisition deadline; check readiness preceded it.
    assert fixture['snapshots'][0]['check_ready'] and not fixture['snapshots'][0]['optional_complete']
    assert fixture['snapshots'][1]['optional_complete']
    assert fixture['arrivals'][-1]['at_seconds'] > fixture['snapshots'][1]['at_seconds']


@pytest.mark.parametrize('catalog,group,key,value', [
    ('scheduler', 'scheduler', 'ingest_poll_seconds', 0),
    ('runtime', 'ingest', 'pending_max_jobs', 0),
    ('runtime', 'ingest', 'listing_page_size', 1001),
    ('runtime', 'ingest', 'download_concurrency', 65),
    ('runtime', 'consumers', 'core_readiness_seconds', 2),
    ('ewmrs_pipeline', 'input_jobs', 'retry_max_attempts', 0),
])
def test_invalid_resource_bounds(catalog, group, key, value):
    from common.config.loader import validate_document, ConfigError
    from common.ingest.mrms.registry import _plain
    document = _plain(load_config(catalog))
    document[group][key] = value
    with pytest.raises(ConfigError):
        validate_document(catalog, document)


def test_auxiliary_freshness_participates_in_fingerprint():
    settings = ingest_auxiliary_settings()
    first = resolve_dependencies(registry(), auxiliary_settings=settings)
    settings['rap']['max_age_minutes'] += 1
    assert first.fingerprint != resolve_dependencies(registry(), auxiliary_settings=settings).fingerprint
