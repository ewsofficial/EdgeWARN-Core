"""Pure scan dependency evaluation over validated inventory selections."""
from dataclasses import dataclass, replace
from datetime import timedelta
import json
import math

from common.ingest.manifest import CycleInputManifest
from common.ingest.mrms.timestamp_utils import round_to_nearest_even_minute
from util.runtime.ingest_handoff import IngestRecordError, utc


@dataclass(frozen=True)
class ScanEvaluation:
    start: CycleInputManifest | None
    integration: CycleInputManifest | None
    final: CycleInputManifest | None
    start_ids: tuple[str, ...]
    integration_ids: tuple[str, ...]
    final_ids: tuple[str, ...]
    missing_check: tuple[str, ...]
    missing_integration: tuple[str, ...]
    optional_dispositions: dict[str, str]


def evaluate_scan(cycle_time, inputs, dependencies, *, start_record=None,
                  integration_record=None, at=None, optional_started_at=None,
                  optional_failures=(), rap_max_age_seconds=None,
                  goes_tolerance_seconds=1200.0):
    """Select only this scan's checks; auxiliary/enrichment use alignment rules.

    ``inputs`` are strict input records validated by Inventory.valid_inputs().
    This function performs no remote I/O or state writes. Existing committed
    phases freeze their selections even when newer aligned observations arrive.
    The caller must start the optional timer at the acquisition boundary and
    persist that time across restarts.
    """
    cycle_time = utc(cycle_time)
    if round_to_nearest_even_minute(cycle_time) != cycle_time:
        raise IngestRecordError('Scan time must be a normalized even UTC minute')
    for phase in (start_record, integration_record):
        if phase is not None and (phase.fingerprint != dependencies.fingerprint or
                                  phase.to_manifest().cycle_time != cycle_time):
            raise IngestRecordError('Pinned phase agreement mismatch')
        if phase is not None:
            validate_phase_dependencies(phase, dependencies)
    auxiliary = json.loads(dependencies.auxiliary_settings_json)
    if rap_max_age_seconds is None:
        minutes = auxiliary.get('rap', {}).get('max_age_minutes')
        if dependencies.rap_enabled and minutes is None:
            raise IngestRecordError('Freeze RAP max_age_minutes in the dependency agreement')
        rap_max_age_seconds = float(minutes) * 60 if minutes is not None else 0.0
    if not all(math.isfinite(value) and value >= 0 for value in
               (rap_max_age_seconds, goes_tolerance_seconds)):
        raise IngestRecordError('Invalid alignment budget')
    options = {'goes_tolerance_seconds': goes_tolerance_seconds,
               'rap_max_age_seconds': rap_max_age_seconds}
    template = CycleInputManifest(cycle_time, **options)
    records = []
    for entry in inputs:
        if entry.fingerprint != dependencies.fingerprint:
            raise IngestRecordError('Inventory dependency fingerprint mismatch')
        staged = entry._input
        if staged is None or entry.kind != 'input':
            raise IngestRecordError('Expected a validated inventory input record')
        records.append((entry.key, staged))
    records.sort(key=lambda pair: (pair[1].analysis_time, pair[0]), reverse=True)

    def select(product=None, family='mrms', exact=False):
        for identity, record in records:
            if record.family != family or (product is not None and record.product != product):
                continue
            if family == 'goes' and 'GLM' not in record.product:
                continue
            if exact and round_to_nearest_even_minute(record.analysis_time) != cycle_time:
                continue
            # Alignment calculation stays pure: no filesystem existence checks.
            delta = (record.analysis_time - cycle_time).total_seconds()
            tolerance = (900 if record.product.startswith('FLASH_') else
                         300 if record.product == 'MergedRhoHV_00.50' else template.mrms_tolerance_seconds)
            if family == 'mrms':
                aligned = -tolerance <= delta <= min(120, template.mrms_tolerance_seconds)
            elif family == 'rap':
                aligned = 0 <= -delta <= template.rap_max_age_seconds
            else:
                aligned = abs(delta) <= template.goes_tolerance_seconds
            if aligned:
                return identity, record
        return None

    def history(product, anchor):
        return next(((identity, replace(record, role='previous')) for identity, record in records
                     if record.family == 'mrms' and record.product == product and
                     record.analysis_time < anchor), None)

    def unpack(phase):
        return list(zip(phase.data['input_ids'], phase.to_manifest().inputs))

    def manifest(pairs):
        return CycleInputManifest(cycle_time, tuple(record for _, record in pairs), **options)

    check = {p: select(p, exact=True) for p in dependencies.check}
    missing_check = tuple(p for p, selected in check.items() if selected is None)
    start_pairs = unpack(start_record) if start_record else []
    if not start_record and not missing_check:
        start_pairs = list(check.values())
        for product in dependencies.previous_detection:
            current = check.get(product)
            previous = history(product, current[1].analysis_time) if current else None
            if previous:
                start_pairs.append(previous)
    start = start_record.to_manifest() if start_record else manifest(start_pairs) if not missing_check else None
    integration_pairs = unpack(integration_record) if integration_record else list(start_pairs)
    missing = []
    required = [(p, 'mrms') for p in dependencies.mandatory_integration]
    if dependencies.rap_enabled:
        required.append(('RAP', 'rap'))
    if dependencies.glm_enabled:
        required.append((None, 'goes'))
    if not integration_record:
        for product, family in required:
            if any(r.role == 'current' and r.family == family and
                   (product is None or r.product == product) for _, r in integration_pairs):
                continue
            selected = select(product, family)
            if selected is None:
                missing.append(product or 'GLM')
            else:
                integration_pairs.append(selected)
    integration = integration_record.to_manifest() if integration_record else manifest(integration_pairs) if start is not None and not missing else None
    final_pairs = list(integration_pairs)
    dispositions = {}
    deadline_elapsed = (at is not None and optional_started_at is not None and
                        utc(at) >= utc(optional_started_at) + timedelta(seconds=dependencies.optional_timeout_seconds))
    for product in dependencies.optional:
        selected = next(((identity, record) for identity, record in final_pairs
                         if record.product == product and record.role == 'current'), None) or select(product)
        if selected:
            dispositions[product] = 'available'
            if selected not in final_pairs:
                final_pairs.append(selected)
        elif product in optional_failures:
            dispositions[product] = 'failed'
        elif deadline_elapsed:
            dispositions[product] = 'expired'
    for product in dependencies.previous_optional:
        current = next((r for _, r in final_pairs if r.product == product and r.role == 'current'), None)
        previous = history(product, current.analysis_time if current else cycle_time)
        if previous and previous not in final_pairs:
            final_pairs.append(previous)
    final = manifest(final_pairs) if integration is not None and len(dispositions) == len(dependencies.optional) else None
    return ScanEvaluation(start, integration, final,
                          tuple(i for i, _ in start_pairs), tuple(i for i, _ in integration_pairs),
                          tuple(i for i, _ in final_pairs), missing_check, tuple(missing), dispositions)


