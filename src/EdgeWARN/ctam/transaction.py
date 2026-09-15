"""Cycle-local CTAM mutations with one strict ownership gate.

The host owns the working set.  Modules can only stage JSON Patch operations
against their declared namespace; this module deliberately has no HTTP or
filesystem dependency so every caller uses the same rules.
"""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .manifest import ModuleManifest, PUBLIC_ROUTE_ID_PATTERN
from .limits import MAX_PUBLIC_ROUTE_DEPTH, MAX_PUBLIC_ROUTE_PAYLOAD_BYTES, MAX_PUBLIC_ROUTE_TOTAL_BYTES
from .api.models import APIError

_PATCH_OPS = frozenset({"add", "replace", "test"})
_RESERVED_MODULE_KEYS = frozenset({"stormprob", "_grid_outputs"})


def _segments(pointer: Any) -> tuple[str, ...]:
    """Parse a RFC 6901 pointer once, without path normalization."""
    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        raise APIError("forbidden_path", "patch path must be a non-root JSON Pointer", 403)
    raw = pointer[1:].split("/")
    if not raw or any(not segment for segment in raw):
        raise APIError("forbidden_path", "patch path cannot address a container or trailing segment", 403, pointer=pointer)
    decoded: list[str] = []
    for segment in raw:
        index = 0
        value = ""
        while index < len(segment):
            if segment[index] != "~":
                value += segment[index]; index += 1; continue
            if index + 1 >= len(segment) or segment[index + 1] not in "01":
                raise APIError("invalid_patch", "patch path has an invalid JSON Pointer escape", 400, pointer=pointer)
            value += "~" if segment[index + 1] == "0" else "/"
            index += 2
        decoded.append(value)
    return tuple(decoded)


def validate_patch_path(manifest: ModuleManifest, pointer: Any) -> tuple[str, ...]:
    """Return allowed decoded segments or fail closed.

    The first two segments are a positive allowlist.  In particular ``..`` is
    not normalized: it is only a literal nested key inside the caller's own
    module value, never a route to a core field.
    """
    segments = _segments(pointer)
    if len(segments) < 2 or segments[0] not in {"modules", "properties"}:
        raise APIError("forbidden_path", "patches may only target modules or properties owned by the caller", 403, pointer=pointer)
    container, key = segments[:2]
    # These are legal JSON Pointer values but are not useful CTAM output keys.
    # Rejecting them avoids any opportunity for a later adapter to normalize a
    # nested object path differently from this authoritative gate.
    if any(segment in {".", "..", "~"} for segment in segments[2:]):
        raise APIError("forbidden_path", "patch path contains a reserved nested key", 403, pointer=pointer)
    if container == "modules":
        if key.casefold() in _RESERVED_MODULE_KEYS or key != manifest.name:
            raise APIError("forbidden_path", "module namespace is not owned by the caller", 403, pointer=pointer)
        return segments
    allowed = set()
    for grant in manifest.writes:
        if grant.resource != "stormcells.current":
            continue
        prefix = "/features/*/properties/"
        if grant.json_pointer.startswith(prefix):
            allowed.add(grant.json_pointer[len(prefix):])
    if key not in allowed or not key.startswith(f"{manifest.module_id}_"):
        raise APIError("forbidden_path", "property key is not a declared caller-owned property", 403, pointer=pointer)
    if len(segments) != 2:
        raise APIError("forbidden_path", "properties patches may only replace one declared scalar", 403, pointer=pointer)
    return segments


def _declares_write(manifest: ModuleManifest, resource: str, segments: tuple[str, ...]) -> bool:
    """Match the entry-local path against the manifest's fixed resource prefix."""
    prefix = "/features/*/" if resource == "stormcells.current" else "/*/"
    wanted = prefix + "/".join(segments)
    return any(grant.resource == resource and grant.json_pointer == wanted for grant in manifest.writes)


def _json_safe(value: Any) -> None:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise APIError("invalid_patch", "patch value must be finite JSON data", 400) from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise APIError("request_too_large", "patch value exceeds the CTAM API limit", 413)


