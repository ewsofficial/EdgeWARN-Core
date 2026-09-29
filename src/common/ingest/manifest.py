"""Immutable, serializable records for one coherent pipeline input set."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from common.ingest.synoptic.config import get_rap_max_age_minutes


_MRMS_TIMESTAMP_RE = re.compile(r"(\d{8})[-_](\d{6})")
_GOES_TIMESTAMP_RE = re.compile(r"s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)")


def _default_rap_max_age_seconds() -> float:
    """The staging window and the validation ceiling are one setting.

    `downloader.py:248` walks back through analysis hours until this budget is
    exhausted, so a raised budget stages an older file on purpose. A ceiling
    pinned here independently would then reject that file as out of range,
    turning a widened budget into a cycle failure.
    """
    return get_rap_max_age_minutes() * 60.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_file_analysis_time(path: str | Path) -> datetime | None:
    """Parse an encoded MRMS/GOES analysis time without consulting mtime."""
    name = Path(path).name

    goes_match = _GOES_TIMESTAMP_RE.search(name)
    if goes_match is not None:
        year, day_of_year, hour, minute, second, _subsecond = map(
            int, goes_match.groups()
        )
        try:
            return datetime.strptime(
                f"{year}{day_of_year:03d}{hour:02d}{minute:02d}{second:02d}",
                "%Y%j%H%M%S",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    mrms_match = _MRMS_TIMESTAMP_RE.search(name)
    if mrms_match is not None:
        try:
            return datetime.strptime(
                "".join(mrms_match.groups()), "%Y%m%d%H%M%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    return None


@dataclass(frozen=True)
class StagedInput:
    """One validated local input selected for a cycle."""

    product: str
    path: str
    analysis_time: datetime
    source: str
    family: str
    validated: bool = True
    role: str = "current"

    def __post_init__(self):
        object.__setattr__(self, "product", str(self.product))
        object.__setattr__(self, "path", str(Path(self.path).resolve()))
        object.__setattr__(self, "analysis_time", _as_utc(self.analysis_time))
        object.__setattr__(self, "source", str(self.source))
        object.__setattr__(self, "family", str(self.family).lower())
        object.__setattr__(self, "validated", bool(self.validated))
        object.__setattr__(self, "role", str(self.role).lower())

    @property
    def local_path(self) -> Path:
        return Path(self.path)

    def as_dict(self) -> dict:
        return {
            "product": self.product,
            "path": self.path,
            "analysis_time": self.analysis_time.isoformat(),
            "source": self.source,
            "family": self.family,
            "validated": self.validated,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "StagedInput":
        return cls(
            product=payload["product"],
            path=payload["path"],
            analysis_time=datetime.fromisoformat(payload["analysis_time"]),
            source=payload["source"],
            family=payload["family"],
            validated=bool(payload.get("validated", False)),
            role=payload.get("role", "current"),
        )


def staged_input_from_path(
    product: str,
    path: str | Path,
    *,
    source: str,
    family: str,
    analysis_time: datetime | None = None,
    validated: bool = True,
    role: str = "current",
) -> StagedInput:
    parsed_time = analysis_time or parse_file_analysis_time(path)
    if parsed_time is None:
        raise ValueError(f"Could not parse analysis timestamp from staged input: {path}")
    return StagedInput(
        product=product,
        path=str(path),
        analysis_time=parsed_time,
        source=source,
        family=family,
        validated=validated,
        role=role,
    )


@dataclass(frozen=True)
class CycleInputManifest:
    """Exact, timestamp-validated inputs owned by one requested cycle."""

    cycle_time: datetime
    inputs: tuple[StagedInput, ...] = ()
    # MRMS product timestamps can lag the requested scan by a little over two
    # minutes while the source finishes publication.  Three minutes preserves
    # strict per-scan selection without rejecting a normal operational lag.
    mrms_tolerance_seconds: float = 180.0
    goes_tolerance_seconds: float = 1200.0
    rap_max_age_seconds: float = field(default_factory=_default_rap_max_age_seconds)

    def __post_init__(self):
        object.__setattr__(self, "cycle_time", _as_utc(self.cycle_time))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    def with_inputs(self, records: Iterable[StagedInput]) -> "CycleInputManifest":
        return CycleInputManifest(
            cycle_time=self.cycle_time,
            inputs=(*self.inputs, *tuple(records)),
            mrms_tolerance_seconds=self.mrms_tolerance_seconds,
            goes_tolerance_seconds=self.goes_tolerance_seconds,
            rap_max_age_seconds=self.rap_max_age_seconds,
        )

    def current_inputs(self, *, family: str | None = None) -> tuple[StagedInput, ...]:
        return tuple(
            record
            for record in self.inputs
            if record.role == "current"
            and (family is None or record.family == family.lower())
        )

    def records_for_product(self, product: str) -> tuple[StagedInput, ...]:
        records = [record for record in self.inputs if record.product == product]
        records.sort(key=lambda record: record.analysis_time)
        return tuple(records)

    def latest_for_product(self, product: str) -> StagedInput | None:
        records = tuple(r for r in self.records_for_product(product) if r.role == "current")
        return records[-1] if records else None

    def latest_for_directory(self, directory: str | Path) -> StagedInput | None:
        target = Path(directory).resolve()
        records = [
            record
            for record in self.inputs
            if record.role == "current" and record.local_path.parent.resolve() == target
        ]
        if not records:
            return None
        return max(records, key=lambda record: record.analysis_time)

    def validate_alignment(self) -> tuple[str, ...]:
        """Return errors for invalid or cross-time current inputs."""
        errors = []
        for record in self.inputs:
            if not record.validated:
                errors.append(f"{record.product}: staged validation failed")
                continue
            if not record.local_path.is_file():
                errors.append(
                    f"{record.product}: staged path is not a file: {record.path}"
                )
                continue
            if record.role != "current":
                continue

            delta_seconds = (record.analysis_time - self.cycle_time).total_seconds()
            if record.family == "mrms":
                # FLASH products are published on a coarser operational
                # cadence than the two-minute scan target.  Their encoded
                # analysis time is still authoritative; accepting the known
                # cadence window prevents a coherent cycle from being
                # rejected solely because it contains a valid FLASH field.
                tolerance_seconds = (
                    900.0
                    if record.product.startswith("FLASH_")
                    # Merged RhoHV is a lower-cadence quality field and can
                    # legitimately trail the common scan by several minutes.
                    else 300.0
                    if record.product == "MergedRhoHV_00.50"
                    else self.mrms_tolerance_seconds
                )
                # Source publication can lag a requested scan, but a future
                # MRMS analysis must remain within the normal two-minute
                # scan window.  This keeps the tolerance directional rather
                # than silently accepting a later, incoherent frame.
                if (
                    delta_seconds > min(120.0, self.mrms_tolerance_seconds)
                    or delta_seconds < -tolerance_seconds
                ):
                    errors.append(
                        f"{record.product}: analysis {record.analysis_time.isoformat()} "
                        f"is {delta_seconds:+.0f}s from cycle"
                    )
            elif record.family == "goes":
                if abs(delta_seconds) > self.goes_tolerance_seconds:
                    errors.append(
                        f"{record.product}: analysis {record.analysis_time.isoformat()} "
                        f"is {delta_seconds:+.0f}s from cycle"
                    )
            elif record.family == "rap":
                age_seconds = -delta_seconds
                if age_seconds < 0 or age_seconds > self.rap_max_age_seconds:
                    errors.append(
                        f"{record.product}: analysis age {age_seconds:.0f}s is outside "
                        f"0..{self.rap_max_age_seconds:.0f}s"
                    )
        return tuple(errors)

    def as_dict(self) -> dict:
        return {
            "cycle_time": self.cycle_time.isoformat(),
            "inputs": [record.as_dict() for record in self.inputs],
            "tolerances": {
                "mrms_seconds": self.mrms_tolerance_seconds,
                "goes_seconds": self.goes_tolerance_seconds,
                "rap_max_age_seconds": self.rap_max_age_seconds,
            },
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> "CycleInputManifest | None":
        if not payload:
            return None
        tolerances = payload.get("tolerances", {})
        return cls(
            cycle_time=datetime.fromisoformat(payload["cycle_time"]),
            inputs=tuple(
                StagedInput.from_dict(record)
                for record in payload.get("inputs", ())
            ),
            mrms_tolerance_seconds=float(tolerances.get("mrms_seconds", 180.0)),
            goes_tolerance_seconds=float(tolerances.get("goes_seconds", 1200.0)),
            rap_max_age_seconds=float(
                tolerances.get(
                    "rap_max_age_seconds", _default_rap_max_age_seconds()
                )
            ),
        )
