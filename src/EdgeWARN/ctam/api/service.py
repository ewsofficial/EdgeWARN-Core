"""Transport-independent, cycle-pinned read operations for CTAM API v1."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..limits import API_VERSION, DEFAULT_HISTORY_WINDOW, MAX_HISTORY_WINDOW, MAX_STREAMED_FILE_BYTES
from ..manifest import ModuleManifest
from ..readiness import CTAMCycleCatalog, READY, evaluate_requirements
from .models import APIError
from ..transaction import CTAMTransactionService


def _timestamp(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(value)


class CTAMReadService:
    """Immutable view of exactly one CTAM cycle.

    The constructor receives cells and catalog entries selected by the pipeline.
    It never discovers files and all content reads go through catalog descriptors.
    """

    def __init__(
        self,
        *,
        catalog: CTAMCycleCatalog,
        cells: Sequence[Mapping[str, Any]],
        manifests: Mapping[str, ModuleManifest],
        state: str = "external_modules_running",
        ctam_ready: bool = True,
        deadline: datetime | None = None,
        transactions: CTAMTransactionService | None = None,
    ) -> None:
        self.catalog = catalog
        self.transactions = transactions
        self._cells = tuple(deepcopy(dict(cell)) for cell in cells)
        self._cell_index = {str(cell.get("id")): cell for cell in self._cells if cell.get("id") is not None}
        self._manifests = dict(manifests)
        self.state = state
        self.ctam_ready = bool(ctam_ready)
        self.deadline = deadline
        self._history_cache: dict[str, list[dict[str, Any]]] = {}

    def _manifest(self, module_id: str) -> ModuleManifest:
        try:
            return self._manifests[module_id]
        except KeyError as exc:
            raise APIError("authentication_failed", "module is not admitted for this cycle", 401) from exc

    def _admitted_ids(self, module_id: str) -> set[str]:
        manifest = self._manifest(module_id)
        admitted: set[str] = set()
        for requirement in manifest.requires:
            admitted.update(entry.file_id for entry in self.catalog.resolve(requirement.selector))
        return admitted

    def _require_admitted(self, module_id: str, file_id: str) -> None:
        """Require a manifest selector before exposing any artifact content.

        Catalog metadata is intentionally global to the cycle, but stormcell
        snapshots and history JSON are content just like a staged GRIB file.
        Keeping this check in the transport-independent service ensures the
        HTTP routes and future in-process adapters cannot diverge.
        """
        if file_id not in self._admitted_ids(module_id):
            raise APIError(
                "file_unavailable",
                "file content is not declared by this module",
                403,
                file_id,
            )

    def health(self) -> dict[str, Any]:
        return {"supported_api_versions": [API_VERSION], "cycle_bound": True}

    def cycle(self, module_id: str) -> dict[str, Any]:
        evaluation = self.requirements(module_id)
        result = {
            "cycle_id": self.catalog.cycle_id,
            "analysis_time": self.catalog.analysis_time,
            "state": self.state,
            "ctam_ready": self.ctam_ready,
            "module_state": "running",
            "requirements_satisfied": evaluation["satisfied"],
            "allowed_operations": (["read_files", "read_stormcells", "read_history", "patch_stormcells", "patch_history", "stage_alerts", "register_routes", "commit"] if self.transactions else ["read_files", "read_stormcells", "read_history"]),
            "historical": self.catalog.historical,
            "cell_count": self.catalog.cell_count,
            "deadline": self.deadline.astimezone(timezone.utc).isoformat() if self.deadline else None,
        }
        return result

    def files(self) -> dict[str, Any]:
        return {"files": list(self.catalog.as_dicts())}

    def descriptor(self, file_id: str) -> dict[str, Any]:
        entry = self.catalog.descriptor(file_id)
        if entry is None:
            raise APIError("not_found", "catalog file was not found", 404, file_id)
        return entry.as_dict()

    def content(self, module_id: str, file_id: str) -> tuple[Path, str, int]:
        entry = self.catalog.descriptor(file_id)
        if entry is None:
            raise APIError("not_found", "catalog file was not found", 404, file_id)
        self._require_admitted(module_id, file_id)
        if not entry.available or not entry.validated or entry.readiness != READY or entry.path is None:
            raise APIError("file_unavailable", "catalogued artifact is not readable for this cycle", 409, file_id)
        try:
            size = entry.path.stat().st_size
        except OSError as exc:
            raise APIError("file_unavailable", "catalogued artifact is no longer readable", 409, file_id) from exc
        if size > MAX_STREAMED_FILE_BYTES:
            raise APIError("request_too_large", "catalogued artifact exceeds the streamed-file limit", 413, file_id, MAX_STREAMED_FILE_BYTES)
        return entry.path, entry.media_type, size

    def requirements(self, module_id: str) -> dict[str, Any]:
        return evaluate_requirements(self._manifest(module_id), self.catalog)

    def stormcells(self, module_id: str) -> dict[str, Any]:
        self._require_admitted(module_id, "stormcells:current")
        cells = self.transactions.cells if self.transactions else {str(cell.get("id")): cell for cell in self._cells}
        revision = max(self.transactions.cell_revisions.values(), default=0) if self.transactions else 0
        return {"revision": revision, "latest_timestamp": self.catalog.cycle_id, "cells": deepcopy(list(cells.values()))}

    def stormcell(self, module_id: str, cell_id: str) -> dict[str, Any]:
        self._require_admitted(module_id, "stormcells:current")
        cell = self.transactions.cells.get(str(cell_id)) if self.transactions else self._cell_index.get(str(cell_id))
        if cell is None:
            raise APIError("not_found", "storm cell was not found", 404, "stormcells.current")
        revision = self.transactions.cell_revisions.get(str(cell_id), 0) if self.transactions else 0
        return {"revision": revision, "cell": deepcopy(cell)}

    def stage_stormcell(self, module_id: str, cell_id: str, *, revision: int, operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.stage_cell(module_id, str(cell_id), revision=revision, operations=operations)

    def stage_history(self, module_id: str, cell_id: str, timestamp: str, *, revision: int, operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.stage_history(module_id, str(cell_id), timestamp, revision=revision, operations=operations)

    def transaction(self, module_id: str) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.transaction(module_id)

    def validate_transaction(self, module_id: str) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.validate(module_id)

    def abandon_transaction(self, module_id: str) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.abandon(module_id)

    def commit_transaction(self, module_id: str, *, idempotency_key: str | None) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.commit(module_id, idempotency_key=idempotency_key)

    def stage_alert(self, module_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.stage_alert(module_id, payload)

    def stage_route(self, module_id: str, route_id: str, payload: Any) -> dict[str, Any]:
        if self.transactions is None: raise APIError("unavailable", "mutations are not enabled for this cycle", 409)
        return self.transactions.stage_route(module_id, route_id, payload)

    def history(self, module_id: str, cell_id: str, *, limit: int = DEFAULT_HISTORY_WINDOW, since: str | None = None) -> dict[str, Any]:
        if limit < 1:
            raise APIError("invalid_patch", "history limit must be positive", 400, "cells.history")
        limit = min(limit, MAX_HISTORY_WINDOW)
        file_id = f"cell_history:{cell_id}"
        self._require_admitted(module_id, file_id)
        entry = self.catalog.descriptor(file_id)
        if entry is None:
            raise APIError("not_found", "cell history was not admitted for this cycle", 404, "cells.history")
        if not entry.available or not entry.validated or entry.path is None:
            raise APIError("file_unavailable", "cell history is not readable for this cycle", 409, "cells.history")
        if file_id not in self._history_cache:
            try:
                from EdgeWARN.stormprob.database import StormProbRepository
                payload = StormProbRepository().legacy_history(
                    cell_id, through=self.catalog.analysis_time)
                if not payload:
                    with entry.path.open("r", encoding="utf-8") as handle:
                        payload = json.load(handle)
            except FileNotFoundError:
                try:
                    with entry.path.open("r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                except (OSError, json.JSONDecodeError) as exc:
                    raise APIError("file_unavailable", "cell history is no longer readable", 409, "cells.history") from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise APIError("file_unavailable", "cell history is no longer readable", 409, "cells.history") from exc
            if not isinstance(payload, list):
                raise APIError("file_unavailable", "cell history has an invalid pinned shape", 409, "cells.history")
            self._history_cache[file_id] = payload
        entries = self._history_cache[file_id]
        if since is not None:
            entries = [item for item in entries if _timestamp(item.get("timestamp", "")) >= since]
        truncated = len(entries) > limit
        entries = entries[-limit:]
        return {"revision": 0, "cell_id": cell_id, "returned": len(entries), "truncated": truncated, "entries": deepcopy(entries)}
