"""EWMRS per-input render consumer.

EWMRS renders each newly committed source input independently. There is no cycle
checkpoint and no aggregate MRMS readiness predicate any more: the ingest
service publishes one immutable ``render-ready`` record per committed input
identity, and this consumer turns each one into work keyed by
``(input-id, layer-id, render-config-fingerprint)``.

Correctness rules (plans/independent-ingest-incremental-rendering-plan.md):

- A notification is durable and independent of Core. A stopped, retrying, or
  never-started Core service never pauses rendering, and a render never waits
  for a poll to finish.
- One input maps to zero or more enabled layers. A product with no configured
  layer mapping is acknowledged explicitly rather than silently retried.
- A layer is acknowledged only after its output *and* its product index are
  published, so a restart after publication reuses the complete output instead
  of rendering twice.
- Layers are independent: one failure retries with bounded backoff while every
  unrelated layer advances, and a terminal failure is recorded with an explicit
  reason rather than dropped.
- An older completion can never move a product's latest timestamp backward.

This module runs only inside the EWMRS service process tree; importing it is
allowed to load the EWMRS render stack lazily inside its methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from util.io import QueueWriter
from util.runtime.config import section
from util.runtime.ingest_handoff import (
    IngestHandoff, IngestRecordError, TERMINAL_STATUSES, render_job_id, utc,
)
from util.runtime.logging import queue_log
from util.runtime.timing import sleep_for

CONSUMER_NAME = "ewmrs-inputs-v1"
INPUT_LAYER = "__input__"


@dataclass(frozen=True)
class RenderJobSettings:
    """Per-input/layer job bounds, resolved from ``ewmrs_pipeline.input_jobs``."""

    pending_max_jobs: int
    max_age_minutes: float
    retry_max_attempts: int
    retry_initial_seconds: float
    retry_max_seconds: float

    @classmethod
    def resolve(cls, config_dir=None):
        from common.config.loader import load_config
        import util.file as fs

        values = load_config(
            "ewmrs_pipeline", config_dir=config_dir or fs.MRMS_CONFIG_DIR)["input_jobs"]
        return cls(
            pending_max_jobs=int(values["pending_max_jobs"]),
            max_age_minutes=float(values["max_age_minutes"]),
            retry_max_attempts=int(values["retry_max_attempts"]),
            retry_initial_seconds=float(values["retry_initial_seconds"]),
            retry_max_seconds=float(values["retry_max_seconds"]),
        )

    def retry_delay(self, attempts):
        exponent = max(0, int(attempts) - 1)
        return min(self.retry_max_seconds, self.retry_initial_seconds * (2 ** exponent))


def render_configuration_fingerprint():
    """Identity of the effective render configuration for one process.

    Acknowledgments are only reusable while the layer set, output locations,
    colormaps, and RAP conversion identity are unchanged, so this fingerprint
    participates in every render job key.
    """
    from EWMRS.rap.config import get_rap_uint16_layers
    from EWMRS.render.config import chunk_format_descriptor, get_mrms_file_list

    payload = {
        "mrms": sorted(
            (str(layer["name"]), str(layer.get("colormap_key")),
             str(layer.get("product")), str(layer.get("outdir")))
            for layer in get_mrms_file_list()),
        "rap": sorted(
            (str(layer["name"]), str(layer.get("outdir")),
             json.dumps(layer.get("scale"), sort_keys=True),
             tuple(sorted(str(name) for name in (
                 layer.get("short_names") if isinstance(layer.get("short_names"), (list, tuple))
                 else [layer.get("short_names")]))))
            for layer in get_rap_uint16_layers()),
        "chunk": chunk_format_descriptor(include_media_type=True),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()


class RenderBacklogFull(RuntimeError):
    """The bounded per-layer job window is saturated."""


class InputRenderConsumer:
    """Turn each committed input into independent, acknowledged layer work."""

    def __init__(self, base_dir, *, run_id, dependencies, log=None, settings=None,
                 pool=None, clock=None, jobs=None):
        self.base_dir = base_dir
        self.dependencies = dependencies
        self._log = log if log is not None else print
        self.settings = settings if settings is not None else RenderJobSettings.resolve()
        self._clock = clock if clock is not None else time.monotonic
        self.handoff = IngestHandoff(base_dir, fingerprint=dependencies.fingerprint,
                                     run_id=run_id)
        self._pool = pool
        self._owns_pool = pool is None
        self._fingerprint = None
        # A long render must never delay accepting the next notification, so
        # admission returns immediately and results are collected on a later
        # pass. The process pool still bounds actual raster concurrency.
        self._jobs = jobs if jobs is not None else ThreadPoolExecutor(
            max_workers=max(1, self._job_workers()),
            thread_name_prefix="ewmrs-input-job")
        self._owns_jobs = jobs is None
        self._inflight: dict[str, tuple[str, str, object, object]] = {}
        self.metrics = {"polled": 0, "dispatched": 0, "succeeded": 0, "failed": 0,
                        "expired": 0, "no_mapping": 0, "reused": 0, "rejected": 0,
                        "completed": 0}

    # -- producer agreement --------------------------------------------

    def require_producer(self, registry, *, now=None):
        """Refuse to render against a producer this process does not agree with.

        A disagreement fails visibly rather than rendering from a different
        effective product or dependency set. Core's state is deliberately not
        consulted: EWMRS rendering continues while Core is stopped.
        """
        from util.runtime.mrms_registry import MrmsProducerUnavailable, require_ingest_agreement

        try:
            return require_ingest_agreement(registry, self.dependencies.fingerprint, now=now)
        except MrmsProducerUnavailable as exc:
            self._log(f"[EWMRS] {exc}")
            return None

    @property
    def fingerprint(self):
        if self._fingerprint is None:
            self._fingerprint = render_configuration_fingerprint()
        return self._fingerprint

    # -- layer resolution ----------------------------------------------

    def layers_for(self, record):
        """Enabled layer names for one committed input, in stable order."""
        staged = record._input
        if staged is None:
            return ()
        if staged.family == "mrms":
            from EWMRS.render.config import get_mrms_file_list

            return tuple(sorted(
                str(layer["name"]) for layer in get_mrms_file_list()
                if layer.get("product") == staged.product and layer.get("outdir")))
        if staged.family == "rap":
            from EWMRS.rap.config import get_rap_uint16_layers

            return tuple(sorted(str(layer["name"]) for layer in get_rap_uint16_layers()))
        # Scan-time GLM is a Core integration input, not an ABI channel.
        # It has no render mapping and receives an explicit acknowledgment.
        return ()

    def layer_definition(self, name, record):
        """Rebuild one configured layer bound to this input's exact path."""
        staged = record._input
        if staged.family == "mrms":
            from EWMRS.render.config import get_mrms_file_list
            from EWMRS.pipeline import pinned_layer

            for layer in get_mrms_file_list():
                if str(layer["name"]) == name and layer.get("product") == staged.product:
                    return pinned_layer(layer, staged.path)
            return None
        if staged.family == "rap":
            from EWMRS.rap.config import get_rap_uint16_layers
            from EWMRS.rap.uint16_pipeline import rap_timestamp_label

            for layer in get_rap_uint16_layers():
                if str(layer["name"]) == name:
                    return {**layer, "input_path": staged.path,
                            "source_type": "rap_uint16",
                            "render_timestamp": rap_timestamp_label(staged.path)}
            return None
        return None

    # -- main pass ------------------------------------------------------

    def poll_once(self, registry=None, *, now=None):
        """Drain notifications once; returns a small metrics mapping.

        Every pass first collects finished renders, then plans and admits new
        work. Nothing here waits on a running render, so an input that arrives
        during one is picked up on the next pass rather than queued behind it.
        """
        at = utc(now) if now is not None else datetime.now(timezone.utc)
        self.metrics["polled"] += 1
        if registry is not None and self.require_producer(registry) is None:
            return dict(self.metrics)
        self._collect_finished(at)
        notifications = self.handoff.records("render-ready")
        plans = []
        for notification in notifications:
            plan = self._plan(notification)
            if plan is not None:
                plans.append((notification, plan))
        for notification, plan in plans:
            self._admit(notification, plan, at)
        self._expire(at)
        return dict(self.metrics)

    def _plan(self, notification):
        input_id = notification.key
        if notification._input is None:
            self._log(f"[EWMRS] Ignoring unusable render notification {input_id[:12]}")
            return None
        plan_key = render_job_id(input_id, "__plan__", self.fingerprint)
        plan = self.handoff.read("render-plan", plan_key)
        if plan is None:
            try:
                plan = self.handoff.plan_render(input_id, self.layers_for(notification),
                                                self.fingerprint)
            except IngestRecordError as exc:
                self._log(f"[EWMRS] Cannot plan render work for {input_id[:12]}: {exc}")
                return None
        if not plan.data["layers"]:
            if self.handoff.read("render-ack", render_job_id(
                    input_id, INPUT_LAYER, self.fingerprint)) is None:
                self.handoff.acknowledge_input(input_id, self.fingerprint)
                self.metrics["no_mapping"] += 1
                self._log(f"[EWMRS] {notification._input.family}:"
                          f"{notification._input.product} has no configured render layer; "
                          "acknowledged without rendering")
        return plan

    def _admit(self, notification, plan, at):
        input_id = notification.key
        for layer in plan.data["layers"]:
            key = render_job_id(input_id, layer, self.fingerprint)
            if key in self._inflight:
                continue
            existing = self.handoff.read("render-ack", key)
            if existing is not None and existing.data["status"] in TERMINAL_STATUSES:
                self._maybe_acknowledge_input(input_id)
                continue
            if existing is not None and not existing.retry_eligible(at):
                continue
            if self._pending_jobs() >= self.settings.pending_max_jobs:
                self.metrics["rejected"] += 1
                self._log(f"[EWMRS] Per-layer job window is full; deferring {layer} for "
                          f"input {input_id[:12]}")
                continue
            definition = self.layer_definition(layer, notification)
            if definition is None:
                self._fail(input_id, layer, existing, 1,
                           f"layer {layer} is no longer configured")
                continue
            self.handoff.verify_input(notification)
            self._inflight[key] = (input_id, layer, existing, self._jobs.submit(
                self._run_layer, definition))
            self.metrics["dispatched"] += 1
            self._log(f"[EWMRS] Rendering {layer} from {definition.get('input_path')} "
                      f"(source time {notification.data['input']['analysis_time']})")

    def _run_layer(self, definition):
        """Render one layer and report ``(reused_complete_output, ok)``."""
        from EWMRS.pipeline import layer_output_complete

        if str(definition.get("source_type", "mrms")).lower() == "rap_uint16":
            return self._run_rap_layer(definition)
        pool = self._ensure_pool()
        outputs = pool.render([definition])
        output = next(iter(outputs.values()), None)
        return output is not None, output is not None and layer_output_complete(definition)

    def _run_rap_layer(self, definition):
        from EWMRS.pipeline import layer_output_complete
        from EWMRS.rap.uint16_pipeline import run_rap_uint16_pipeline

        name = str(definition["name"])
        if layer_output_complete(definition):
            return True, True
        results = run_rap_uint16_pipeline(definition["input_path"], layers=[definition],
                                         cleanup=False)
        return False, bool(results.get(name)) and layer_output_complete(definition)

    def _apply_result(self, input_id, layer, existing, outcome):
        attempts = (existing.data["attempts"] + 1) if existing is not None else 1
        reused, ok = outcome
        if ok:
            self.handoff.disposition(
                "render-ack", "", status="success", attempts=attempts,
                input_ids=[], input_id=input_id, layer_id=layer,
                render_fingerprint=self.fingerprint)
            if reused:
                self.metrics["reused"] += 1
            self.metrics["succeeded"] += 1
        else:
            self._fail(input_id, layer, existing, attempts, "no usable artifacts published")
        self._maybe_acknowledge_input(input_id)

    def _fail(self, input_id, layer, existing, attempts, reason):
        exhausted = attempts >= self.settings.retry_max_attempts
        if exhausted:
            self.handoff.disposition(
                "render-ack", "", status="expired", reason=reason, attempts=attempts,
                input_ids=[], input_id=input_id, layer_id=layer,
                render_fingerprint=self.fingerprint)
            self.metrics["expired"] += 1
            self._log(f"[EWMRS] Layer {layer} for input {input_id[:12]} expired after "
                      f"{attempts} attempt(s): {reason}")
        else:
            delay = self.settings.retry_delay(attempts)
            self.handoff.disposition(
                "render-ack", "", status="retry", reason=reason, attempts=attempts,
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
                input_ids=[], input_id=input_id, layer_id=layer,
                render_fingerprint=self.fingerprint)
            self.metrics["failed"] += 1
            self._log(f"[EWMRS] Layer {layer} for input {input_id[:12]} failed "
                      f"(attempt {attempts}/{self.settings.retry_max_attempts}); "
                      f"retrying in {delay:.1f}s: {reason}")
        self._maybe_acknowledge_input(input_id)

    def _maybe_acknowledge_input(self, input_id):
        """Release the input's render reference once every layer is terminal."""
        key = render_job_id(input_id, INPUT_LAYER, self.fingerprint)
        if self.handoff.read("render-ack", key) is not None:
            return
        plan = self.handoff.read("render-plan",
                                 render_job_id(input_id, "__plan__", self.fingerprint))
        if plan is None:
            return
        for layer in plan.data["layers"]:
            state = self.handoff.read("render-ack",
                                      render_job_id(input_id, layer, self.fingerprint))
            if state is None or state.data["status"] not in TERMINAL_STATUSES:
                return
        disposition = self.handoff.acknowledge_input(input_id, self.fingerprint)
        self._log(f"[EWMRS] Input {input_id[:12]} acknowledged as "
                  f"{disposition.data['status']}")

    def _collect_finished(self, at):
        """Record every finished render, then expire work that aged out."""
        for key in [key for key, entry in self._inflight.items() if entry[3].done()]:
            input_id, layer, existing, future = self._inflight.pop(key)
            self.metrics["completed"] += 1
            try:
                outcome = future.result()
            except Exception as exc:
                attempts = (existing.data["attempts"] + 1) if existing is not None else 1
                self._fail(input_id, layer, existing, attempts,
                           f"{type(exc).__name__}: {exc}")
                continue
            self._apply_result(input_id, layer, existing, outcome)
        self._expire(at)

    def _expire(self, at):
        for record in self.handoff.records("render-ack"):
            if record.data["status"] in TERMINAL_STATUSES or record.data["layer_id"] == INPUT_LAYER:
                continue
            age = at - utc(record.published_at)
            if age <= timedelta(minutes=self.settings.max_age_minutes):
                continue
            self.handoff.disposition(
                "render-ack", "", status="expired",
                reason=(f"pending render job exceeded "
                        f"{self.settings.max_age_minutes:.0f} minutes"),
                attempts=record.data["attempts"], input_ids=[],
                input_id=record.data["input_id"], layer_id=record.data["layer_id"],
                render_fingerprint=record.data["render_fingerprint"])
            self.metrics["expired"] += 1
            self._log(f"[EWMRS] Expired layer {record.data['layer_id']} for input "
                      f"{record.data['input_id'][:12]} after "
                      f"{self.settings.max_age_minutes:.0f} minutes")
            self._maybe_acknowledge_input(record.data["input_id"])

    def _pending_jobs(self):
        return sum(1 for record in self.handoff.records("render-ack")
                   if record.data["status"] not in TERMINAL_STATUSES)

    def _ensure_pool(self):
        if self._pool is None:
            from EWMRS.pipeline import RenderLayerPool

            self._pool = RenderLayerPool(phase_name=section("render")["phase_name"])
        return self._pool

    @staticmethod
    def _job_workers():
        from EWMRS.pipeline import render_worker_budget

        return render_worker_budget()

    @property
    def in_flight(self):
        """Currently running (input, layer) pairs, for diagnostics and tests."""
        return tuple(sorted((input_id, layer) for input_id, layer, _, _ in
                            self._inflight.values()))

    def close(self):
        if self._jobs is not None and self._owns_jobs:
            self._jobs.shutdown(wait=True, cancel_futures=True)
            self._jobs = None
        if self._pool is not None and self._owns_pool:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None


