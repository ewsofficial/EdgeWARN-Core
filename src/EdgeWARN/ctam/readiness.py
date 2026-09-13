"""Immutable per-cycle file catalog and requirement evaluation for CTAM.

Nothing here launches, imports, or talks to module code. Phase 1 answers two
questions only: *what artifacts does this cycle have*, and *which declared
requirements does each installed module therefore meet*. Phase 5 skips a module
whose requirements are unmet, so a wrong verdict here either starves a module
that could have run or runs one against a file that is not on disk.

The catalog is built from the pinned ``CycleInputManifest``, the in-memory cell
list, and the history files of exactly those cells. It is deliberately **not**
built by scanning a data directory. The input set for a cycle is already decided
upstream; a rescan would let CTAM advertise a file the rest of the pipeline did
not use, and the two would then disagree about what the cycle was. Temp files,
``.idx`` siblings, stale caches, and unrelated runtime products are excluded by
construction rather than by a filter that has to be kept correct.

Serialized shapes are frozen in ``docs/ctam/schema/``:
``file-descriptor.schema.json`` for :meth:`CatalogFile.as_dict`,
``requirements-evaluation.schema.json`` for :func:`evaluate_requirements`, and
``status-record.schema.json`` for :func:`cycle_status`. Those documents are the
source of truth; where a literal below restates one it says so.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from common.ingest.manifest import CycleInputManifest, StagedInput
from util.atomic import atomic_write_json

from .discovery import (
    STATE_DISCOVERED,
    STATE_INVALID,
    STATE_SKIPPED_DISABLED,
    DiscoveredModule,
    DiscoveryResult,
)
from .limits import (
    API_VERSION,
    MAX_HISTORY_WINDOW,
    MAX_STREAMED_FILE_BYTES,
    RESERVED_MODULE_IDS,
    STATUS_SCHEMA_VERSION,
)
from .manifest import ModuleManifest, ModuleRequirement, Selector, parse_selector

# --- Frozen vocabularies ----------------------------------------------------
# Each tuple restates one enum from docs/ctam/schema/. They are duplicated
# rather than derived for the reason manifest.py gives: the schemas are
# documentation-tree JSON, and readiness must not import the documentation tree
# to run. tests/core/ctam/test_readiness.py reads the schema files and asserts
# these agree, so the copies cannot drift silently.

# file-descriptor.schema.json, cycle_id in status-record.schema.json.
FILE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z"
CYCLE_ID_PATTERN = r"^[0-9]{8}-[0-9]{6}\Z"

# file-descriptor.schema.json, kind / role / readiness.
KIND_INPUT = "input"
KIND_STORMCELLS = "stormcells"
KIND_CELL_HISTORY = "cell_history"
KIND_ALERTS = "alerts"
KINDS = (KIND_INPUT, KIND_STORMCELLS, KIND_CELL_HISTORY, KIND_ALERTS)

ROLE_CURRENT = "current"
ROLE_PREVIOUS = "previous"
ROLE_HISTORY = "history"
ROLE_FORECAST = "forecast"
ROLES = (ROLE_CURRENT, ROLE_PREVIOUS, ROLE_HISTORY, ROLE_FORECAST)

READY = "ready"
PENDING = "pending"
UNAVAILABLE = "unavailable"
INVALID = "invalid"
STALE = "stale"
READINESS_VALUES = (READY, PENDING, UNAVAILABLE, INVALID, STALE)

# requirements-evaluation.schema.json, requirements.*.unmet_condition. `None` is
# an enum *member* there, not an absent key, so a satisfied requirement emits an
# explicit null rather than omitting the property.
UNMET_MISSING = "missing"
UNMET_NOT_VALIDATED = "not_validated"
UNMET_MAX_AGE = "max_age_seconds"
UNMET_MIN_HISTORY = "min_history_entries"
UNMET_ROLE_MISMATCH = "role_mismatch"
UNMET_ANALYSIS_TIME_MISMATCH = "analysis_time_mismatch"
UNMET_CONDITIONS = (
    UNMET_MISSING,
    UNMET_NOT_VALIDATED,
    UNMET_MAX_AGE,
    UNMET_MIN_HISTORY,
    UNMET_ROLE_MISMATCH,
    UNMET_ANALYSIS_TIME_MISMATCH,
    None,
)

# status-record.schema.json, state. Phase 1 only reaches the first three: no
# module process exists yet, so nothing can be running, committing, or failed.
CYCLE_STATE_CATALOG_BUILDING = "catalog_building"
CYCLE_STATE_REQUIREMENTS_EVALUATED = "requirements_evaluated"
CYCLE_STATE_NOT_READY = "not_ready"
CYCLE_STATES = (
    CYCLE_STATE_CATALOG_BUILDING,
    CYCLE_STATE_REQUIREMENTS_EVALUATED,
    CYCLE_STATE_NOT_READY,
    "stormprob_running",
    "external_modules_running",
    "committing",
    "completed",
    "failed",
)

# status-record.schema.json, modules.*.state.
MODULE_STATE_WAITING = "waiting"
MODULE_STATE_READY = "ready"
MODULE_STATE_SKIPPED_MISSING_REQUIREMENTS = "skipped_missing_requirements"
MODULE_STATES = (
    STATE_DISCOVERED,
    STATE_INVALID,
    MODULE_STATE_WAITING,
    MODULE_STATE_READY,
    "running",
    "committing",
    "completed",
    STATE_SKIPPED_DISABLED,
    MODULE_STATE_SKIPPED_MISSING_REQUIREMENTS,
    "timed_out",
    "failed",
)

# --- Media types ------------------------------------------------------------
# Matched against `^[a-z]+/[A-Za-z0-9.+_-]+\Z` in file-descriptor.schema.json.
# Keyed by family first because a staged MRMS product may be `.grib2` or
# `.grib2.gz` and the wire type is the same either way.
_FAMILY_MEDIA_TYPES = {
    "mrms": "application/x-grib2",
    "rap": "application/x-grib2",
    "goes": "application/x-netcdf",
}
_SUFFIX_MEDIA_TYPES = {
    ".json": "application/json",
    ".nc": "application/x-netcdf",
    ".grib2": "application/x-grib2",
    ".grb2": "application/x-grib2",
    ".grib": "application/x-grib2",
}
_DEFAULT_MEDIA_TYPE = "application/octet-stream"
_JSON_MEDIA_TYPE = "application/json"

# A host-owned artifact that is absent is `pending` only while the host could
# still write it. docs/ctam/internal-api-limits.md grounds 120 seconds as the
# hard cadence ceiling on everything CTAM does (the scheduler can only select an
# even minute), so once the wall clock is a full cadence past the cycle the host
# is not going to produce it and calling it `pending` would be a lie. Not a row
# in that document's Values table, hence a local constant rather than a
# limits.py entry.
PENDING_GRACE_SECONDS = 120.0


def _as_utc(value: datetime) -> datetime:
    """Every serialized timestamp carries an explicit offset. Never naive."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat()


