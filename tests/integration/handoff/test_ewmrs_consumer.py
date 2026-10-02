"""EWMRS per-input render consumer (plan phase 6).

Covers: a non-check arrival rendering before Core readiness, two arrivals for
one scan producing separate work, a later scan arriving while a render is in
flight, partial RAP success, per-layer retry with unrelated layers advancing,
acknowledgment loss after output publication, a stopped Core not pausing
rendering, an explicit no-mapping acknowledgment, and expiry with a reason.

The renderer is faked at the layer boundary, so no raster work and no upstream
access happens here. The durable handoff is real.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest
from common.config.loader import load_config
from common.config.mrms_products import parse_product_id
from common.ingest.inventory import InputInventory
from common.ingest.mrms.core_contract import resolve_dependencies
from common.ingest.mrms.registry import _plain, build_registry
from common.ingest.manifest import StagedInput
from common.ingest.objects import CommittedInput
from util.runtime.ewmrs_consumer import (
    INPUT_LAYER,
    InputRenderConsumer,
    RenderJobSettings,
    render_configuration_fingerprint,
)
from util.runtime.ingest_handoff import render_job_id
from util.runtime.mrms_registry import publish_ingest_registry
from util.runtime.services import ServiceHeartbeat, heartbeat_path, write_heartbeat

UTC = timezone.utc
T = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
REFLECTIVITY = "MergedReflectivityQCComposite_00.50"
PRECIP_RATE = "PrecipRate_00.00"
UNMAPPED = "RadarQualityIndex_00.00"
RAP = "RAP"


class FakeLayerPool:
    """A single-worker render pool whose jobs a test can hold open."""

    def __init__(self, *, failing=(), outputs=None, gate=None):
        self.failing = set(failing)
        self.outputs = outputs
        self.calls = []
        self.gate = gate
        self.closed = False

    def render(self, layers):
        results = {}
        for layer in layers:
            name = str(layer["name"])
            self.calls.append((name, layer.get("input_path")))
            if self.gate is not None:
                self.gate.wait(30)
            if name in self.failing:
                results[name] = None
            elif self.outputs is not None:
                results[name] = self.outputs.get(name)
            else:
                results[name] = f"{name}.png"
        return results

    def shutdown(self, **kwargs):
        self.closed = True


def make_registry(base_dir):
    catalog = _plain(load_config("ingest")["mrms"])
    catalog["products"] = [f"MRMS_{PRECIP_RATE}", f"MRMS_{UNMAPPED}"]
    return build_registry(catalog, Path(base_dir))


def make_dependencies(registry):
    return resolve_dependencies(
        registry, include_rap=True, include_glm=False,
        auxiliary_settings={"rap": {"max_age_minutes": 180}})


def commit(inventory, product, at=T, *, family="mrms", name=None):
    suffix = "grib2"
    if name is None:
        name = (f"MRMS_PROBSEVERE_{at:%Y%m%d_%H%M%S}.json" if product == "ProbSevere"
                else f"MRMS_{product}_{at:%Y%m%d-%H%M%S}.{suffix}")
    path = inventory.base_dir / "data" / product / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"validated {product} {at.isoformat()}".encode()
    path.write_bytes(body)
    staged = StagedInput(product, str(path), at, "s3", family)
    return inventory.commit_input(
        CommittedInput(staged, hashlib.sha256(body).hexdigest(), f"s3://bucket/{product}"))


def notify(inventory, committed):
    return inventory.handoff.publish_render_ready(committed.key)


def drain(consumer, registry, *, now=None, passes=200):
    """Poll until nothing is in flight and no new work was admitted.

    Renders are admitted asynchronously on purpose, so a test that wants the
    durable result drains instead of assuming one pass is enough.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and passes > 0:
        before = consumer.metrics["dispatched"]
        consumer.poll_once(registry, now=now)
        passes -= 1
        if not consumer.in_flight and consumer.metrics["dispatched"] == before:
            return
    assert not consumer.in_flight, "render work never completed"


