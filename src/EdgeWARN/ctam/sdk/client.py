"""stdlib-only client; deliberately imports no EdgeWARN implementation code."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class CTAMAPIError(RuntimeError):
    def __init__(self, status: int, payload: dict):
        self.status, self.payload = status, payload
        super().__init__(payload.get("errors", [{}])[0].get("message", "CTAM API request failed"))


class CTAMClient:
    def __init__(self, url: str, token: str): self.url, self.token = url.rstrip("/"), token
    @classmethod
    def from_environment(cls):
        import os
        return cls(os.environ["CTAM_API_URL"], os.environ["CTAM_API_TOKEN"])
    def _request(self, path: str, *, headers=None, raw=False, method=None, payload=None):
        data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode() if payload is not None else None
        request = Request(self.url + path, data=data, method=method, headers={"Authorization": f"Bearer {self.token}", "X-CTAM-API-Version": "1", **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
        try:
            with urlopen(request, timeout=30) as response:
                return response.read() if raw else json.loads(response.read())['data']
        except HTTPError as error:
            try: payload = json.loads(error.read())
            except Exception: payload = {"errors": [{"message": "CTAM API request failed"}]}
            raise CTAMAPIError(error.code, payload) from error
    def health(self): return self._request("/health")
    def cycle(self): return self._request("/cycle")
    def files(self): return self._request("/files")["files"]
    def file(self, file_id): from urllib.parse import quote; return self._request("/files/" + quote(file_id, safe=""))
    def requirements(self): return self._request("/requirements")
    def check_requirements(self):
        request = Request(self.url + "/requirements/check", method="POST", headers={"Authorization": f"Bearer {self.token}", "X-CTAM-API-Version": "1", "Content-Length": "0"})
        with urlopen(request, timeout=30) as response: return json.loads(response.read())["data"]
    def stormcells(self): return self._request("/stormcells")
    def stormcell(self, cell_id): from urllib.parse import quote; return self._request("/stormcells/" + quote(str(cell_id), safe=""))
    def history(self, cell_id, *, limit=None, since=None):
        from urllib.parse import quote, urlencode
        query = {key: value for key, value in {"limit": limit, "since": since}.items() if value is not None}
        return self._request("/cells/" + quote(str(cell_id), safe="") + ("?" + urlencode(query) if query else ""))
    def content(self, file_id, *, byte_range=None):
        from urllib.parse import quote
        headers = {"Range": f"bytes={byte_range}"} if byte_range else None
        return self._request("/files/" + quote(file_id, safe="") + "/content", headers=headers, raw=True)
    def materialize(self, file_id, *, directory=None) -> Path:
        target_dir = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="ctam-module-"))
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "artifact"
        target.write_bytes(self.content(file_id))
        return target
    def patch_stormcell(self, cell_id, *, revision, operations):
        from urllib.parse import quote
        return self._request("/stormcells/" + quote(str(cell_id), safe=""), method="PATCH", payload={"revision": revision, "operations": operations})
    def patch_history(self, cell_id, timestamp, *, revision, operations):
        from urllib.parse import quote
        return self._request("/cells/" + quote(str(cell_id), safe="") + "/entries/" + quote(str(timestamp), safe=""), method="PATCH", payload={"revision": revision, "operations": operations})
    def transaction(self): return self._request("/transaction")
    def abandon_transaction(self): return self._request("/transaction", method="DELETE")
    def validate_transaction(self): return self._request("/transaction/validate", method="POST", payload={})
    def commit_transaction(self, *, idempotency_key=None):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("/transaction/commit", method="POST", payload={}, headers=headers)
    def stage_alert(self, payload): return self._request("/alerts", method="POST", payload=payload)
    def register_route(self, route_id, payload):
        from urllib.parse import quote
        return self._request("/routes/" + quote(str(route_id), safe=""), method="PUT", payload=payload)
