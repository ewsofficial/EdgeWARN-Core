"""Fixed-clock ingest service behavior and shutdown (plan phase 4).

Every source is faked: no NOAA request is made, the monotonic clock only moves
when the service itself waits, and each test owns a temporary runtime root. The
completion check for this phase is that a slow source and a blocked
consumer-equivalent do not stop scheduled polls or unrelated input publication,
and that duplicate discoveries create no extra jobs.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from common.config.loader import load_config
from common.config.mrms_products import parse_product_id
from common.ingest.mrms.core_contract import resolve_dependencies
from common.ingest.mrms.registry import _plain, build_registry
from common.ingest.mrms.source import DiscoveredObject, source_for
from common.ingest.manifest import staged_input_from_path
from common.ingest.objects import CommittedInput
from util.runtime.handoff import ServiceLock, canonical_cycle_id
from util.runtime.ingest_service import IngestResources, IngestService

UTC = timezone.utc
T = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
REFLECTIVITY = "MergedReflectivityQCComposite_00.50"
PRECIP_FLAG = "PrecipFlag_00.00"
PROB_SEVERE = "ProbSevere"
PRECIP_RATE = "PrecipRate_00.00"
CHECKS = (REFLECTIVITY, PRECIP_FLAG, PROB_SEVERE)


class FixedClock:
    """A monotonic clock that advances only when the service itself waits.

    Recording the requested wait is what proves the timer never slept for real
    time, never drifted after work, and never replayed a burst of catch-up ticks
    after a missed deadline. ``on_wait`` lets a test settle the owned workers at
    each period boundary so downstream assertions are deterministic, and
    ``jump_to`` simulates a stalled period that must coalesce into one refresh.
    """

    def __init__(self, *, start=0.0, stop_event=None, stop_after_waits=None, real_delay=0.1):
        self.value = start
        self.waits = []
        self._stop_event = stop_event
        self._stop_after = stop_after_waits
        self._real_delay = real_delay
        self._jump_to = None

    def __call__(self):
        return self.value

    def jump_to(self, value):
        self._jump_to = float(value)

    def wait(self, seconds):
        time.sleep(self._real_delay)
        self.waits.append(round(float(seconds), 6))
        self.value = max(self.value + float(seconds), self._jump_to or 0.0)
        if self._stop_event is not None and self._stop_after is not None \
                and len(self.waits) >= self._stop_after:
            self._stop_event.set()
        return False


class ScriptedSource:
    """A fake MRMS upstream with per-product reveal times and behaviors."""

    def __init__(self, registry, *, reveal=None, slow=(), failing=(), block_event=None):
        self.registry = registry
        self.reveal = dict(reveal or {})
        self.slow = set(slow)
        self.failing = set(failing)
        self.block_event = block_event
        self.listings = []
        self.acquisitions = []

    def lister(self, spec, start, end):
        self.listings.append((spec.product_id, start, end))
        return tuple(self.discovered(spec, observation)
                     for observation in self.reveal.get(spec.product_id, ())
                     if start <= observation <= end)

    @staticmethod
    def _name(spec, observation):
        return (f"MRMS_PROBSEVERE_{observation:%Y%m%d_%H%M%S}.json"
                if spec.adapter == "probsevere_json"
                else f"MRMS_{spec.product_id}_{observation:%Y%m%d-%H%M%S}.grib2")

    def discovered(self, spec, observation):
        source = source_for(parse_product_id("MRMS_" + spec.product_id))
        return DiscoveredObject(spec.product_id, observation, "s3",
                                source.s3_prefix(observation) + self._name(spec, observation))

    def acquirer(self, discovered):
        self.acquisitions.append(discovered.logical_identity)
        if discovered.product_id in self.slow:
            # A stalled source holds its worker for the whole run without ever
            # becoming a reason to skip or delay another product's tick.
            self.block_event.wait(60)
        if discovered.product_id in self.failing:
            raise TimeoutError(f"{discovered.product_id} upstream timed out")
        spec = self.registry.require(discovered.product_id)
        body = f"payload {discovered.product_id} {discovered.observation_time.isoformat()}"
        path = spec.directory / self._name(spec, discovered.observation_time)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        staged = staged_input_from_path(spec.product_id, path, source="s3", family="mrms")
        return CommittedInput(staged, hashlib.sha256(body.encode()).hexdigest(), discovered.locator)


def make_registry(base_dir, additions=(f"MRMS_{PRECIP_RATE}",)):
    """A four-product registry: the three protected checks plus one optional."""
    catalog = _plain(load_config("ingest")["mrms"])
    catalog["products"] = list(additions)
    return build_registry(catalog, Path(base_dir))


def make_service(tmp_path, source, *, clock=None, stop=None, wall=None, **settings):
    dependencies = resolve_dependencies(
        source.registry, include_rap=False, include_glm=False,
        auxiliary_settings={"rap": {"max_age_minutes": 180}})
    clock = clock if clock is not None else FixedClock()
    return IngestService(
        base_dir=tmp_path, run_id="ingest-test", registry=source.registry,
        dependencies=dependencies, io=None,
        stop_event=stop if stop is not None else threading.Event(), clock=clock,
        wall_clock=wall if wall is not None else (lambda: T), wait=clock.wait,
        resources=IngestResources.resolve().with_overrides(**settings),
        lister=source.lister, acquirer=source.acquirer, auxiliary=lambda kind, target: ())


def wait_for(predicate, message, *, attempts=1000, delay=0.01):
    for _ in range(attempts):
        if predicate():
            return
        time.sleep(delay)
    raise AssertionError(message)


def _work(kind):
    from util.runtime.ingest_service import _Work

    return _Work(kind=kind)


def idle(service):
    """True when no acquisition work is queued, in flight, or waiting to retry."""
    counts = service.ledger.counts()
    return counts["pending_jobs"] == 0 and counts["in_flight_jobs"] == 0


def poll_status(service):
    record = service.inventory.handoff.read("poll-status", "poll-status")
    if record is None:
        return {}
    return {**record.data["counts"], "reasons": record.data["reasons"]}


def record_poll_statuses(service):
    """Capture every published poll status, whose reason list is reset each pass."""
    published = []
    handoff = service.inventory.handoff
    original = handoff.publish_poll_status

    def capture(counts, reasons=()):
        published.append({"counts": dict(counts), "reasons": list(reasons)})
        return original(counts, reasons)

    handoff.publish_poll_status = capture
    return published


def run_fixed(service, clock, stop, *, timeout=60):
    """Run the real ``run()`` loop until the clock stops it, then join it.

    The clock, not the test thread, decides when the loop ends, so the recorded
    wait sequence is exactly what the service itself requested.
    """
    worker = threading.Thread(target=service.run, name="ingest-run", daemon=True)
    worker.start()
    worker.join(timeout)
    stop.set()
    worker.join(timeout)
    assert not worker.is_alive(), "the fixed-clock run loop never returned"
    service.shutdown()


def settle(service, clock):
    """Admit whatever the just-dispatched listings produced, then drain.

    Uses the service's own admission entry point so the test exercises the
    production rule rather than a private shortcut.
    """
    for _ in range(500):
        service.dispatch_pending(clock.value)
        # unfinished_tasks, not empty(): the publisher drains a whole batch
        # off the queue before it finishes processing (and evaluating) it.
        if idle(service) and service._work.unfinished_tasks == 0:
            return
        time.sleep(0.01)
    raise AssertionError("the acquisition window and publisher queue never drained")


def poll_periods(service, periods, *, start=0.0, period=10.0):
    """Dispatch *periods* scheduled polls, settling every owned worker between."""
    clock = service._clock
    clock.value = start
    for _ in range(periods):
        service._tick(clock.value)
        clock.value += period
        settle(service, clock)
    return clock.value


class TestScheduledPollCadence:
    def test_a_stalled_source_stops_neither_the_cadence_nor_other_publication(self, tmp_path):
        """Phase completion check: polls keep their schedule and unrelated
        inputs publish while one product's transfer is still running."""
        registry = make_registry(tmp_path)
        block = threading.Event()
        source = ScriptedSource(registry,
                                reveal={REFLECTIVITY: [T], PRECIP_RATE: [T]},
                                slow={REFLECTIVITY}, block_event=block)
        stop = threading.Event()
        clock = FixedClock(stop_event=stop, stop_after_waits=4)
        service = make_service(tmp_path, source, clock=clock, stop=stop,
                               wall=lambda: T + timedelta(seconds=clock.value),
                               download_concurrency=4)
        try:
            run_fixed(service, clock, stop)
            wait_for(lambda: service.status()["notifications"] >= 1,
                     "the unstalled product never published its render notification")
        finally:
            block.set()
            service.shutdown()
        assert clock.waits == [10.0, 10.0, 10.0, 10.0]
        assert service.status()["polls"] == 4
        records = service.inventory.handoff.records("render-ready")
        assert [record.data["input"]["product"] for record in records] == [PRECIP_RATE]
        status = poll_status(service)
        assert status["polls"] == 4
        assert status["committed_inputs"] == 1
        assert status["listing_overruns"] == 0
        assert status["coalesced_ticks"] == 0
        # The stalled product was attempted exactly once and never resolved, so
        # it created no duplicate work and no partial input.
        assert source.acquisitions.count((REFLECTIVITY, T.isoformat())) == 1
        assert all(record.data["input"]["product"] != REFLECTIVITY
                   for record in service.inventory.handoff.records("input"))

    def test_one_active_listing_per_product_while_a_listing_is_slow(self, tmp_path):
        registry = make_registry(tmp_path)
        source = ScriptedSource(registry)
        listing_started = threading.Event()
        release = threading.Event()

        def slow_lister(spec, start, end):
            source.listings.append((spec.product_id, start, end))
            listing_started.set()
            release.wait(60)
            return ()

        source.lister = slow_lister
        stop = threading.Event()
        clock = FixedClock(stop_event=stop, stop_after_waits=3)
        service = make_service(tmp_path, source, clock=clock, stop=stop)
        worker = threading.Thread(target=service.run, name="ingest-run", daemon=True)
        worker.start()
        try:
            assert listing_started.wait(30)
            time.sleep(0.3)
            assert len(source.listings) == len(registry.products)
        finally:
            release.set()
            stop.set()
            worker.join(60)
            service.shutdown()

    def test_missed_deadlines_coalesce_into_one_refresh(self, tmp_path):
        registry = make_registry(tmp_path)
        source = ScriptedSource(registry)
        stop = threading.Event()
        clock = FixedClock(stop_event=stop, stop_after_waits=2)
        clock.jump_to(45.0)
        service = make_service(tmp_path, source, clock=clock, stop=stop)
        run_fixed(service, clock, stop)
        status = poll_status(service)
        assert status["polls"] == 2
        assert status["coalesced_ticks"] == 3