@pytest.fixture
def world(tmp_path):
    registry = make_registry(tmp_path)
    dependencies = make_dependencies(registry)
    inventory = InputInventory(tmp_path, fingerprint=dependencies.fingerprint,
                               run_id="ingest")
    publish_ingest_registry(registry, "ingest", dependency_fingerprint=dependencies.fingerprint)
    write_heartbeat(ServiceHeartbeat("ingest", 1, "ingest", datetime.now(UTC)),
                    heartbeat_path(tmp_path, "ingest"))
    return registry, dependencies, inventory


def make_consumer(world, pool, **settings):
    registry, dependencies, inventory = world
    values = dict(pending_max_jobs=64, max_age_minutes=120, retry_max_attempts=3,
                  retry_initial_seconds=5, retry_max_seconds=30)
    values.update(settings)
    consumer = InputRenderConsumer(
        inventory.base_dir, run_id="ewmrs", dependencies=dependencies,
        log=lambda _message: None, settings=RenderJobSettings(**values), pool=pool)
    return consumer


class StubRenderer:
    """Patches the two rendering seams the consumer calls."""

    def __init__(self, *, failing=(), gate=None):
        self.failing = set(failing)
        self.gate = gate
        self.mrms_calls = []
        self.rap_calls = []
        self.complete = set()

    def layer_output_complete(self, layer):
        name = str(layer.get("name"))
        if name in self.failing:
            return False
        if name not in self.complete:
            self.complete.add(name)
        return True

    def _wait(self):
        if self.gate is not None:
            self.gate.wait(30)


@pytest.fixture
def renderer(monkeypatch):
    stub = StubRenderer()
    import EWMRS.pipeline as pipeline_module
    import EWMRS.rap.uint16_pipeline as rap_module

    monkeypatch.setattr(pipeline_module, "layer_output_complete", stub.layer_output_complete)

    def run_rap(rap_file, dt=None, layers=None, **kwargs):
        stub._wait()
        names = [str(layer["name"]) for layer in (layers or [])]
        stub.rap_calls.append((str(rap_file), tuple(names)))
        return {name: (None if name in stub.failing else f"{name}.u16") for name in names}

    monkeypatch.setattr(rap_module, "run_rap_uint16_pipeline", run_rap)
    monkeypatch.setattr(pipeline_module, "run_rap_uint16_pipeline", run_rap, raising=False)
    return stub


