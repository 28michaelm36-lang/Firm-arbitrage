"""The website: a JSON API over the scanner, the static page, and a background scan loop."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Scanner

STATIC = Path(__file__).parent / "static"
# Only these files are served; nothing from the request path reaches the filesystem.
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class Runner:
    """Rescans prices every `market_minutes` and headlines every `news_minutes`, in one thread."""

    def __init__(self, scanner: Scanner, market_minutes: float, news_minutes: float, news: bool = True):
        self.scanner = scanner
        scanner.refresh_minutes = market_minutes
        scanner.news_refresh_minutes = news_minutes if news else None
        self.market_every = market_minutes * 60
        self.news_every = news_minutes * 60
        self.news = news
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._force = False
        self.thread = threading.Thread(target=self._loop, name="scanner", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def refresh(self):
        self._force = True
        self._wake.set()

    def _loop(self):
        next_market = next_news = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            force, self._force = self._force, False
            if force or now >= next_market:
                self._safely(self.scanner.scan_markets)
                next_market = time.monotonic() + self.market_every
            if self.news and (force or now >= next_news):
                self._safely(self.scanner.scan_news)
                next_news = time.monotonic() + self.news_every
            due = min(next_market, next_news) if self.news else next_market
            self._wake.wait(max(1.0, due - time.monotonic()))
            self._wake.clear()

    def _safely(self, scan):
        try:
            scan()
        except Exception as exc:
            self.scanner.status = "idle"
            self.scanner.log(f"[runner] {scan.__name__} failed: {type(exc).__name__}: {exc}")


def make_handler(scanner: Scanner, runner: Runner | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FuturesScanner/1.0"

        def log_message(self, format, *args):    # keep the console for alerts
            pass

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = HTTPStatus.OK):
            self._send(status, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path
            if path in FILES:
                name, content_type = FILES[path]
                return self._send(HTTPStatus.OK, (STATIC / name).read_bytes(), content_type)
            if path == "/api/overview":
                return self._json(scanner.overview())
            if path.startswith("/api/markets/"):
                detail = scanner.detail(urllib.parse.unquote(path.removeprefix("/api/markets/")))
                if detail is None:
                    return self._json({"error": "unknown market"}, HTTPStatus.NOT_FOUND)
                return self._json(detail)
            if path == "/api/alerts":
                try:
                    after, limit = int(query.get("after", 0)), min(int(query.get("limit", 100)), 500)
                except ValueError:
                    return self._json({"error": "after and limit must be integers"}, HTTPStatus.BAD_REQUEST)
                return self._json({"alerts": scanner.store.alerts(
                    after=after, limit=limit, market=query.get("market"), kind=query.get("kind"))})
            if path == "/api/news":
                return self._json({"news": scanner.store.news(query.get("market"), limit=100)})
            if path == "/api/health":
                return self._json({"ok": True, "status": scanner.status})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            # A custom header cannot be sent cross-site without a CORS preflight, which
            # this server never grants, so other websites cannot trigger scans.
            if self.path != "/api/refresh":
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if self.headers.get("X-Scanner") != "1":
                return self._json({"error": "missing X-Scanner header"}, HTTPStatus.FORBIDDEN)
            if runner is None:
                return self._json({"error": "no background runner"}, HTTPStatus.SERVICE_UNAVAILABLE)
            runner.refresh()
            return self._json({"ok": True}, HTTPStatus.ACCEPTED)

    return Handler


def serve(scanner: Scanner, host: str, port: int, runner: Runner | None = None) -> ThreadingHTTPServer:
    """Bind the server; the caller runs serve_forever()."""
    server = ThreadingHTTPServer((host, port), make_handler(scanner, runner))
    server.daemon_threads = True
    return server