class TestDeduplication:
    def test_repeated_discoveries_create_no_extra_jobs(self, tmp_path):
        registry = make_registry(tmp_path)
        source = ScriptedSource(registry, reveal={REFLECTIVITY: [T]})
        service = make_service(tmp_path, source)
        service.start()
        try:
            poll_periods(service, 4)
        finally:
            service.shutdown()
        assert source.acquisitions == [(REFLECTIVITY, T.isoformat())]
        status = poll_status(service)
        assert status["polls"] == 4
        assert status["committed_inputs"] == 1
        assert status["duplicate_discoveries"] >= 1
        assert len(service.inventory.handoff.records("render-ready")) == 1

    def test_an_unchanged_input_creates_no_second_render_record(self, tmp_path):
        registry = make_registry(tmp_path)
        source = ScriptedSource(registry, reveal={REFLECTIVITY: [T]})
        service = make_service(tmp_path, source)
        service.start()
        try:
            poll_periods(service, 2)
        finally:
            service.shutdown()
        assert service.inventory.reconcile()["published"] == ()

    def test_two_arrivals_for_one_scan_are_independent_notifications(self, tmp_path):
        registry = make_registry(tmp_path)
        previous = T - timedelta(minutes=2)
        source = ScriptedSource(registry, reveal={REFLECTIVITY: [previous, T]})
        service = make_service(tmp_path, source)
        service.start()
        try:
            poll_periods(service, 2)
        finally:
            service.shutdown()
        assert service.status()["notifications"] == 2
        times = sorted(record.data["input"]["analysis_time"]
                       for record in service.inventory.handoff.records("render-ready"))
        assert times == [previous.isoformat(), T.isoformat()]


