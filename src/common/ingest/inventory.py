"""Validated input inventory, crash reconciliation, and reference-aware retention."""
import hashlib
import logging
import uuid
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
        # A malformed existing identity must never be silently overwritten.
        self.handoff.read('input', completed.input_id)
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
        """Persist validated acquisition before Core readiness publication."""
        with input_lock(self.base_dir):
            return self._commit_input(completed)

    def valid_inputs(self, *, strict=False):
        """Isolate lost/changed bytes so unrelated scans can still advance."""
        valid = []
        for record in self.handoff.records('input'):
            try:
                self.handoff.verify_input(record)
            except IngestRecordError as exc:
                if strict:
                    raise
                logging.getLogger(__name__).warning('Invalidating input %s: %s', record.key, exc)
                path = self.handoff.path('input', record.key)
                path.rename(path.with_suffix(f'.invalid-{uuid.uuid4().hex}'))
            else:
                valid.append(record)
        return tuple(valid)

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
        Existing inventory needs no network retry when readiness publication fails.
        """
        with input_lock(self.base_dir):
            # Finish interrupted deletions before checking committed-file integrity.
            for retired in self.handoff.records('retired-input'):
                path = contained(self.base_dir, retired.data['input']['path'])
                if path.exists() and file_digest(path) != retired.data['sha256']:
                    raise IngestRecordError('Retired path was replaced with conflicting bytes')
                path.unlink(missing_ok=True)
                self.handoff.path('input', retired.key).unlink(missing_ok=True)
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
            self.valid_inputs()
            return {'adopted': tuple(adopted)}

    def referenced_inputs(self):
        """Every input identity a live Core phase or worker still needs.

        Exposed for retention maintenance outside this class (the ingest
        service's RAP analysis cap) so no other module has to reach into the
        private reference walk.
        """
        with input_lock(self.base_dir):
            return self._references()

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
        return references

    def cleanup(self, *, before, protected_products=(), family_before=None):
        """Delete only completed unreferenced inputs older than the rediscovery window.

        Keep each requested product's latest observation for previous history.
        Family-specific cutoffs preserve slower analyses across radar scans.
        Compact inventory alongside deletion only after Core and worker
        pins have released their references. No operational directory
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
                cutoff = (family_before or {}).get(record.data['input']['family'], before)
                if record.key in references or utc(record.data['input']['analysis_time']) >= utc(cutoff):
                    continue
                self._retire_record(record)
                removed.append(record.key)
            return tuple(removed)

    def _retire_record(self, record):
        path = contained(self.base_dir, record.data['input']['path'])
        if path.exists() and file_digest(path) != record.data['sha256']:
            raise IngestRecordError('Cannot retire changed committed bytes')
        self.handoff._write('retired-input', record.key, record.data)
        path.unlink(missing_ok=True)
        self.handoff.path('input', record.key).unlink(missing_ok=True)

    def retire_inputs(self, identities):
        """Retire specified inventory IDs, atomically yielding to all references."""
        with input_lock(self.base_dir):
            references = self._references()
            removed = []
            for identity in identities:
                record = self.handoff.read('input', identity)
                if record is None or identity in references:
                    continue
                self._retire_record(record)
                removed.append(identity)
            return tuple(removed)

    def compact(self, *, before, tombstones_before=None):
        """Prune expired unpinned scan state and metadata for retired inputs.

        Live Core pins and active consumer state always win over retention.
        The rediscovery window must fit within the tombstone retention window.
        """
        import shutil
        from util.runtime.handoff import parse_cycle_id

        removed = []
        with input_lock(self.base_dir):
            handoff = self.handoff
            pinned = set()
            for pin in handoff.records('pin'):
                pinned.update(pin.data['input_ids'])
            for directory in (handoff.root / 'scans').glob('*'):
                if not directory.is_dir():
                    continue
                try:
                    stamp = parse_cycle_id(directory.name)
                except ValueError:
                    continue
                if stamp >= utc(before):
                    continue
                state = handoff.read('core-state', directory.name)
                if state and state.data['status'] == 'active':
                    continue
                phases = [handoff.read(kind, directory.name) for kind in
                          ('core-start-ready', 'core-integration-ready', 'core-final-ready')]
                ids = {identity for phase in phases if phase for identity in phase.data['input_ids']}
                if ids & pinned:
                    continue
                shutil.rmtree(contained(self.base_dir, directory))
                handoff.path('core-state', directory.name).unlink(missing_ok=True)
                removed.append(directory.name)
            for state in handoff.records('core-state'):
                if (parse_cycle_id(state.key) < utc(before)
                        and state.data['status'] in TERMINAL_STATUSES
                        and not set(state.data['input_ids']) & pinned):
                    handoff.path('core-state', state.key).unlink(missing_ok=True)
            live = {record.key for record in handoff.records('input')}
            for record in handoff.records('retired-input'):
                if (record.published_at < utc(tombstones_before or before)
                        and record.key not in live and record.key not in pinned):
                    path = contained(self.base_dir, record.data['input']['path'])
                    # Reconciliation must finish an interrupted deletion first.
                    if not path.exists():
                        handoff.path('retired-input', record.key).unlink(missing_ok=True)
            handoff.prune_caches()
        return tuple(removed)