class TestPerInputWork:
    def test_a_non_check_arrival_renders_before_core_readiness(self, world, renderer):
        """A renderable input becomes work without any Core phase record."""
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        assert inventory.handoff.records("core-start-ready") == ()

        drain(consumer, registry)
        metrics = consumer.metrics

        assert metrics["dispatched"] == 1
        assert metrics["succeeded"] == 1
        assert pool.calls and pool.calls[0][1] == committed.data["input"]["path"]
        assert inventory.handoff.read(
            "render-ack", render_job_id(committed.key, INPUT_LAYER,
                                        consumer.fingerprint)) is not None

    def test_two_arrivals_for_one_scan_create_separate_work(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        first = notify(inventory, commit(inventory, PRECIP_RATE))
        second = notify(inventory, commit(inventory, PRECIP_RATE, T + timedelta(minutes=2)))

        drain(consumer, registry)

        assert len(inventory.handoff.records("render-ready")) == 2
        assert {path for _, path in pool.calls} == {
            first.data["input"]["path"], second.data["input"]["path"]}
        assert {record.data["input_id"]
                for record in inventory.handoff.records("render-ack")} == {
            first.key, second.key}
        for record in inventory.handoff.records("render-ack"):
            assert record.data["input_id"] in {first.key, second.key}
        assert consumer.metrics["dispatched"] == 2

    def test_a_later_scan_arriving_during_a_render_is_not_blocked(self, world, renderer):
        """Acceptance continues while an earlier render is still running."""
        registry, dependencies, inventory = world
        gate = threading.Event()
        pool = FakeLayerPool(gate=gate)
        consumer = make_consumer(world, pool)
        first = notify(inventory, commit(inventory, PRECIP_RATE))

        consumer.poll_once(registry)
        while not pool.calls:
            threading.Event().wait(0.01)
        # The T input is still rendering when the T+2 input arrives.
        assert consumer.in_flight
        later = notify(inventory, commit(inventory, PRECIP_RATE, T + timedelta(minutes=2)))
        assert {path for _, path in pool.calls} == {
            inventory.handoff.read("input", first.key).data["input"]["path"]}

        consumer.poll_once(registry)

        # The later arrival was admitted on the next pass even though the first
        # render had not finished, and it renders from its own source file.
        assert {path for _, path in pool.calls} == {
            inventory.handoff.read("input", first.key).data["input"]["path"],
            later.data["input"]["path"]}
        gate.set()
        drain(consumer, registry)
        assert {record.data["input_id"]
                for record in inventory.handoff.records("render-ack")} == {
            first.key, later.key}

    def test_an_input_with_no_layer_mapping_is_acknowledged_explicitly(self, world,
                                                                        renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        committed = notify(inventory, commit(inventory, UNMAPPED))
        # An enabled product with no EWMRS render layer configured.
        assert consumer.layers_for(inventory.handoff.read("render-ready", committed.key)) == ()

        consumer.poll_once(registry)

        assert pool.calls == []
        ack = inventory.handoff.read(
            "render-ack", render_job_id(committed.key, INPUT_LAYER, consumer.fingerprint))
        assert ack.data["status"] == "no-mapping"
        assert "No configured layer mapping" in ack.data["reason"]


class TestPerLayerIndependence:
    def test_unrelated_layers_advance_while_one_fails(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, retry_initial_seconds=0, retry_max_seconds=0)
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        layers = consumer.layers_for(
            inventory.handoff.read("render-ready", committed.key))
        renderer.failing = {layers[0]}

        drain(consumer, registry)

        states = {layer: inventory.handoff.read(
            "render-ack", render_job_id(committed.key, layer, consumer.fingerprint))
            for layer in layers}
        assert states[layers[0]].data["status"] == "retry"
        assert all(states[layer].data["status"] == "success" for layer in layers[1:])
        # The input is not acknowledged while one mapped layer is still retrying.
        assert inventory.handoff.read(
            "render-ack", render_job_id(committed.key, INPUT_LAYER,
                                        consumer.fingerprint)) is None

    def test_a_failed_layer_retries_and_only_itself(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, retry_initial_seconds=0, retry_max_seconds=0)
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        layers = consumer.layers_for(
            inventory.handoff.read("render-ready", committed.key))
        renderer.failing = {layers[0]}
        drain(consumer, registry)
        first_calls = list(pool.calls)

        renderer.failing = set()
        drain(consumer, registry, now=datetime.now(UTC) + timedelta(seconds=1))

        retried = [name for name, _ in pool.calls[len(first_calls):]]
        assert retried == [layers[0]]
        ack = inventory.handoff.read(
            "render-ack", render_job_id(committed.key, layers[0], consumer.fingerprint))
        assert ack.data["status"] == "success"
        assert inventory.handoff.read(
            "render-ack", render_job_id(committed.key, INPUT_LAYER,
                                        consumer.fingerprint)).data["status"] == "success"

    def test_a_permanently_failing_layer_expires_with_a_reason(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, retry_initial_seconds=0, retry_max_seconds=0,
                                 retry_max_attempts=1)
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        layers = consumer.layers_for(
            inventory.handoff.read("render-ready", committed.key))
        renderer.failing = {layers[0]}

        drain(consumer, registry)

        ack = inventory.handoff.read(
            "render-ack", render_job_id(committed.key, layers[0], consumer.fingerprint))
        assert ack.data["status"] == "expired"
        assert "no usable artifacts" in ack.data["reason"]
        final = inventory.handoff.read(
            "render-ack", render_job_id(committed.key, INPUT_LAYER, consumer.fingerprint))
        assert final.data["status"] == "expired"

    def test_a_render_exception_is_a_failed_job(self, world, renderer, monkeypatch):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, retry_initial_seconds=0, retry_max_seconds=0)

        def explode(_layers):
            raise RuntimeError("renderer crashed")

        pool.render = explode
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        drain(consumer, registry)
        layer = consumer.layers_for(inventory.handoff.read("render-ready", committed.key))[0]
        ack = inventory.handoff.read(
            "render-ack", render_job_id(committed.key, layer, consumer.fingerprint))
        assert ack.data["status"] in {"retry", "expired"}
        assert "renderer crashed" in ack.data["reason"]

    def test_a_render_exception_logs_its_stack_trace(self, world, renderer):
        """test-run-1001 logged only "KeyError: 'render'", which hid the call site."""
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, retry_initial_seconds=0, retry_max_seconds=0)
        logged = []
        consumer._log = logged.append

        def explode(_layers):
            raise KeyError("render")

        pool.render = explode
        notify(inventory, commit(inventory, PRECIP_RATE))
        drain(consumer, registry)
        traces = [message for message in logged if "Traceback" in message]
        assert traces and "explode" in traces[0] and "KeyError: 'render'" in traces[0]