def validate_phase_dependencies(record, dependencies):
    """Consumer preflight on a parsed phase, without weakening configured gates."""
    if record.fingerprint != dependencies.fingerprint:
        raise IngestRecordError('Phase dependency fingerprint mismatch')
    manifest = record.to_manifest()
    if manifest is None or record.kind not in {'core-start-ready', 'core-integration-ready', 'core-final-ready'}:
        raise IngestRecordError('Expected a Core readiness phase')
    current = manifest.current_inputs()
    checks = {r.product for r in current if r.family == 'mrms' and
              round_to_nearest_even_minute(r.analysis_time) == manifest.cycle_time}
    missing = set(dependencies.check) - checks
    if missing:
        raise IngestRecordError(f'Phase lacks current check inputs: {sorted(missing)}')
    if record.kind != 'core-start-ready':
        products = {r.product for r in current if r.family == 'mrms'}
        missing = set(dependencies.mandatory_integration) - products
        if missing:
            raise IngestRecordError(f'Phase lacks mandatory integration inputs: {sorted(missing)}')
        if dependencies.rap_enabled and not any(r.family == 'rap' and r.product == 'RAP' for r in current):
            raise IngestRecordError('Phase lacks RAP')
        if dependencies.glm_enabled and not any(r.family == 'goes' and 'GLM' in r.product for r in current):
            raise IngestRecordError('Phase lacks GLM')
    if record.kind == 'core-final-ready' and set(record.data['optional_dispositions']) != set(dependencies.optional):
        raise IngestRecordError('Final phase lacks terminal optional dispositions')
    return record
