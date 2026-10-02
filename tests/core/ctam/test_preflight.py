"""Phase 6 deployment and cycle dependency gates."""

from datetime import datetime, timezone
from pathlib import Path
import importlib

import pytest

from common.ingest.manifest import CycleInputManifest
from EdgeWARN.ctam import preflight
from EdgeWARN.ctam.manifest import ModuleManifest, ModuleRequirement, Selector
from EdgeWARN.ctam.readiness import CTAMCycleCatalog, CatalogFile, READY
from EdgeWARN.ctam.api.service import CTAMReadService
from EdgeWARN.ctam.api.models import APIError
from EdgeWARN.ctam.transaction import CTAMTransactionService
from util.cli import build_service_parser


MODULE = '''schema_version = 1
id = "hail"
name = "Hail"
version = "1.0.0"
api_version = "1"
enabled = true
required = false
scope = "stormcells"
entrypoint = ["{python}", "main.py"]
timeout_seconds = 10

[[writes]]
resource = "stormcells.current"
json_pointer = "/features/*/modules/Hail"
'''


def _module(root: Path, requirement: str = '') -> Path:
    folder = root / 'hail'
    folder.mkdir(parents=True)
    (folder / 'main.py').write_text('pass\n')
    path = folder / 'module.toml'
    path.write_text(MODULE + requirement)
    return path


def _preflight(monkeypatch, root, base, *, disable_stormprob=True):
    original = preflight.load_config

    def catalog(name, *, config_dir=None):
        if name == 'ingest':
            return {'schema_version': 2, 'mrms': {
                'products': [], 'cleanup_max_age_minutes': 60, 'remove_old_files': True},
                'goes': original('ingest', config_dir=config_dir)['goes']}
        return original(name, config_dir=config_dir)

    monkeypatch.setattr(preflight, 'load_config', catalog)
    return preflight.check_core_startup(
        config_dir='config', base_dir=base, module_root=root,
        disable_ctam=False, disable_ctam_modules=False,
        disable_stormprob=disable_stormprob,
    )


