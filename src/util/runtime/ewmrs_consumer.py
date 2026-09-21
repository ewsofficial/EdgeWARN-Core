"""EWMRS phase-record consumer (decomposition Phase 4).

Consumes committed ``mrms-ready`` and ``rap-ready`` phase records published by
the primary service. MRMS records trigger a best-effort per-layer scan of local
inputs; RAP conversion continues to use the exact pinned input in its record.
Correctness rules (plans/realtime-runner-decomposition-plan.md):

- Records are processed in cycle-timestamp order; a malformed record stops
  the drain so newer cycles are never rendered under older timestamps.
- A consumer checkpoint advances after the MRMS best-effort scan completes or
  RAP publishes usable artifacts; renderer exceptions are retried on the next
  poll without advancing.
- Cycles beyond ``cycle.max_backlog_cycles`` are marked unrecoverable
  explicitly and skipped without rendering; processing resumes at the oldest
  still-valid record.
- RAP validation failures (missing or misaligned exact inputs) mark that cycle
  unrecoverable explicitly rather than blocking the backlog forever.

This module runs only inside the EWMRS service process tree; importing it is
allowed to load the EWMRS render stack lazily inside its methods.
"""

import signal
import sys

from util.io import QueueWriter
from util.runtime.config import section
from util.runtime.handoff import (
    ConsumerCheckpointStore,
    PhaseRecord,
    PhaseRecordError,
    phase_record_path,
    read_phase_record_strict,
    select_pending_records,
    shadow_validate_phase_record,
)
from util.runtime.logging import queue_log
from util.runtime.timing import sleep_for

CONSUMER_NAME = "ewmrs"


class RapRecordMissingInput(Exception):
    """A committed rap-ready record pins no RAP-family input.

    Retrying cannot fix a producer-side schema problem, so the drain marks
    such cycles unrecoverable instead of blocking the phase forever.
    """