class TestCoreReadinessEvaluation:
    def test_detection_waits_for_every_local_check_input(self, tmp_path):
        registry = make_registry(tmp_path)
        source = ScriptedSource(registry, reveal={REFLECTIVITY: [T], PRECIP_RATE: [T]})
        service = make_service(tmp_path, source)
        service.start()
        try:
            poll_periods(service, 1)
            assert service.status()["notifications"] == 2
            assert service.inventory.handoff.read(
                "core-start-ready", canonical_cycle_id(T)) is None
            source.reveal[PRECIP_FLAG] = [T]
            source.reveal[PROB_SEVERE] = [T]
            poll_periods(service, 2, start=10.0)
            assert service.status()["notifications"] == 4
            wait_for(lambda: service.inventory.handoff.read(
                "core-start-ready", canonical_cycle_id(T)) is not None,
                "the scan never became start-ready once every check arrived")
        finally:
            service.shutdown()

    def test_a_failed_check_never_produces_a_start_record(self, tmp_path):
        registry = make_registry(tmp_path)
        source = ScriptedSource(registry, reveal={product: [T] for product in CHECKS},
                                failing={PROB_SEVERE})
        service = make_service(tmp_path, source, retry_initial_seconds=0,
                               retry_max_seconds=0, retry_max_attempts=2)
        published = record_poll_statuses(service)
        service.start()
        try:
            poll_periods(service, 4)
        finally:
            service.shutdown()
        settled = service.ledger.state(f"mrms:{PROB_SEVERE}|{T.isoformat()}")
        assert settled[0] == "abandoned"
        assert service.inventory.handoff.read(
            "core-start-ready", canonical_cycle_id(T)) is None
        assert any(PROB_SEVERE in reason
                   for status in published for reason in status["reasons"])
        assert source.acquisitions.count((PROB_SEVERE, T.isoformat())) == 2

    def test_an_incomplete_scan_expires_with_an_explicit_missing_list(self, tmp_path):
        registry = make_registry(tmp_path)
        source = ScriptedSource(registry, reveal={REFLECTIVITY: [T]})
        service = make_service(tmp_path, source, scan_deadline_seconds=1,
                               reconcile_interval_seconds=1)
        service.start()
        try:
            service._tick(0.0)
            wait_for(lambda: service.status()["notifications"] == 1,
                     "the arrival was not published")
            later = T + timedelta(seconds=30)
            service._wall = lambda: later
            service._work.put(_work("maintenance"))
            wait_for(lambda: service._work.unfinished_tasks == 0, "the maintenance pass never ran")
        finally:
            service.shutdown()
        terminal = service.inventory.handoff.read("terminal", canonical_cycle_id(T))
        assert terminal is not None
        assert terminal.data["status"] == "expired"
        assert PRECIP_FLAG in terminal.data["reason"]
        assert PROB_SEVERE in terminal.data["reason"]
        assert service.inventory.handoff.read(
            "core-start-ready", canonical_cycle_id(T)) is None


