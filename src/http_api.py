from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "EmissionDesk/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, (ValidationError, ValueError)):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                parts = [p for p in path.split("/") if p]
                actor, role = self._identity()
                del actor
                query = parse_qs(parsed.query)
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif parts == ["api", "cases"]:
                    status = query.get("status", [None])[0]
                    self._json(200, {"cases": service.list_cases(role, status)})
                elif len(parts) == 3 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
                    self._json(200, service.get_case(int(parts[2]), role))
                elif (len(parts) == 4 and parts[:2] == ["api", "cases"]
                      and parts[2].isdigit() and parts[3] in ("records", "archives")):
                    case_id = int(parts[2])
                    if parts[3] == "records":
                        self._json(200, {"records": service.list_records(case_id, role)})
                    else:
                        self._json(200, {"archives": service.list_archives(case_id, role)})
                elif parts == ["api", "audit"]:
                    case_id = query.get("case_id", [None])[0]
                    self._json(200, {"events": service.audit(
                        role, int(case_id) if case_id else None)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                actor, role = self._identity()
                body = self._body()
                # /api/cases
                if parts == ["api", "cases"]:
                    self._json(201, service.register(body, actor, role))
                # /api/cases/{id}/{review|correct|close}
                elif (len(parts) == 4 and parts[:2] == ["api", "cases"]
                      and parts[2].isdigit() and parts[3] in ("review", "correct", "close")):
                    case_id = int(parts[2])
                    action = parts[3]
                    if action == "review":
                        result = service.review(case_id, body, actor, role)
                    elif action == "correct":
                        result = service.correct(case_id, body, actor, role)
                    else:
                        result = service.close_case(case_id, body, actor, role)
                    self._json(200, result)
                # /api/cases/{id}/records
                elif (len(parts) == 4 and parts[:2] == ["api", "cases"]
                      and parts[2].isdigit() and parts[3] == "records"):
                    self._json(201, service.add_record(int(parts[2]), body, actor, role))
                # /api/cases/{id}/records/{rid}/close
                elif (len(parts) == 6 and parts[:2] == ["api", "cases"]
                      and parts[2].isdigit() and parts[3] == "records"
                      and parts[4].isdigit() and parts[5] == "close"):
                    self._json(200, service.close_record(
                        int(parts[2]), int(parts[4]), actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
