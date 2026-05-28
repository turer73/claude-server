"""Panola-Social /api/health endpoint — Uptime Kuma için.

Deploy: /opt/panola-social/health_endpoint.py
Entegrasyon: main.py'de veya ayrı süreç olarak çalıştır.

    # main.py içine ekle:
    from health_endpoint import run_health_server
    import threading
    threading.Thread(target=run_health_server, daemon=True).start()

Uptime Kuma: HTTP(S) monitor → http://localhost:8421/api/health
Beklenen cevap: {"status": "ok"} + HTTP 200
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8421"))
DB_PATH = os.environ.get("SOCIAL_DB_PATH", "/opt/panola-social/data/social.db")

_start_time = time.time()


def _check_db() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:100]}


def _build_response() -> tuple[int, dict]:
    db = _check_db()
    uptime = int(time.time() - _start_time)
    payload = {
        "status": "ok" if db["ok"] else "degraded",
        "db": db,
        "uptime_seconds": uptime,
        "service": "panola-social",
    }
    status_code = 200 if db["ok"] else 503
    return status_code, payload


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access log noise
        pass

    def do_GET(self):  # noqa: N802
        if self.path not in ("/api/health", "/health", "/"):
            self.send_response(404)
            self.end_headers()
            return
        code, payload = _build_response()
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_health_server(port: int = HEALTH_PORT) -> None:
    server = HTTPServer(("0.0.0.0", port), _Handler)
    logger.info("panola-social health server started on port %d", port)
    server.serve_forever()


def start_health_server_thread(port: int = HEALTH_PORT) -> threading.Thread:
    t = threading.Thread(target=run_health_server, args=(port,), daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_health_server()