class TestRun1001Regressions:
    """Acquisition ordering and publisher contention seen in test-run-1001."""

    def test_a_burst_of_completions_evaluates_each_scan_once(self, tmp_path):
        from util.runtime.ingest_service import _Work

        registry = make_registry(tmp_path)
        products = (*CHECKS, PRECIP_RATE)
        source = ScriptedSource(registry, reveal={product: [T] for product in products})
        service = make_service(tmp_path, source)
        calls = []
        original = service.inventory.publish_scan

        def spy(scan, *args, **kwargs):
            calls.append(scan)
            return original(scan, *args, **kwargs)

        service.inventory.publish_scan = spy
        for product in products:
            discovered = source.discovered(registry.require(product), T)
            service._work.put(_Work(kind="completion", completed=source.acquirer(discovered)))
        service._work.put(_Work(kind="shutdown"))
        # Run the publisher synchronously: the whole burst is one batch.
        service._publisher_loop()

        assert calls == [T]
        assert service._metrics["committed"] == len(products)
        assert service.inventory.handoff.read(
            "core-start-ready", canonical_cycle_id(T)) is not None

    def test_an_auxiliary_job_is_never_dispatched_as_mrms(self, tmp_path):
        from util.runtime.ingest_service import AcquisitionJob

        registry = make_registry(tmp_path)
        source = ScriptedSource(registry)
        attempts = []

        def auxiliary(kind, target):
            attempts.append((kind, target))
            if len(attempts) == 1:
                raise TimeoutError("GLM upstream timed out")
            return ()

        service = make_service(tmp_path, source, retry_initial_seconds=0,
                               retry_max_seconds=0)
        service._auxiliary = auxiliary
        submitted = {"ingest-download": [], "ingest-auxiliary": []}

        class Pool:
            def __init__(self, name):
                self.name = name

            def submit(self, function, *args):
                submitted[self.name].append((function, args))

        service._pools = {name: Pool(name) for name in submitted}
        identity = f"glm:{T.isoformat()}"
        assert service.ledger.offer(AcquisitionJob(identity=identity, kind="glm",
                                                   product_id="GLM", target=T))

        service.dispatch_pending(0.0)
        assert submitted["ingest-download"] == []
        service._dispatch_auxiliary(T, 0.0)
        function, args = submitted["ingest-auxiliary"].pop()
        assert function == service._acquire_auxiliary
        function(*args)  # first attempt fails and is requeued

        service.dispatch_pending(1e9)
        assert submitted["ingest-download"] == []
        service._dispatch_auxiliary(T, 1e9)
        function, args = submitted["ingest-auxiliary"].pop()
        function(*args)  # the retry runs on the auxiliary pool and succeeds

        assert attempts == [("glm", T), ("glm", T)]
        assert service.ledger.state(identity) == ("committed", "")
        assert service.ledger.counts()["in_flight_jobs"] == 0

    def test_a_cold_start_backlog_fetches_the_newest_scan_first(self, tmp_path):
        registry = make_registry(tmp_path)
        scans = [T - timedelta(minutes=2 * step) for step in range(4, -1, -1)]
        source = ScriptedSource(registry, reveal={product: scans
                                                  for product in (*CHECKS, PRECIP_RATE)})
        service = make_service(tmp_path, source, download_concurrency=1)
        service.start()
        try:
            poll_periods(service, 1)
        finally:
            service.shutdown()
        first = source.acquisitions[:len(CHECKS)]
        assert {product for product, _ in first} == set(CHECKS)
        assert {at for _, at in first} == {T.isoformat()}
        # Checks outrank optional products and arrive newest first throughout.
        checks = [at for product, at in source.acquisitions if product in CHECKS]
        assert checks == sorted(checks, reverse=True)
        assert source.acquisitions.index((PRECIP_RATE, T.isoformat())) == len(CHECKS) * len(scans)


