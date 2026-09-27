from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Tuple
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

        def _body(self) -> dict:
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
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        # /api/checks/{id}/{sub}[/{rid}]
        @staticmethod
        def _parse_check_path(path: str):
            parts = [p for p in path.split("/") if p]
            # api, checks, id, ...
            if len(parts) < 3 or parts[0] != "api" or parts[1] != "checks":
                return None
            try:
                check_id = int(parts[2])
            except ValueError:
                return None
            sub = parts[3] if len(parts) > 3 else None
            tail = parts[4:] if len(parts) > 4 else []
            return check_id, sub, tail

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/checks":
                    _, role = self._identity()
                    status = parse_qs(parsed.query).get("status", [None])[0]
                    self._json(200, {"checks": service.list_checks(role, status)})
                elif path == "/api/audit":
                    _, role = self._identity()
                    check_id = parse_qs(parsed.query).get("check_id", [None])[0]
                    check_id = int(check_id) if check_id else None
                    self._json(200, {"events": service.audit(role, check_id)})
                else:
                    match = self._parse_check_path(path)
                    if match:
                        check_id, sub, tail = match
                        _, role = self._identity()
                        if sub is None:
                            self._json(200, service.get_check(check_id, role))
                        elif sub == "records" and not tail:
                            self._json(200, {"records": service.list_records(check_id, role)})
                        elif sub == "history" and not tail:
                            self._json(200, {"history": service.list_history(check_id, role)})
                        else:
                            self._json(404, {"error": "not_found"})
                    else:
                        self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/checks":
                    self._json(201, service.create_check(body, actor, role))
                    return
                match = self._parse_check_path(path)
                if match:
                    check_id, sub, tail = match
                    if sub == "correction" and not tail:
                        self._json(200, service.correct_check(check_id, body, actor, role))
                    elif sub == "review" and not tail:
                        self._json(200, service.review_check(check_id, body, actor, role))
                    elif sub == "close" and not tail:
                        self._json(200, service.close_check(check_id, body, actor, role))
                    elif sub == "records" and not tail:
                        self._json(201, service.add_record(check_id, body, actor, role))
                    elif sub == "records" and len(tail) == 2 and tail[1] == "close":
                        self._json(200, service.close_rectification(
                            check_id, int(tail[0]), actor, role))
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
