"""Realtime Core is a pure local-readiness consumer (plan phase 5).

Every source client is stubbed to fail loudly if the realtime Core path attempts
any acquisition, so "zero source-acquisition calls" is an enforced property
rather than a claim. The producer side is the durable ingest v1 namespace: the
tests commit inputs and publish phases through the real
``InputInventory``/``IngestHandoff`` writers and then drive Core.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest
from common.ingest.mrms.core_contract import resolve_dependencies
from common.ingest.mrms.registry import _plain, build_registry
from common.config.loader import load_config
from common.ingest.inventory import InputInventory
from common.ingest.manifest import StagedInput
from common.ingest.objects import CommittedInput
from util.runtime.cycle import (
    PrimaryCycleConfig,
    run_primary_cycle_once,
)
from util.runtime.handoff import canonical_cycle_id
from util.runtime.ingest_handoff import IngestHandoff, IngestRecordError
from util.runtime.primary_service import LocalReadinessReader

UTC = timezone.utc
T = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
REFLECTIVITY = "MergedReflectivityQCComposite_00.50"
PRECIP_FLAG = "PrecipFlag_00.00"
PROB_SEVERE = "ProbSevere"
PRECIP_RATE = "PrecipRate_00.00"
CHECKS = (REFLECTIVITY, PRECIP_FLAG, PROB_SEVERE)
PRODUCER_RUN = "ingest-producer"


class AcquisitionAttempted(RuntimeError):
    """The realtime Core path tried to download a source."""


@pytest.fixture(autouse=True)
def no_source_clients(monkeypatch):
    """Make every realtime acquisition entry point fail if it is reached."""

    def forbidden(*_args, **_kwargs):
        raise AcquisitionAttempted("realtime Core must not acquire sources")

    from common.ingest.mrms import acquisition, discovery
    from common.ingest.synoptic import main as synoptic
    from common.pipeline import coordinator
    from util.runtime import cycle as cycle_module, goes

    for module, name in (
        (acquisition, "acquire_batch"),
        (acquisition, "acquire_batch_sync"),
        (acquisition, "acquire_object"),
        (acquisition, "acquire_object_sync"),
        (discovery, "discover_objects"),
        (discovery, "discover_objects_sync"),
        (coordinator, "run_staged_ingest_cycle"),
        (goes, "download_glm_for_scan"),
        (goes, "acquire_glm_inputs_for_scan"),
        (synoptic, "download_rap"),
        (synoptic, "download_rap_async"),
        (synoptic, "acquire_rap_input"),
        (cycle_module, "download_glm_for_scan"),
    ):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, forbidden)
    yield


def make_registry(base_dir):
    catalog = _plain(load_config("ingest")["mrms"])
    catalog["products"] = [f"MRMS_{PRECIP_RATE}"]
    return build_registry(catalog, Path(base_dir))


def make_dependencies(registry, **overrides):
    return resolve_dependencies(
        registry, include_rap=False, include_glm=False,
        auxiliary_settings={"rap": {"max_age_minutes": 180}}, **overrides)


@pytest.fixture
def producer(tmp_path):
    registry = make_registry(tmp_path)
    dependencies = make_dependencies(registry)
    inventory = InputInventory(tmp_path, fingerprint=dependencies.fingerprint,
                               run_id=PRODUCER_RUN)
    return registry, dependencies, inventory


def commit(inventory, product, at=T):
    path = inventory.base_dir / "data" / product / f"MRMS_{product}_{at:%Y%m%d-%H%M%S}.grib2"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"validated {product} {at.isoformat()}".encode()
    path.write_bytes(body)
    staged = StagedInput(product, str(path), at, "s3", "mrms")
    return inventory.commit_input(
        CommittedInput(staged, hashlib.sha256(body).hexdigest(), f"s3://bucket/{product}"))


def notify(inventory, committed):
    return inventory.handoff.publish_render_ready(committed.key)


def publish(inventory, dependencies, at=T, **options):
    return inventory.publish_scan(at, dependencies, **options)


def make_config(tmp_path, dependencies, *, run_id="core-run", **overrides):
    values = dict(
        lat_limits=(20.0, 55.0), lon_limits=(230.0, 300.0), profile=False,
        disable_ctam=True, disable_ctam_modules=True, disable_tracking=False,
        disable_polygon_expansion=False, refl_threshold=20.0,
        min_seed_percentage=10.0, drop_offset=0.0, config_dir=None,
        goes_enabled=False, mrms_core_only=False, base_dir=str(tmp_path),
        handoff_enabled=True, disable_stormprob=True,
        dependencies=dependencies, ingest_run_id=run_id,
        readiness_check_seconds=0.01,
    )
    values.update(overrides)
    return PrimaryCycleConfig(**values)


class StubWorker:
    """Stands in for the spawned EdgeWARN process, observable across the fork.

    Observations go to a file rather than an attribute so they survive the
    process boundary exactly as a real worker's durable effects would.
    """

    def __init__(self, log_path, *, integration=True):
        self.log_path = log_path
        self.integration = integration

    def _note(self, entry):
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def __call__(self, log_queue, shared_state, detection_ready_event,
                 integration_ready_event, dt, *_args, **kwargs):
        self._note(["started", dt.isoformat()])
        detection_ready_event.wait()
        self._note(["detection_manifest",
                    bool(shared_state.get("detection_manifest"))])
        shared_state["edgewarn_generated_file"] = "stormcells.json"
        integration_ready_event.wait()
        self._note(["integration_manifest",
                    bool(shared_state.get("integration_manifest"))])
        shared_state["edgewarn_stage"] = {
            "status": "completed" if self.integration else "unavailable",
            "produced_artifacts": ["stormcells.json"],
            "errors": [] if self.integration else ["integration unavailable"],
        }
        return None


def observations(worker):
    if not Path(worker.log_path).exists():
        return []
    return [json.loads(line) for line in
            Path(worker.log_path).read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from util.runtime import cycle

    stub = StubWorker(tmp_path / "worker-observations.jsonl")
    monkeypatch.setattr(cycle, "edgewarn_cycle_worker", stub)
    return stub


@pytest.fixture
def worker_factory(tmp_path, monkeypatch):
    """Install successive worker stubs, each with its own observation log."""
    from util.runtime import cycle

    created = []

    def factory(**kwargs):
        stub = StubWorker(tmp_path / f"worker-{len(created)}.jsonl", **kwargs)
        created.append(stub)
        monkeypatch.setattr(cycle, "edgewarn_cycle_worker", stub)
        return stub

    return factory


def run_cycle(tmp_path, dependencies, dt=T, *, overrides=None):
    manager = multiprocessing.Manager()
    try:
        return run_primary_cycle_once(
            dt, manager, config=make_config(tmp_path, dependencies, **(overrides or {})))
    finally:
        manager.shutdown()


class TestStartGate:
    @pytest.mark.parametrize("missing", CHECKS)
    def test_every_individual_missing_check_blocks_start(self, producer, tmp_path, missing,
                                                          worker):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            if product != missing:
                notify(inventory, commit(inventory, product))
        outcome = run_cycle(tmp_path, dependencies)
        assert not outcome.completed
        assert outcome.stages["ingest"].status.value == "unavailable"
        assert "start readiness" in outcome.stages["ingest"].errors[0]
        assert observations(worker) == []
        assert inventory.handoff.read("core-start-ready", canonical_cycle_id(T)) is None

    def test_a_complete_local_check_set_starts_detection(self, producer, tmp_path, worker):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        outcome = run_cycle(tmp_path, dependencies)
        assert outcome.completed
        seen = observations(worker)
        assert seen[0] == ["started", T.isoformat()]
        assert ["detection_manifest", True] in seen
        assert ["integration_manifest", True] in seen
        assert outcome.input_manifest is not None
        assert outcome.input_manifest.cycle_time == T

    def test_remote_presence_without_a_local_commit_blocks_start(self, producer, tmp_path,
                                                                 worker):
        """An upstream listing is not readiness: only a committed local input is."""
        registry, dependencies, inventory = producer
        for product in CHECKS:
            commit(inventory, product)
        # Committed, but never notified and never published as a phase.
        outcome = run_cycle(tmp_path, dependencies)
        assert not outcome.completed
        assert observations(worker) == []
        publish(inventory, dependencies)
        assert run_cycle(tmp_path, dependencies).completed

    def test_a_disabled_durable_handoff_is_rejected_with_a_diagnostic(self, producer,
                                                                     tmp_path, worker):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        outcome = run_cycle(tmp_path, dependencies, overrides={"handoff_enabled": False})
        assert not outcome.completed
        assert "runtime.handoff.enabled is false" in outcome.stages["ingest"].errors[0]
        assert observations(worker) == []


class TestIntegrationWait:
    def test_detection_finishes_before_a_slow_non_check_input(self, producer, tmp_path,
                                                              worker):
        """Detection begins at check readiness; integration waits locally."""
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        later = T + timedelta(minutes=1)
        published = []

        def release_integration():
            # The optional layer arrives only after detection has finished, which
            # proves detection never waited on it.
            assert ["detection_manifest", True] in observations(worker)
            notify(inventory, commit(inventory, PRECIP_RATE, later))
            publish(inventory, dependencies, optional_started_at=later)
            published.append(True)

        import threading

        timer = threading.Timer(0.05, release_integration)
        timer.start()
        try:
            outcome = run_cycle(tmp_path, dependencies)
        finally:
            timer.cancel()
        assert published == [True]
        assert outcome.completed
        assert outcome.stages["ingest"].status.value == "completed"

    def test_a_terminal_scan_wakes_the_wait_with_a_truthful_failure(self, producer,
                                                                   tmp_path, worker):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        import threading

        def expire():
            inventory.handoff.disposition(
                "terminal", canonical_cycle_id(T), status="expired",
                reason="incomplete scan expired; missing ['PrecipRate_00.00']")

        timer = threading.Timer(0.05, expire)
        timer.start()
        try:
            outcome = run_cycle(tmp_path, dependencies)
        finally:
            timer.cancel()
        assert not outcome.completed
        assert outcome.retryable
        assert outcome.stages["ingest"].status.value == "unavailable"
        assert any("terminal ingest disposition" in error
                   for error in outcome.stages["ingest"].errors)

    def test_a_worker_restart_after_a_partial_wait_still_installs_the_snapshot(
            self, producer, tmp_path, worker_factory):
        """A second cycle for the same scan reuses the pinned, immutable record."""
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        first = worker_factory(integration=False)
        assert not run_cycle(tmp_path, dependencies).completed
        assert ["started", T.isoformat()] in observations(first)
        publish(inventory, dependencies, optional_started_at=T + timedelta(seconds=1))
        second = worker_factory()
        outcome = run_cycle(tmp_path, dependencies)
        assert outcome.completed
        assert observations(second)[0] == ["started", T.isoformat()]
        assert ["integration_manifest", True] in observations(second)


class TestSelection:
    def test_only_locally_complete_scans_are_pending(self, producer):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        notify(inventory, commit(inventory, REFLECTIVITY, T + timedelta(minutes=2)))
        reader = LocalReadinessReader(base_dir=str(producer[2].base_dir),
                                      run_id="core-run", dependencies=dependencies,
                                      log=lambda _message: None)
        assert reader.pending_scans() == (T,)

    def test_a_terminal_scan_is_no_longer_pending(self, producer):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        reader = LocalReadinessReader(base_dir=str(inventory.base_dir), run_id="core-run",
                                      dependencies=dependencies, log=lambda _m: None)
        assert reader.pending_scans() == (T,)
        reader.skip(T, "backlog exceeded the configured cap")
        assert reader.pending_scans() == ()
        terminal = inventory.handoff.read("terminal", canonical_cycle_id(T))
        assert terminal.data["status"] == "skipped"
        assert "backlog" in terminal.data["reason"]

    def test_a_dependency_set_that_no_longer_matches_is_never_a_candidate(self, producer):
        """A changed producer/consumer agreement fails visibly."""
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        assert LocalReadinessReader(
            base_dir=str(inventory.base_dir), run_id="core-run",
            dependencies=dependencies, log=lambda _m: None).pending_scans() == (T,)
        altered = replace(dependencies, check=dependencies.check[:1],
                          detection=dependencies.detection[:1])
        reader = LocalReadinessReader(base_dir=str(inventory.base_dir), run_id="core-run",
                                      dependencies=altered, log=lambda _m: None)
        # A disagreement raises rather than degrading to an empty scan list, so
        # it can never be mistaken for "no work available".
        with pytest.raises(IngestRecordError, match="fingerprint"):
            reader.pending_scans()
        from util.runtime.mrms_registry import (
            MrmsProducerUnavailable, publish_ingest_registry, require_ingest_agreement,
        )

        publish_ingest_registry(registry, "ingest-producer",
                                dependency_fingerprint=dependencies.fingerprint)
        with pytest.raises(MrmsProducerUnavailable, match="mismatch"):
            require_ingest_agreement(registry, altered.fingerprint)


class TestPinLifecycle:
    def test_a_running_cycle_holds_a_reference_that_retention_yields_to(self, producer,
                                                                       tmp_path, worker):
        registry, dependencies, inventory = producer
        committed = [notify(inventory, commit(inventory, product)) for product in CHECKS]
        publish(inventory, dependencies)
        outcome = run_cycle(tmp_path, dependencies)
        assert outcome.completed
        # Retention skips inputs a live Core phase still references, so the pin
        # the consumer takes is what keeps the exact bytes on disk.
        assert not any(pin.key.startswith("core:")
                      for pin in inventory.handoff.records("pin"))
        # Unreferenced, unrendered inputs stay referenced by the outbox, so a
        # live phase or a pending render job always wins over the age window.
        assert {record.key for record in committed} == {
            record.key for record in inventory.handoff.records("input")}


class TestHandoffWiring:
    def test_realtime_core_publishes_no_legacy_render_triggers(self, producer, tmp_path,
                                                                worker):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        run_cycle(tmp_path, dependencies)
        legacy = inventory.base_dir / "state" / "realtime" / "cycles"
        assert not legacy.exists() or not list(legacy.glob("*/mrms-ready.json"))
        assert not (inventory.base_dir / "state" / "realtime" / "ingest-reports").exists()

    def test_a_mismatched_dependency_fingerprint_is_rejected(self, producer, tmp_path, worker):
        registry, dependencies, inventory = producer
        for product in CHECKS:
            notify(inventory, commit(inventory, product))
        publish(inventory, dependencies)
        other = IngestHandoff(tmp_path, fingerprint='b' * 64, run_id="core-run")
        from util.runtime.cycle import read_ready_phase

        with pytest.raises(Exception):
            read_ready_phase(other, "core-start-ready", canonical_cycle_id(T), dependencies,
                             str(tmp_path), "core:test")


class TestCoreImportsNoAcquisition:
    """Structural proof that the realtime Core path cannot acquire sources.

    Behavioural stubbing would miss a future import added outside the covered
    call, so the module graph itself is pinned.
    """

    FORBIDDEN = {
        "common.ingest.mrms.acquisition",
        "common.ingest.mrms.discovery",
        "common.ingest.mrms.downloader",
        "common.ingest.mrms.main",
        "common.ingest.synoptic.main",
        "common.pipeline.coordinator",
    }

    @pytest.mark.parametrize("relative", ("util/runtime/cycle.py", "util/runtime/primary_service.py"))
    def test_no_realtime_module_imports_an_acquisition_implementation(self, relative):
        import ast

        source = Path(__file__).resolve().parents[3] / "src" / relative
        imported = set()
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module)
        assert not (imported & self.FORBIDDEN), sorted(imported & self.FORBIDDEN)

    def test_the_entry_point_does_not_import_the_remote_checker(self):
        source = (Path(__file__).resolve().parents[3] / "src" / "run_edgewarn.py").read_text(
            encoding="utf-8")
        assert "MRMSUpdateChecker" not in source
        assert "publish_registry" not in source
