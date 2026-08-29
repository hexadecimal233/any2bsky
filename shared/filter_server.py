"""Local filter server for manual event selection (mini server, no deps).

Security model:
- binds 127.0.0.1 ONLY;
- a random token (uuid-ish) is generated at startup and embedded in the URL;
  every /api/* and /media request must carry it (`secrets.compare_digest`),
  otherwise 403 -- malicious remote/local web pages cannot drive the API;
- media paths are validated against the export root (no traversal).

Frontend (tools/editor.html) is served at "/"; it lists events from
/api/events and posts drop decisions back to /api/filter, which applies the
filter to events.json on the BACKEND side.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from shared.paths import events_path

BLOCK_SIZE = 1 << 20


def _mime(path: str) -> str:
    t, _ = mimetypes.guess_type(path)
    if t:
        return t
    ext = os.path.splitext(path)[1].lower()
    return {
        ".avif": "image/avif",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")


class FilterServer:
    """Serves the editor and applies event filters to data/<src>/events.json."""

    def __init__(self, root: str, port: int = 0) -> None:
        self.root = os.path.abspath(root)
        self.events_json = events_path(self.root)
        self.token = secrets.token_urlsafe(24)
        self.requested_port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.events: list[dict] = []
        self._reload()

    # -- data ----------------------------------------------------------------
    def _reload(self) -> None:
        if not os.path.exists(self.events_json):
            raise FileNotFoundError(
                f"{self.events_json} missing -- run `any2bsky convert <root>` first"
            )
        with open(self.events_json, "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.events = doc.get("events", [])
        # media paths in events are RELATIVE to the SOURCE export root: take it
        # from events.json (source.root); the CLI root only locates events.json.
        sroot = (doc.get("source") or {}).get("root")
        self.media_root = (
            os.path.abspath(sroot) if sroot and os.path.isdir(sroot) else self.root
        )

    def apply_filter(self, dropped_indexes: list[int]) -> tuple[int, int]:
        """Full-set semantics: indexes in the list are dropped, everything else
        is restored (a re-checked event gets its drop flag cleared). Persists.

        Returns (dropped_count, total_dropped).
        """
        dropped_set = set(dropped_indexes)
        dropped_total = 0
        for i, e in enumerate(self.events):
            if i in dropped_set:
                e["drop"] = True
                dropped_total += 1
            else:
                e.pop("drop", None)
        self._save()
        return len(dropped_set), dropped_total

    def restore(self, kept_indexes: list[int] | None = None) -> tuple[int, int]:
        if kept_indexes is not None:
            for i in kept_indexes:
                if 0 <= i < len(self.events):
                    self.events[i].pop("drop", None)
        else:
            for e in self.events:
                e.pop("drop", None)
        self._save()
        return sum(1 for e in self.events if not e.get("drop")), 0

    def _save(self) -> None:
        with open(self.events_json, "r", encoding="utf-8") as f:
            doc = json.load(f)
        doc["events"] = self.events
        doc["count"] = len(self.events)
        with open(self.events_json, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

    # -- http ----------------------------------------------------------------
    def start(self) -> str:
        """Start the server; returns the token URL."""
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.requested_port), handler)
        port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}/?token={self.token}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()

    def media_path(self, rel: str) -> str | None:
        """Resolve a media relative path against the source export root (traversal-safe)."""
        root_real = os.path.realpath(self.media_root)
        p = os.path.realpath(os.path.join(self.media_root, rel))
        if not (p == root_real or p.startswith(root_real + os.sep)):
            return None
        return p if os.path.isfile(p) else None


def _make_handler(server: FilterServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep logs quiet
            pass

        def _token_ok(self) -> bool:
            qs = parse_qs(urlparse(self.path).query)
            tok = (qs.get("token") or [""])[0]
            return secrets.compare_digest(tok, server.token)

        def _send(
            self, code: int, body: bytes, ctype: str = "application/json"
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/favicon.ico":
                self._send(204, b"")
                return
            if path == "/":
                html = os.path.join(
                    os.path.dirname(__file__), "..", "tools", "editor.html"
                )
                try:
                    with open(html, "r", encoding="utf-8") as f:
                        body = f.read().encode("utf-8")
                    self._send(200, body, "text/html; charset=utf-8")
                except OSError:
                    self._send(404, b"editor.html missing")
                return
            if not self._token_ok():
                self._send(403, json.dumps({"error": "forbidden"}).encode())
                return
            if path == "/api/events":
                self._send(
                    200,
                    json.dumps({"events": server.events}, ensure_ascii=False).encode(
                        "utf-8"
                    ),
                )
                return
            if path == "/media":
                qs = parse_qs(urlparse(self.path).query)
                rel = (qs.get("path") or [""])[0]
                fp = server.media_path(rel)
                if fp is None:
                    self._send(404, b"no such media")
                    return
                try:
                    with open(fp, "rb") as f:
                        data = f.read()
                    self._send(200, data, _mime(fp))
                except OSError:
                    self._send(500, b"media read error")
                return
            self._send(404, b"not found")

        def do_POST(self):
            if not self._token_ok():
                self._send(403, json.dumps({"error": "forbidden"}).encode())
                return
            if urlparse(self.path).path != "/api/filter":
                self._send(404, b"not found")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                dropped_now, total = server.apply_filter(
                    payload.get("droppedIndexes", [])
                )
                self._send(
                    200,
                    json.dumps(
                        {
                            "ok": True,
                            "dropped_now": dropped_now,
                            "total_dropped": total,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                )
            except (ValueError, KeyError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode())

        do_PUT = do_POST

    return Handler
