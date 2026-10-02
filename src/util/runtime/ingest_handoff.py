"""Strict durable contracts for the independent ingest v1 stream.

This namespace is deliberately separate from the legacy cycle handoff. All
mutations share the replay input lease; callers holding it use private helpers
so selection, reference publication and deletion form one critical section.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from common.ingest.manifest import CycleInputManifest, StagedInput, parse_file_analysis_time
from common.ingest.objects import CommittedInput
from common.ingest.replay import input_lock
from util.atomic import atomic_write_json
from util.runtime.handoff import canonical_cycle_id, parse_cycle_id


class IngestRecordError(ValueError):
    """Corrupt, incompatible, or unsafe durable ingest state."""


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IngestRecordError('Timestamp must have an explicit timezone')
    return value.astimezone(timezone.utc)


def now():
    return datetime.now(timezone.utc)


def digest_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise IngestRecordError('Expected a SHA256 identity/fingerprint')
    return value


def contained(root, path):
    root = Path(root).resolve()
    path = Path(path)
    if not path.is_absolute() or not path.resolve().is_relative_to(root):
        raise IngestRecordError(f'Path escapes runtime root: {path}')
    return path.resolve()


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


KINDS = {'input', 'render-ready', 'core-start-ready', 'core-integration-ready',
         'core-final-ready', 'terminal', 'core-state', 'scan-state', 'render-ack', 'render-plan', 'retired-input', 'poll-status', 'pin'}
STATUSES = {'pending', 'active', 'retry', 'success', 'abandoned', 'expired', 'skipped', 'no-mapping'}
TERMINAL_STATUSES = {'success', 'abandoned', 'expired', 'skipped', 'no-mapping'}


@dataclass(frozen=True)
class IngestRecord:
    kind: str
    key: str
    fingerprint: str
    run_id: str
    published_at: datetime
    data: dict
    _input: StagedInput | None = field(default=None, repr=False, compare=False)
    _manifest: CycleInputManifest | None = field(default=None, repr=False, compare=False)

    def as_dict(self):
        return {'schema_version': 1, 'producer_service': 'ingest',
                'kind': self.kind, 'key': self.key, 'fingerprint': self.fingerprint,
                'run_id': self.run_id, 'published_at': utc(self.published_at).isoformat(),
                'data': self.data}

    @classmethod
    def from_dict(cls, payload, base_dir, *, fingerprint=None):
        try:
            if not isinstance(payload, dict) or set(payload) != {
                'schema_version', 'producer_service', 'kind', 'key', 'fingerprint',
                'run_id', 'published_at', 'data'}:
                raise IngestRecordError('Malformed record envelope')
            if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
                raise IngestRecordError('Unsupported schema version')
            if payload['producer_service'] != 'ingest' or payload['kind'] not in KINDS:
                raise IngestRecordError('Unsupported producer or record kind')
            digest_id(payload['fingerprint'])
            if fingerprint is not None and payload['fingerprint'] != fingerprint:
                raise IngestRecordError('Dependency fingerprint mismatch')
            if not isinstance(payload['run_id'], str) or not payload['run_id']:
                raise IngestRecordError('Producer run ID is required')
            if not isinstance(payload['key'], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', payload['key']):
                raise IngestRecordError('Unsafe record key')
            record = cls(payload['kind'], payload['key'], payload['fingerprint'],
                         payload['run_id'], utc(payload['published_at']), payload['data'])
            record.validate(base_dir)
            return record
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise IngestRecordError(str(exc)) from exc

    def validate(self, root):
        d = self.data
        if not isinstance(d, dict):
            raise IngestRecordError('Record data must be an object')
        if self.kind in {'input', 'render-ready', 'retired-input'}:
            if set(d) != {'input_id', 'input', 'sha256', 'source_locator', 'remote_version', 'validation'}:
                raise IngestRecordError('Malformed committed input')
            digest_id(d['input_id']); digest_id(d['sha256'])
            raw = d['input']
            if set(raw) != {'product', 'path', 'analysis_time', 'source', 'family', 'validated', 'role'}:
                raise IngestRecordError('Malformed staged input')
            if raw['validated'] is not True or raw['role'] != 'current':
                raise IngestRecordError('Input must be validated current data')
            if raw['family'] not in {'mrms', 'rap', 'goes'} or not all(
                isinstance(raw[k], str) and raw[k] for k in ('product', 'path', 'source')):
                raise IngestRecordError('Invalid source identity')
            source_time = utc(raw['analysis_time'])
            path = contained(root, raw['path'])
            if raw['family'] == 'rap':
                from common.ingest.synoptic.main import parse_rap_analysis_time
                encoded = parse_rap_analysis_time(path)
            else:
                encoded = parse_file_analysis_time(path)
            if encoded is None or utc(encoded) != source_time:
                raise IngestRecordError('Encoded source timestamp mismatch')
            if d['validation'] != {'payload_validated': True, 'sha256': d['sha256']}:
                raise IngestRecordError('Missing validation evidence')
            if not isinstance(d['source_locator'], str) or not d['source_locator']:
                raise IngestRecordError('Missing source locator')
            if d['remote_version'] is not None and not isinstance(d['remote_version'], str):
                raise IngestRecordError('Invalid remote version')
            staged = StagedInput.from_dict(raw)
            object.__setattr__(self, '_input', staged)
            committed = CommittedInput(staged, d['sha256'], d['source_locator'])
            if self.key != d['input_id'] or committed.input_id != self.key:
                raise IngestRecordError('Input identity mismatch')
        elif self.kind.startswith('core-') and self.kind != 'core-state':
            if set(d) != {'manifest', 'input_ids', 'optional_dispositions'}:
                raise IngestRecordError('Malformed phase data')
            parse_cycle_id(self.key)
            raw_manifest = d['manifest']
            if not isinstance(raw_manifest, dict) or set(raw_manifest) != {'cycle_time', 'inputs', 'tolerances'}:
                raise IngestRecordError('Malformed manifest')
            if not isinstance(raw_manifest['inputs'], list):
                raise IngestRecordError('Manifest inputs must be a list')
            tolerances = raw_manifest['tolerances']
            if not isinstance(tolerances, dict) or set(tolerances) != {'mrms_seconds', 'goes_seconds', 'rap_max_age_seconds'} or not all(
                type(v) in {int, float} and math.isfinite(v) and v >= 0 for v in tolerances.values()):
                raise IngestRecordError('Invalid alignment tolerances')
            for raw in raw_manifest['inputs']:
                if not isinstance(raw, dict) or set(raw) != {'product', 'path', 'analysis_time', 'source', 'family', 'validated', 'role'}:
                    raise IngestRecordError('Malformed manifest input')
                if raw['validated'] is not True or raw['role'] not in {'current', 'previous'}:
                    raise IngestRecordError('Unvalidated manifest input')
                utc(raw['analysis_time'])
            manifest = CycleInputManifest.from_dict(raw_manifest)
            object.__setattr__(self, '_manifest', manifest)
            if manifest is None or canonical_cycle_id(manifest.cycle_time) != self.key:
                raise IngestRecordError('Phase timestamp mismatch')
            utc(d['manifest']['cycle_time'])
            if not isinstance(d['input_ids'], list) or len(d['input_ids']) != len(manifest.inputs):
                raise IngestRecordError('Phase input identities missing')
            if len(set(d['input_ids'])) != len(d['input_ids']):
                raise IngestRecordError('Duplicate phase input identities')
            for identity, staged in zip(d['input_ids'], manifest.inputs):
                digest_id(identity); contained(root, staged.path)
                if staged.family not in {'mrms', 'rap', 'goes'}:
                    raise IngestRecordError('Unknown input family')
                if not staged.validated or staged.role not in {'current', 'previous'}:
                    raise IngestRecordError('Invalid phase input')
            if not isinstance(d['optional_dispositions'], dict) or not all(
                isinstance(k, str) and v in {'available', 'failed', 'expired'}
                for k, v in d['optional_dispositions'].items()):
                raise IngestRecordError('Invalid optional disposition')
        elif self.kind in {'terminal', 'core-state', 'render-ack'}:
            expected = {'status', 'attempts', 'retry_at', 'reason', 'input_ids'}
            if self.kind == 'render-ack':
                expected |= {'input_id', 'layer_id', 'render_fingerprint'}
            if set(d) != expected or d['status'] not in STATUSES:
                raise IngestRecordError('Invalid consumer disposition')
            if type(d['attempts']) is not int or d['attempts'] < 0 or not isinstance(d['reason'], str):
                raise IngestRecordError('Invalid retry state')
            if d['status'] in {'expired', 'abandoned', 'skipped', 'no-mapping'} and not d['reason']:
                raise IngestRecordError('Terminal disposition requires a reason')
            if d['retry_at'] is not None:
                utc(d['retry_at'])
            if d['status'] == 'retry' and d['retry_at'] is None:
                raise IngestRecordError('Retry requires eligibility time')
            if not isinstance(d['input_ids'], list):
                raise IngestRecordError('Invalid references')
            for identity in d['input_ids']:
                digest_id(identity)
            if self.kind == 'render-ack':
                digest_id(d['input_id']); digest_id(d['render_fingerprint'])
                if not isinstance(d['layer_id'], str) or not d['layer_id']:
                    raise IngestRecordError('Missing layer ID')
                if self.key != render_job_id(d['input_id'], d['layer_id'], d['render_fingerprint']):
                    raise IngestRecordError('Render job identity mismatch')
            else:
                parse_cycle_id(self.key)
                if self.kind == 'terminal' and d['status'] not in {'expired', 'abandoned', 'skipped'}:
                    raise IngestRecordError('Invalid scan terminal status')
        elif self.kind == 'scan-state':
            if set(d) != {'first_seen_at', 'optional_started_at'}:
                raise IngestRecordError('Invalid scan timing state')
            parse_cycle_id(self.key)
            utc(d['first_seen_at']); utc(d['optional_started_at'])
        elif self.kind == 'render-plan':
            if set(d) != {'input_id', 'layers', 'render_fingerprint'}:
                raise IngestRecordError('Invalid render plan')
            digest_id(d['input_id']); digest_id(d['render_fingerprint'])
            if not isinstance(d['layers'], list) or not all(isinstance(v, str) and v and v != '__input__' for v in d['layers']):
                raise IngestRecordError('Invalid layer mapping')
            if len(set(d['layers'])) != len(d['layers']):
                raise IngestRecordError('Duplicate mapped layer')
            if self.key != render_job_id(d['input_id'], '__plan__', d['render_fingerprint']):
                raise IngestRecordError('Render plan identity mismatch')
        elif self.kind == 'pin':
            if set(d) != {'input_ids'} or not isinstance(d['input_ids'], list):
                raise IngestRecordError('Invalid pin')
            for identity in d['input_ids']:
                digest_id(identity)
        elif self.kind == 'poll-status':
            if set(d) != {'counts', 'reasons'} or not isinstance(d['counts'], dict) or not all(
                isinstance(k, str) and type(v) is int and v >= 0 for k, v in d['counts'].items()):
                raise IngestRecordError('Invalid poll counts')
            if not isinstance(d['reasons'], list) or not all(isinstance(v, str) for v in d['reasons']):
                raise IngestRecordError('Invalid poll reasons')

    def to_manifest(self):
        return self._manifest

    def retry_eligible(self, at):
        return self.data['status'] in {'pending', 'retry'} and (
            self.data['retry_at'] is None or utc(self.data['retry_at']) <= utc(at))


def render_job_id(input_id, layer_id, fingerprint):
    digest_id(input_id); digest_id(fingerprint)
    return hashlib.sha256(json.dumps([input_id, layer_id, fingerprint], separators=(',', ':')).encode()).hexdigest()


class IngestHandoff:
    def __init__(self, base_dir, *, fingerprint, run_id):
        self.base_dir = Path(base_dir).resolve()
        self.fingerprint = digest_id(fingerprint)
        if not isinstance(run_id, str) or not run_id:
            raise IngestRecordError('Producer run ID is required')
        self.run_id = run_id
        self.root = self.base_dir / 'state/realtime/ingest/v1'
        self._verified_files = {}
        # Parsed records keyed by path and validated against the file's
        # (inode, size, mtime_ns) on every read. Records are published by
        # atomic replace, so any rewrite changes the signature and re-parses.
        self._parsed = {}

    def path(self, kind, key):
        if kind not in KINDS or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
            raise IngestRecordError('Unsafe record destination')
        if kind in {'core-start-ready', 'core-integration-ready', 'core-final-ready', 'terminal', 'scan-state'}:
            parse_cycle_id(key)
            path = self.root / 'scans' / key / f'{kind}.json'
        elif kind == 'core-state':
            parse_cycle_id(key)
            path = self.base_dir / 'state/realtime/consumers/core-ingest-v1' / f'{key}.json'
        elif kind == 'render-ack':
            path = self.base_dir / 'state/realtime/consumers/ewmrs-inputs-v1' / f'{key}.json'
        elif kind == 'poll-status':
            path = self.root / 'poll-status.json'
        else:
            path = self.root / {'input': 'inputs', 'pin': 'pins'}.get(kind, kind) / f'{key}.json'
        if kind in {'input', 'render-ready', 'retired-input', 'pin', 'render-plan', 'render-ack'}:
            digest_id(key)
        if kind == 'poll-status' and key != 'poll-status':
            raise IngestRecordError('Invalid poll status key')
        return contained(self.base_dir, path)

    @staticmethod
    def _signature(path):
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    def read(self, kind, key):
        path = self.path(kind, key)
        try:
            before = self._signature(path)
            cached = self._parsed.get(path)
            if cached is not None and cached[0] == before:
                return cached[1]
            payload = json.loads(path.read_text())
            after = self._signature(path)
        except FileNotFoundError:
            self._parsed.pop(path, None)
            return None
        except (OSError, ValueError) as exc:
            raise IngestRecordError(f'Cannot read {path}: {exc}') from exc
        record = IngestRecord.from_dict(payload, self.base_dir, fingerprint=self.fingerprint)
        if record.kind != kind or record.key != key:
            raise IngestRecordError('Record destination mismatch')
        if before == after:
            # Only a read that saw one stable file version may be reused.
            self._parsed[path] = (after, record)
        return record

    def records(self, kind):
        if kind in {'core-start-ready', 'core-integration-ready', 'core-final-ready', 'terminal', 'scan-state'}:
            paths = sorted((self.root / 'scans').glob(f'*/{kind}.json'))
            self._forget_missing(lambda p: p.name == f'{kind}.json' and p.parent.parent == self.root / 'scans', paths)
            return tuple(self.read(kind, path.parent.name) for path in paths)
        key = '20000101T000000Z' if kind == 'core-state' else 'poll-status' if kind == 'poll-status' else '0' * 64
        directory = self.path(kind, key).parent
        paths = [self.path(kind, key)] if kind == 'poll-status' else sorted(directory.glob('*.json'))
        if kind != 'poll-status':
            self._forget_missing(lambda p: p.parent == directory, paths)
        return tuple(record for path in paths if (record := self.read(kind, path.stem)) is not None)

    def _forget_missing(self, belongs, present):
        """Drop cached records whose files a full listing no longer contains."""
        present = {path.resolve() for path in present}
        for path in [p for p in self._parsed if belongs(p) and p not in present]:
            del self._parsed[path]

    def _write(self, kind, key, data, *, mutable=False):
        record = IngestRecord.from_dict(IngestRecord(kind, key, self.fingerprint, self.run_id,
                                                     now(), data).as_dict(), self.base_dir)
        existing = self.read(kind, key)
        if existing is not None and not mutable:
            if existing.data != record.data:
                raise IngestRecordError(f'Incompatible committed {kind}: {key}')
            return existing
        atomic_write_json(self.path(kind, key), record.as_dict())
        return record

    def publish_render_ready(self, input_id):
        with input_lock(self.base_dir):
            source = self.read('input', input_id)
            if source is None:
                raise IngestRecordError('Cannot notify an uncommitted input')
            return self._write('render-ready', input_id, source.data)

    def publish_phase(self, kind, manifest, input_ids, *, optional_dispositions=None):
        if kind not in {'core-start-ready', 'core-integration-ready', 'core-final-ready'}:
            raise IngestRecordError('Unknown phase')
        with input_lock(self.base_dir):
            return self._publish_phase(kind, manifest, input_ids, optional_dispositions or {})

    def _publish_phase(self, kind, manifest, input_ids, dispositions):
        key = canonical_cycle_id(manifest.cycle_time)
        if self.read('terminal', key) is not None:
            raise IngestRecordError('Scan has a terminal disposition')
        for staged in manifest.inputs:
            anchor = next((r.analysis_time for r in manifest.inputs if r.role == 'current'
                           and r.product == staged.product and r.family == staged.family), manifest.cycle_time)
            if staged.role == 'previous' and staged.analysis_time >= anchor:
                raise IngestRecordError('Previous input is not earlier than current selection')
        errors = manifest.validate_alignment()
        if errors:
            raise IngestRecordError('; '.join(errors))
        for identity, staged in zip(input_ids, manifest.inputs):
            source = self.read('input', identity)
            if source is None or {**staged.as_dict(), 'role': 'current'} != source.data['input']:
                raise IngestRecordError('Phase does not match committed input')
            self.verify_input(source)
        predecessor = {'core-integration-ready': 'core-start-ready',
                       'core-final-ready': 'core-integration-ready'}.get(kind)
        if len(input_ids) != len(manifest.inputs):
            raise IngestRecordError('Phase input identities missing')
        if predecessor:
            pinned = self.read(predecessor, key)
            if pinned is None:
                raise IngestRecordError('Previous phase is missing')
            selections = list(zip(input_ids, [r.as_dict() for r in manifest.inputs]))
            for selection in zip(pinned.data['input_ids'], pinned.data['manifest']['inputs']):
                if selection not in selections:
                    raise IngestRecordError('Phase changed pinned selections')
        return self._write(kind, key, {'manifest': manifest.as_dict(),
                                      'input_ids': list(input_ids), 'optional_dispositions': dispositions})

    def disposition(self, kind, key, *, status, reason='', attempts=0, retry_at=None,
                    input_ids=(), input_id=None, layer_id=None, render_fingerprint=None):
        data = {'status': status, 'reason': reason, 'attempts': attempts,
                'retry_at': utc(retry_at).isoformat() if retry_at is not None else None,
                'input_ids': list(input_ids)}
        if kind == 'render-ack':
            key = render_job_id(input_id, layer_id, render_fingerprint)
            data.update(input_id=input_id, layer_id=layer_id, render_fingerprint=render_fingerprint)
        if kind not in {'terminal', 'core-state', 'render-ack'}:
            raise IngestRecordError('Unknown disposition kind')
        with input_lock(self.base_dir):
            if kind == 'core-state' and status in {'pending', 'active', 'retry'} and not input_ids:
                references = set()
                for phase in ('core-start-ready', 'core-integration-ready', 'core-final-ready'):
                    pinned = self.read(phase, key)
                    if pinned:
                        references.update(pinned.data['input_ids'])
                data['input_ids'] = sorted(references)
            existing = self.read(kind, key)
            if existing and attempts < existing.data['attempts']:
                raise IngestRecordError('Retry attempts cannot decrease')
            if existing and existing.data['status'] in TERMINAL_STATUSES:
                if existing.data != data:
                    raise IngestRecordError('Cannot change terminal consumer state')
                return existing
            if kind == 'render-ack':
                if self.read('input', input_id) is None:
                    raise IngestRecordError('Cannot acknowledge an unknown input')
                plan = self.read('render-plan', render_job_id(input_id, '__plan__', render_fingerprint))
                if plan is None or layer_id not in [*plan.data['layers'], '__input__']:
                    raise IngestRecordError('Layer is absent from committed render plan')
                if layer_id == '__input__':
                    if plan.data['layers']:
                        states = [self.read('render-ack', render_job_id(input_id, layer, render_fingerprint))
                                  for layer in plan.data['layers']]
                        if not all(state and state.data['status'] in TERMINAL_STATUSES for state in states):
                            raise IngestRecordError('Mapped layers still have pending work')
                        expected = 'success' if all(state.data['status'] == 'success' for state in states) else 'expired'
                        if status != expected:
                            raise IngestRecordError('Input disposition disagrees with mapped layers')
                    elif status != 'no-mapping':
                        raise IngestRecordError('Unmapped input requires an explicit no-mapping acknowledgment')
            for identity in data['input_ids']:
                if self.read('input', identity) is None:
                    raise IngestRecordError('Consumer reference is not committed')
            return self._write(kind, key, data, mutable=kind != 'terminal')

    def pin(self, owner, input_ids):
        key = hashlib.sha256(owner.encode()).hexdigest()
        with input_lock(self.base_dir):
            for identity in input_ids:
                if self.read('input', identity) is None:
                    raise IngestRecordError('Cannot pin an unknown input')
            return self._write('pin', key, {'input_ids': list(input_ids)}, mutable=True)

    def pin_phase(self, owner, record):
        """Verify and pin one phase's exact selections under a single lease.

        A consumer must re-check the committed bytes and take its reference in
        one critical section, otherwise retention could delete a file between
        the check and the pin. Nesting :meth:`pin` inside the lease would try to
        take the same advisory lock twice, so both steps share one acquisition.
        """
        if record.fingerprint != self.fingerprint or record.to_manifest() is None:
            raise IngestRecordError('Phase agreement mismatch')
        key = hashlib.sha256(owner.encode()).hexdigest()
        with input_lock(self.base_dir):
            staged = record.to_manifest().inputs
            for identity, selection in zip(record.data['input_ids'], staged):
                source = self.read('input', identity)
                if source is None or source.data['input'] != {**selection.as_dict(), 'role': 'current'}:
                    raise IngestRecordError('Phase input identity mismatch')
                self.verify_input(source)
            return self._write('pin', key, {'input_ids': list(record.data['input_ids'])},
                               mutable=True)

    def release_pin(self, owner):
        with input_lock(self.base_dir):
            self.path('pin', hashlib.sha256(owner.encode()).hexdigest()).unlink(missing_ok=True)

    def release_stale_pins(self, *, prefix, keep=None):
        """Remove ``<prefix><scan>`` pins for every known scan except ``keep``.

        Pin files are named by owner digest, so the candidate owners are the
        scan keys present in the scan namespace and the Core consumer state.
        Returns the released owner names.
        """
        consumer = self.path('core-state', '20000101T000000Z').parent
        keys = {path.name for path in (self.root / 'scans').glob('*') if path.is_dir()}
        keys.update(path.stem for path in consumer.glob('*.json'))
        released = []
        with input_lock(self.base_dir):
            for key in sorted(keys):
                owner = f'{prefix}{key}'
                if owner == keep or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
                    continue
                path = self.path('pin', hashlib.sha256(owner.encode()).hexdigest())
                if path.exists():
                    path.unlink(missing_ok=True)
                    released.append(owner)
        return tuple(released)

    def publish_poll_status(self, counts, reasons=()):
        with input_lock(self.base_dir):
            return self._write('poll-status', 'poll-status', {'counts': counts, 'reasons': list(reasons)}, mutable=True)

    def plan_render(self, input_id, layers, render_fingerprint):
        """Freeze enabled mappings before accepting per-layer acknowledgments."""
        key = render_job_id(input_id, '__plan__', render_fingerprint)
        with input_lock(self.base_dir):
            if self.read('render-ready', input_id) is None:
                raise IngestRecordError('Cannot map an input without a notification')
            return self._write('render-plan', key, {'input_id': input_id,
                'layers': sorted(layers), 'render_fingerprint': render_fingerprint})

    def acknowledge_input(self, input_id, render_fingerprint):
        # disposition() validates the plan and all constituent jobs again under
        # the shared lock. A concurrent layer update cannot bypass validation.
        plan = self.read('render-plan', render_job_id(input_id, '__plan__', render_fingerprint))
        if plan is None:
            raise IngestRecordError('Missing render plan')
        states = [self.read('render-ack', render_job_id(input_id, layer, render_fingerprint))
                  for layer in plan.data['layers']]
        if not plan.data['layers']:
            status, reason = 'no-mapping', 'No configured layer mapping'
        elif all(state and state.data['status'] == 'success' for state in states):
            status, reason = 'success', ''
        else:
            status, reason = 'expired', 'One or more mapped layers have a terminal failure'
        return self.disposition('render-ack', '', status=status, reason=reason,
                                input_id=input_id, layer_id='__input__',
                                render_fingerprint=render_fingerprint)

    def validate_phase_inputs(self, record):
        """Recheck exact selections before a consumer uses a committed phase.

        Call under the input lease when taking an active-worker pin. Parsed
        records remain readable for diagnosis after their raw inputs retire.
        """
        if record.fingerprint != self.fingerprint or record.to_manifest() is None:
            raise IngestRecordError('Phase agreement mismatch')
        manifest = record.to_manifest()
        errors = manifest.validate_alignment()
        if errors:
            raise IngestRecordError('; '.join(errors))
        for identity, staged in zip(record.data['input_ids'], manifest.inputs):
            source = self.read('input', identity)
            if source is None or source.data['input'] != {**staged.as_dict(), 'role': 'current'}:
                raise IngestRecordError('Phase input identity mismatch')
            self.verify_input(source)
        return record

    def verify_input(self, record):
        """Verify bytes once per unchanged file version, never derive time from stat.

        Immutable producer publication plus inode/size/mtime/ctime checks keep
        repeated selection locks short. Restarts rehash from scratch; any file
        replacement or mutation invalidates the process-local verification cache.
        """
        path = contained(self.base_dir, record.data['input']['path'])
        try:
            def signature():
                stat = path.stat()
                return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
            before = signature()
            expected = record.data['sha256']
            if self._verified_files.get(record.key) != (str(path), before, expected):
                if file_digest(path) != expected or signature() != before:
                    raise IngestRecordError(f'Committed input bytes changed: {record.key}')
                self._verified_files[record.key] = (str(path), before, expected)
        except OSError as exc:
            raise IngestRecordError(f'Committed input missing or unreadable: {record.key}') from exc
        return record