class TestShutdown:
    def test_shutdown_joins_every_owned_worker(self, tmp_path):
        registry = make_registry(tmp_path)
        block = threading.Event()
        source = ScriptedSource(registry, reveal={REFLECTIVITY: [T]},
                                slow={REFLECTIVITY}, block_event=block)
        service = make_service(tmp_path, source, shutdown_timeout_seconds=30)
        service.start()
        service._tick(0.0)
        wait_for(lambda: source.acquisitions, "the slow acquisition never started")
        block.set()
        service.shutdown()
        assert not service._pools
        assert service._publisher is not None and not service._publisher.is_alive()
        assert [thread.name for thread in threading.enumerate()
                if thread.name.startswith("ingest-") and thread.is_alive()] == []

    def test_shutdown_publishes_a_final_poll_status(self, tmp_path):
        registry = make_registry(tmp_path)
        service = make_service(tmp_path, ScriptedSource(registry))
        service.start()
        service.shutdown()
        assert "polls" in poll_status(service)
        assert service.status()["phase"] == "stopping"


class TestIngestEntryPoint:
    @staticmethod
    def _launch(tmp_path, **env_overrides):
        root = Path(__file__).resolve().parents[3]
        env = dict(os.environ, EDGEWARN_CONFIG_DIR=str(root / "config"),
                   EDGEWARN_BASE_DIR=str(tmp_path / "runtime"), **env_overrides)
        return subprocess.run([sys.executable, str(root / "src" / "run_ingest.py")],
                              capture_output=True, text=True, env=env, cwd=str(root), timeout=300)

    def test_disabled_durable_handoff_is_rejected_before_any_runtime_mutation(self, tmp_path):
        completed = self._launch(tmp_path, EDGEWARN_HANDOFF_ENABLED="0")
        assert completed.returncode == 1
        assert "handoff.enabled=false is not supported" in completed.stdout
        assert not (tmp_path / "runtime").exists()

    def test_a_duplicate_producer_lock_is_rejected(self, tmp_path):
        holder = ServiceLock(tmp_path / "runtime", "ingest")
        holder.acquire()
        try:
            completed = self._launch(tmp_path)
        finally:
            holder.release()
        assert completed.returncode == 1
        assert "lock" in completed.stdout

    def test_service_lock_still_fails_fast_while_held(self, tmp_path):
        """Bounded waits are for the input mutex only: ownership stays fail-fast."""
        holder = ServiceLock(tmp_path, "ingest")
        holder.acquire()
        try:
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="single-instance lock"):
                ServiceLock(tmp_path, "ingest").acquire()
            assert time.monotonic() - started < 0.5
        finally:
            holder.release()

    def test_the_parser_owns_only_acquisition_and_shared_dependency_flags(self):
        from util import cli

        parser = cli.build_service_parser("ingest")
        for argv in (["--base_dir", "/tmp/a"], ["--base-dir", "/tmp/a"],
                     ["--config-dir", "/tmp/c"], ["--profile"], ["--no-profile"],
                     ["--disable-goes"], ["--mrms-core-only"], ["--no-mrms-core-only"],
                     ["--disable-ctam"], ["--no-disable-stormprob"]):
            assert parser.parse_args(argv) is not None
        for argv in (["--lat_limits", "1", "2"], ["--disable-metar"], ["--disable-ewmrs"],
                     ["--disable-nexrad"], ["--ctam-module-dir", "/tmp/m"],
                     ["--refl-threshold", "1.0"], ["--disable-wpc"], ["--disable-nws"]):
            with pytest.raises(SystemExit):
                parser.parse_args(argv)
