"""Validated input inventory, crash reconciliation, and reference-aware retention."""
import hashlib
from pathlib import Path

from common.ingest.manifest import parse_file_analysis_time
from common.ingest.replay import input_lock
from util.runtime.handoff import canonical_cycle_id
from util.runtime.ingest_handoff import (
    IngestHandoff, IngestRecordError, TERMINAL_STATUSES, contained, file_digest, utc, now,
)


class InputInventory:
    def __init__(self, base_dir, *, fingerprint, run_id):
        self.handoff = IngestHandoff(base_dir, fingerprint=fingerprint, run_id=run_id)
        self.base_dir = self.handoff.base_dir

    def _validate_completion(self, completed):
        record = completed.record
        path = contained(self.base_dir, record.path)
        if not record.validated or record.role != 'current' or not path.is_file():
            raise IngestRecordError('Completion is not a usable current input')
        if record.family == 'rap':
            from common.ingest.synoptic.main import parse_rap_analysis_time
            encoded = parse_rap_analysis_time(path)
        else:
            encoded = parse_file_analysis_time(path)
        if encoded is None or utc(encoded) != record.analysis_time:
            raise IngestRecordError('Encoded source timestamp mismatch')
        if file_digest(path) != completed.sha256:
            raise IngestRecordError('Completion digest mismatch')

    def _commit_input(self, completed):
        self._validate_completion(completed)
        for existing in (*self.handoff.records('input'), *self.handoff.records('retired-input')):
            raw = existing.data['input']
            if (raw['family'], raw['product'], raw['analysis_time']) == (
                completed.record.family, completed.record.product, completed.record.analysis_time.isoformat()):
                if existing.key != completed.input_id:
                    raise IngestRecordError('Conflicting content at committed product/time')
                # Mirrors and local reuse preserve the first provenance/path.
                if existing.kind == 'retired-input':
                    raise IngestRecordError('Input was retired outside the rediscovery window')
                original = contained(self.base_dir, raw['path'])
                if not original.is_file() or file_digest(original) != existing.data['sha256']:
                    raise IngestRecordError('Previously committed input is missing or changed')
                return existing
            if raw['path'] == completed.record.path:
                raise IngestRecordError('Input path already belongs to another identity')
        return self.handoff._write('input', completed.input_id, {
            'input_id': completed.input_id, 'input': completed.record.as_dict(),
            'sha256': completed.sha256, 'source_locator': completed.source_locator,
            'remote_version': completed.remote_version,
            'validation': {'payload_validated': True, 'sha256': completed.sha256}})

    def commit_input(self, completed):
        """Persist acquisition separately from notification publication."""
        with input_lock(self.base_dir):
            return self._commit_input(completed)

    def valid_inputs(self):
        """Fail visibly on lost/changed committed bytes instead of trusting existence."""
        result = self.handoff.records('input')
        for record in result:
            self.handoff.verify_input(record)
        return result

    def publish_scan(self, cycle_time, dependencies, **options):
        """Select and publish references under the same lock as retention."""
        from common.pipeline.readiness import evaluate_scan
        if dependencies.fingerprint != self.handoff.fingerprint:
            raise IngestRecordError('Dependency agreement mismatch')
        key = canonical_cycle_id(utc(cycle_time))
        from common.ingest.mrms.timestamp_utils import round_to_nearest_even_minute
        if round_to_nearest_even_minute(utc(cycle_time)) != utc(cycle_time):
            raise IngestRecordError('Scan time must be a normalized even UTC minute')
        with input_lock(self.base_dir):
            if self.handoff.read('terminal', key) is not None:
                raise IngestRecordError('Scan has a terminal disposition')
            timing = self.handoff.read('scan-state', key)
            if timing is None:
                first_seen = utc(options.get('at') or now())
                optional_started = utc(options.get('optional_started_at') or first_seen)
                timing = self.handoff._write('scan-state', key, {
                    'first_seen_at': first_seen.isoformat(),
                    'optional_started_at': optional_started.isoformat()})
            options['optional_started_at'] = utc(timing.data['optional_started_at'])
            options.setdefault('at', now())
            start = self.handoff.read('core-start-ready', key)
            integration = self.handoff.read('core-integration-ready', key)
            final = self.handoff.read('core-final-ready', key)
            validated = self.valid_inputs()
            history_ids = []
            for product in (*dependencies.previous_detection, *dependencies.previous_optional):
                candidates = [r for r in validated if r.data['input']['family'] == 'mrms'
                              and r.data['input']['product'] == product]
                if candidates:
                    history_ids.append(max(candidates, key=lambda r: utc(r.data['input']['analysis_time'])).key)
            self.handoff._write('pin', hashlib.sha256(b'core-history').hexdigest(),
                                {'input_ids': sorted(set(history_ids))}, mutable=True)
            evaluation = evaluate_scan(cycle_time, validated, dependencies,
                                       start_record=start, integration_record=integration, **options)
            for kind, existing, manifest, ids in (
                ('core-start-ready', start, evaluation.start, evaluation.start_ids),
                ('core-integration-ready', integration, evaluation.integration, evaluation.integration_ids),
                ('core-final-ready', final, evaluation.final, evaluation.final_ids)):
                if existing is None and manifest is not None:
                    self.handoff._publish_phase(kind, manifest, ids,
                        evaluation.optional_dispositions if kind == 'core-final-ready' else {})
            return evaluation

    def reconcile(self, candidates=(), *, revalidate=None):
        """Adopt explicitly discovered files through the source payload validator.

        Candidate enumeration belongs to the source registry/service. The
        callback must return the phase-2 CommittedInput after payload decoding;
        a filename or digest alone never establishes validation on adoption.
        Existing inventory needs no network retry when outbox publication fails.
        Unacknowledged notifications are returned for retryable delivery.
        """
        with input_lock(self.base_dir):
            # Finish interrupted deletions before checking committed-file integrity.
            for retired in self.handoff.records('retired-input'):
                path = contained(self.base_dir, retired.data['input']['path'])
                if path.exists() and file_digest(path) != retired.data['sha256']:
                    raise IngestRecordError('Retired path was replaced with conflicting bytes')
                path.unlink(missing_ok=True)
                self.handoff.path('input', retired.key).unlink(missing_ok=True)
                self.handoff.path('render-ready', retired.key).unlink(missing_ok=True)
            known = {r.data['input']['path'] for r in (
                *self.handoff.records('input'), *self.handoff.records('retired-input'))}
            adopted = []
            for candidate in candidates:
                path = contained(self.base_dir, Path(candidate).absolute())
                if str(path) in known:
                    continue
                if revalidate is None:
                    raise IngestRecordError('Adoption requires a payload validator')
                completed = revalidate(path)
                if Path(completed.record.path).resolve() != path:
                    raise IngestRecordError('Validator returned a different file')
                adopted.append(self._commit_input(completed).key)
                known.add(str(path))
            published = []
            for record in self.valid_inputs():
                if self.handoff.read('render-ready', record.key) is None:
                    self.handoff._write('render-ready', record.key, record.data)
                    published.append(record.key)
            for notification in self.handoff.records('render-ready'):
                source = self.handoff.read('input', notification.key)
                if source is None or source.data != notification.data:
                    raise IngestRecordError('Notification does not match inventory')
            acks = self.handoff.records('render-ack')
            plans = self.handoff.records('render-plan')
            pending = tuple(record.key for record in self.handoff.records('render-ready')
                            if not self._render_complete(record.key, plans, acks))
            return {'adopted': tuple(adopted), 'published': tuple(published), 'pending': pending}

    @staticmethod
    def _render_complete(identity, plans, acks):
        mappings = [p for p in plans if p.data['input_id'] == identity]
        return bool(mappings) and all(any(
            a.data['input_id'] == identity and a.data['layer_id'] == '__input__' and
            a.data['render_fingerprint'] == p.data['render_fingerprint'] and
            a.data['status'] in TERMINAL_STATUSES for a in acks) for p in mappings)

    def _references(self):
        references = set()
        for pin in self.handoff.records('pin'):
            references.update(pin.data['input_ids'])
        for directory in (self.handoff.root / 'scans').glob('*'):
            key = directory.name
            state = self.handoff.read('core-state', key)
            terminal = self.handoff.read('terminal', key)
            if terminal or state and state.data['status'] in TERMINAL_STATUSES:
                continue
            for kind in ('core-start-ready', 'core-integration-ready', 'core-final-ready'):
                record = self.handoff.read(kind, key)
                if record:
                    references.update(record.data['input_ids'])
        for state in self.handoff.records('core-state'):
            if state.data['status'] not in TERMINAL_STATUSES:
                references.update(state.data['input_ids'])
        acks = self.handoff.records('render-ack')
        plans = self.handoff.records('render-plan')
        # Inventory without outbox is also pending work: protect the crash window.
        for record in self.handoff.records('input'):
            if not self._render_complete(record.key, plans, acks):
                references.add(record.key)
        return references

    def cleanup(self, *, before, protected_products=()):
        """Delete only completed unreferenced inputs older than the rediscovery window.

        Keep each requested product's latest observation for previous history.
        Compact inventory/outbox alongside deletion only after both consumer
        dispositions have released their references. No operational directory
        enumeration or arbitrary-path deletion is permitted.
        """
        with input_lock(self.base_dir):
            records = self.handoff.records('input')
            references = self._references()
            for product in protected_products:
                candidates = [r for r in records if r.data['input']['product'] == product]
                if candidates:
                    references.add(max(candidates, key=lambda r: utc(r.data['input']['analysis_time'])).key)
            removed = []
            for record in records:
                if record.key in references or utc(record.data['input']['analysis_time']) >= utc(before):
                    continue
                path = contained(self.base_dir, record.data['input']['path'])
                if path.exists() and file_digest(path) != record.data['sha256']:
                    raise IngestRecordError('Cannot retire changed committed bytes')
                # Keep the identity tombstone before deleting any referenced bytes.
                self.handoff._write('retired-input', record.key, record.data)
                path.unlink(missing_ok=True)
                self.handoff.path('render-ready', record.key).unlink(missing_ok=True)
                self.handoff.path('input', record.key).unlink(missing_ok=True)
                removed.append(record.key)
            return tuple(removed)