def ewmrs_consumer_loop(base_dir, log_queue, *, stop_event=None, run_id=None, disable_goes=False):
    """Supervised child target: render every notified input until stopped.

    ``stop_event`` is optional; without it the loop runs until SIGTERM (whose
    handler raises SystemExit through the interruptible sleep) or SIGINT.
    """
    from common.ingest.mrms.config import get_ingest_dependencies
    from util.runtime.process_identity import set_parent_death_signal, set_process_name

    set_process_name("EWMRS-Consumer")
    set_parent_death_signal()
    sys.stdout = QueueWriter(log_queue)
    sys.stderr = QueueWriter(log_queue)

    def _stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)

    import util.file as fs

    dependencies = get_ingest_dependencies(disable_goes=disable_goes)
    consumer = InputRenderConsumer(
        base_dir, run_id=run_id or "ewmrs", dependencies=dependencies,
        log=lambda msg: queue_log(log_queue, str(msg)))
    consumers = section("consumers")
    intervals = section("background_intervals")
    sleep_seconds = float(consumers["ewmrs_notification_seconds"])
    poll_interval = float(intervals["ewmrs_consumer_interval_seconds"])
    if poll_interval <= 0:
        poll_interval = max(sleep_seconds, 0.01)
    try:
        from common.ingest.mrms.config import get_registry

        registry = get_registry()
    except Exception:
        registry = None
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            consumer.poll_once(registry)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            queue_log(log_queue, f"ERROR: EWMRS input consumer pass failed: {exc}")
        if stop_event is not None:
            for _ in range(max(1, int(sleep_seconds / poll_interval))):
                if stop_event.is_set():
                    break
                sleep_for(poll_interval)
        else:
            sleep_for(sleep_seconds, interval=poll_interval)
    consumer.close()
