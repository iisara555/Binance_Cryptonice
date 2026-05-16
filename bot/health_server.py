"""Lightweight HTTP health server for the trading bot runtime."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def _normalize_health_path(path: str) -> str:
    text = str(path or "/health").strip()
    if not text:
        return "/health"
    return text if text.startswith("/") else f"/{text}"


def _health_metrics_paths(path: str) -> set[str]:
    normalized = _normalize_health_path(path)
    base_metrics_path = f"{normalized.rstrip('/')}/metrics" if normalized != "/" else "/metrics"
    return {"/metrics", base_metrics_path}


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class BotHealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.host = host
        self.port = int(port)
        self.path = _normalize_health_path(path)
        self.status_provider = status_provider or (lambda: {"status": "unknown", "healthy": False})
        self._server: Optional[_ReusableThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _make_handler(self):
        server_ref = self
        metrics_paths = _health_metrics_paths(server_ref.path)
        health_paths = {server_ref.path, "/health"}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request_path = urlsplit(self.path).path or "/"
                if request_path in metrics_paths:
                    try:
                        from metrics import get_metrics

                        metrics_data = get_metrics().export().encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                        self.send_header("Content-Length", str(len(metrics_data)))
                        self.end_headers()
                        self.wfile.write(metrics_data)
                        return
                    except ImportError:
                        pass

                if request_path not in health_paths and request_path not in metrics_paths:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "not_found"}).encode("utf-8"))
                    return

                payload = dict(server_ref.status_provider() or {})
                payload.setdefault(
                    "timestamp",
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                )
                status_code = 200 if payload.get("healthy") else 503
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("BotHealthServer %s", format % args)

        return Handler

    def start(self) -> bool:
        if self.port <= 0:
            return False
        if self._server is not None:
            return True

        self._server = _ReusableThreadingHTTPServer((self.host, self.port), self._make_handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="BotHealthServer",
        )
        self._thread.start()
        logger.info("Bot health server listening on %s:%s%s", self.host, self.port, self.path)
        return True

    def stop(self) -> None:
        if self._server is None:
            return

        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None

        server.shutdown()
        server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
