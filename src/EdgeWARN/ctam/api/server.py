"""Lifecycle and HTTP transport for the private loopback CTAM API."""

from __future__ import annotations

import secrets
import threading
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from ..limits import API_VERSION, MAX_REQUEST_BODY_BYTES, STREAM_CHUNK_BYTES, SUPPORTED_API_VERSIONS
from .models import APIError
from .service import CTAMReadService

_PREFIX = "/internal/ctam/v1"
_LOG = logging.getLogger(__name__)


class LoopbackCTAMServer:
    """Start a short-lived server bound exclusively to ``127.0.0.1``."""

    def __init__(self, service: CTAMReadService, *, tokens: dict[str, str] | None = None) -> None:
        self.service = service
        self.tokens = dict(tokens or {module_id: secrets.token_urlsafe(32) for module_id in service._manifests})
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def token_for(self, module_id: str) -> str:
        return self.tokens[module_id]

    @property
    def url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("CTAM API server is not running")
        return f"http://127.0.0.1:{self._httpd.server_port}{_PREFIX}"

    def start(self) -> "LoopbackCTAMServer":
        if self._httpd is not None:
            return self
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "EdgeWARNCTAM/1"
            def log_message(self, _format, *_args):
                return  # Authorization headers and module payloads are never logged.
            def _envelope(self, module_id, request_id, data, errors):
                return {"api_version": API_VERSION, "cycle_id": outer.service.catalog.cycle_id if module_id else None, "module_id": module_id, "request_id": request_id, "data": data, "errors": errors}
            def _send_json(self, status, payload):
                import json
                encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
                self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
            def _body(self):
                import json
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if not raw: return {}
                try: return json.loads(raw)
                except json.JSONDecodeError as exc: raise APIError("invalid_patch", "request body must be JSON", 400) from exc
            def _auth(self, request_id):
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    raise APIError("authentication_failed", "a valid bearer token is required", 401)
                token = header[7:]
                for module_id, expected in outer.tokens.items():
                    if secrets.compare_digest(token, expected): return module_id
                raise APIError("authentication_failed", "a valid bearer token is required", 401)
            def _handle(self):
                request_id = secrets.token_urlsafe(12).replace("-", "_")
                module_id = None
                status = 500
                try:
                    module_id = self._auth(request_id)
                    requested_version = self.headers.get("X-CTAM-API-Version")
                    if requested_version is not None and requested_version not in SUPPORTED_API_VERSIONS:
                        raise APIError("unsupported_version", "requested CTAM API version is unsupported", 426)
                    content_length = self.headers.get("Content-Length")
                    if content_length is not None and int(content_length) > MAX_REQUEST_BODY_BYTES:
                        raise APIError("request_too_large", "request body exceeds the CTAM API limit", 413, limit=MAX_REQUEST_BODY_BYTES)
                    split = urlsplit(self.path)
                    path = split.path
                    if not path.startswith(_PREFIX): raise APIError("not_found", "route was not found", 404)
                    route = path[len(_PREFIX):]
                    query = parse_qs(split.query)
                    if self.command == "GET" and route == "/health": data = outer.service.health()
                    elif self.command == "GET" and route == "/cycle": data = outer.service.cycle(module_id)
                    elif self.command == "GET" and route == "/files": data = outer.service.files()
                    elif self.command == "GET" and route == "/requirements": data = outer.service.requirements(module_id)
                    elif self.command == "POST" and route == "/requirements/check": data = outer.service.requirements(module_id)
                    elif self.command == "GET" and route == "/stormcells": data = outer.service.stormcells(module_id)
                    elif self.command == "PATCH" and route.startswith("/stormcells/"):
                        body = self._body(); data = outer.service.stage_stormcell(module_id, unquote(route.rsplit("/", 1)[1]), revision=body.get("revision"), operations=body.get("operations"))
                    elif self.command == "GET" and route.startswith("/stormcells/"):
                        data = outer.service.stormcell(module_id, unquote(route.rsplit("/", 1)[1]))
                    elif self.command == "GET" and route == "/transaction": data = outer.service.transaction(module_id)
                    elif self.command == "POST" and route == "/transaction/validate": data = outer.service.validate_transaction(module_id)
                    elif self.command == "POST" and route == "/transaction/commit": data = outer.service.commit_transaction(module_id, idempotency_key=self.headers.get("Idempotency-Key"))
                    elif self.command == "DELETE" and route == "/transaction": data = outer.service.abandon_transaction(module_id)
                    elif self.command == "POST" and route == "/alerts": data = outer.service.stage_alert(module_id, self._body())
                    elif self.command == "PUT" and route.startswith("/routes/"):
                        raw_route_id = route[len("/routes/"):]
                        if not raw_route_id or "/" in raw_route_id:
                            raise APIError("not_found", "route was not found", 404)
                        route_id = unquote(raw_route_id)
                        if "/" in route_id or "\\" in route_id or unquote(route_id) != route_id:
                            raise APIError("invalid_patch", "route id must be one safe decoded path segment", 400)
                        data = outer.service.stage_route(module_id, route_id, self._body())
                    elif self.command == "PATCH" and route.startswith("/cells/") and "/entries/" in route:
                        cell_part, timestamp = route[len("/cells/"):].split("/entries/", 1)
                        body = self._body(); data = outer.service.stage_history(module_id, unquote(cell_part), unquote(timestamp), revision=body.get("revision"), operations=body.get("operations"))
                    elif self.command == "GET" and route.startswith("/cells/"):
                        data = outer.service.history(module_id, unquote(route.rsplit("/", 1)[1]), limit=int(query.get("limit", ["5"])[0]), since=query.get("since", [None])[0])
                    elif self.command == "GET" and route.startswith("/files/") and route.endswith("/content"):
                        file_id = unquote(route[len("/files/"):-len("/content")])
                        status = self._send_content(module_id, request_id, file_id); return
                    elif self.command == "GET" and route.startswith("/files/"):
                        data = outer.service.descriptor(unquote(route[len("/files/"):]))
                    else: raise APIError("not_found", "route was not found", 404)
                    status = 200; self._send_json(status, self._envelope(module_id, request_id, data, []))
                except APIError as error:
                    status = error.status; self._send_json(status, self._envelope(module_id, request_id, None, [error.as_dict()]))
                except (TypeError, ValueError):
                    error = APIError("invalid_patch", "request parameters are invalid", 400)
                    status = error.status; self._send_json(status, self._envelope(module_id, request_id, None, [error.as_dict()]))
                except Exception:
                    error = APIError("internal_error", "internal CTAM API error", 500)
                    status = error.status; self._send_json(status, self._envelope(module_id, request_id, None, [error.as_dict()]))
                finally:
                    # Never log headers, query values, payloads, host paths, or
                    # exception text: any of them could contain module secrets.
                    _LOG.info(
                        "ctam_api_request cycle_id=%s module_id=%s request_id=%s method=%s status=%s",
                        outer.service.catalog.cycle_id, module_id, request_id, self.command, status,
                    )
            def _send_content(self, module_id, request_id, file_id):
                path, media_type, size = outer.service.content(module_id, file_id)
                start, end, status = 0, size - 1, HTTPStatus.OK
                raw_range = self.headers.get("Range")
                if raw_range:
                    if not raw_range.startswith("bytes=") or "," in raw_range: raise APIError("invalid_patch", "only one byte range is supported", 416, file_id)
                    left, _, right = raw_range[6:].partition("-")
                    if not left and not right: raise APIError("invalid_patch", "byte range is invalid", 416, file_id)
                    if left: start = int(left)
                    if right: end = int(right)
                    elif left: end = size - 1
                    if not left: start = max(0, size - int(right))
                    if start < 0 or start >= size or end < start: raise APIError("invalid_patch", "byte range is unsatisfiable", 416, file_id)
                    end = min(end, size - 1); status = HTTPStatus.PARTIAL_CONTENT
                length = end - start + 1
                self.send_response(status); self.send_header("Content-Type", media_type); self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(length))
                if status == HTTPStatus.PARTIAL_CONTENT: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with path.open("rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining:
                        chunk = handle.read(min(STREAM_CHUNK_BYTES, remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining -= len(chunk)
                return int(status)
            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_PATCH = _handle
            do_DELETE = _handle

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ctam-api", daemon=True); self._thread.start()
        return self

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown(); self._httpd.server_close(); self._httpd = None
        if self._thread is not None: self._thread.join(timeout=2); self._thread = None

    def __enter__(self): return self.start()
    def __exit__(self, *_args): self.close()
