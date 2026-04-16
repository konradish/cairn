#!/usr/bin/env python3
"""cairn — build-on-request http server.

Runs `build.py` on each `/` request, then serves the resulting `index.html`.
Stdlib only. Dev-local tool: build failures return HTTP 500 with the
traceback in the response body so you can see what broke.
"""
from __future__ import annotations

import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"
BUILD_PY = HERE / "build.py"

HOST = "0.0.0.0"
PORT = 8080


def run_build() -> tuple[bool, str]:
    """Run build.py. Return (ok, detail)."""
    try:
        result = subprocess.run(
            [sys.executable, str(BUILD_PY)],
            capture_output=True,
            text=True,
            cwd=str(HERE),
            timeout=30,
        )
    except Exception:  # noqa: BLE001
        return False, traceback.format_exc()
    if result.returncode != 0:
        detail = (
            f"build.py exited {result.returncode}\n\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
        )
        return False, detail
    return True, result.stderr or result.stdout


class CairnHandler(BaseHTTPRequestHandler):
    server_version = "cairn/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/healthz", "/health"):
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        # Only build on root; other paths 404 (we don't serve static assets here)
        if self.path not in ("/", "/index.html"):
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return

        ok, detail = run_build()
        if not ok:
            body = (
                "cairn: build failed\n\n" + detail
            ).encode("utf-8", errors="replace")
            self._send(500, body, "text/plain; charset=utf-8")
            return

        try:
            html = INDEX_HTML.read_bytes()
        except OSError as exc:
            body = f"cairn: cannot read {INDEX_HTML}: {exc}\n".encode("utf-8")
            self._send(500, body, "text/plain; charset=utf-8")
            return
        self._send(200, html, "text/html; charset=utf-8")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write(
            "[cairn] %s - %s\n" % (self.address_string(), format % args)
        )


def main() -> int:
    if not BUILD_PY.exists():
        print(f"error: build.py missing at {BUILD_PY}", file=sys.stderr)
        return 2
    srv = ThreadingHTTPServer((HOST, PORT), CairnHandler)
    print(f"cairn: listening on http://{HOST}:{PORT}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("cairn: shutting down", file=sys.stderr)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