def parse_cycle_id(timestamp: str) -> datetime:
    """Validate a pipeline timestamp and return its UTC analysis time.

    The pipeline timestamp ``"20260805-120000"`` is already exactly the frozen
    ``cycle_id`` pattern, so the cycle id is that string verbatim -- no
    reformatting step exists that could disagree with the rest of the pipeline
    about which cycle this is.
    """
    if not isinstance(timestamp, str) or re.search(CYCLE_ID_PATTERN, timestamp) is None:
        raise ValueError(
            f"cycle timestamp {timestamp!r} is not a legal cycle_id; "
            f"docs/ctam/schema/status-record.schema.json requires "
            f"{CYCLE_ID_PATTERN} , for example '20260805-120000'"
        )
    return datetime.strptime(timestamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CatalogFile:
    """One catalog descriptor, mirroring ``file-descriptor.schema.json``.

    ``path`` is the host's writable source location. It is a field on the
    dataclass because the host needs it to read content, and it is excluded from
    :meth:`as_dict` because physical paths are deliberately not part of the
    portable module contract -- the schema is closed with
    ``additionalProperties: false`` and requires exactly the other twelve keys.
    """

    file_id: str
    kind: str
    family: str | None
    product: str | None
    role: str
    analysis_time: str
    available: bool
    validated: bool
    readiness: str
    reason: str | None
    size_bytes: int | None
    media_type: str
    path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        """Exactly the twelve schema fields. ``path`` must not appear."""
        return {
            "file_id": self.file_id,
            "kind": self.kind,
            "family": self.family,
            "product": self.product,
            "role": self.role,
            "analysis_time": self.analysis_time,
            "available": self.available,
            "validated": self.validated,
            "readiness": self.readiness,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }

    @property
    def analysis_datetime(self) -> datetime:
        """The parsed ``analysis_time``, for age arithmetic."""
        return _as_utc(datetime.fromisoformat(self.analysis_time))


@dataclass(frozen=True)
class CTAMCycleCatalog:
    """The frozen artifact set for one cycle."""

    cycle_id: str
    analysis_time: str
    historical: bool
    cell_count: int
    files: tuple[CatalogFile, ...]

    def descriptor(self, file_id: str) -> CatalogFile | None:
        for entry in self.files:
            if entry.file_id == file_id:
                return entry
        return None

    def as_dicts(self) -> tuple[dict[str, Any], ...]:
        return tuple(entry.as_dict() for entry in self.files)

    def resolve(self, selector: Selector | str) -> tuple[CatalogFile, ...]:
        """Descriptors a manifest selector names, possibly none.

        A raw string is parsed, so a caller holding manifest text does not have
        to reach for :func:`parse_selector` itself and cannot accidentally admit
        a selector the manifest validator would have rejected.
        """
        if isinstance(selector, str):
            selector = parse_selector(selector)

        if selector.kind == KIND_CELL_HISTORY:
            # `cells.history` is a fleet selector: it names every active cell's
            # history, not one file.
            return tuple(e for e in self.files if e.kind == KIND_CELL_HISTORY)
        if selector.kind == KIND_STORMCELLS:
            return tuple(
                e
                for e in self.files
                if e.kind == KIND_STORMCELLS and e.role == selector.role
            )
        return tuple(
            e
            for e in self.files
            if e.kind == KIND_INPUT
            and e.family == selector.family
            and e.product == selector.product
            and e.role == selector.role
        )


# --------------------------------------------------------------------------
# Descriptor construction
# --------------------------------------------------------------------------


def _probe(path: Path) -> tuple[bool, int | None, str | None]:
    """Existence, size, and an actionable reason when the artifact is absent.

    Only the file *name* goes into the reason: the reason is published to
    modules through the catalog, and an absolute host path is not part of the
    portable contract.
    """
    try:
        if not path.is_file():
            return False, None, f"no readable artifact at {path.name}"
        return True, path.stat().st_size, None
    except OSError as exc:
        return False, None, f"cannot read {path.name}: {exc.strerror or exc}"


def _media_type(family: str | None, path: Path | None) -> str:
    if family and family in _FAMILY_MEDIA_TYPES:
        return _FAMILY_MEDIA_TYPES[family]
    if path is not None:
        return _SUFFIX_MEDIA_TYPES.get(path.suffix.lower(), _DEFAULT_MEDIA_TYPE)
    return _DEFAULT_MEDIA_TYPE


def _json_payload(path: Path) -> tuple[Any, str | None]:
    """Parse a host-owned JSON artifact, or return why it is unusable."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), None
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is not parseable JSON: line {exc.lineno} {exc.msg}"
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc.strerror or exc}"
    except UnicodeDecodeError:
        return None, f"{path.name} is not valid UTF-8 JSON"


def _alignment_errors(
    input_manifest: CycleInputManifest, record: StagedInput
) -> tuple[str, ...]:
    """Cycle-alignment verdict for one staged input, from the pinned manifest.

    Reuses ``CycleInputManifest.validate_alignment`` on a one-record probe that
    carries the same tolerances rather than restating the per-family windows.
    Those windows already encode operational knowledge -- the directional MRMS
    lag, the coarser FLASH cadence, the RAP age budget tied to the staging
    budget -- and a second copy here would drift from the selection the rest of
    the pipeline made. The caller has already ruled out the missing and
    unvalidated cases that ``validate_alignment`` reports first, so anything it
    returns at this point is an alignment failure.
    """
    probe = CycleInputManifest(
        cycle_time=input_manifest.cycle_time,
        inputs=(record,),
        mrms_tolerance_seconds=input_manifest.mrms_tolerance_seconds,
        goes_tolerance_seconds=input_manifest.goes_tolerance_seconds,
        rap_max_age_seconds=input_manifest.rap_max_age_seconds,
    )
    return probe.validate_alignment()


def _describe_input(
    record: StagedInput, input_manifest: CycleInputManifest
) -> CatalogFile:
    path = record.local_path
    descriptor = CatalogFile(
        file_id=f"input:{record.family}:{record.product}:{record.role}",
        kind=KIND_INPUT,
        family=record.family,
        product=record.product,
        role=record.role,
        analysis_time=_isoformat(record.analysis_time),
        available=True,
        validated=True,
        readiness=READY,
        reason=None,
        size_bytes=None,
        media_type=_media_type(record.family, path),
        path=path,
    )

    available, size, access_reason = _probe(path)
    if not available:
        # `unavailable`: a module can never obtain this file during this cycle.
        # The pinned manifest named it, so its disappearance is a real fault,
        # not a race the module should retry.
        return replace(
            descriptor,
            available=False,
            validated=False,
            readiness=UNAVAILABLE,
            reason=access_reason,
            size_bytes=None,
        )

    if size is not None and size > MAX_STREAMED_FILE_BYTES:
        # Also `unavailable`, per the "Maximum streamed file size" row of
        # docs/ctam/internal-api-limits.md: the content endpoint refuses it
        # rather than truncating, so no module can ever read it. `available`
        # stays true because the artifact does exist and is readable -- the
        # schema says a module must never infer readiness from `available`
        # alone, and this is the case that proves it.
        return replace(
            descriptor,
            validated=False,
            readiness=UNAVAILABLE,
            size_bytes=size,
            reason=(
                f"{path.name} is {size} bytes, above the "
                f"{MAX_STREAMED_FILE_BYTES}-byte streamed-file limit; it cannot "
                f"be served to a module"
            ),
        )

    if not record.validated:
        # `invalid`: present and readable, but the source coordinator's own
        # kind-specific validation rejected it. Distinct from `unavailable`
        # because the fix is upstream re-staging, not waiting.
        return replace(
            descriptor,
            validated=False,
            readiness=INVALID,
            size_bytes=size,
            reason=(
                f"{path.name} failed staged validation for product "
                f"{record.product}; the pinned cycle manifest marks it "
                f"validated = false"
            ),
        )

    errors = _alignment_errors(input_manifest, record)
    if errors:
        # `stale`: present and valid, but outside the family's cycle alignment
        # window, so using it would silently mix two analysis times.
        return replace(
            descriptor,
            readiness=STALE,
            size_bytes=size,
            reason=(
                f"{path.name} is outside the cycle alignment window: "
                f"{'; '.join(errors)}"
            ),
        )

    return replace(descriptor, size_bytes=size)


def _cell_history_file_id(cell_id: str) -> str:
    """A pattern-safe, collision-free ``cell_history:`` id.

    A cell id is host-generated and normally plain, but the ``file_id`` pattern
    is frozen and a descriptor that violated it would fail schema validation at
    the API boundary rather than here. Sanitizing alone could map two cells onto
    one id, so an unusual id keeps a digest of its exact original.
    """
    candidate = f"{KIND_CELL_HISTORY}:{cell_id}"
    if re.search(FILE_ID_PATTERN, candidate) is not None:
        return candidate
    digest = hashlib.sha256(cell_id.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", cell_id)[:48].strip("-.") or "cell"
    return f"{KIND_CELL_HISTORY}:{safe}.{digest}"


def _host_artifact_absent_readiness(
    cycle_time: datetime, now: datetime, historical: bool
) -> str:
    """`pending` while the host could still write it, else `unavailable`.

    This is the distinction the five-member readiness enum exists for. A
    brand-new cell has no history file and that is not an error -- it is a cell
    on its first scan, and a module that needs history should wait rather than
    treat the cycle as broken. Once the wall clock is a full scan cadence past
    the cycle, nothing further is coming, and `pending` would mislead an
    operator into waiting for an artifact no writer will produce. A historical
    rebuild is exempt: its cycle is intentionally in the past, so the wall clock
    says nothing about whether the artifact exists.
    """
    if historical:
        return PENDING
    if (now - cycle_time).total_seconds() > PENDING_GRACE_SECONDS:
        return UNAVAILABLE
    return PENDING


def _describe_stormcells(
    stormcell_path: Path | None,
    *,
    cycle_id: str,
    cycle_time: datetime,
    now: datetime,
    historical: bool,
) -> CatalogFile:
    analysis_time = _isoformat(cycle_time)
    descriptor = CatalogFile(
        file_id="stormcells:current",
        kind=KIND_STORMCELLS,
        family=None,
        product=None,
        role=ROLE_CURRENT,
        analysis_time=analysis_time,
        available=True,
        validated=True,
        readiness=READY,
        reason=None,
        size_bytes=None,
        media_type=_JSON_MEDIA_TYPE,
        path=stormcell_path,
    )

    if stormcell_path is None:
        readiness = _host_artifact_absent_readiness(cycle_time, now, historical)
        return replace(
            descriptor,
            available=False,
            validated=False,
            readiness=readiness,
            reason=(
                f"the host has not written a stormcell snapshot for cycle "
                f"{cycle_id} yet"
            ),
        )

    available, size, access_reason = _probe(stormcell_path)
    if not available:
        readiness = _host_artifact_absent_readiness(cycle_time, now, historical)
        return replace(
            descriptor,
            available=False,
            validated=False,
            readiness=readiness,
            reason=access_reason,
        )

    if size is not None and size > MAX_STREAMED_FILE_BYTES:
        return replace(
            descriptor,
            validated=False,
            readiness=UNAVAILABLE,
            size_bytes=size,
            reason=(
                f"{stormcell_path.name} is {size} bytes, above the "
                f"{MAX_STREAMED_FILE_BYTES}-byte streamed-file limit"
            ),
        )

    payload, parse_reason = _json_payload(stormcell_path)
    if parse_reason is not None:
        return replace(
            descriptor,
            validated=False,
            readiness=INVALID,
            size_bytes=size,
            reason=parse_reason,
        )
    if not isinstance(payload, dict) or "features" not in payload:
        # The snapshot is a flat dict with `source, product, version,
        # latest_timestamp, features` -- not a GeoJSON FeatureCollection, and it
        # has no `type` key. See plans/modular-ctam-phase0-findings.md finding 3.
        return replace(
            descriptor,
            validated=False,
            readiness=INVALID,
            size_bytes=size,
            reason=(
                f"{stormcell_path.name} is not a stormcell snapshot: expected a "
                f"JSON object with a 'features' member"
            ),
        )

    recorded = payload.get("latest_timestamp")
    if isinstance(recorded, str) and recorded and recorded != cycle_id:
        # `stale`: a real snapshot, but of another cycle. This is the wrong-cycle
        # file the readiness enum has to be able to distinguish from a missing
        # one, because the bytes parse perfectly and only the timestamp betrays it.
        return replace(
            descriptor,
            readiness=STALE,
            size_bytes=size,
            reason=(
                f"{stormcell_path.name} records latest_timestamp {recorded!r} but "
                f"this cycle is {cycle_id}"
            ),
        )

    return replace(descriptor, size_bytes=size)


def _describe_cell_history(
    cell_id: str,
    path: Path,
    *,
    cycle_time: datetime,
    now: datetime,
    historical: bool,
) -> CatalogFile:
    descriptor = CatalogFile(
        file_id=_cell_history_file_id(cell_id),
        kind=KIND_CELL_HISTORY,
        family=None,
        product=None,
        role=ROLE_HISTORY,
        analysis_time=_isoformat(cycle_time),
        available=True,
        validated=True,
        readiness=READY,
        reason=None,
        size_bytes=None,
        media_type=_JSON_MEDIA_TYPE,
        path=path,
    )

    available, size, access_reason = _probe(path)
    if not available:
        readiness = _host_artifact_absent_readiness(cycle_time, now, historical)
        reason = (
            f"cell {cell_id} has no history file yet; this is its first scan"
            if readiness == PENDING
            else access_reason
        )
        return replace(
            descriptor,
            available=False,
            validated=False,
            readiness=readiness,
            reason=reason,
        )

    if size is not None and size > MAX_STREAMED_FILE_BYTES:
        return replace(
            descriptor,
            validated=False,
            readiness=UNAVAILABLE,
            size_bytes=size,
            reason=(
                f"{path.name} is {size} bytes, above the "
                f"{MAX_STREAMED_FILE_BYTES}-byte streamed-file limit"
            ),
        )

    payload, parse_reason = _json_payload(path)
    if parse_reason is not None:
        return replace(
            descriptor,
            validated=False,
            readiness=INVALID,
            size_bytes=size,
            reason=parse_reason,
        )
    if not isinstance(payload, list):
        return replace(
            descriptor,
            validated=False,
            readiness=INVALID,
            size_bytes=size,
            reason=(
                f"{path.name} is a {type(payload).__name__}, but a cell history "
                f"file is a JSON array of past states"
            ),
        )

    return replace(descriptor, size_bytes=size)


def build_catalog(
    *,
    cells: Sequence[Mapping[str, Any]],
    timestamp: str,
    stormcell_path: Path | str | None = None,
    input_manifest: CycleInputManifest | None = None,
    historical: bool = False,
    now: datetime | None = None,
) -> CTAMCycleCatalog:
    """Freeze the artifact set for one cycle.

    The only sources consulted are the pinned ``input_manifest``, the in-memory
    ``cells``, and ``data/cells/<id>.json`` for exactly those cells. There is no
    directory walk and no glob: see the module docstring for why a rescan would
    be a correctness bug rather than a convenience.

    ``input_manifest=None`` yields a catalog with no ``input:`` descriptors. That
    is a legal degraded state -- a caller that has not threaded the pinned
    manifest through gets a catalog that honestly advertises no inputs, not an
    exception and not an invented input set.
    """
    import util.file as fs

    cycle_time = parse_cycle_id(timestamp)
    now = _as_utc(now) if now is not None else datetime.now(timezone.utc)

    entries: list[CatalogFile] = []
    seen: set[str] = set()

    if input_manifest is not None:
        for record in input_manifest.inputs:
            if record.role not in ROLES:
                # A role outside the frozen enum is not addressable by any legal
                # selector, and emitting it would produce a schema-invalid
                # descriptor. Dropping it is the only honest option.
                continue
            descriptor = _describe_input(record, input_manifest)
            if descriptor.file_id in seen:
                # The pinned manifest is expected to hold one record per
                # (family, product, role). If it holds two, manifest order
                # decides, so the catalog is a function of the manifest rather
                # than of iteration luck.
                continue
            seen.add(descriptor.file_id)
            entries.append(descriptor)

    entries.append(
        _describe_stormcells(
            Path(stormcell_path) if stormcell_path is not None else None,
            cycle_id=timestamp,
            cycle_time=cycle_time,
            now=now,
            historical=historical,
        )
    )

    # Read the cell directory global off the module at call time rather than
    # binding it at import: util.file rebinds these 113 globals in
    # `initialize_filesystem`, and a test that redirects the data tree needs the
    # rebind to be visible here.
    cell_dir = Path(fs.CELL_DIR)
    for cell in cells:
        raw_id = cell.get("id") if isinstance(cell, Mapping) else None
        if raw_id is None or str(raw_id) == "":
            # A cell with no id has no history file to name. Emitting
            # `cell_history:None` would advertise an artifact that cannot exist
            # and would collide across every such cell, so it is skipped -- and
            # `cell_count` below still counts it, so the omission is visible as
            # a gap between `cell_count` and the descriptor count.
            continue
        cell_id = str(raw_id)
        descriptor = _describe_cell_history(
            cell_id,
            cell_dir / f"{cell_id}.json",
            cycle_time=cycle_time,
            now=now,
            historical=historical,
        )
        if descriptor.file_id in seen:
            continue
        seen.add(descriptor.file_id)
        entries.append(descriptor)

    return CTAMCycleCatalog(
        cycle_id=timestamp,
        analysis_time=_isoformat(cycle_time),
        historical=bool(historical),
        cell_count=len(cells),
        files=tuple(entries),
    )


def ctam_status_path(cycle_id: str, *, base_dir: Path | str | None = None) -> Path:
    """``<data>/ctam/cycles/<cycle_id>/status.json``.

    ``util.file`` deliberately exposes no ``CTAM_DIR``, so the directory is
    computed here from ``DATA_DIR`` rather than added to that module's global
    block.
    """
    parse_cycle_id(cycle_id)
    if base_dir is None:
        import util.file as fs

        base_dir = fs.DATA_DIR
    return Path(base_dir) / "ctam" / "cycles" / cycle_id / "status.json"


# --------------------------------------------------------------------------
# Requirement evaluation
# --------------------------------------------------------------------------


def _history_entry_count(path: Path | None) -> int | None:
    """Entries in a cell history, clamped to the maximum read window.

    The clamp is why an over-large ``min_history_entries`` is rejected at
    manifest parse time: no read can return more than ``MAX_HISTORY_WINDOW``
    entries, so counting past it could only ever satisfy a requirement the API
    would then be unable to serve.
    """
    if path is None:
        return None
    payload, reason = _json_payload(path)
    if reason is not None or not isinstance(payload, list):
        return None
    return min(len(payload), MAX_HISTORY_WINDOW)


def _qualify(
    descriptor: CatalogFile, requirement: ModuleRequirement, now: datetime
) -> tuple[str | None, str | None, float | None, float | None]:
    """``(unmet_condition, reason, observed, threshold)`` for one descriptor."""
    selector = requirement.selector

    if descriptor.readiness == INVALID:
        return (
            UNMET_NOT_VALIDATED,
            f"{selector.raw}: {descriptor.file_id} did not pass validation "
            f"({descriptor.reason})",
            None,
            None,
        )
    if descriptor.readiness == STALE:
        return (
            UNMET_ANALYSIS_TIME_MISMATCH,
            f"{selector.raw}: {descriptor.file_id} is not this cycle's artifact "
            f"({descriptor.reason})",
            None,
            None,
        )
    if descriptor.readiness in (UNAVAILABLE, PENDING):
        return (
            UNMET_MISSING,
            f"{selector.raw}: {descriptor.file_id} is {descriptor.readiness} "
            f"({descriptor.reason})",
            None,
            None,
        )

    if requirement.max_age_seconds is not None:
        threshold = float(requirement.max_age_seconds)
        age = (now - descriptor.analysis_datetime).total_seconds()
        if age > threshold:
            return (
                UNMET_MAX_AGE,
                f"{selector.raw}: {descriptor.file_id} is {age:.0f}s old, above "
                f"the declared maximum of {threshold:.0f}s",
                age,
                threshold,
            )
        return None, None, age, threshold

    if requirement.min_history_entries is not None:
        threshold = float(requirement.min_history_entries)
        count = _history_entry_count(descriptor.path)
        observed = float(0 if count is None else count)
        if observed < threshold:
            return (
                UNMET_MIN_HISTORY,
                f"{selector.raw}: {descriptor.file_id} holds {observed:.0f} "
                f"entries, below the declared minimum of {threshold:.0f}",
                observed,
                threshold,
            )
        return None, None, observed, threshold

    return None, None, None, None


def _most_informative(candidates: list[tuple[Any, ...]], index: int):
    """The candidate with the largest measured value, else the first.

    A fan-out selector produces one verdict per cell. Reporting the largest
    observation names the cell that came closest, which is what an operator
    needs to decide whether the shortfall is systemic or one new cell.
    """
    with_value = [c for c in candidates if c[index] is not None]
    if with_value:
        return max(with_value, key=lambda c: c[index])
    return candidates[0]


def _evaluate_one(
    requirement: ModuleRequirement, catalog: CTAMCycleCatalog, now: datetime
) -> dict[str, Any]:
    selector = requirement.selector
    matches = catalog.resolve(selector)
    entry: dict[str, Any] = {
        "selector": selector.raw,
        "required": requirement.required,
        "satisfied": False,
        "reason": None,
        "file_ids": [m.file_id for m in matches],
        "unmet_condition": None,
        "observed": None,
        "threshold": None,
    }

    if not matches:
        wrong_role = sorted(
            {
                e.role
                for e in catalog.files
                if e.kind == KIND_INPUT
                and e.family == selector.family
                and e.product == selector.product
            }
        )
        if selector.kind == KIND_INPUT and wrong_role:
            entry["unmet_condition"] = UNMET_ROLE_MISMATCH
            entry["reason"] = (
                f"{selector.raw}: the pinned cycle staged {selector.product} as "
                f"{', '.join(wrong_role)}, not {selector.role}"
            )
        else:
            entry["unmet_condition"] = UNMET_MISSING
            entry["reason"] = (
                f"{selector.raw}: no catalog entry matched, so nothing was staged "
                f"for it in cycle {catalog.cycle_id}"
            )
        return entry

    qualifying: list[tuple[Any, ...]] = []
    failures: list[tuple[Any, ...]] = []
    for descriptor in matches:
        condition, reason, observed, threshold = _qualify(descriptor, requirement, now)
        if condition is None:
            qualifying.append((descriptor, observed, threshold))
        else:
            failures.append((descriptor, condition, reason, observed, threshold))

    if qualifying:
        # One qualifying descriptor is enough. For `stormcells.current` and an
        # `input:` selector there is at most one anyway; for the `cells.history`
        # fan-out, requiring every cell would mean a single brand-new cell could
        # never be satisfied and would block the module for the whole fleet.
        _descriptor, observed, threshold = _most_informative(qualifying, 1)
        entry["satisfied"] = True
        entry["observed"] = observed
        entry["threshold"] = threshold
        return entry

    _descriptor, condition, reason, observed, threshold = _most_informative(failures, 3)
    entry["unmet_condition"] = condition
    entry["reason"] = reason
    entry["observed"] = observed
    entry["threshold"] = threshold
    return entry


def evaluate_requirements(
    manifest: ModuleManifest,
    catalog: CTAMCycleCatalog,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate every ``[[requires]]`` block against a frozen catalog.

    Every declared selector appears in the result whether or not it was met, in
    manifest order, so an unsatisfied optional input can never masquerade as a
    ready one. Only an unsatisfied ``required = true`` entry clears the
    top-level ``satisfied``.
    """
    now = _as_utc(now) if now is not None else datetime.now(timezone.utc)

    requirements: list[dict[str, Any]] = []
    satisfied = True
    for requirement in manifest.requires:
        evaluated = _evaluate_one(requirement, catalog, now)
        requirements.append(evaluated)
        if requirement.required and not evaluated["satisfied"]:
            satisfied = False

    return {
        "module_id": manifest.module_id,
        "evaluated_at": _isoformat(now),
        "satisfied": satisfied,
        "requirements": requirements,
    }


# --------------------------------------------------------------------------
# Status record
# --------------------------------------------------------------------------


def _one_line(text: str) -> str:
    """Collapse whitespace so a reason cannot smuggle in a newline.

    ``\\S``-patterned schema fields are matched with ``re.search``, and Python's
    ``$``-style anchoring accepts a trailing newline
    (plans/modular-ctam-phase0-findings.md finding 9), so newlines are removed
    at the point of writing rather than trusted to a pattern.
    """
    return " ".join(str(text).split())


def _module_entry(
    module: DiscoveredModule, evaluation: Mapping[str, Any] | None, state: str | None
) -> dict[str, Any]:
    manifest = module.manifest

    if state is None:
        if module.state == STATE_INVALID:
            state = STATE_INVALID
        elif module.state == STATE_SKIPPED_DISABLED:
            state = STATE_SKIPPED_DISABLED
        elif evaluation is None:
            state = STATE_DISCOVERED
        elif evaluation.get("satisfied"):
            state = MODULE_STATE_READY
        else:
            state = MODULE_STATE_SKIPPED_MISSING_REQUIREMENTS
    if state not in MODULE_STATES:
        raise ValueError(
            f"module state {state!r} is not one of the frozen states "
            f"{MODULE_STATES}"
        )

    entry: dict[str, Any] = {
        "state": state,
        # A module whose manifest would not parse has no version and no declared
        # api_version. The frozen pattern forbids an empty string, so the only
        # legal answer is the schema's own null, which its description names
        # explicitly: "Null when the manifest could not be parsed."
        "version": manifest.version if manifest is not None else None,
        "api_version": manifest.api_version if manifest is not None else None,
        # Never launched in Phase 1, and the schema's null means exactly that.
        "duration_ms": None,
        "requirements_satisfied": (
            bool(evaluation["satisfied"]) if evaluation is not None else None
        ),
    }

    if evaluation is not None:
        unmet = [
            _one_line(item["selector"])
            for item in evaluation.get("requirements", ())
            if item.get("required") and not item.get("satisfied")
        ]
        if unmet:
            entry["unmet_requirements"] = unmet

    if module.module_id in RESERVED_MODULE_IDS:
        entry["reserved"] = True

    if module.reason:
        # The plan requires discovery to publish "an actionable path and reason".
        # The schema's redaction rule targets module-supplied content -- bearer
        # tokens, raw stdout -- and this text is host-authored: it is the
        # operator's only pointer to which installed directory to fix.
        entry["error"] = _one_line(module.reason)

    return entry


def cycle_status(
    *,
    catalog: CTAMCycleCatalog,
    discovery: DiscoveryResult | Iterable[DiscoveredModule],
    evaluations: Mapping[str, Mapping[str, Any]] | None = None,
    state: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    ctam_ready: bool | None = None,
    module_states: Mapping[str, str] | None = None,
    publication: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a record validating against ``status-record.schema.json``.

    Discovery is passed in rather than run here: the status record reports what a
    cycle decided, and a second scan could disagree with the one the cycle acted
    on. Every discovered module appears in ``modules``, including invalid and
    disabled ones -- a module that vanishes silently is the bug this record
    exists to make impossible.
    """
    if state not in CYCLE_STATES:
        raise ValueError(
            f"cycle state {state!r} is not one of the frozen states {CYCLE_STATES}"
        )

    modules_seq = (
        discovery.modules if isinstance(discovery, DiscoveryResult) else tuple(discovery)
    )
    evaluations = dict(evaluations or {})
    module_states = dict(module_states or {})

    modules: dict[str, Any] = {}
    for module in modules_seq:
        modules[module.module_id] = _module_entry(
            module,
            evaluations.get(module.module_id),
            module_states.get(module.module_id),
        )

    record: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "cycle_id": catalog.cycle_id,
        "analysis_time": catalog.analysis_time,
        "api_version": API_VERSION,
        "state": state,
        "started_at": _isoformat(started_at),
        "finished_at": _isoformat(finished_at) if finished_at is not None else None,
        "historical": catalog.historical,
        "cell_count": catalog.cell_count,
        "modules": modules,
    }
    if ctam_ready is not None:
        record["ctam_ready"] = bool(ctam_ready)
    if publication is not None:
        record["publication"] = dict(publication)
    return record


def write_cycle_status(
    status: Mapping[str, Any], *, base_dir: Path | str | None = None
) -> Path:
    """Persist a status record atomically and return where it landed.

    ``base_dir`` is the data directory, defaulting to ``util.file.DATA_DIR``.
    The write goes through ``util.atomic.atomic_write_json`` because a truncated
    status record is worse than none: a reader cannot tell a half-written cycle
    from a cycle that genuinely reported nothing.
    """
    destination = ctam_status_path(str(status["cycle_id"]), base_dir=base_dir)
    atomic_write_json(destination, dict(status), indent=2)
    return destination