def _json_payload(value: Any) -> tuple[Any, int]:
    def depth(item: Any) -> int:
        if isinstance(item, Mapping):
            return 1 + max((depth(child) for child in item.values()), default=0)
        if isinstance(item, (list, tuple)):
            return 1 + max((depth(child) for child in item), default=0)
        return 0
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise APIError("invalid_patch", "route payload must be finite JSON data", 400) from exc
    if depth(value) > MAX_PUBLIC_ROUTE_DEPTH:
        raise APIError("invalid_patch", "route payload exceeds the maximum JSON nesting depth", 400)
    if len(encoded) > MAX_PUBLIC_ROUTE_PAYLOAD_BYTES:
        raise APIError("request_too_large", "route payload exceeds the per-route limit", 413, limit=MAX_PUBLIC_ROUTE_PAYLOAD_BYTES)
    return deepcopy(value), len(encoded)


def _resolve_parent(cell: dict[str, Any], segments: tuple[str, ...], *, create: bool) -> tuple[dict[str, Any], str]:
    container = segments[0]
    if container not in cell:
        if not create:
            raise APIError("invalid_patch", "patch target does not exist", 400)
        cell[container] = {}
    if not isinstance(cell[container], dict):
        raise APIError("invalid_patch", "patch target container has invalid shape", 400)
    current = cell[container]
    for segment in segments[1:-1]:
        if segment not in current:
            if not create:
                raise APIError("invalid_patch", "patch target does not exist", 400)
            current[segment] = {}
        if not isinstance(current[segment], dict):
            raise APIError("invalid_patch", "patch target parent is not an object", 400)
        current = current[segment]
    return current, segments[-1]


@dataclass
class ModuleTransaction:
    module_id: str
    transaction_id: str = field(default_factory=lambda: f"txn_{uuid.uuid4().hex}")
    staged_cells: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    staged_history: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    staged_routes: dict[str, Any] = field(default_factory=dict)
    sealed: bool = False
    abandoned: bool = False
    commit_result: dict[str, Any] | None = None