def test_required_product_of_optional_module_fails_before_directory_creation(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules', '''\n[[requires]]\nselector = "input:MRMS:MESH_00.50:current"\nrequired = true\n''')
    base = tmp_path / 'runtime'
    with pytest.raises(preflight.PreflightError) as error:
        _preflight(monkeypatch, module.parent.parent, base)
    message = str(error.value)
    assert 'hail' in message and 'MRMS_MESH_00.50' in message
    assert str(module) in message and 'ingest.yaml' in message
    assert not base.exists()


def test_dependency_audit_sees_module_discarded_for_unrelated_manifest_error(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules', '''\n[[requires]]\nselector = "input:MRMS:MESH_00.50:current"\n''')
    module.write_text(module.read_text().replace(
        '/features/*/modules/Hail', '/invalid/writes'))
    with pytest.raises(preflight.PreflightError, match='MRMS_MESH_00.50'):
        _preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime')


def test_optional_disabled_product_and_empty_declaration(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules', '''\n[[requires]]\nselector = "input:MRMS:MESH_00.50:current"\nrequired = false\n''')
    result = _preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime')
    assert len(result.runnable) == 1
    module.write_text(MODULE.replace('[[writes]]', 'requires = []\n\n[[writes]]'))
    assert len(_preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime').runnable) == 1


def test_optional_invalid_mrms_selector_still_fails_declaration_audit(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules', '''\n[[requires]]\nselector = "input:MRMS:MESH_00.5x:current"\nrequired = false\n''')
    with pytest.raises(preflight.PreflightError, match='invalid MRMS selector'):
        _preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime')


def test_probsevere_selector_is_enabled_by_core_contract(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules', '''\n[[requires]]\nselector = "input:MRMS:ProbSevere:current"\n''')
    assert len(_preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime').runnable) == 1


def test_previous_input_requires_sufficient_retention(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules', '''\n[[requires]]\nselector = "input:MRMS:ProbSevere:previous"\nmax_age_seconds = 7200\n''')
    with pytest.raises(preflight.PreflightError, match='keeps only 3600s'):
        _preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime')


def test_missing_requires_is_a_startup_migration_error(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules')
    with pytest.raises(preflight.PreflightError, match=r'requires = \[\]'):
        _preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime')


def test_disabled_module_is_excluded_from_dependency_gate(tmp_path, monkeypatch):
    module = _module(tmp_path / 'modules', '''\n[[requires]]\nselector = "input:MRMS:MESH_00.50:current"\n''')
    module.write_text(module.read_text().replace('enabled = true', 'enabled = false'))
    result = _preflight(monkeypatch, module.parent.parent, tmp_path / 'runtime')
    assert result.runnable == ()


def test_stormprob_missing_additions_are_fatal_only_when_enabled(tmp_path, monkeypatch):
    with pytest.raises(preflight.PreflightError) as error:
        _preflight(monkeypatch, tmp_path / 'missing', tmp_path / 'runtime', disable_stormprob=False)
    assert 'MRMS_Reflectivity_0C_00.50' in str(error.value)
    assert 'Ref0' in str(error.value)
    assert '--disable-stormprob' in str(error.value)
    _preflight(monkeypatch, tmp_path / 'missing', tmp_path / 'runtime', disable_stormprob=True)


def test_stormprob_cycle_requires_final_snapshot_and_complete_features():
    time = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    empty = CycleInputManifest(cycle_time=time)
    assert preflight.validate_stormprob_cycle([], empty) is None
    with pytest.raises(preflight.StormProbDependencyError, match='MRMS_Reflectivity_0C_00.50'):
        preflight.validate_stormprob_cycle([{'id': '7'}], empty)


def _stormprob_manifest(tmp_path, cycle, offset_seconds):
    from datetime import timedelta
    from common.ingest.manifest import StagedInput

    inputs = []
    stamp = cycle + timedelta(seconds=offset_seconds)
    for source in preflight.STORMPROB_MRMS_SOURCES:
        path = tmp_path / f"MRMS_{source.product}_{stamp:%Y%m%d-%H%M%S}.grib2"
        path.write_bytes(b"x")
        inputs.append(StagedInput(source.product, str(path), stamp, "s3", "mrms"))
    rap = tmp_path / "RAP.grib2"
    rap.write_bytes(b"x")
    inputs.append(StagedInput("RAP", str(rap), cycle.replace(minute=0), "nomads", "rap"))
    return CycleInputManifest(cycle_time=cycle, inputs=tuple(inputs))


def _ready_cell():
    from EdgeWARN.stormprob import features

    quality = {name: "ok" for name in features.UNIVERSAL_PROPERTY_FEATURES}
    return {"id": "1", "stormprob": {"observation": {"inference_ready": True, "quality": quality}}}


@pytest.mark.parametrize("offset_seconds", [38, 120, -60, -180])
def test_stormprob_accepts_inputs_stamped_within_the_scan_window(tmp_path, offset_seconds):
    """MRMS stamps a scan ~38 s after its even minute; the selector picks that
    file, so the gate must accept it (it rejected any positive offset)."""
    cycle = datetime(2026, 10, 1, 23, 12, tzinfo=timezone.utc)
    manifest = _stormprob_manifest(tmp_path, cycle, offset_seconds)
    assert preflight.validate_stormprob_cycle([_ready_cell()], manifest) is None


@pytest.mark.parametrize("offset_seconds", [121, -181])
def test_stormprob_rejects_inputs_outside_the_scan_window(tmp_path, offset_seconds):
    cycle = datetime(2026, 10, 1, 23, 12, tzinfo=timezone.utc)
    manifest = _stormprob_manifest(tmp_path, cycle, offset_seconds)
    with pytest.raises(preflight.StormProbDependencyError, match="unavailable, stale or invalid"):
        preflight.validate_stormprob_cycle([_ready_cell()], manifest)


def test_stormprob_disable_keeps_external_execution_and_enabled_failure_stops_it(
    tmp_path, monkeypatch,
):
    runner = importlib.import_module('EdgeWARN.ctam.run')
    monkeypatch.setattr(runner.AlertManager, 'cleanup_expired', lambda: None)
    monkeypatch.setattr(runner.AlertManager, 'create_snapshot', lambda _time: None)
    monkeypatch.setattr(runner, '_run_phase1_discovery_dry_run', lambda *_args: None)
    external_calls = []

    def external(cells, *_args, **_kwargs):
        external_calls.append(True)
        return cells, ('external',), (), {}

    monkeypatch.setattr(runner, '_run_external_modules', external)
    moment = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    manifest = CycleInputManifest(cycle_time=moment)
    result = runner.run_ctam_result(
        [{'id': '7'}], timestamp='20260805-120000', input_manifest=manifest,
        disable_stormprob=True,
    )
    assert result.manifests == ('external',)
    assert external_calls == [True]
    with pytest.raises(preflight.StormProbDependencyError):
        runner.run_ctam_result(
            [{'id': '7'}], timestamp='20260805-120000', input_manifest=manifest,
        )
    assert external_calls == [True]


def test_external_disable_skips_discovery(tmp_path, monkeypatch):
    runner = importlib.import_module('EdgeWARN.ctam.run')
    monkeypatch.setattr(runner.AlertManager, 'cleanup_expired', lambda: None)
    monkeypatch.setattr(runner.AlertManager, 'create_snapshot', lambda _time: None)
    monkeypatch.setattr(runner.discovery, 'discover_modules',
                        lambda: pytest.fail('external discovery should be disabled'))
    result = runner.run_ctam_result(
        [], timestamp='20260805-120000', disable_stormprob=True,
        disable_ctam_modules=True,
    )
    assert result.manifests == ()


def test_cli_explicit_negative_stormprob_switch():
    parser = build_service_parser('edgewarn')
    assert parser.parse_args(['--disable-stormprob']).disable_stormprob is True
    assert parser.parse_args(['--no-disable-stormprob']).disable_stormprob is False


def test_ctam_metadata_is_limited_to_declared_selectors(tmp_path):
    moment = '2026-08-05T12:00:00+00:00'
    files = tuple(CatalogFile(
        f'input:mrms:{product}:current', 'input', 'mrms', product, 'current', moment,
        True, True, READY, None, 1, 'application/x-grib2', tmp_path / product,
    ) for product in ('VIL_00.50', 'MESH_00.50'))
    catalog = CTAMCycleCatalog('20260805-120000', moment, False, 0, files)
    manifest = ModuleManifest(
        'hail', 'Hail', '1.0.0', '1', True, False, 'cycle', (), 10, (),
        (ModuleRequirement(Selector('input:MRMS:VIL_00.50:current', 'input', 'mrms',
                                    'VIL_00.50', 'current'), True, None, None),),
        (), tmp_path, tmp_path / 'module.toml',
    )
    transactions = CTAMTransactionService(cells=[], manifests={'hail': manifest})
    service = CTAMReadService(
        catalog=catalog, cells=[], manifests={'hail': manifest}, transactions=transactions)
    assert [item['product'] for item in service.files('hail')['files']] == ['VIL_00.50']
    with pytest.raises(APIError) as error:
        service.descriptor('hail', 'input:mrms:MESH_00.50:current')
    assert error.value.code == 'requirement_unmet'
    assert service.contract_violations('hail') == ('input:mrms:MESH_00.50:current',)
    with pytest.raises(APIError) as commit_error:
        service.commit_transaction('hail', idempotency_key='attempt')
    assert commit_error.value.code == 'requirement_unmet'