class EwmrsRecordConsumer:
    """Ordered, checkpointed consumer of primary-published phase records.

    Each phase keeps its own durable checkpoint so a failure in one phase can
    never be masked by the other phase's progress.
    """

    def __init__(self, base_dir, *, log=None, max_backlog=None):
        self.base_dir = base_dir
        self._log = log if log is not None else print
        self._max_backlog = (
            int(section("cycle").get("max_backlog_cycles", 10))
            if max_backlog is None
            else int(max_backlog)
        )

    def checkpoint_for(self, phase: str):
        return ConsumerCheckpointStore(
            self.base_dir, f"{CONSUMER_NAME}-{phase}"
        ).load()

    def process_pending_once(self) -> tuple[int, int]:
        """Drain both phases once; returns ``(processed, skipped)`` counts."""
        processed = skipped = 0
        for phase in ("mrms-ready", "rap-ready"):
            phase_processed, phase_skipped = self._drain(
                phase,
                self._handle_mrms_ready if phase == "mrms-ready" else self._handle_rap_ready,
            )
            processed += phase_processed
            skipped += phase_skipped
        return processed, skipped

    def _drain(self, phase: str, handler) -> tuple[int, int]:
        checkpoint_store = ConsumerCheckpointStore(
            self.base_dir, f"{CONSUMER_NAME}-{phase}"
        )
        pending = select_pending_records(
            self.base_dir,
            phase,
            checkpoint=checkpoint_store.load(),
            max_backlog=self._max_backlog,
        )
        processed = skipped = 0
        for cycle_id, record, status in pending:
            if status == "already-processed":
                # These records are retained for observability by the handoff
                # selector. They are expected on every steady-state poll, so
                # emitting one line per historical cycle is pure log noise.
                continue
            if status == "abandoned-backlog":
                # Backlog-cap abandonment applies regardless of record state:
                # a malformed oldest record must still be cap-able out.
                self._log(
                    f"[EWMRS] {phase} cycle {cycle_id} exceeded the "
                    "backlog cap; recorded as unrecoverable without rendering"
                )
                checkpoint_store.record(cycle_id)
                skipped += 1
                continue
            if record is None:
                # Distinguish evidence problems from in-flight commits: a
                # missing file means the producer has not committed this
                # phase yet (cycle dirs appear when the first phase lands),
                # so wait quietly; an existing but unparseable file is real
                # damage and stops the drain so newer cycles are never
                # rendered under an older timestamp's identity.
                if not phase_record_path(self.base_dir, cycle_id, phase).exists():
                    break
                try:
                    read_phase_record_strict(
                        phase_record_path(self.base_dir, cycle_id, phase)
                    )
                except PhaseRecordError as exc:
                    self._log(
                        f"[EWMRS] {phase} record for {cycle_id} cannot be read safely; "
                        f"stopping this drain: {exc}"
                    )
                break
            try:
                # Re-read strictly at the consumption boundary.  A transient
                # read failure must not be reported as producer corruption.
                record = read_phase_record_strict(
                    phase_record_path(self.base_dir, cycle_id, phase)
                )
                if record is None:
                    break
                # MRMS records are cycle triggers. Their inputs are useful
                # provenance, but rendering deliberately scans each layer's
                # local directory so one absent/lagging product cannot block
                # all other products. RAP remains an exact-input contract.
                problems = (
                    ()
                    if phase == "mrms-ready"
                    else shadow_validate_phase_record(record, base_dir=self.base_dir)
                )
                if problems:
                    self._log(
                        f"[EWMRS] {phase} cycle {cycle_id} is unrecoverable; "
                        f"validation problems: {list(problems)}"
                    )
                    checkpoint_store.record(cycle_id)
                    skipped += 1
                    continue
                handler(record)
                checkpoint_store.record(cycle_id)
                processed += 1
                self._log(f"[EWMRS] Consumed {phase} cycle {cycle_id}")
            except RapRecordMissingInput as exc:
                self._log(f"[EWMRS] {phase} cycle {cycle_id} is unrecoverable: {exc}")
                checkpoint_store.record(cycle_id)
                skipped += 1
                continue
            except PhaseRecordError as exc:
                self._log(
                    f"[EWMRS] {phase} record for {cycle_id} cannot be read safely; "
                    f"stopping this drain: {exc}"
                )
                break
            except Exception as exc:
                # Render failure: do not advance the checkpoint; the record is
                # retried on the next poll.
                self._log(f"[EWMRS] {phase} cycle {cycle_id} failed: {exc}")
                break
        return processed, skipped

    def _handle_mrms_ready(self, record: PhaseRecord):
        from EWMRS.pipeline import mrms_required_layer_failures, run_mrms_render_pipeline

        results = run_mrms_render_pipeline(record.analysis_time)
        failed_required, failed_optional = mrms_required_layer_failures(results)
        for missing_layer in (*failed_required, *failed_optional):
            self._log(
                f"[EWMRS] MRMS layer unavailable for this pass; "
                f"will scan again on the next cycle: {missing_layer}"
            )

    def _handle_rap_ready(self, record: PhaseRecord):
        from EWMRS.pipeline import run_rap_uint16_pipeline

        rap_inputs = [
            staged for staged in record.inputs if getattr(staged, "family", "") == "rap"
        ]
        if not rap_inputs:
            # A producer bug (for example mrms-core-only publishing without
            # staging RAP). Unrecoverable by retrying, so the drain treats it
            # like any other validation failure via the raised error path --
            # but raise a distinct type the caller can mark unrecoverable.
            raise RapRecordMissingInput(
                "rap-ready record pins no RAP input; producer is misconfigured"
            )
        current = next((staged for staged in rap_inputs if staged.role == "current"), rap_inputs[0])
        results = run_rap_uint16_pipeline(current.path, record.analysis_time)
        incomplete = [name for name, path in results.items() if path is None]
        if not results or len(incomplete) == len(results):
            raise RuntimeError("RAP Uint16 conversion produced no usable layers")


def ewmrs_consumer_loop(base_dir, log_queue, *, stop_event=None):
    """Supervised child target: consume committed records until stopped.

    ``stop_event`` is optional; without it the loop runs until SIGTERM (whose
    handler raises SystemExit through the interruptible sleep) or SIGINT.
    """
    from util.runtime.process_identity import set_parent_death_signal, set_process_name

    set_process_name("EWMRS-Consumer")
    set_parent_death_signal()
    sys.stdout = QueueWriter(log_queue)
    sys.stderr = QueueWriter(log_queue)

    def _stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)

    consumer = EwmrsRecordConsumer(base_dir, log=lambda msg: queue_log(log_queue, str(msg)))
    intervals = section("background_intervals")
    sleep_seconds = float(intervals["ewmrs_consumer_seconds"])
    poll_interval = float(intervals["ewmrs_consumer_interval_seconds"])
    if poll_interval <= 0:
        poll_interval = max(sleep_seconds, 0.01)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            processed, skipped = consumer.process_pending_once()
            if processed or skipped:
                queue_log(
                    log_queue,
                    f"INFO: EWMRS consumer pass: {processed} record(s) rendered, "
                    f"{skipped} marked unrecoverable",
                )
        except KeyboardInterrupt:
            return
        except Exception as exc:
            queue_log(log_queue, f"ERROR: EWMRS consumer pass failed: {exc}")
        if stop_event is not None:
            for _ in range(max(1, int(sleep_seconds / poll_interval))):
                if stop_event.is_set():
                    return
                sleep_for(poll_interval)
        else:
            sleep_for(sleep_seconds, interval=poll_interval)
