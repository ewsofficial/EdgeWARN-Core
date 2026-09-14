"""Canonical realtime service-name registry and heartbeat contract.

Phase 0 of the realtime runner decomposition
(plans/realtime-runner-decomposition-plan.md): the three decomposed services are
published under exactly one canonical name each, and every service publishes an
atomic heartbeat beneath ``<BASE_DIR>/state/realtime/services/<name>.json``.

The filenames under that directory are the registry. Accessory loops (METAR,
NWS, WPC, GOES ABI) are deliberately absent: they surface as child entries
inside their owning service's heartbeat, never as top-level names.

This module is schema only. Producers write heartbeats; the unified Node API is
the consumer that classifies them. Python services never read heartbeats for
correctness -- correctness uses committed phase records and checkpoints.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from util.atomic import atomic_write_json

#: Canonical service names. The single-instance locks, heartbeats, leases, and
#: API discovery all key off these strings; nothing else may appear under the
#: services directory.
CANONICAL_SERVICE_NAMES: tuple[str, ...] = ("edgewarn", "ewmrs", "nexrad")

HEARTBEAT_SCHEMA_VERSION = 1

#: Route-family-to-service dependency map. Each public route family declares
#: exactly one required service; legacy adapters inherit the requirement of the
#: v3 family they adapt.
ROUTE_SERVICE_REQUIREMENTS: dict[str, str] = {
    "/api/v3/cells": "edgewarn",
    "/api/v3/storm-snapshots": "edgewarn",
    "/api/v3/alert-snapshots": "edgewarn",
    "/api/v3/alerts": "edgewarn",
    "/api/v3/render-products": "ewmrs",
    "/api/v3/models/rap": "ewmrs",
    "/api/v3/analyses/wpc": "ewmrs",
    "/api/v3/radar-sites": "nexrad",
    "/renders": "ewmrs",
    "/wpc": "ewmrs",
    "/rap": "ewmrs",
    "/nexrad": "nexrad",
}

#: Heartbeat states reported by :func:`classify_heartbeat_state`.
SERVICE_STATES = ("active", "stale", "disabled", "degraded", "unsupported-schema")


class HeartbeatSchemaError(ValueError):
    """A heartbeat file does not match the supported schema."""


def services_dir(base_dir: str | os.PathLike) -> Path:
    """Directory holding one heartbeat file per canonical service."""
    return Path(base_dir) / "state" / "realtime" / "services"


def heartbeat_path(base_dir: str | os.PathLike, name: str) -> Path:
    """Registry path for *name*'s heartbeat; rejects non-canonical names."""
    if name not in CANONICAL_SERVICE_NAMES:
        raise ValueError(
            f"{name!r} is not a canonical service name "
            f"(expected one of {', '.join(CANONICAL_SERVICE_NAMES)})"
        )
    return services_dir(base_dir) / f"{name}.json"


def required_service_for_route(route_path: str) -> str | None:
    """Return the canonical service a request path depends on, or ``None``.

    Longest-prefix matching so ``/api/v3/cells/ABC123`` resolves to the same
    family as ``/api/v3/cells``.
    """
    best: tuple[int, str] | None = None
    for prefix, service in ROUTE_SERVICE_REQUIREMENTS.items():
        if route_path == prefix or route_path.startswith(prefix + "/"):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), service)
    return best[1] if best else None