class CTAMTransactionService:
    """Revisioned in-memory working set for exactly one CTAM cycle."""
    def __init__(self, *, cells: Sequence[Mapping[str, Any]], histories: Mapping[str, Sequence[Mapping[str, Any]]] | None = None, manifests: Mapping[str, ModuleManifest]) -> None:
        # The loopback server is a ThreadingHTTPServer, so two modules writing
        # at the same time can interleave inside any service call. Every
        # working-set mutation therefore runs under this lock: without it,
        # commit()'s validate -> deepcopy -> assign sequence can drop a
        # concurrent module's committed namespace entirely.
        self._lock = threading.RLock()
        self.cells = {str(c["id"]): deepcopy(dict(c)) for c in cells if c.get("id") is not None}
        self.histories = {str(key): deepcopy(list(value)) for key, value in (histories or {}).items()}
        self.manifests = dict(manifests)
        self.cell_revisions = {key: 0 for key in self.cells}
        self.history_revisions = {key: 0 for key in self.histories}
        self.transactions = {key: ModuleTransaction(key) for key in self.manifests}

    def _transaction(self, module_id: str) -> ModuleTransaction:
        if module_id not in self.transactions:
            raise APIError("authentication_failed", "module is not admitted for this cycle", 401)
        return self.transactions[module_id]

    @staticmethod
    def _require_open(tx: ModuleTransaction) -> None:
        if tx.sealed or tx.abandoned:
            raise APIError("transaction_sealed", "transaction is already sealed or abandoned", 409)

    def stage_cell(self, module_id: str, cell_id: str, *, revision: int, operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        with self._lock:
            tx = self._transaction(module_id)
            self._require_open(tx)
            if cell_id not in self.cells: raise APIError("not_found", "storm cell was not found", 404)
            observed = self.cell_revisions[cell_id]
            if revision != observed: raise APIError("stale_revision", "storm cell revision is stale", 409, expected_revision=revision, observed_revision=observed)
            self._stage(self.manifests[module_id], tx.staged_cells.setdefault(cell_id, []), operations, resource="stormcells.current", existing=self.cells[cell_id])
            return {"revision": observed, "staged_operations": len(tx.staged_cells[cell_id])}

    def stage_history(self, module_id: str, cell_id: str, timestamp: str, *, revision: int, operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        with self._lock:
            tx = self._transaction(module_id)
            self._require_open(tx)
            history = self.histories.get(cell_id)
            if history is None: raise APIError("not_found", "cell history was not found", 404)
            observed = self.history_revisions[cell_id]
            if revision != observed: raise APIError("stale_revision", "cell history revision is stale", 409, expected_revision=revision, observed_revision=observed)
            entry = next((item for item in history if str(item.get("timestamp")) == timestamp), None)
            if entry is None: raise APIError("not_found", "history timestamp was not found", 404)
            key = (cell_id, timestamp)
            self._stage(self.manifests[module_id], tx.staged_history.setdefault(key, []), operations, resource="cells.history", existing=entry)
            return {"revision": observed, "staged_operations": len(tx.staged_history[key])}

    def _stage(self, manifest, destination, operations, *, resource, existing):
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)) or not operations:
            raise APIError("invalid_patch", "operations must be a non-empty array", 400)
        for operation in operations:
            if not isinstance(operation, Mapping) or operation.get("op") not in _PATCH_OPS:
                raise APIError("invalid_patch", "only add, replace, and test operations are supported", 400)
            segments = validate_patch_path(manifest, operation.get("path"))
            if not _declares_write(manifest, resource, segments):
                raise APIError("forbidden_path", "patch path is not declared for this resource", 403, pointer=operation.get("path"))
            if operation["op"] in {"add", "replace", "test"} and "value" not in operation:
                raise APIError("invalid_patch", "patch operation requires value", 400)
            if "value" in operation: _json_safe(operation["value"])
            if segments[0] == "properties" and segments[1] in existing.get("properties", {}):
                # A pre-existing key is host-owned unless this module owns it
                # (same rule as manifest admission: exactly module_id or
                # module_id + "_"). Owned keys stay rewritable across cycles;
                # anything else is never grantable, so it stays frozen.
                owned = segments[1] == manifest.module_id or segments[1].startswith(f"{manifest.module_id}_")
                if not owned:
                    raise APIError("forbidden_path", "modules cannot overwrite host-owned properties", 403, pointer=operation.get("path"))
            destination.append(deepcopy(dict(operation)))

    def stage_alert(self, module_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            tx = self._transaction(module_id)
            self._require_open(tx)
            if not isinstance(payload, Mapping) or payload.get("source") != self.manifests[module_id].name:
                raise APIError("invalid_patch", "alert source must be the caller's display name", 400)
            if str(payload.get("cell_id")) not in self.cells or not payload.get("id") or not payload.get("geometry"):
                raise APIError("invalid_patch", "alert must identify an active cell, id, and geometry", 400)
            _json_safe(dict(payload)); tx.alerts.append(deepcopy(dict(payload)))
            return {"staged_alerts": len(tx.alerts)}

    def stage_route(self, module_id: str, route_id: str, payload: Any) -> dict[str, Any]:
        with self._lock:
            tx = self._transaction(module_id)
            self._require_open(tx)
            if not isinstance(route_id, str) or re.fullmatch(PUBLIC_ROUTE_ID_PATTERN, route_id) is None:
                raise APIError("invalid_patch", "route id must be one safe decoded path segment", 400)
            declared = {route.route_id for route in self.manifests[module_id].public_routes}
            if route_id not in declared:
                raise APIError("route_not_declared", "route is not declared by the caller's manifest", 403)
            candidate, byte_count = _json_payload(payload)
            existing_bytes = sum(len(json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")) for key, value in tx.staged_routes.items() if key != route_id)
            if existing_bytes + byte_count > MAX_PUBLIC_ROUTE_TOTAL_BYTES:
                raise APIError("request_too_large", "staged routes exceed the per-module aggregate limit", 413, limit=MAX_PUBLIC_ROUTE_TOTAL_BYTES)
            tx.staged_routes[route_id] = candidate
            return {"route_id": route_id, "bytes": byte_count}

    def transaction(self, module_id: str) -> dict[str, Any]:
        with self._lock:
            return self._transaction_snapshot(module_id)

    def _transaction_snapshot(self, module_id: str) -> dict[str, Any]:
        tx = self._transaction(module_id)
        cell_ops = sum(map(len, tx.staged_cells.values()))
        history_ops = sum(map(len, tx.staged_history.values()))
        state = "sealed" if tx.sealed else "abandoned" if tx.abandoned else "open"
        route_bytes = sum(len(json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")) for value in tx.staged_routes.values())
        return {"transaction_id": tx.transaction_id, "module_id": module_id, "state": state, "staged": {"stormcell_operations": cell_ops, "history_operations": history_ops, "alerts": len(tx.alerts), "routes": len(tx.staged_routes), "route_bytes": route_bytes, "cells_touched": sorted({int(key) if key.isdigit() else key for key in (*tx.staged_cells, *(key for key, _ in tx.staged_history))}, key=str), "bytes": len(json.dumps([*tx.staged_cells.values(), *tx.staged_history.values(), tx.alerts], allow_nan=False, default=str).encode()) + route_bytes}, "conflicts": [], "commit_id": tx.commit_result.get("commit_id") if tx.commit_result else None, "idempotency_key": tx.commit_result.get("idempotency_key") if tx.commit_result else None, "revisions": {"stormcells.current": max(self.cell_revisions.values(), default=0), "cells.history": max(self.history_revisions.values(), default=0)}}

    def abandon(self, module_id: str) -> dict[str, Any]:
        with self._lock:
            tx = self._transaction(module_id)
            if tx.sealed or tx.abandoned:
                raise APIError("transaction_sealed", "transaction is already sealed", 409)
            tx.staged_cells.clear(); tx.staged_history.clear(); tx.alerts.clear(); tx.staged_routes.clear(); tx.abandoned = True
            return self._transaction_snapshot(module_id)

    def validate(self, module_id: str) -> dict[str, Any]:
        with self._lock:
            tx = self._transaction(module_id)
            for cell_id, operations in tx.staged_cells.items(): self._apply(self.cells[cell_id], operations)
            for (cell_id, timestamp), operations in tx.staged_history.items():
                entry = next(item for item in self.histories[cell_id] if str(item.get("timestamp")) == timestamp)
                self._apply(entry, operations)
            return self._transaction_snapshot(module_id)

    def commit(self, module_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            tx = self._transaction(module_id)
            if tx.sealed: return self._transaction_snapshot(module_id)
            self.validate(module_id)
            for cell_id, operations in tx.staged_cells.items():
                self.cells[cell_id] = self._apply(self.cells[cell_id], operations); self.cell_revisions[cell_id] += 1
            for (cell_id, timestamp), operations in tx.staged_history.items():
                history = self.histories[cell_id]
                index = next(index for index, item in enumerate(history) if str(item.get("timestamp")) == timestamp)
                history[index] = self._apply(history[index], operations); self.history_revisions[cell_id] += 1
            tx.sealed = True
            tx.commit_result = {"commit_id": f"commit_{uuid.uuid4().hex}", "idempotency_key": idempotency_key}
            return self._transaction_snapshot(module_id)

    def committed_alerts(self) -> list[dict[str, Any]]:
        """Alerts eligible for host publication; unsealed work stays private."""
        with self._lock:
            return [deepcopy(alert) for tx in self.transactions.values() if tx.sealed for alert in tx.alerts]

    def committed_routes(self) -> dict[str, dict[str, Any]]:
        """Committed route payloads keyed by authenticated module and route id."""
        with self._lock:
            return {
                module_id: deepcopy(tx.staged_routes)
                for module_id, tx in self.transactions.items()
                if tx.sealed and tx.staged_routes
            }

    @staticmethod
    def _apply(cell: dict[str, Any], operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        candidate = deepcopy(cell)
        for operation in operations:
            parent, key = _resolve_parent(candidate, _segments(operation["path"]), create=operation["op"] == "add")
            if operation["op"] == "test":
                if parent.get(key, object()) != operation["value"]: raise APIError("conflict", "patch test operation failed", 409, pointer=operation["path"])
            elif operation["op"] == "replace":
                if key not in parent: raise APIError("invalid_patch", "replace target does not exist", 400, pointer=operation["path"])
                parent[key] = deepcopy(operation["value"])
            else: parent[key] = deepcopy(operation["value"])
        return candidate
