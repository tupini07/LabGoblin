"""Loopback-only, token-authenticated UI; no research-control endpoints."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from labgoblin.inspection_evidence import Evidence
from labgoblin.protocol import UncertainExecution, integer, table
from labgoblin.storage import canonical


ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error, UncertainExecution)
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/inspector.js": ("inspector.js", "text/javascript; charset=utf-8"),
          "/inspector.css": ("inspector.css", "text/css; charset=utf-8")}


class InspectionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, inspector, *, port=0, chat=False):
        integer(port, "port", zero=True)
        if port > 65535:
            raise ValueError("Port must be between 0 and 65535")
        self.inspector, self.chat = inspector, chat
        self.token = secrets.token_urlsafe(32)
        self.job_lock, self.job, self.error = threading.Lock(), None, ""
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def url(self):
        return self.origin + "/#" + self.token

    def submit(self, question):
        if not self.chat:
            raise ValueError("This server is view-only; restart with an explicit separate --credits allowance for chat")
        if not isinstance(question, str) or not question.strip() or len(question.encode("utf-8")) > 4096:
            raise ValueError("Enter a nonempty question of at most 4096 UTF-8 bytes")
        with self.job_lock:
            if self.job is not None and self.job.is_alive():
                raise ValueError("An inspection question is already running")
            self.error = ""
            self.inspector.cancel_requested.clear()

            def answer():
                try:
                    receipt = self.inspector.ask(question)
                    if receipt["status"] != "completed":
                        self.error = f"{receipt['status']}: {receipt['reason']}"
                except ERRORS as error:
                    self.error = f"{type(error).__name__}: {error}"

            self.job = threading.Thread(target=answer, name="labgoblin-inspection", daemon=True)
            self.job.start()
        return {"accepted": True}

    def close_questions(self):
        if self.job is not None and self.job.is_alive():
            self.inspector.cancel()
            self.job.join(timeout=45)
            if self.job.is_alive():
                raise UncertainExecution("Inspection worker is still settling; its finite deadline and reservation remain")


class Handler(BaseHTTPRequestHandler):
    server: InspectionServer

    def log_message(self, format, *args):
        # URLs may contain human-authored queries; do not copy them into server logs.
        pass

    def send(self, status, body, mime="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src data: blob:; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, *, api=True):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            raise ValueError("Only this loopback host is accepted")
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            raise ValueError("Cross-origin requests are not permitted")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ValueError("Cross-site requests are not permitted")
        if api and not secrets.compare_digest(self.headers.get("Authorization", ""),
                                              "Bearer " + self.server.token):
            raise PermissionError("Open the inspector's exact local URL to authorize this browser")

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            self.authorized(api=parsed.path not in ASSETS)
            if parsed.path in ASSETS:
                name, mime = ASSETS[parsed.path]
                self.send(200, (Path(__file__).parent / "inspection_assets" / name).read_bytes(), mime)
                return
            query = parse_qs(parsed.query, strict_parsing=True)
            if parsed.path == "/api/status":
                result = {**self.server.inspector.status(), "chat_enabled": self.server.chat,
                          "busy": self.server.job is not None and self.server.job.is_alive(),
                          "error": self.server.error}
            elif parsed.path == "/api/history":
                offset = int(query["offset"][0]) if "offset" in query else None
                if offset is not None:
                    integer(offset, "history offset", zero=True)
                limit = integer(int(query.get("limit", ["40"])[0]), "history page size")
                if limit > 40:
                    raise ValueError("History pages are limited to 40 questions")
                result = self.server.inspector.history(offset=offset, limit=limit)
            elif parsed.path == "/api/source":
                result = self.server.inspector.source(query["run"][0], query["citation"][0])
            else:
                self.send(404, canonical({"error": "Unknown inspection endpoint"}))
                return
            self.send(200, canonical(result))
        except PermissionError as error:
            self.send(403, canonical({"error": str(error)}))
        except (*ERRORS, KeyError) as error:
            self.send(400, canonical({"error": f"{type(error).__name__}: {error}"}))

    def do_POST(self):
        try:
            self.authorized()
            self.connection.settimeout(5)
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not length.isdigit() or not 0 < int(length) <= 8192:
                raise ValueError("A bounded Content-Length is required")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Expected application/json")
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise ValueError("Incomplete request body")
            args = json.loads(body)
            if self.path == "/api/question":
                table(args, "question", {"question"})
                result = self.server.submit(args.get("question"))
            elif self.path == "/api/cancel":
                table(args, "cancel", set())
                result = self.server.inspector.cancel()
            elif self.path in ("/api/read", "/api/listing", "/api/search", "/api/session", "/api/execution"):
                evidence = Evidence(self.server.inspector.project, self.server.inspector.project_id)
                result = getattr(evidence, self.path.removeprefix("/api/"))(args)
            else:
                self.send(404, canonical({"error": "Unknown inspection endpoint"}))
                return
            self.send(200, canonical(result))
        except PermissionError as error:
            self.send(403, canonical({"error": str(error)}))
        except (*ERRORS, KeyError, TypeError) as error:
            self.send(400, canonical({"error": f"{type(error).__name__}: {error}"}))


def serve(inspector, *, port=0, chat=False, open_browser=False):
    server = InspectionServer(inspector, port=port, chat=chat)
    print(json.dumps({"inspector_url": server.url, "chat_enabled": chat,
                      "history": str(inspector.root), "note": "Read-only; Ctrl+C closes the inspector, not research"}), flush=True)
    try:
        if open_browser:
            webbrowser.open(server.url)
        server.serve_forever(poll_interval=0.2)
    finally:
        try:
            server.close_questions()
        finally:
            server.server_close()