class TestPartialRapSuccess:
    def test_successful_rap_layers_are_acknowledged_separately(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, retry_initial_seconds=0, retry_max_seconds=0)
        rap_name = "RAP.20260930-12z.awp130pgrbf00.grib2"
        committed = notify(inventory, commit(
            inventory, RAP, datetime(2026, 9, 30, 12, 0, tzinfo=UTC), family="rap",
            name=rap_name))
        layers = consumer.layers_for(inventory.handoff.read("render-ready", committed.key))
        assert layers
        renderer.failing = {layers[0]}

        drain(consumer, registry)

        # One call carrying only the selected layer, reusing the pinned analysis.
        assert renderer.rap_calls == [(str(inventory.base_dir / "data" / RAP / rap_name),
                                       (layers[0],))]
        states = {layer: inventory.handoff.read(
            "render-ack", render_job_id(committed.key, layer, consumer.fingerprint))
            for layer in layers}
        assert states[layers[0]].data["status"] == "retry"
        assert all(states[layer].data["status"] == "success" for layer in layers[1:])

    def test_a_reused_rap_analysis_produces_no_second_notification(self, world, renderer):
        registry, dependencies, inventory = world
        consumer = make_consumer(world, FakeLayerPool())
        rap_name = "RAP.20260930-12z.awp130pgrbf00.grib2"
        first = notify(inventory, commit(
            inventory, RAP, datetime(2026, 9, 30, 12, 0, tzinfo=UTC), family="rap",
            name=rap_name))
        path = inventory.base_dir / "data" / RAP / rap_name
        again = inventory.commit_input(CommittedInput(
            StagedInput(RAP, str(path), datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
                        "synoptic", "rap"),
            hashlib.sha256(path.read_bytes()).hexdigest(), "s3://rap"))
        assert again.key == first.key
        assert len(inventory.handoff.records("render-ready")) == 1