def _parse_utc_datetime(value, dotted: str) -> datetime:
    if not isinstance(value, str):
        raise HeartbeatSchemaError(f"{dotted} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HeartbeatSchemaError(f"{dotted}: {value!r} is not ISO-8601") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ServiceHeartbeat:
    """One atomic service heartbeat record."""

    service: str
    pid: int
    run_id: str
    updated_at: datetime
    phase: str = "unknown"
    version: str | None = None
    last_successful_activity: datetime | None = None
    degraded_children: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.service not in CANONICAL_SERVICE_NAMES:
            raise HeartbeatSchemaError(
                f"service {self.service!r} is not a canonical service name"
            )
        if not isinstance(self.pid, int) or self.pid <= 0:
            raise HeartbeatSchemaError("pid must be a positive integer")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "service": self.service,
            "pid": self.pid,
            "run_id": self.run_id,
            "updated_at": self.updated_at.isoformat(),
            "phase": self.phase,
            "version": self.version,
            "last_successful_activity": (
                self.last_successful_activity.isoformat()
                if self.last_successful_activity is not None
                else None
            ),
            "degraded_children": list(self.degraded_children),
        }

    @classmethod
    def from_dict(cls, payload) -> "ServiceHeartbeat":
        if not isinstance(payload, dict):
            raise HeartbeatSchemaError("heartbeat must be a JSON object")
        try:
            schema_version = payload["schema_version"]
        except KeyError:
            raise HeartbeatSchemaError("schema_version is required") from None
        if schema_version != HEARTBEAT_SCHEMA_VERSION:
            raise HeartbeatSchemaError(
                f"unsupported schema_version {schema_version!r}; "
                f"this build supports {HEARTBEAT_SCHEMA_VERSION}"
            )
        for required in ("service", "pid", "run_id", "updated_at"):
            if required not in payload:
                raise HeartbeatSchemaError(f"{required} is required")
        degraded = payload.get("degraded_children") or []
        if not isinstance(degraded, list) or not all(isinstance(c, str) for c in degraded):
            raise HeartbeatSchemaError("degraded_children must be a list of names")
        activity = payload.get("last_successful_activity")
        return cls(
            service=payload["service"],
            pid=payload["pid"],
            run_id=payload["run_id"],
            updated_at=_parse_utc_datetime(payload["updated_at"], "updated_at"),
            phase=payload.get("phase") or "unknown",
            version=payload.get("version"),
            last_successful_activity=(
                _parse_utc_datetime(activity, "last_successful_activity")
                if activity is not None
                else None
            ),
            degraded_children=tuple(degraded),
        )


def write_heartbeat(heartbeat: ServiceHeartbeat, destination: str | os.PathLike) -> Path:
    """Atomically publish a heartbeat to its sibling-temp + replace commit point."""
    return atomic_write_json(destination, heartbeat.as_dict())


def read_heartbeat_file(path: str | os.PathLike) -> ServiceHeartbeat | None:
    """Parse a heartbeat file; ``None`` for missing/unreadable/mismatched files."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        return ServiceHeartbeat.from_dict(payload)
    except HeartbeatSchemaError:
        return None


def classify_heartbeat_state(
    path: str | os.PathLike,
    *,
    stale_after_seconds: float,
    now: datetime | None = None,
) -> tuple[str, ServiceHeartbeat | None]:
    """Classify one registry file into a service state.

    Returns ``(state, heartbeat)`` where *heartbeat* is parsed whenever the file
    exists and matches the supported schema:

    - ``disabled``: no heartbeat file (never started or intentionally omitted).
    - ``unsupported-schema``: file exists but fails schema validation.
    - ``active`` / ``degraded``: fresh record; ``degraded`` additionally reports
      degraded children. Degraded services still serve requests.
    - ``stale``: ``updated_at`` exceeds *stale_after_seconds* -- crashed, hung,
      or killed without cleanup.
    """
    path = Path(path)
    if not path.exists():
        return ("disabled", None)
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        beat = ServiceHeartbeat.from_dict(payload)
    except (OSError, json.JSONDecodeError, HeartbeatSchemaError):
        return ("unsupported-schema", None)

    reference = now if now is not None else datetime.now(timezone.utc)
    age = (reference - beat.updated_at).total_seconds()
    if age < -stale_after_seconds:
        # Clock skew must not make a stale record look healthy forever.
        return ("stale", beat)
    if age > stale_after_seconds:
        return ("stale", beat)
    if beat.degraded_children:
        return ("degraded", beat)
    return ("active", beat)


def scan_service_states(
    base_dir: str | os.PathLike,
    *,
    stale_after_seconds: float,
    now: datetime | None = None,
) -> dict[str, tuple[str, ServiceHeartbeat | None]]:
    """Classify every canonical service name against one base directory."""
    return {
        name: classify_heartbeat_state(
            heartbeat_path(base_dir, name),
            stale_after_seconds=stale_after_seconds,
            now=now,
        )
        for name in CANONICAL_SERVICE_NAMES
    }
