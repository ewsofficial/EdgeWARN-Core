"""Independently supervised realtime source acquisition.

This service owns every realtime MRMS, raw RAP, and scan-time GLM acquisition.
It is deliberately free of scientific analysis and rendering: its durable outbox
describes *source products*, and EWMRS owns the product-to-layer mapping. No
EWMRS or Core analysis module is imported here, so a Core crash or a long Core
cycle can never pause acquisition.

Scheduling contract
-------------------
Discovery runs on a monotonic deadline grid (``t0 + n * poll``) so a slow
listing cannot shift later ticks. Listing and download work never runs on the
timer path: the timer only submits bounded work to owned executors. Missed
deadlines coalesce into exactly one refresh instead of a burst of catch-up
ticks, and a per-product active-listing guard stops a slow request from
overlapping itself. The cadence is a scheduling guarantee, not a promise that a
stalled network finishes inside one period.

Durability contract
-------------------
Every acquisition completion -- whenever it finishes, including after the poll
that discovered it has already ended -- is routed through one publisher thread
that commits the input to the inventory, publishes its render notification, and
re-evaluates affected scans. Keeping every durable mutation on a single thread
means the shared input lease is never re-entered, so publication cannot
self-deadlock, and a completion is never delayed behind an unrelated tick.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time

from common.ingest.mrms.timestamp_utils import round_to_nearest_even_minute
from util.runtime.ingest_handoff import IngestRecordError, contained, now, utc

UTC = timezone.utc
SETTLED_STATES = ("committed", "abandoned")

#: Acquisition priority tiers, most urgent first. Check products decide whether
#: a scan can start at all, mandatory-integration products gate Core
#: integration, and everything else is optional enrichment or rendering.
TIER_CHECK = 0
TIER_MANDATORY = 1
TIER_OPTIONAL = 2


class AcquisitionBacklogFull(RuntimeError):
    """The bounded job window is saturated; callers must report backpressure."""


@dataclass(frozen=True)
class IngestResources:
    """Every runtime bound the service uses, resolved once from the catalogs.

    Nothing here is a service-side constant: each value comes from
    ``scheduler.yaml``, the ``ingest`` group of ``runtime.yaml`` (with its
    documented inheritance from the MRMS/RAP catalogs), or ``synoptic_rap.yaml``.
    Changing a bound is a configuration change and needs a process restart, like
    every other catalog value in this runtime.
    """

    poll_seconds: float
    lookback_hours: float
    listing_concurrency: int
    listing_timeout_seconds: float
    listing_page_size: int
    listing_max_pages: int
    listing_max_objects: int
    download_concurrency: int
    decode_concurrency: int
    download_timeout_seconds: float
    auxiliary_concurrency: int
    auxiliary_timeout_seconds: float
    pending_max_jobs: int
    retry_max_attempts: int
    retry_initial_seconds: float
    retry_max_seconds: float
    scan_deadline_seconds: float
    reconcile_interval_seconds: float
    retention_minutes: float
    disk_budget_mb: float
    shutdown_timeout_seconds: float
    rap_max_files: int
    check_reserved_slots: int = 1

    def __post_init__(self):
        if self.poll_seconds <= 0:
            raise ValueError("ingest poll cadence must be positive")
        if self.check_reserved_slots < 0:
            raise ValueError("reserved ingest capacity must not be negative")
        for name in ("listing_concurrency", "download_concurrency", "decode_concurrency",
                     "auxiliary_concurrency", "pending_max_jobs", "listing_page_size",
                     "listing_max_pages", "listing_max_objects", "retry_max_attempts",
                     "shutdown_timeout_seconds"):
            if getattr(self, name) < 1:
                raise ValueError(f"ingest resource {name} must be positive")

    @property
    def listing_window_minutes(self):
        """Effective listing lookback, never wider than raw-input retention.

        Listing past retention only downloads inputs that maintenance retires
        as soon as they render. Catalog validation rejects such a lookback; the
        clamp also protects resources built with explicit overrides.
        """
        return min(self.lookback_hours * 60, self.retention_minutes)

    @property
    def reserved_slots(self):
        """Slots always left for non-check products so neither class starves."""
        return min(self.check_reserved_slots, max(0, self.download_concurrency - 1))

    @classmethod
    def resolve(cls, *, config_dir=None):
        from common.config.loader import load_config
        from common.ingest.mrms.config import get_ingest_settings
        from common.ingest.synoptic.config import rap_max_files

        settings = get_ingest_settings(config_dir=config_dir)
        scheduler = load_config("scheduler", config_dir=config_dir)["scheduler"]
        return cls(
            poll_seconds=float(scheduler["ingest_poll_seconds"]),
            lookback_hours=float(scheduler["s3_lookback_hours"]),
            listing_concurrency=int(settings["listing_concurrency"]),
            listing_timeout_seconds=float(settings["listing_timeout_seconds"]),
            listing_page_size=int(settings["listing_page_size"]),
            listing_max_pages=int(settings["listing_max_pages"]),
            listing_max_objects=int(settings["listing_max_objects"]),
            download_concurrency=int(settings["download_concurrency"]),
            decode_concurrency=int(settings["decode_concurrency"]),
            download_timeout_seconds=float(settings["download_timeout_seconds"]),
            auxiliary_concurrency=int(settings["auxiliary_concurrency"]),
            auxiliary_timeout_seconds=float(settings["auxiliary_timeout_seconds"]),
            pending_max_jobs=int(settings["pending_max_jobs"]),
            retry_max_attempts=int(settings["retry_max_attempts"]),
            retry_initial_seconds=float(settings["retry_initial_seconds"]),
            retry_max_seconds=float(settings["retry_max_seconds"]),
            scan_deadline_seconds=float(settings["scan_deadline_seconds"]),
            reconcile_interval_seconds=float(settings["reconcile_interval_seconds"]),
            retention_minutes=float(settings["retention_minutes"]),
            disk_budget_mb=float(settings["disk_budget_mb"]),
            shutdown_timeout_seconds=float(settings["shutdown_timeout_seconds"]),
            rap_max_files=int(rap_max_files()),
        )

    def retry_delay(self, attempts):
        exponent = max(0, int(attempts) - 1)
        return min(self.retry_max_seconds, self.retry_initial_seconds * (2 ** exponent))

    def disk_budget_bytes(self):
        return int(self.disk_budget_mb * 1024 * 1024)

    def with_overrides(self, **values):
        from dataclasses import replace

        return replace(self, **values)


@dataclass
class AcquisitionJob:
    """One unit of realtime source work, keyed by a stable logical identity.

    ``identity`` is what the ledger dedupes on. For MRMS it is the discovered
    object's mirror-independent logical identity, so an S3 and an HTTPS
    observation of the same product minute collapse into one job. For RAP it is
    the analysis hour, and for GLM the validated scan minute. ``tier`` orders
    admission (see ``TIER_*``); ``protected`` marks check products for the
    reserved-slot rule.
    """

    identity: str
    kind: str
    product_id: str
    target: datetime
    protected: bool = False
    tier: int = TIER_OPTIONAL
    discovered: object | None = None
    attempts: int = 0
    retry_at: float = 0.0
    reason: str = ""


class AcquisitionLedger:
    """Bounded dedupe of queued, in-flight, and settled acquisition identities.

    Three properties matter:

    * a repeated discovery of the same identity never produces a second job;
    * a settled identity is remembered so later listings cannot re-download it
      within this process, while ``capacity`` still bounds that memory;
    * a *failed* identity is remembered separately and is never mistaken for a
      successful acquisition, so the successful cursor never advances past it.
    """

    def __init__(self, *, capacity, clock=None):
        if capacity < 1:
            raise ValueError("ledger capacity must be positive")
        self.capacity = capacity
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._queued: dict[str, AcquisitionJob] = {}
        self._settled: dict[str, tuple[str, str]] = {}
        self._in_flight: set[str] = set()
        self.rejected = 0

    def offer(self, job):
        """Admit one job, or report the duplicate/backlog condition.

        ``False`` means the identity is already queued, in flight, or already
        settled, which is a normal steady-state observation rather than an
        error. :class:`AcquisitionBacklogFull` is raised instead when the bounded
        window is saturated, so the caller can report backpressure explicitly.
        """
        with self._lock:
            if (job.identity in self._queued or job.identity in self._in_flight
                    or job.identity in self._settled):
                return False
            if len(self._queued) + len(self._in_flight) >= self.capacity:
                self.rejected += 1
                raise AcquisitionBacklogFull(job.identity)
            self._queued[job.identity] = job
            return True

    def due(self, at_monotonic):
        """Eligible jobs by tier, newest observation first within a tier.

        Newest-first means a cold-start backlog fetches the freshest complete
        scan before working back through older history, so realtime readiness
        never waits behind a window of stale downloads.
        """
        with self._lock:
            ready = [job for job in self._queued.values() if job.retry_at <= at_monotonic]
        ready.sort(key=lambda job: (job.tier, -job.target.timestamp(), job.identity))
        return tuple(ready)

    def waiting_checks(self, at_monotonic):
        return any(job.protected for job in self.due(at_monotonic))

    def take(self, identity):
        with self._lock:
            job = self._queued.pop(identity, None)
            if job is not None:
                self._in_flight.add(identity)
            return job

    def requeue(self, job, *, delay):
        with self._lock:
            self._in_flight.discard(job.identity)
            job.attempts += 1
            job.retry_at = self._clock() + delay
            self._queued.setdefault(job.identity, job)

    def settle(self, identity, state, reason=""):
        if state not in SETTLED_STATES:
            raise ValueError(f"unknown settlement state {state!r}")
        with self._lock:
            self._in_flight.discard(identity)
            self._settled[identity] = (state, reason)
            while len(self._settled) > self.capacity:
                del self._settled[next(iter(self._settled))]

    def state(self, identity):
        with self._lock:
            return self._settled.get(identity)

    def counts(self):
        with self._lock:
            return {
                "pending_jobs": len(self._queued),
                "in_flight_jobs": len(self._in_flight),
                "committed_objects": sum(1 for state, _ in self._settled.values()
                                         if state == "committed"),
                "failed_objects": sum(1 for state, _ in self._settled.values()
                                      if state != "committed"),
                "rejected_objects": self.rejected,
            }


@dataclass
class _Work:
    """One publisher-thread work item."""

    kind: str
    payload: dict = field(default_factory=dict)
    completed: object | None = None
    job: AcquisitionJob | None = None


class IngestService:
    """Poll, acquire, and publish realtime source inputs on a fixed cadence."""

    SERVICE_LABEL = "Ingest"

    def __init__(self, *, base_dir, run_id, registry, dependencies, resources=None,
                 io=None, stop_event=None, clock=None, wall_clock=None, wait=None,
                 lister=None, acquirer=None, auxiliary=None, startup_latest_only=True):
        from common.ingest.inventory import InputInventory

        self.base_dir = Path(base_dir).resolve()
        self.run_id = run_id
        self.registry = registry
        self.dependencies = dependencies
        self.resources = resources if resources is not None else IngestResources.resolve()
        self._io = io
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._clock = clock if clock is not None else time.monotonic
        self._wall = wall_clock if wall_clock is not None else now
        self._wait = wait if wait is not None else self._stop.wait
        self._lister = lister
        self._acquirer = acquirer
        self._auxiliary = auxiliary
        self.inventory = InputInventory(
            self.base_dir, fingerprint=dependencies.fingerprint, run_id=run_id)

        self.ledger = AcquisitionLedger(capacity=self.resources.pending_max_jobs,
                                         clock=self._clock)
        self._work: queue.Queue = queue.Queue()
        self._index_lock = threading.RLock()
        self._publisher: threading.Thread | None = None
        self._pools: dict[str, ThreadPoolExecutor] = {}
        self._decode_slots = threading.BoundedSemaphore(max(1, self.resources.decode_concurrency))
        self._s3_local = threading.local()
        self._listing_active: set[str] = set()
        # Startup selection: the first listing of every product is held back
        # until all have reported, then only the latest common scan is queued.
        self._startup_latest_only = bool(startup_latest_only)
        self._startup_listings: dict[str, tuple] = {}
        self._startup_floor: datetime | None = None
        self._startup_resolved = not self._startup_latest_only
        self._outstanding = 0
        self._outstanding_optional = 0
        self._committed_scans: dict[str, set[datetime]] = {}
        self._notified: set[str] = set()
        self._reasons: list[str] = []
        self._metrics = {"polls": 0, "discovered": 0, "committed": 0, "duplicates": 0,
                         "listing_overruns": 0, "coalesced_ticks": 0, "expired_scans": 0,
                         "retired_inputs": 0, "render_notifications": 0}
        self._last_reconcile: float | None = None
        self._last_poll_at: datetime | None = None
        self._phase = "starting"
        self._started = False

    # -- logging ---------------------------------------------------------

    def _info(self, message):
        if self._io is not None:
            self._io.write_info(f"[{self.SERVICE_LABEL}] {message}")
        else:
            print(f"[{self.SERVICE_LABEL}] {message}", flush=True)

    def _warn(self, message):
        if self._io is not None:
            self._io.write_warning(f"[{self.SERVICE_LABEL}] {message}")
        else:
            print(f"[{self.SERVICE_LABEL}] WARN: {message}", flush=True)

    def _debug(self, message):
        if self._io is not None:
            self._io.write_debug(f"[{self.SERVICE_LABEL}] {message}")

    def _add_reason(self, reason):
        with self._index_lock:
            if reason not in self._reasons and len(self._reasons) < 32:
                self._reasons.append(str(reason))

    # -- lifecycle -------------------------------------------------------

    def start(self):
        """Reconcile durable state, then take the first poll immediately."""
        if self._started:
            return
        self._started = True
        self._publisher = threading.Thread(
            target=self._publisher_loop, name="ingest-publisher", daemon=True)
        self._publisher.start()
        for name, size in (("ingest-listing", self.resources.listing_concurrency),
                           ("ingest-download", self.resources.download_concurrency),
                           ("ingest-auxiliary", self.resources.auxiliary_concurrency)):
            self._pools[name] = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix=name)
        self._work.put(_Work(kind="startup"))
        self._phase = "polling"

    def run(self):
        """Dispatch discovery on the monotonic deadline grid until stopped."""
        self.start()
        origin = self._clock()
        index = 0
        try:
            while not self._stop.is_set():
                current = self._clock()
                due = origin + index * self.resources.poll_seconds
                if current < due:
                    self._wait(min(due - current, self._idle_quantum()))
                    continue
                behind = int((current - origin) // self.resources.poll_seconds) - index
                if behind > 0:
                    # Missed deadlines coalesce into one refresh; the schedule
                    # never replays a burst of catch-up ticks.
                    self._metrics["coalesced_ticks"] += behind
                    self._debug(f"coalesced {behind} missed poll deadline(s)")
                index = int((current - origin) // self.resources.poll_seconds) + 1
                self._tick(current)
        except KeyboardInterrupt:
            self._info("CTRL+C detected, stopping ingest service...")
        finally:
            self.shutdown()

    def _idle_quantum(self):
        return max(0.01, min(self.resources.poll_seconds, self.resources.reconcile_interval_seconds))

    def shutdown(self):
        """Stop dispatching, then join every owned worker within its bound."""
        if not self._started:
            return
        self._started = False
        self._phase = "stopping"
        self._work.put(_Work(kind="poll-status"))
        self._work.put(_Work(kind="shutdown"))
        bound = self.resources.shutdown_timeout_seconds
        if self._publisher is not None:
            self._publisher.join(timeout=bound)
            if self._publisher.is_alive():
                self._warn(f"publisher thread did not stop within {bound}s")
        joined = []
        for name, pool in self._pools.items():
            stopper = threading.Thread(
                target=pool.shutdown, kwargs={"wait": True, "cancel_futures": True},
                name=f"{name}-stop", daemon=True)
            stopper.start()
            stopper.join(timeout=bound)
            if stopper.is_alive():
                self._warn(f"{name} workers did not stop within {bound}s")
            else:
                joined.append(name)
        self._pools.clear()
        self._info(f"joined owned workers: {', '.join(sorted(joined)) or 'none'}")

    # -- timer path ------------------------------------------------------

    def _tick(self, monotonic_now):
        at = self._wall()
        self._metrics["polls"] += 1
        self._last_poll_at = at
        self._dispatch_listings(monotonic_now, at)
        self._dispatch_acquisitions(monotonic_now)
        self._dispatch_auxiliary(at, monotonic_now)
        if (self._last_reconcile is None or
                monotonic_now - self._last_reconcile >= self.resources.reconcile_interval_seconds):
            self._last_reconcile = monotonic_now
            self._work.put(_Work(kind="maintenance"))
        self._work.put(_Work(kind="poll-status"))
        self._phase = "acquiring" if self._outstanding else "polling"

    def _dispatch_listings(self, monotonic_now, at):
        start, end = self._observation_window(at)
        for spec in self.registry.products:
            if spec.product_id in self._listing_active:
                # One active listing per product: a slow request never overlaps
                # itself, and the product is retried on the next due refresh.
                continue
            self._listing_active.add(spec.product_id)
            self._pools["ingest-listing"].submit(self._list_product, spec, start, end)

    def _observation_window(self, at):
        """Sliding observation lookback, bounded by the retention horizon.

        It is not widened for older pending scans: a scan older than retention
        cannot keep its inputs, so listing for it is wasted work.
        """
        return at - timedelta(minutes=self.resources.listing_window_minutes), at

    def _list_product(self, spec, start, end):
        dispatched = time.monotonic()
        try:
            found = self._run_listing(spec, start, end)
        except Exception as exc:
            self._listing_active.discard(spec.product_id)
            self._warn(f"listing {spec.product_id} failed: {type(exc).__name__}: {exc}")
            self._add_reason(f"listing {spec.product_id} failed: {type(exc).__name__}: {exc}")
            self._apply_startup_selection(spec, ())
            return
        self._listing_active.discard(spec.product_id)
        resolving = not self._startup_resolved
        found = self._apply_startup_selection(spec, found)
        if found and time.monotonic() - dispatched > self.resources.listing_timeout_seconds:
            self._metrics["listing_overruns"] += 1
            self._warn(
                f"listing {spec.product_id} overran the "
                f"{self.resources.listing_timeout_seconds:g}s deadline; the next refresh "
                "waits for it to finish")
        offered = 0
        for obj in found:
            identity = "mrms:" + "|".join(str(part) for part in obj.logical_identity)
            job = AcquisitionJob(
                identity=identity, kind="mrms", product_id=obj.product_id,
                target=obj.observation_time, protected=spec.discovery,
                tier=self._tier(spec), discovered=obj)
            try:
                if self.ledger.offer(job):
                    offered += 1
                else:
                    self._metrics["duplicates"] += 1
            except AcquisitionBacklogFull:
                self._add_reason(f"acquisition backlog full; deferred {identity}")
                break
        self._metrics["discovered"] += len(found)
        if resolving and self._startup_resolved and self._startup_floor is not None:
            # The startup selection just released its jobs: start downloading now
            # instead of waiting up to a full poll period for the next tick.
            self.dispatch_pending(self._clock())
        self._debug(
            f"listed {spec.product_id}: {len(found)} object(s) in window, {offered} newly "
            f"queued, {len(found) - offered} already known")

    def _apply_startup_selection(self, spec, found):
        """Restrict the cold-start backlog to the latest common scan.

        Until every product has completed its first listing, results are held.
        The scan chosen is the newest even-minute scan present in every product
        that listed objects (falling back to the newest scan with the widest
        product coverage). That scan becomes a floor: this and every later poll
        only queue objects at or after it, so older history is never fetched.
        Returns the objects the caller may offer to the ledger now.
        """
        if self._startup_resolved:
            floor = self._startup_floor
            if floor is None:
                return found
            return tuple(o for o in found
                         if round_to_nearest_even_minute(o.observation_time) >= floor)
        with self._index_lock:
            if self._startup_resolved:
                held = ()
            else:
                self._startup_listings[spec.product_id] = tuple(found)
                if len(self._startup_listings) < len(self.registry.products):
                    return ()
                held = self._resolve_startup_locked()
        floor = self._startup_floor
        if floor is None:
            return found
        # Offer every held product's objects; this call's own product is among them.
        for product_id, objects in held:
            if product_id == spec.product_id:
                continue
            self._offer_found(product_id, objects)
        return next((objs for pid, objs in held if pid == spec.product_id), ())

    def _resolve_startup_locked(self):
        scans: dict[str, set] = {}
        for product_id, objects in self._startup_listings.items():
            if objects:
                scans[product_id] = {round_to_nearest_even_minute(o.observation_time)
                                     for o in objects}
        self._startup_resolved = True
        if not scans:
            return ()
        common = set.intersection(*scans.values())
        if common:
            floor = max(common)
        else:
            coverage: dict = {}
            for values in scans.values():
                for scan in values:
                    coverage[scan] = coverage.get(scan, 0) + 1
            floor = max(coverage, key=lambda scan: (coverage[scan], scan))
            self._warn("no common scan across products at startup; using "
                       f"{floor.isoformat()} with widest coverage")
        self._startup_floor = floor
        self._info(f"startup: downloading only latest common scan {floor.isoformat()}")
        return tuple(
            (pid, tuple(o for o in objs
                        if round_to_nearest_even_minute(o.observation_time) == floor))
            for pid, objs in self._startup_listings.items())

    def _offer_found(self, product_id, objects):
        spec = next((s for s in self.registry.products if s.product_id == product_id), None)
        if spec is None:
            return
        for obj in objects:
            identity = "mrms:" + "|".join(str(part) for part in obj.logical_identity)
            job = AcquisitionJob(
                identity=identity, kind="mrms", product_id=obj.product_id,
                target=obj.observation_time, protected=spec.discovery,
                tier=self._tier(spec), discovered=obj)
            try:
                if not self.ledger.offer(job):
                    self._metrics["duplicates"] += 1
            except AcquisitionBacklogFull:
                self._add_reason(f"acquisition backlog full; deferred {identity}")
                break
        self._metrics["discovered"] += len(objects)

    def _tier(self, spec):
        if spec.discovery:
            return TIER_CHECK
        if spec.product_id in self.dependencies.mandatory_integration:
            return TIER_MANDATORY
        return TIER_OPTIONAL

    def _run_listing(self, spec, start, end):
        if self._lister is not None:
            return tuple(self._lister(spec, start, end))
        from common.ingest.mrms.discovery import discover_objects_sync

        return discover_objects_sync(
            spec, start, end, s3=self._s3_client(), io=self._io,
            max_objects=self.resources.listing_max_objects,
            max_pages=self.resources.listing_max_pages,
            page_size=self.resources.listing_page_size,
            timeout_seconds=self.resources.listing_timeout_seconds)

    def _s3_client(self):
        cached = getattr(self._s3_local, "client", "unset")
        if cached != "unset":
            return cached
        client = None
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.client import Config
            from common.ingest.aws_async_compat import ensure_aiobotocore_endpoint_compat

            ensure_aiobotocore_endpoint_compat()
            timeout = self.resources.listing_timeout_seconds
            client = boto3.client("s3", config=Config(
                signature_version=UNSIGNED, connect_timeout=timeout, read_timeout=timeout,
                retries={"max_attempts": 1},
                max_pool_connections=self.resources.listing_concurrency))
        except Exception as exc:
            self._warn(f"MRMS S3 listing client unavailable; using HTTPS: {exc}")
            client = None
        self._s3_local.client = client
        return client

    # -- acquisition dispatch -------------------------------------------

    def _dispatch_acquisitions(self, monotonic_now):
        self.dispatch_pending(monotonic_now)

    def dispatch_pending(self, monotonic_now):
        """Admit every due acquisition job into the bounded download window.

        The timer path calls this once per poll; exposing it keeps the same
        admission rule reachable without a real clock, which is what the
        fixed-clock service tests drive.
        """
        pool = self._pools.get("ingest-download")
        if pool is None:
            return
        capacity = self.resources.download_concurrency
        # Auxiliary RAP/GLM jobs share the ledger for dedupe and settlement
        # only; they are dispatched on their own pool by _dispatch_auxiliary.
        due = tuple(job for job in self.ledger.due(monotonic_now) if job.kind == "mrms")
        if not due:
            return
        waiting_other = any(not job.protected for job in due)
        admitted_checks = 0
        for job in due:
            if self._outstanding >= capacity:
                break
            if job.protected:
                # Check inputs are prioritized, but never occupy every slot:
                # other enabled products keep a reserved share of the window.
                if waiting_other and admitted_checks >= capacity - self.resources.reserved_slots:
                    continue
                admitted_checks += 1
            if self.ledger.take(job.identity) is None:
                continue
            self._outstanding += 1
            if not job.protected:
                self._outstanding_optional += 1
            pool.submit(self._acquire_mrms, job)

    def _acquire_mrms(self, job):
        completed = None
        try:
            completed = self._run_acquisition(job)
        except Exception as exc:
            self._fail(job, f"{type(exc).__name__}: {exc}")
        else:
            # Only a usable, validated file advances the successful cursor. The
            # durable commit and its render notification are a separate,
            # recoverable step on the publisher thread.
            self.ledger.settle(job.identity, "committed")
        finally:
            self._outstanding -= 1
            if not job.protected:
                self._outstanding_optional -= 1
        if completed is not None:
            self._work.put(_Work(kind="completion", completed=completed, job=job))

    def _run_acquisition(self, job):
        if self._acquirer is not None:
            return self._acquirer(job.discovered)
        from common.ingest.mrms.acquisition import acquire_object_sync

        return acquire_object_sync(self.registry, job.discovered, self._io)

    def _fail(self, job, reason):
        job.reason = reason
        attempts = job.attempts + 1
        if attempts >= self.resources.retry_max_attempts:
            self.ledger.settle(job.identity, "abandoned", reason)
            self._add_reason(f"abandoned {job.identity}: {reason}")
            self._warn(f"abandoning {job.identity} after {attempts} attempt(s): {reason}")
            return
        delay = self.resources.retry_delay(attempts)
        self.ledger.requeue(job, delay=delay)
        self._add_reason(f"{job.identity} failed ({attempts}/{self.resources.retry_max_attempts}): {reason}")
        self._warn(f"{job.identity} attempt {attempts} failed, retrying in {delay:.1f}s: {reason}")

    # -- auxiliary sources ----------------------------------------------

    def _dispatch_auxiliary(self, at, monotonic_now=None):
        """Submit due auxiliary retries, then newly offered RAP/GLM targets.

        Every submission first takes the job out of the ledger's queue, so a
        job is in flight exactly once and the MRMS dispatcher never sees it.
        """
        monotonic_now = self._clock() if monotonic_now is None else monotonic_now
        for job in self.ledger.due(monotonic_now):
            if job.kind != "mrms":
                self._submit_auxiliary(job)
        for kind, target in self._auxiliary_targets(at):
            identity = f"{kind}:{target.isoformat()}"
            job = AcquisitionJob(identity=identity, kind=kind,
                                 product_id=kind.upper(), target=target)
            try:
                if self.ledger.offer(job):
                    self._submit_auxiliary(job)
                else:
                    self._metrics["duplicates"] += 1
            except AcquisitionBacklogFull:
                self._add_reason(f"auxiliary backlog full; deferred {identity}")

    def _submit_auxiliary(self, job):
        if self.ledger.take(job.identity) is None:
            return
        self._pools["ingest-auxiliary"].submit(self._acquire_auxiliary, job)

    def _auxiliary_targets(self, at):
        """Candidate RAP analysis hours and GLM scan times, in that order.

        Neither auxiliary source is scheduled from a Core cursor, and neither
        one can delay a discovery tick or an MRMS render event: they run on a
        separate bounded pool.
        """
        targets = []
        if self.dependencies.rap_enabled:
            analysis = at.replace(minute=0, second=0, microsecond=0)
            if 0 <= (at - analysis).total_seconds() / 60 <= self._rap_max_age_minutes():
                targets.append(("rap", analysis))
        if self.dependencies.glm_enabled:
            for scan in sorted(self._tracked_scans() | self._ready_scans()):
                targets.append(("glm", scan))
        return targets

    def _rap_max_age_minutes(self):
        settings = json.loads(self.dependencies.auxiliary_settings_json)
        minutes = settings.get("rap", {}).get("max_age_minutes")
        if minutes is None:
            raise IngestRecordError("Frozen dependencies must carry rap.max_age_minutes")
        return float(minutes)

    def _acquire_auxiliary(self, job):
        completed = ()
        try:
            with self._decode_slots:
                completed = tuple(self._run_auxiliary(job))
        except Exception as exc:
            self._fail(job, f"{type(exc).__name__}: {exc}")
            return
        # One analysis hour or scan minute resolves to a single acquisition
        # identity, so reusing it for another scan publishes no second event.
        self.ledger.settle(job.identity, "committed")
        for item in completed:
            self._work.put(_Work(kind="completion", completed=item, job=job))

    def _run_auxiliary(self, job):
        if self._auxiliary is not None:
            return tuple(self._auxiliary(job.kind, job.target))
        if job.kind == "rap":
            import asyncio
            from common.ingest.synoptic.main import acquire_rap_input

            return (asyncio.run(acquire_rap_input(job.target)),)
        from util.runtime.goes import acquire_glm_inputs_for_scan

        return acquire_glm_inputs_for_scan(job.target)

    # -- publisher thread ------------------------------------------------

    def _publisher_loop(self):
        """Drain queued work in batches, evaluating each affected scan once.

        A backfill commits many files for the same scan in quick succession.
        Evaluating the scan after every one re-reads the whole inventory under
        the shared input lock, so completions only collect their scans and the
        union is evaluated once before any other kind of work (and at the end
        of each drained batch), preserving ordering against maintenance.
        """
        while True:
            batch = [self._work.get()]
            while True:
                try:
                    batch.append(self._work.get_nowait())
                except queue.Empty:
                    break
            pending_scans: set[datetime] = set()
            stop = False
            try:
                for item in batch:
                    if stop:
                        continue
                    if item.kind != "completion":
                        self._flush_evaluations(pending_scans)
                    if item.kind == "shutdown":
                        stop = True
                        continue
                    try:
                        scans = self._apply(item)
                        if scans:
                            pending_scans.update(scans)
                    except Exception as exc:
                        self._warn(f"publisher {item.kind} failed: {type(exc).__name__}: {exc}")
                        self._add_reason(f"publisher {item.kind} failed: {type(exc).__name__}: {exc}")
                self._flush_evaluations(pending_scans)
            finally:
                # Acknowledge only after the batch's evaluations ran, so an
                # observer of ``unfinished_tasks`` sees fully processed work.
                for _ in batch:
                    self._work.task_done()
            if stop:
                return

    def _flush_evaluations(self, scans):
        if not scans:
            return
        pending = set(scans)
        scans.clear()
        try:
            self._evaluate(pending)
        except Exception as exc:
            self._warn(f"publisher evaluation failed: {type(exc).__name__}: {exc}")
            self._add_reason(f"publisher evaluation failed: {type(exc).__name__}: {exc}")

    def _apply(self, item):
        if item.kind == "completion":
            return self._publish_completion(item)
        elif item.kind == "startup":
            self._reconcile()
        elif item.kind == "maintenance":
            self._maintain()
        elif item.kind == "poll-status":
            self._publish_poll_status()

    # -- durable publication --------------------------------------------

    def _publish_completion(self, item):
        completed = item.completed
        record = self.inventory.commit_input(completed)
        if record.key not in self._notified:
            self.inventory.handoff.publish_render_ready(record.key)
            self._notified.add(record.key)
            self._metrics["render_notifications"] += 1
        self._metrics["committed"] += 1
        self._index_committed(record.key, completed.record)
        self._info(
            f"committed {completed.record.family}:{completed.record.product} for "
            f"{completed.record.analysis_time.isoformat()} as input {record.key[:12]}"
            f"{' (reused)' if completed.reused else ''}")
        # The publisher loop evaluates the union of a batch's scans once.
        return self._scans_for(completed.record)

    def _index_committed(self, key, staged):
        with self._index_lock:
            if staged.family == "mrms":
                scan = round_to_nearest_even_minute(staged.analysis_time)
                self._committed_scans.setdefault(staged.product, set()).add(scan)

    def _scans_for(self, staged):
        if staged.family == "mrms":
            return {round_to_nearest_even_minute(staged.analysis_time)}
        if staged.family == "goes":
            return self._scans_around(staged.analysis_time, self._goes_tolerance_seconds())
        return set(self._tracked_scans() | self._ready_scans())

    def _scans_around(self, at, tolerance_seconds):
        anchor = round_to_nearest_even_minute(at)
        span = int(tolerance_seconds // 60) + 1
        return {
            candidate
            for candidate in (anchor + timedelta(minutes=2 * offset)
                              for offset in range(-span, span + 1))
            if abs((candidate - at).total_seconds()) <= tolerance_seconds
        }

    def _goes_tolerance_seconds(self):
        from common.ingest.manifest import CycleInputManifest

        return CycleInputManifest(cycle_time=datetime(2000, 1, 1, tzinfo=UTC)).goes_tolerance_seconds

    def _evaluate(self, scans):
        for scan in sorted(scan for scan in scans if scan is not None):
            self._evaluate_scan(scan)

    def _evaluate_scan(self, scan):
        from util.runtime.handoff import canonical_cycle_id

        key = canonical_cycle_id(scan)
        if self.inventory.handoff.read("terminal", key) is not None:
            return None
        try:
            evaluation = self.inventory.publish_scan(
                scan, self.dependencies, at=self._wall())
        except IngestRecordError as exc:
            self._warn(f"scan {scan.isoformat()} readiness evaluation failed: {exc}")
            self._add_reason(f"scan {scan.isoformat()}: {exc}")
            return None
        missing = evaluation.missing_check or evaluation.missing_integration
        if missing:
            self._debug(f"scan {scan.isoformat()} still waiting for {list(missing)}")
        return evaluation

    # -- maintenance -----------------------------------------------------

    def _reconcile(self):
        outcome = self.inventory.reconcile()
        if any(outcome.values()):
            self._info(
                f"reconciled durable state: adopted={len(outcome['adopted'])} "
                f"republished={len(outcome['published'])} pending={len(outcome['pending'])}")
        with self._index_lock:
            self._notified = {record.key
                              for record in self.inventory.handoff.records("render-ready")}
            for record in self.inventory.handoff.records("input"):
                staged = record._input
                if staged is not None and staged.family == "mrms":
                    self._committed_scans.setdefault(staged.product, set()).add(
                        round_to_nearest_even_minute(staged.analysis_time))
        return outcome

    def _maintain(self):
        at = self._wall()
        self._reconcile()
        self._retire(at)
        self._prune_rap()
        self._collect_staging(at)
        pressure = self._disk_pressure()
        if pressure:
            # Report pressure before releasing anything, then age the window.
            self._add_reason(pressure)
            self._warn(pressure)
            self._retire(at, aggressive=True)
        for scan in sorted(self._tracked_scans()):
            self._expire_scan(scan, at)

    def _retire(self, at, *, aggressive=False):
        minutes = self.resources.retention_minutes / 2 if aggressive else self.resources.retention_minutes
        removed = self.inventory.cleanup(
            before=at - timedelta(minutes=minutes),
            protected_products=self.dependencies.previous_detection)
        if removed:
            self._metrics["retired_inputs"] += len(removed)
            self._debug(f"retired {len(removed)} unreferenced input(s) older than {minutes:.0f} min")
        return removed

    def _disk_pressure(self):
        budget = self.resources.disk_budget_bytes()
        used = 0
        for record in self.inventory.handoff.records("input"):
            try:
                used += os.path.getsize(contained(self.base_dir, record.data["input"]["path"]))
            except OSError:
                continue
        if used <= budget:
            return ""
        return (f"raw inventory disk pressure: {used / (1024 * 1024):.0f} MiB used of a "
                f"{self.resources.disk_budget_mb:.0f} MiB budget")

    def _prune_rap(self):
        """Apply the RAP analysis cap, yielding to every active reference."""
        from common.ingest.synoptic.main import parse_rap_analysis_time

        if not self.dependencies.rap_enabled:
            return ()
        try:
            import util.file as fs

            directory = Path(fs.RAP_DIR)
        except (AttributeError, TypeError):
            return ()
        if not directory.resolve().is_relative_to(self.base_dir) or not directory.is_dir():
            return ()
        candidates = []
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() == ".idx":
                continue
            analysis = parse_rap_analysis_time(path)
            if analysis is not None:
                candidates.append((analysis, path))
        candidates.sort(key=lambda item: item[0], reverse=True)
        protected = self.inventory.referenced_inputs()
        removed = []
        for rank, (_, path) in enumerate(candidates):
            if rank < self.resources.rap_max_files or str(path) in protected:
                continue
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                self._warn(f"could not retire RAP analysis {path.name}: {exc}")
        if removed:
            self._debug(f"retired {len(removed)} unreferenced RAP analysis file(s)")
        return tuple(removed)

    def _collect_staging(self, at):
        """Garbage-collect owned staging directories left behind by a crash."""
        cutoff = at.timestamp() - max(self.resources.retention_minutes, 1) * 60
        collected = 0
        for root in (self.base_dir / "state" / "mrms" / "staging",
                     self.base_dir / "state" / "ingest-staging"):
            if not root.resolve().is_relative_to(self.base_dir) or not root.is_dir():
                continue
            for entry in root.iterdir():
                try:
                    if entry.is_dir() and entry.stat().st_mtime <= cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                        collected += 1
                except OSError:
                    continue
        if collected:
            self._debug(f"collected {collected} stale staging directory(ies)")

    def _expire_scan(self, scan, at):
        handoff = self.inventory.handoff
        key = _cycle_key(scan)
        if handoff.read("terminal", key) is not None:
            return
        evaluation = self._evaluate_scan(scan)
        if evaluation is None:
            return
        if evaluation.start is not None and evaluation.integration is not None \
                and evaluation.final is not None:
            return
        state = handoff.read("scan-state", key)
        if state is None:
            return
        deadline = utc(state.data["first_seen_at"]) + timedelta(
            seconds=self.resources.scan_deadline_seconds)
        if at < deadline:
            return
        missing = evaluation.missing_check or evaluation.missing_integration
        reason = (f"incomplete scan expired after {self.resources.scan_deadline_seconds:g}s; "
                  f"missing {list(missing) or ['optional inputs']}")
        handoff.disposition("terminal", key, status="expired", reason=reason)
        self._metrics["expired_scans"] += 1
        self._warn(f"scan {scan.isoformat()} abandoned: {reason}")

    # -- status ----------------------------------------------------------

    def _publish_poll_status(self):
        counts = dict(self.ledger.counts())
        counts.update({
            "polls": self._metrics["polls"],
            "discovered_objects": self._metrics["discovered"],
            "committed_inputs": self._metrics["committed"],
            "render_notifications": self._metrics["render_notifications"],
            "duplicate_discoveries": self._metrics["duplicates"],
            "listing_overruns": self._metrics["listing_overruns"],
            "coalesced_ticks": self._metrics["coalesced_ticks"],
            "expired_scans": self._metrics["expired_scans"],
            "retired_inputs": self._metrics["retired_inputs"],
            "tracked_scans": len(self._tracked_scans()),
        })
        try:
            self.inventory.handoff.publish_poll_status(counts, list(self._reasons))
        except Exception as exc:
            self._warn(f"poll status publication failed: {type(exc).__name__}: {exc}")
        with self._index_lock:
            self._reasons.clear()

    # -- derived views ---------------------------------------------------

    def _ready_scans(self):
        """Scans whose complete effective check set is locally valid."""
        required = set(self.dependencies.check)
        if not required:
            return set()
        with self._index_lock:
            per_product = [self._committed_scans.get(product, set()) for product in required]
        if any(not entry for entry in per_product):
            return set()
        return set.intersection(*per_product)

    def _tracked_scans(self):
        from util.runtime.handoff import parse_cycle_id

        scans = set(self._ready_scans())
        for record in self.inventory.handoff.records("scan-state"):
            try:
                scans.add(parse_cycle_id(record.key))
            except ValueError:
                continue
        return scans

    def status(self):
        with self._index_lock:
            reasons = list(self._reasons)
        return {
            "phase": self._phase,
            "polls": self._metrics["polls"],
            "committed": self._metrics["committed"],
            "notifications": self._metrics["render_notifications"],
            "duplicates": self._metrics["duplicates"],
            "last_poll_at": self._last_poll_at,
            "reasons": reasons,
            **self.ledger.counts(),
        }


def _cycle_key(scan):
    from util.runtime.handoff import canonical_cycle_id

    return canonical_cycle_id(scan)


__all__ = [
    "AcquisitionBacklogFull",
    "AcquisitionJob",
    "AcquisitionLedger",
    "IngestResources",
    "IngestService",
]