class TestPublicationAndRecovery:
    def test_a_complete_output_is_reused_after_acknowledgment_loss(self, world, renderer):
        """Restart after publication but before acknowledgment reuses the output."""
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        drain(consumer, registry)
        layer = consumer.layers_for(inventory.handoff.read("render-ready", committed.key))[0]
        input_ack = render_job_id(committed.key, INPUT_LAYER, consumer.fingerprint)
        assert inventory.handoff.read("render-ack", input_ack) is not None
        # Simulate a crash after output publication but before the input-level
        # acknowledgment was durable.
        inventory.handoff.path("render-ack", input_ack).unlink()
        calls_before = list(pool.calls)

        restarted = make_consumer(world, pool)
        drain(restarted, registry)

        assert pool.calls == calls_before
        assert inventory.handoff.read(
            "render-ack", render_job_id(committed.key, INPUT_LAYER,
                                        restarted.fingerprint)).data["status"] == "success"

    def test_an_unchanged_input_creates_no_new_render_work(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        notify(inventory, commit(inventory, PRECIP_RATE))
        drain(consumer, registry)
        before = list(pool.calls)

        consumer.poll_once(registry)

        assert pool.calls == before
        assert len(inventory.handoff.records("render-ack")) >= 1

    def test_a_changed_render_configuration_reopens_the_work(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        drain(consumer, registry)
        first = consumer.fingerprint

        consumer._fingerprint = "f" * 64
        assert consumer.fingerprint != first
        calls_before = len(pool.calls)
        drain(consumer, registry)
        assert len(pool.calls) > calls_before
        assert render_configuration_fingerprint() != "f" * 64
        assert committed.data["input"]["path"] in {path for _, path in pool.calls}

    def test_pending_work_expires_with_an_explicit_reason(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, retry_max_attempts=99,
                                 retry_initial_seconds=3600, retry_max_seconds=3600)
        committed = notify(inventory, commit(inventory, PRECIP_RATE))
        layer = consumer.layers_for(inventory.handoff.read("render-ready", committed.key))[0]
        renderer.failing = {layer}
        drain(consumer, registry)
        stale = datetime.now(UTC) + timedelta(minutes=consumer.settings.max_age_minutes + 1)

        consumer.poll_once(registry, now=stale)

        ack = inventory.handoff.read(
            "render-ack", render_job_id(committed.key, layer, consumer.fingerprint))
        assert ack.data["status"] == "expired"
        assert "exceeded" in ack.data["reason"]

    def test_a_bounded_job_window_defers_new_work_explicitly(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool, pending_max_jobs=1, retry_max_attempts=99,
                                 retry_initial_seconds=3600, retry_max_seconds=3600)
        first = notify(inventory, commit(inventory, PRECIP_RATE))
        layers = consumer.layers_for(inventory.handoff.read("render-ready", first.key))
        renderer.failing = {layers[0]}
        drain(consumer, registry)
        notify(inventory, commit(inventory, PRECIP_RATE, T + timedelta(minutes=2)))

        consumer.poll_once(registry)

        assert consumer.metrics["rejected"] >= 1
        assert len(pool.calls) == 1


class TestProducerAgreement:
    def test_a_stopped_core_does_not_pause_rendering(self, world, renderer):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        notify(inventory, commit(inventory, PRECIP_RATE))

        drain(consumer, registry)

        assert pool.calls
        assert consumer.metrics["succeeded"] >= 1

    def test_a_missing_producer_pauses_visibly(self, world, renderer, tmp_path):
        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        notify(inventory, commit(inventory, PRECIP_RATE))
        (Path(tmp_path) / "state/realtime/services/ingest-mrms-registry.json").unlink()

        consumer.poll_once(registry)

        assert pool.calls == []
        assert consumer.metrics["dispatched"] == 0

    def test_a_dependency_mismatch_pauses_visibly(self, world, renderer):
        from dataclasses import replace

        registry, dependencies, inventory = world
        pool = FakeLayerPool()
        consumer = make_consumer(world, pool)
        notify(inventory, commit(inventory, PRECIP_RATE))
        consumer.dependencies = replace(dependencies, check=("Only_This_00.00",))

        consumer.poll_once(registry)

        assert pool.calls == []

    def test_the_render_fingerprint_covers_every_enabled_layer(self, world, renderer):
        from EWMRS.rap.config import get_rap_uint16_layers
        from EWMRS.render.config import get_mrms_file_list

        first = render_configuration_fingerprint()
        assert first == render_configuration_fingerprint()
        assert len(first) == 64
        assert get_mrms_file_list() and get_rap_uint16_layers()
        payload = json.dumps({"mrms": len(get_mrms_file_list()),
                              "rap": len(get_rap_uint16_layers())})
        assert payload


def test_the_default_render_pool_builds_against_the_real_catalog(world, tmp_path):
    """test-run-1001: _ensure_pool read ``render`` from runtime.yaml (it lives in
    ewmrs_pipeline.yaml), so every MRMS layer failed with KeyError: 'render'."""
    from EWMRS.pipeline_config import render_phase_name

    registry, dependencies, inventory = world
    consumer = InputRenderConsumer(tmp_path, run_id="ewmrs", dependencies=dependencies,
                                   log=lambda _message: None)
    try:
        pool = consumer._ensure_pool()
        assert pool.phase_name == render_phase_name()
        assert consumer._ensure_pool() is pool
    finally:
        consumer.close()
