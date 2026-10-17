"""Retirement removes consumer state while preserving Core pins and operator files."""
import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone

import pytest
from common.ingest.inventory import InputInventory
from common.ingest.manifest import StagedInput
from common.ingest.objects import CommittedInput
from util.runtime.ingest_handoff import IngestRecordError
from edgewarn_cli.main import build_parser
from edgewarn_cli.run import TOPOLOGIES, WORKERS
from util.runtime.services import CANONICAL_SERVICE_NAMES, scan_service_states


def test_only_supported_runtime_surfaces_remain(tmp_path):
    retired = "ew" + "mrs"
    assert importlib.util.find_spec("run_" + retired) is None
    assert TOPOLOGIES["all"] == ("ingest", "edgewarn", "nexrad")
    assert TOPOLOGIES["core"] == ("ingest", "edgewarn")
    assert WORKERS == ("ingest", "core", "nexrad")
    assert CANONICAL_SERVICE_NAMES == ("ingest", "edgewarn", "nexrad")
    stale = tmp_path / "state/realtime/services" / (retired + ".json")
    stale.parent.mkdir(parents=True)
    stale.write_text('{}')
    assert retired not in scan_service_states(tmp_path, stale_after_seconds=30)
    assert stale.read_text() == '{}'
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", retired])
    assert error.value.code == 2


def test_inventory_has_only_core_readiness_and_preserves_pins(tmp_path):
    inventory = InputInventory(tmp_path, fingerprint='f' * 64, run_id='ingest')
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    raw = tmp_path / 'data/MRMS_Test_20260101-000000.grib2'
    raw.parent.mkdir()
    raw.write_bytes(b'validated')
    staged = StagedInput('Test', str(raw), stamp, 's3', 'mrms')
    committed = inventory.commit_input(CommittedInput(staged, hashlib.sha256(raw.read_bytes()).hexdigest(), 's3://test'))
    inventory.handoff.pin('core:test', [committed.key])
    assert inventory.reconcile() == {'adopted': ()}
    with pytest.raises(IngestRecordError):
        inventory.handoff.path('render-ready', committed.key)
    assert not (inventory.handoff.root / 'render-ready').exists()
    legacy = tmp_path / 'gui/RAP/legacy/data.u16'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'operator artifact')
    assert inventory.cleanup(before=stamp + timedelta(days=1)) == ()
    inventory.handoff.release_pin('core:test')
    newer = tmp_path / 'data/MRMS_Test_20260101-000200.grib2'
    newer.write_bytes(b'validated2')
    inventory.commit_input(CommittedInput(
        StagedInput('Test', str(newer), stamp + timedelta(minutes=2), 's3', 'mrms'),
        hashlib.sha256(newer.read_bytes()).hexdigest(), 's3://test'))
    assert inventory.cleanup(before=stamp + timedelta(days=1)) == (committed.key,)
    assert not raw.exists()
    assert legacy.read_bytes() == b'operator artifact'
