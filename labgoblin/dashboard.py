"""Read-only loopback views of retained research records and owned evidence."""

from datetime import datetime, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import shlex
import sqlite3
import sys
import urllib.parse

from markdown_it import MarkdownIt

from labgoblin import journal, reporting
from labgoblin.config import ChatSettings
from labgoblin.dashboard_chat import ChatError, ObserverService, load_chat_settings
from labgoblin.dashboard_data import (
    FAILED, RESEARCH_SOURCES, attempt_filter, numeric_metrics,
    observation_context, report_text, snapshot, source_preview, read_text as _read_text,
)
from labgoblin.evidence import observation, tail
from labgoblin.protocol import ACTIVE
from labgoblin.processes import CampaignLease
from labgoblin.paths import configuration_path, state_directory
from labgoblin.scheduler import ResourceLedger
from labgoblin.state import State
from labgoblin.worker import launch_directory


PAGE_SIZE = 50
LOG_LIMIT = 64 * 1024
DOCUMENT_LIMIT = 128 * 1024
JOURNAL_PAGE_SIZE = 20
JOURNAL_PREVIEW_LIMIT = 16 * 1024
STATIC = Path(__file__).with_name("static")
NAV = (
    ("/", "Brief"), ("/evidence", "Evidence"), ("/work", "Work"), ("/history", "History"),
)
SECTIONS = {
    "/evidence": (("/evidence", "Questions"), ("/artifacts", "Measurements"), ("/compare", "Compare"), ("/goal", "Goal & protocol")),
    "/work": (("/work", "Now & next"), ("/jobs", "Experiments"), ("/debug", "Recovery"), ("/resources", "Limits & resources"), ("/activity", "Agent turns")),
    "/history": (("/history", "Research history"), ("/journal", "Journal"), ("/reports", "Reports"), ("/changes", "Recorded changes")),
}
ROUTE_GROUP = {
    "/job": "/work", "/hypotheses": "/evidence", "/hypothesis": "/evidence",
    "/turn": "/work", "/observation": "/evidence", "/view": "/history", "/report": "/history",
    **{path: group for group, items in SECTIONS.items() for path, _ in items},
}
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
       "img-src 'self'; connect-src 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


def _escape(value):
    return html.escape(str(value)) if value is not None else ""


def _url(path, **params):
    value = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)}, doseq=True)
    return path + ("?" + value if value else "")


def _link(path, label, **params):
    return f'<a href="{_escape(_url(path, **params))}">{_escape(label)}</a>'


def _badge(status):
    tone = ("success" if status in ("completed", "accepted", "assessed", "valid") else
            "danger" if status in (*FAILED, "recovery_required", "blocked", "invalid", "incomplete") else
            "accent" if status in ("running", "open", "executing") else
            "warning" if status in ("queued", "starting", "pending", "wait", "paused", "unassessed") else "neutral")
    return f'<span class="badge {tone}">{_escape(status.replace("_", " "))}</span>'


def _duration(seconds):
    if seconds is None:
        return "Not recorded"
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m" if hours else
            f"{minutes}m {seconds}s" if minutes else f"{seconds}s")


def _timestamp(value):
    if value is None or value == "":
        return '<span class="muted">Not recorded</span>'
    date = datetime.fromtimestamp(value, timezone.utc)
    return f'<time datetime="{date.isoformat()}">{date:%Y-%m-%d %H:%M:%S} UTC</time>'


def _size(value):
    if value is None:
        return "Not recorded"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{value:,} B"
        value /= 1024


def _markdown(text):
    return MarkdownIt("commonmark", {"html": False}).enable("table").disable("image").render(text)


def _chat_markdown(text):
    parser = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    tokens = parser.parse(text)
    allowed = {path for path, _ in NAV} | set(ROUTE_GROUP)
    for block in tokens:
        for token in block.children or []:
            if token.type == "link_open":
                target = urllib.parse.urlsplit(token.attrGet("href") or "")
                if target.scheme or target.netloc or target.path not in allowed:
                    token.attrSet("href", "#")
                    token.attrSet("title", "Only dashboard evidence links are enabled in chat")
    return parser.renderer.render(tokens, parser.options, {})


def _empty(title, description):
    return f'<div class="empty"><h3>{_escape(title)}</h3><p>{_escape(description)}</p></div>'


def _panel(title, body, action=""):
    return f'<section class="panel"><div class="section-heading"><h2>{_escape(title)}</h2>{action}</div>{body}</section>'


def _table(headers, rows, *, empty="No records in this scope", explanation="No matching retained records were found."):
    if not rows:
        return _empty(empty, explanation)
    return ('<div class="table-scroll" tabindex="0"><table><thead><tr>'
            + "".join(f'<th scope="col">{_escape(h)}</th>' for h in headers) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f'<td data-label="{_escape(label)}">{cell}</td>'
                                      for label, cell in zip(headers, row)) + "</tr>" for row in rows)
            + "</tbody></table></div>")


def _details(title, body, key=""):
    return f'<details class="technical" data-key="{_escape(key or title)}"><summary>{_escape(title)}</summary>{body}</details>'


def _facts(items):
    return '<dl class="facts">' + "".join(
        f"<div><dt>{_escape(label)}</dt><dd>{value}</dd></div>" for label, value in items) + "</dl>"


def _meter(label, used, limit, detail):
    progress = (f'<meter min="0" max="{limit}" value="{min(used, limit)}" aria-label="{_escape(label)}"></meter>'
                if limit > 0 else "")
    return f'<div class="budget"><div><strong>{_escape(label)}</strong><span>{_escape(detail)}</span></div>{progress}</div>'


class DashboardError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address, config_path, *, chat=None, observer=None):
        if address[0] not in ("127.0.0.1", "localhost"):
            raise ValueError("The dashboard must bind to loopback")
        self.config_path = str(configuration_path(config_path))
        self.state = State.open(state_directory(self.config_path))
        self.chat_token = secrets.token_urlsafe(32)
        self.configuration_error = ""
        try:
            settings = load_chat_settings(self.config_path, enabled=chat)
        except (OSError, ValueError) as error:
            self.configuration_error = str(error)
            settings = ChatSettings(enabled=False)
        self.observer = observer or ObserverService(self.config_path, settings)
        self.reader_lease = CampaignLease(self.state.root).__enter__()
        try:
            super().__init__(address, DashboardHandler)
        except BaseException:
            self.reader_lease.close()
            self.observer.close()
            raise

    def server_close(self):
        try:
            self.observer.close()
        finally:
            try:
                super().server_close()
            finally:
                self.reader_lease.close()


class DashboardHandler(BaseHTTPRequestHandler):
    def _check_host(self):
        host = self.headers.get("Host", "").lower()
        allowed = {"localhost", "127.0.0.1", f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        if host not in allowed:
            raise DashboardError(403, "Use the dashboard's loopback address.")
        return host

    def do_GET(self):
        self.settings = {}
        self.view_snapshot = {}
        self.context = {}
        self.connection = None
        parsed = urllib.parse.urlsplit(self.path)
        self.path_name = parsed.path
        self.params = {k: values[0] for k, values in urllib.parse.parse_qs(parsed.query).items()}
        try:
            self._check_host()
            if self.path_name in ("/static/dashboard.css", "/static/dashboard.js", "/static/dashboard-chat.js"):
                name = self.path_name.rsplit("/", 1)[1]
                mime = "text/css" if name.endswith(".css") else "text/javascript"
                self._send((STATIC / name).read_bytes(), mime + "; charset=utf-8")
                return
            if self.path_name == "/chat/status":
                data = self.server.observer.availability()
                if self.params.get("conversation_id"):
                    data["conversation"] = self._chat_snapshot(self.params["conversation_id"])
                self._json(data)
                return
            self.state = self.server.state
            self.root, self.db = self.state.root, self.state.path
            routes = {
                "/": ("Research brief", self._overview), "/evidence": ("Evidence", self._evidence),
                "/work": ("Work", self._work), "/history": ("History", self._history),
                "/compare": ("Compare measurements", self._compare), "/changes": ("Recorded changes", self._changes),
                "/report": ("Retained report", self._report), "/jobs": ("Experiments", self._jobs),
                "/job": ("Experiment details", self._job), "/hypotheses": ("Hypotheses", self._hypotheses),
                "/hypothesis": ("Hypothesis details", self._hypothesis), "/activity": ("Agent activity", self._activity),
                "/turn": ("Agent turn", self._turn), "/artifacts": ("Artifacts", self._artifacts),
                "/observation": ("Evidence revision", self._observation), "/reports": ("Reports", self._reports),
                "/view": ("Historical source view", self._view), "/resources": ("Resources", self._resources),
                "/journal": ("Research journal", self._journal), "/goal": ("Research goal", self._goal),
                "/debug": ("Recovery", self._debug),
            }
            with self.state.db.read() as self.connection:
                self.settings = json.loads(self._rows(
                    "SELECT content FROM configs WHERE id=(SELECT config_revision FROM campaign)")[0]["content"])
                self.view_snapshot = snapshot(self.state, self.connection)
                self.current = self.view_snapshot["campaign"]
                self.budget = self.view_snapshot["budget"]
                if self.path_name == "/artifact":
                    self._download()
                    return
                if self.path_name not in routes:
                    raise DashboardError(404, "This dashboard page does not exist.")
                title, render = routes[self.path_name]
                body = self._page(title, render()).encode("utf-8")
            self.connection = None
            self._send(body)
        except (DashboardError, ChatError) as error:
            if self.path_name.startswith("/chat/"):
                self._json({"error": str(error)}, status=error.status)
            else:
                self._send(self._page(str(error.status), _empty("Page unavailable", str(error))).encode(), status=error.status)
        except ConnectionError as error:
            logging.getLogger(__name__).debug("Dashboard client disconnected: %s", error)
        except (OSError, sqlite3.Error, ValueError) as error:
            logging.getLogger(__name__).exception("Unable to read dashboard data")
            self._send(self._page("Data unavailable", _empty("Dashboard data unavailable", str(error))).encode(), status=503)

    def _chat_snapshot(self, conversation_id):
        value = self.server.observer.snapshot(conversation_id)
        for message in value["messages"]:
            message["html"] = _chat_markdown(message["answer"])
        return value

    def _json(self, data, *, status=200):
        self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status=status)

    def do_POST(self):
        try:
            host = self._check_host()
            path = urllib.parse.urlsplit(self.path).path
            if path not in ("/chat/message", "/chat/cancel", "/chat/clear"):
                raise DashboardError(404, "No dashboard control endpoint exists here.")
            if self.headers.get("Origin") not in (None, f"http://{host}"):
                raise DashboardError(403, "Cross-origin chat requests are not allowed.")
            token = self.headers.get("X-LabGoblin-Token", "")
            if not token.isascii() or not secrets.compare_digest(token, self.server.chat_token):
                raise DashboardError(403, "Reload the dashboard before using chat.")
            if self.headers.get_content_type() != "application/json":
                raise DashboardError(415, "Chat requests must use application/json.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise DashboardError(400, "Invalid content length.") from None
            if length <= 0 or length > 20000:
                raise DashboardError(413, "Chat request must be between 1 and 20000 bytes.")
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise DashboardError(400, "Chat request must be an object.")
            allowed = {"conversation_id", "request_id", "message", "context"} if path == "/chat/message" else {"conversation_id"}
            if set(data) - allowed or not isinstance(data.get("conversation_id", ""), str):
                raise DashboardError(400, "Invalid chat request fields.")
            cid = data.get("conversation_id", "")
            if path == "/chat/message":
                result = self.server.observer.send(cid, data.get("request_id"), data.get("message"), context=data.get("context"))
                self._json(self._chat_snapshot(result["conversation_id"]), status=202)
            elif path == "/chat/cancel":
                self.server.observer.cancel(cid)
                self._json(self._chat_snapshot(cid))
            else:
                self.server.observer.clear(cid)
                self._json({"cleared": True})
        except (DashboardError, ChatError) as error:
            self._json({"error": str(error)}, status=error.status)
        except ConnectionError as error:
            logging.getLogger(__name__).debug("Dashboard chat client disconnected: %s", error)
        except (ValueError, UnicodeError, TimeoutError) as error:
            self._json({"error": f"Invalid chat request: {error}"}, status=400)

    def _headers(self, status, mime, length):
        self.send_response(status)
        for name, value in (("Content-Type", mime), ("Content-Length", str(length)), ("Cache-Control", "no-store"),
                            ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
                            ("Referrer-Policy", "no-referrer")):
            self.send_header(name, value)

    def _send(self, body, mime="text/html; charset=utf-8", *, status=200):
        self._headers(status, mime, len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def _rows(self, sql, params=()):
        return [dict(row) for row in self.connection.execute(sql, params)]

    def _page(self, title, content):
        project = self.settings.get("project", {}).get("name", "Research dashboard")
        active = ROUTE_GROUP.get(self.path_name, self.path_name)
        nav = "".join(f'<a href="{path}"' + (' aria-current="page"' if active == path else "")
                      + f">{label}</a>" for path, label in NAV)
        subnav = '<nav class="section-nav" aria-label="Section navigation">' + "".join(
            f'<a href="{path}"' + (' aria-current="page"' if self.path_name == path else "")
            + f">{label}</a>" for path, label in SECTIONS.get(active, ())) + "</nav>" if active in SECTIONS else ""
        refresh = ('<label class="live-toggle"><input id="live-refresh" type="checkbox"> Check for updates (15s)</label>'
                   if self.path_name in ("/", "/work", "/jobs", "/activity", "/artifacts", "/resources") else "")
        current = self.view_snapshot.get("campaign", {})
        frame = {key: self.view_snapshot.get(key) for key in ("read_at", "source_cutoff", "event_cutoff", "change_token")}
        frame.update(campaign=current.get("id"), generation=current.get("generation"))
        context = {"label": title,
                   **{key: value for key, value in frame.items() if value is not None and key != "change_token"}, **self.context}
        context["label"] = context["label"][:240]
        if self.path_name in ({path for path, _ in NAV} | set(ROUTE_GROUP)):
            url = _url(self.path_name, **urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
            if len(url) <= 2048:
                context["url"] = url
            else:
                context["label"] = context["label"][:180] + " (filter URL omitted: exceeds 2,048 characters)"
        if self.server.configuration_error:
            content = ('<div class="notice danger">Current configuration unavailable; showing retained records. '
                       + _escape(self.server.configuration_error) + "</div>" + content)
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="labgoblin-chat-token" content="{self.server.chat_token}">
<title>{_escape(title)} | {_escape(project)} | LabGoblin</title>
<link rel="stylesheet" href="/static/dashboard.css">
<script src="/static/dashboard.js" defer></script></head>
<body><a class="skip-link" href="#main">Skip to content</a>
<aside class="sidebar"><a class="brand" href="/"><span class="brand-mark">L</span>LabGoblin</a>
<div class="project-label">RESEARCH WORKSPACE</div><div class="project-name">{_escape(project)}</div>
<span class="mode">Local campaign</span><nav aria-label="Main navigation">{nav}</nav>
<div class="sidebar-note"><span class="status-dot"></span> Read-only dashboard<br>
<span>Local evidence. No remote assets.</span></div></aside>
<div class="workspace"><header class="topbar"><div><span class="eyebrow">READ-ONLY RESEARCH</span>
<h1>{_escape(title)}</h1></div><div class="toolbar">{refresh}
<button id="chat-open" type="button" aria-controls="chat-panel" aria-expanded="false">Ask Copilot</button>
<button id="refresh" type="button">Refresh</button></div></header>
<div class="refresh-status" id="refresh-status" role="status" aria-live="polite">Page read at
{datetime.now(timezone.utc):%H:%M:%S} UTC &middot; Not a live process probe</div>
<main id="main" tabindex="-1" data-snapshot="{_escape(json.dumps(frame))}" data-chat-context="{_escape(json.dumps(context))}">{subnav}{content}</main>
<footer>Times in UTC &middot; Recorded state, not a live process probe &middot;
Manage campaigns through the CLI</footer></div>
<aside id="chat-panel" class="chat-panel" role="dialog" aria-modal="false" aria-labelledby="chat-title" hidden>
<div class="chat-heading"><div><span class="eyebrow">READ-ONLY OBSERVER</span><h2 id="chat-title">Dashboard Copilot</h2></div>
<button id="chat-close" type="button" aria-label="Close chat">Close</button></div>
<p class="chat-notice" id="chat-notice">On-demand answers grounded in campaign evidence. No steering or file edits.</p>
<div class="chat-model" id="chat-model"></div>
<p class="chat-context" id="chat-context"></p>
<div class="chat-messages" id="chat-messages" aria-label="Chat conversation">
<div class="empty" id="chat-empty"><h3>Understand the research</h3><p>Why is the campaign waiting? What changed recently?
Which conclusions have experimental support?</p></div></div>
<div id="chat-status" class="chat-status" role="status" aria-live="polite"></div>
<form id="chat-form"><label for="chat-question">Ask about this campaign</label>
<textarea id="chat-question" rows="3" maxlength="4000" placeholder="What is happening, and why?" required></textarea>
<div class="chat-actions"><button type="submit" id="chat-send" class="primary">Send</button>
<button type="button" id="chat-cancel" disabled>Cancel answer</button>
<button type="button" id="chat-clear">New chat</button></div></form></aside>
<script src="/static/dashboard-chat.js" defer></script></body></html>"""

    def _int(self, name, default=0):
        try:
            value = int(self.params.get(name, default))
        except ValueError:
            raise DashboardError(400, f"{name} must be an integer") from None
        if not 0 <= value <= 1_000_000_000:
            raise DashboardError(400, f"{name} is outside its supported range")
        return value

    def _pagination(self, count, *, page_size=PAGE_SIZE):
        page = self._int("page", 1)
        if page < 1:
            raise DashboardError(400, "Page must be positive")
        pages = max(1, (count + page_size - 1) // page_size)
        if page > pages:
            raise DashboardError(404, "This results page does not exist.")
        unit = "entries" if self.path_name == "/journal" else "records"
        controls = f"<span>{count:,} {unit} &middot; Page {page} of {pages}</span>"
        params = {k: v for k, v in self.params.items() if k not in ("page", "entry")}
        for other, label in ((page - 1, "Previous"), (page + 1, "Next")):
            if 1 <= other <= pages:
                controls += _link(self.path_name, label, **params, page=other)
        return (page - 1) * page_size, f'<nav class="pagination" aria-label="Results pages">{controls}</nav>'

    def _overview(self):
        current = self.current
        content = self._recovery_summary() if current["blockers"] else ""
        heads = dict((r["name"], r["source_id"]) for r in self._rows(
            "SELECT name,source_id FROM source_heads WHERE name IN ('goal','protocol','rationale')"))
        goal = self._source_excerpt(heads["goal"], 900) if "goal" in heads else _empty(
            "No recorded goal", "A research interpretation needs a versioned question and evaluation criteria.")
        content += _panel("The research question", goal, _link("/goal", "Goal & protocol"))
        rationale = None
        rationale_generation = None
        if "rationale" in heads:
            value = journal.entry(self.state.db, heads["rationale"])
            rationale = value.get("handoff")
            if rationale:
                self.context["source_id"] = heads["rationale"]
                turns = self._rows("SELECT generation FROM turns WHERE id=?", (rationale["turn_id"],))
                rationale_generation = turns[0]["generation"] if turns else None
        closed = current["generation_state"] != "open"
        if closed:
            answer = self._closure_summary()
        elif rationale:
            answer = (f'<h2 class="brief-answer">{_escape(rationale["summary"])}</h2>'
                      f'<div class="markdown">{_markdown(rationale["rationale"][:2400])}</div>'
                      + ('<p class="muted">Rationale excerpt; open the retained entry for the full text.</p>'
                         if len(rationale["rationale"]) > 2400 else "")
                      + f'<p class="muted">Recorded researcher interpretation &middot; {_timestamp(value["created"])}. '
                      "Not an independently verified scientific verdict.</p>"
                      + (f'<p class="notice">Historical rationale from generation {_escape(rationale_generation)}; '
                         'not a newly accepted plan for this generation.</p>'
                         if rationale_generation != current["generation"] else "")
                      + self._reference_links([ref for item in rationale["evidence"] for ref in item["references"]])
                      + '<div class="actions">' + _link("/journal", "Read the recorded rationale", entry=heads["rationale"])
                      + _link("/evidence", "Inspect evidence") + "</div>")
        else:
            answer = _empty("No recorded research interpretation yet",
                            "Experiment activity alone does not establish a finding. No agent turns yet with an accepted research handoff.")
        next_step = (rationale["next_step"][:1600] if rationale else current["reason"])
        if closed:
            next_step = "This generation is sealed. Earlier next-step instructions are historical, not a new work schedule."
        elif current["operator_mode"] in ("paused", "stopping", "stopped"):
            next_step = f'Operator intent is {current["operator_mode"]}. The last research plan is not permission for new admission.'
        elif current["blockers"]:
            next_step = "Resolve the recorded blocker before assuming the research plan can proceed."
        elif rationale and rationale_generation != current["generation"]:
            next_step = "No accepted next step is recorded for this generation. The prior research plan is historical."
        now = (f'<p class="next-step">{_escape(next_step or "No next step has been recorded.")}</p>'
               + self._lifecycle()
               + '<p class="muted">No complete action-owner record is available. Absence of a blocker is not an all-clear.</p>'
               + _link("/work", "Inspect work and limits"))
        content += '<div class="brief-grid">' + _panel(
            "Closure handoff" if closed else "Latest recorded interpretation", answer) + _panel("Now & next", now) + "</div>"
        source_cutoff = self.view_snapshot["source_cutoff"]
        events = self.view_snapshot["event_cutoff"]
        catchup = ('<div class="catchup-controls" hidden><p id="catchup-status" role="status"></p>'
                   '<div class="actions"><button type="button" id="catchup-save">Mark caught up</button>'
                   '<button type="button" id="catchup-clear">Forget checkpoint</button>'
                   '<a id="catchup-link" href="/changes">Inspect change interval</a></div>'
                   '<p class="muted">Saves the displayed source/event cutoffs on this browser only. '
                   'Does not acknowledge research events. Latest records below are a bounded preview, not the entire interval.</p></div>')
        content += '<div class="brief-grid">' + _panel(
            "Recent recorded changes", catchup + self._recent_sources(0, source_cutoff, limit=4),
            _link("/changes", "All recorded changes", source_cutoff=source_cutoff, event_cutoff=events))
        content += _panel("Admission allowance", self._budget_summary(), _link("/resources", "All limits")) + "</div>"
        return content

    def _source_excerpt(self, source_id, limit=1600):
        value = source_preview(self.connection, source_id, limit)
        return (f'<div class="markdown">{_markdown(value["text"])}</div>'
                + ('<p class="muted">Bounded excerpt; full source remains available.</p>' if value["truncated"] else "")
                + '<div class="source-line">' + _timestamp(value["created"]) + " &middot; "
                + _link("/journal", "Exact " + value["kind"] + " revision", entry=source_id) + "</div>")

    def _lifecycle(self):
        current = self.current
        active = self._rows("SELECT COUNT(*) n FROM attempts a WHERE a.status IN (" +
                            ",".join("?" for _ in ACTIVE) + ")", tuple(ACTIVE))[0]["n"]
        return _facts([
            ("Operator intent", _badge(current["operator_mode"])),
            ("Research progression", _badge(current["progress"])),
            ("Research outcome", _badge(current["research_outcome"])),
            ("Recorded active work", f"{active} attempts; includes queued and unknown ownership"),
            ("Execution knowledge", "Recorded state only; live execution is not verified by this page"),
            ("Recovery", _link("/debug", f'{len(current["blockers"])} unresolved blockers')),
        ])

    def _budget_summary(self):
        content = ""
        for key, label in (("elapsed_admission_seconds", "Elapsed admission horizon"), ("managed_invocations", "Managed invocations")):
            value = self.budget[key]
            formatter = _duration if key == "elapsed_admission_seconds" else lambda n: f"{n:,.0f}"
            limit = "Unlimited" if value["unlimited"] else formatter(value["configured"])
            remaining = "Unlimited" if value["unlimited"] else formatter(value["remaining"]) + " remaining"
            detail = f'{formatter(value["used"])} / {limit}; {remaining}'
            if value["reserved"]:
                detail += f'; {formatter(value["reserved"])} reserved'
            content += _meter(label, value["used"] + value["reserved"], value["configured"], detail)
        return content + ('<p class="muted">Last admitted limits. Pauses and restarts count toward the elapsed horizon. '
                          'Expiry gates new admission; admitted work keeps its own finite deadline. '
                          'Invocation commitments include armed/uncertain launches, not tokens or money. '
                          'Ordinary research is also subject to the final-analysis reserve. Observer usage is separate.</p>')

    def _closure_summary(self):
        current = self.current
        generation = self._rows("SELECT * FROM generations WHERE id=?", (current["generation"],))[0]
        content = f'<h2 class="brief-answer">{_escape(current["research_outcome"].replace("_", " ").capitalize())}</h2>'
        content += f'<p>{_escape(generation["reason"] or current["reason"] or "No closure rationale recorded.")}</p>'
        content += _facts([("Generation", str(current["generation"])), ("Admission sealed", _timestamp(generation["sealed"])),
                           ("Generation state", _badge(generation["state"]))])
        if current["research_outcome"] != "assessed":
            content += '<p class="notice">No assessed demonstration of the goal is recorded by this closure. Closed is not scientific success.</p>'
        if current["assessment_scope_stale"]:
            content += '<p class="notice danger">Later goal/protocol/constraints changed. The historical assessment does not cover that newer authority.</p>'
        if generation["view_id"]:
            content += '<div class="actions">' + _link("/view", "Sealed evidence inventory", id=generation["view_id"])
            reports = self._rows("SELECT id FROM reports WHERE view_id=? ORDER BY created DESC LIMIT 1", (generation["view_id"],))
            content += (_link("/report", "Open retained report", id=reports[0]["id"]) if reports else
                        '<span>No report output is registered for this closure view.</span>') + "</div>"
        else:
            content += '<p class="muted">A closure source view is not recorded yet.</p>'
        return content + '<p class="muted">Operational ownership remains a separate recorded fact; this page performs no liveness probe.</p>'

    def _recent_sources(self, after, cutoff, *, limit=20, offset=0):
        rows = self._rows("""SELECT id,seq,kind,created,substr(CAST(body AS TEXT),1,500) preview,
            CASE WHEN kind='handoff' THEN substr(json_extract(CAST(body AS TEXT),'$.summary'),1,240) END summary
            FROM sources WHERE seq>? AND seq<=? AND kind IN ('goal','protocol','handoff','journal_import','summary','directive')
            ORDER BY seq DESC LIMIT ? OFFSET ?""", (after, cutoff, limit, offset))
        if not rows:
            return _empty("No recorded research changes in this interval", "This does not establish the absence of unrecorded activity.")
        return '<ol class="change-list">' + "".join(
            f'<li><div class="source-line">{_escape(r["kind"].replace("_", " "))} &middot; {_timestamp(r["created"])}</div>'
            + _link("/journal", r["summary"] or (r["preview"].splitlines() or [""])[0][:200] or r["kind"], entry=r["id"])
            + "</li>" for r in rows) + "</ol>"

    def _reference_links(self, references, *, view_id=""):
        links = []
        for ref in dict.fromkeys(references):
            row = self._rows("SELECT kind FROM sources WHERE id=?", (ref,))
            if row and row[0]["kind"] in RESEARCH_SOURCES:
                link = _link("/journal", "Retained " + row[0]["kind"], entry=ref)
            elif self._rows("SELECT id FROM observations WHERE id=?", (ref,)):
                link = _link("/observation", "Exact observation", id=ref, view=view_id)
            elif self._rows("SELECT id FROM views WHERE id=?", (ref,)):
                link = _link("/view", "Historical inventory", id=ref)
            else:
                link = f'<span>Reference {_escape(ref)} is unavailable on this surface.</span>'
            links.append("<li>" + link + "</li>")
        return ('<details class="reference-links"><summary>Recorded evidence references (not an inferred support/contradiction classification)</summary><ul>'
                + "".join(links) + "</ul></details>") if links else (
                    '<p class="muted">Narrative only: exact evidence links are not recorded in this handoff. Do not infer them from names or values.</p>')

    def _evidence(self):
        return ('<p class="intro">Questions and recorded measurements, not a leaderboard. '
                'Capture integrity, validation and scientific assessment are different facts.</p>'
                + self._hypotheses()
                + _panel("Inspect the measurement record", '<p>Read exact numeric values with their validation context, including invalid and negative results. '
                         'Similar experiment names do not establish comparable methods or paired baselines.</p><div class="actions">'
                         + _link("/artifacts", "Browse measurements") + _link("/compare", "Compare exact observations") + "</div>"))

    def _work(self):
        content = self._recovery_summary() if self.current["blockers"] else _panel(
            "No recovery blockers recorded", '<p>This is not proof of live execution or a complete owner-action checklist.</p>')
        content += _panel("Now & next", f'<p>{_escape(self.current["reason"] or "No next action recorded.")}</p>' + self._lifecycle())
        cards = []
        for status, label, hint in (("active", "Recorded active work", "Includes queued and unknown ownership"),
                                    ("attention", "Recorded problems", "Execution, collection or validation; not necessarily a human task"),
                                    ("", "All experiments", "Complete history, including work not performed")):
            where, args = attempt_filter(status)
            count = self._rows(f"SELECT COUNT(*) n FROM attempts a WHERE {where}", args)[0]["n"]
            cards.append(f'<a class="stat-card" href="{_escape(_url("/jobs", status=status))}"><span>{label}</span>'
                         f'<strong>{count}</strong><small>{hint}</small></a>')
        content += '<div class="stat-grid">' + "".join(cards) + "</div>"
        turns = self._rows("SELECT * FROM turns ORDER BY created DESC LIMIT 1")
        return content + '<div class="two-column">' + _panel("Admission allowance", self._budget_summary()) + _panel(
            "Latest agent turn (recorded history)", self._turn_summary(turns[0]) if turns else _empty(
                "No agent turns yet", "No owned reasoning turn is recorded.")) + "</div>"

    def _history(self):
        count = self._rows("SELECT COUNT(*) n FROM generations")[0]["n"]
        offset, paging = self._pagination(count, page_size=20)
        generations = self._rows("""SELECT id,state,outcome,created,sealed,reason,view_id FROM generations
            ORDER BY id DESC LIMIT 20 OFFSET ?""", (offset,))
        content = _panel("Research generations", _table(["Generation", "Recorded outcome", "Admission sealed", "Sources"], [
            [str(r["id"]), _badge(r["outcome"]) + f'<p>{_escape(r["reason"])}</p>', _timestamp(r["sealed"]),
             _link("/view", "Sealed inventory", id=r["view_id"]) if r["view_id"] else "No sealed source view"] for r in generations])) + paging
        content += _panel("Recent research entries", self._recent_sources(0, self.view_snapshot["source_cutoff"], limit=6),
                          _link("/journal", "Read journal"))
        return content + self._reports(limit=5)

    def _changes(self):
        if self.params.get("campaign", self.current["id"]) != self.current["id"]:
            raise DashboardError(409, "This saved checkpoint belongs to another campaign.")
        if "generation" in self.params and self._int("generation") != self.current["generation"]:
            raise DashboardError(409, "The generation changed. Start a new viewing checkpoint; previous research remains in History.")
        source_cutoff = self._int("source_cutoff", self.view_snapshot["source_cutoff"])
        event_cutoff = self._int("event_cutoff", self.view_snapshot["event_cutoff"])
        after_source, after_event = self._int("after_source"), self._int("after_event")
        if not (after_source <= source_cutoff <= self.view_snapshot["source_cutoff"]
                and after_event <= event_cutoff <= self.view_snapshot["event_cutoff"]):
            raise DashboardError(409, "This comparison interval is unavailable in the retained history.")
        self.params.update(source_cutoff=str(source_cutoff), event_cutoff=str(event_cutoff))
        source_count = self._rows("""SELECT COUNT(*) n FROM sources WHERE seq>? AND seq<=?
            AND kind IN ('goal','protocol','handoff','journal_import','summary','directive')""", (after_source, source_cutoff))[0]["n"]
        event_count = self._rows("SELECT COUNT(*) n FROM events WHERE seq>? AND seq<=?", (after_event, event_cutoff))[0]["n"]
        offset, paging = self._pagination(max(source_count, event_count), page_size=20)
        sources = self._recent_sources(after_source, source_cutoff, limit=20, offset=offset)
        events = self._rows("""SELECT seq,kind,created,substr(json_extract(payload,'$.reason'),1,400) reason,
            json_extract(payload,'$.attempt_id') attempt_id,json_extract(payload,'$.action') action
            FROM events WHERE seq>? AND seq<=? ORDER BY seq DESC LIMIT 20 OFFSET ?""", (after_event, event_cutoff, offset))
        work = _table(["Recorded event", "When", "Context"], [
            [_escape(r["kind"].replace("_", " ")), _timestamp(r["created"]),
             (_link("/job", "Inspect affected experiment", id=r["attempt_id"]) if r["attempt_id"] else "")
             + f'<p>{_escape(r["reason"] or r["action"] or "No additional event summary recorded.")}</p>'] for r in events])
        return (f'<p class="intro">{source_count} research sources and {event_count} operational events in this explicit interval. '
                f'Source sequences ({after_source}, {source_cutoff}]; event sequences ({after_event}, {event_cutoff}]. '
                'Ordered by ingestion, not inferred scientific importance. Research event acknowledgements are not personal unread markers.</p>'
                + paging + '<div class="two-column">' + _panel("Research record changes", sources)
                + _panel("Operational record changes", work) + "</div>" + paging)

    def _job_table(self, rows):
        return _table(["Experiment", "Execution", "Collection / validation", "Runtime", "Submitted"], [
            [_link("/job", r["experiment_id"], id=r["id"]),
             _badge(r["status"]), _badge(r["collection"]) + " " + _badge(r["validation"]),
             _duration(r["elapsed"]), _timestamp(r["created"])] for r in rows])

    def _jobs(self):
        status, query = self.params.get("status", ""), self.params.get("q", "")
        condition, args = attempt_filter(status, query, self.params.get("hypothesis_id", ""))
        count = self._rows(f"SELECT COUNT(*) n FROM attempts a WHERE {condition}", args)[0]["n"]
        offset, paging = self._pagination(count)
        rows = self._rows(f"SELECT a.* FROM attempts a WHERE {condition} ORDER BY a.created DESC,a.id DESC LIMIT ? OFFSET ?",
                          (*args, PAGE_SIZE, offset))
        statuses = ["", "active", "attention", "invalid"] + [r["status"] for r in self._rows("SELECT DISTINCT status FROM attempts ORDER BY status")]
        labels = {"": "All statuses", "active": "Recorded active work", "attention": "Recorded problems", "invalid": "Invalid measurements"}
        options = "".join(f'<option value="{_escape(s)}"' + (" selected" if s == status else "")
                          + f'>{_escape(labels.get(s, s.replace("_", " ")))}</option>' for s in statuses)
        filters = (f'<form class="filters" action="/jobs"><label>Search experiments<input type="search" name="q" value="{_escape(query)}"></label>'
                   f'<label>Status<select name="status">{options}</select></label><label>Hypothesis<input name="hypothesis_id" '
                   f'value="{_escape(self.params.get("hypothesis_id", ""))}"></label><button type="submit">Filter</button></form>')
        return (filters + '<p class="intro">All retained attempts remain inspectable. Recorded problems include execution, collection or validation '
                'problems; they are not necessarily outstanding human tasks.</p>' + _panel("Experiment history", self._job_table(rows)) + paging)

    def _job(self):
        rows = self._rows("SELECT * FROM attempts WHERE id=?", (self.params.get("id", ""),))
        if not rows:
            raise DashboardError(404, "Unknown experiment.")
        row = rows[0]
        self.context.update(attempt_id=row["id"], label=row["experiment_id"])
        spec = json.loads(row.pop("spec"))
        content = _panel(row["experiment_id"], _facts([
            ("Execution", _badge(row["status"])), ("Validation", _badge(row["validation"])),
            ("Collection", _badge(row["collection"])), ("Runtime", _duration(row["elapsed"])),
            ("Exit code", _escape(row["exit_code"]) if row["exit_code"] is not None else "Not recorded"),
            ("Reason", _escape(row["reason"]) or "Not recorded"),
            ("Collection reason", _escape(row["collection_reason"]) or "Not recorded"), ("Generation", str(row["generation"])),
            ("Hypothesis", _link("/hypothesis", row["hypothesis_id"], id=row["hypothesis_id"]) if row["hypothesis_id"] else "Support work"),
        ]))
        if row["status"] == "recovery_required":
            content += self._inspection(row["id"])
        content += _panel("Registered evidence", self._artifact_table(self._observations("WHERE o.attempt_id=?", (row["id"],))))
        return (content + self._work_logs(row["id"]) + _details("Technical details: frozen provenance",
                f'<pre>{_escape(json.dumps({k: v for k, v in spec.items() if k not in ("environment", "inputs")}, indent=2))}</pre>'))

    def _hypotheses(self):
        count = self._rows("SELECT COUNT(*) n FROM hypotheses")[0]["n"]
        offset, paging = self._pagination(count)
        rows = self._rows("""SELECT h.*,COUNT(a.id) attempts FROM hypotheses h LEFT JOIN attempts a ON a.hypothesis_id=h.id
            GROUP BY h.id ORDER BY h.created DESC,h.id LIMIT ? OFFSET ?""", (PAGE_SIZE, offset))
        return _panel("Research questions", '<p class="muted">Recorded hypothesis labels are not certified scientific verdicts. '
                      'A frozen statement means evaluation was admitted, not that the hypothesis was accepted.</p>'
                      + _table(["Hypothesis statement", "Recorded label", "Evaluation admission", "Experiments", "Updated"], [
            [_link("/hypothesis", r["statement"][:240], id=r["id"]) + f'<code class="identifier">{_escape(r["id"])}</code>',
             _badge(r["status"]), "Frozen" if r["frozen"] else "Not yet admitted", str(r["attempts"]), _timestamp(r["updated"])]
            for r in rows])) + paging

    def _hypothesis(self):
        hid = self.params.get("id", "")
        rows = self._rows("SELECT * FROM hypotheses WHERE id=?", (hid,))
        if not rows:
            raise DashboardError(404, "Unknown hypothesis.")
        row = rows[0]
        self.context.update(hypothesis_id=hid, label=row["statement"][:160])
        content = _panel("Hypothesis statement", f'<div class="markdown">{_markdown(row["statement"])}</div>' + _facts([
            ("Recorded hypothesis label", _badge(row["status"]) + '<p>Not an independently verified assessment.</p>'),
            ("Evaluation admission", "Frozen; changed claims require a new ID" if row["frozen"] else "Not yet admitted; a status label alone is not experimental support"),
            ("Supersedes", _link("/hypothesis", row["supersedes"], id=row["supersedes"]) if row["supersedes"] else "None"),
        ]))
        if row["conclusion"]:
            content += _panel("Recorded conclusion", f'<div class="markdown">{_markdown(row["conclusion"])}</div>')
        metadata = json.loads(row["metadata"])
        if metadata:
            content += _details("Technical claim metadata", f'<pre>{_escape(json.dumps(metadata, indent=2))}</pre>')
        references = self._rows("""SELECT DISTINCT h.source_id,h.created FROM dispositions d
            JOIN turns t ON t.id=d.turn_id AND t.state='accepted' JOIN handoffs h ON h.turn_id=t.id
            JOIN json_each(d.reference_ids) refs JOIN observations o ON o.id=refs.value
            JOIN attempts a ON a.id=o.attempt_id WHERE a.hypothesis_id=? ORDER BY h.created DESC LIMIT 10""", (hid,))
        content += _panel("Assessments referencing this hypothesis's observations",
                          '<ul>' + "".join("<li>" + _link("/journal", "Exact recorded assessment", entry=r["source_id"]) + "</li>"
                                          for r in references) + "</ul>" if references else
                          '<p>No explicit assessment-to-observation links were found for this hypothesis. Related prose below is lexical context, not proof of support.</p>')
        matches = journal.search(self.state.db, hid, result_limit=6)
        content += _panel("Related journal context", self._search_results(matches))
        return content + _panel("Recent experiments", self._job_table(self._rows(
            "SELECT * FROM attempts WHERE hypothesis_id=? ORDER BY created DESC LIMIT 10", (hid,))),
            _link("/jobs", "All experiments", hypothesis_id=hid))

    def _turn_summary(self, turn):
        result = json.loads(turn["result"] or "{}")
        return (_badge(turn["state"]) + f'<p>{_escape(result.get("reason") or turn["reason"] or "No decision recorded yet.")}</p>'
                + _facts([("Prepared", _timestamp(turn["created"])), ("Ended", _timestamp(turn["ended"])),
                          ("Disposition", _escape(result.get("disposition", "Not recorded")))])
                + _link("/turn", "Inspect turn and logs", id=turn["id"]))

    def _activity(self):
        count = self._rows("SELECT COUNT(*) n FROM turns")[0]["n"]
        offset, paging = self._pagination(count)
        rows = self._rows("SELECT * FROM turns ORDER BY created DESC LIMIT ? OFFSET ?", (PAGE_SIZE, offset))
        content = _panel("Agent turns", _table(["Turn", "State", "Prepared", "Ended"], [
            [_link("/turn", r["kind"], id=r["id"]), _badge(r["state"]), _timestamp(r["created"]), _timestamp(r["ended"])] for r in rows]))
        events = self._rows("SELECT id,seq,kind,created,acknowledged_by FROM events ORDER BY seq DESC LIMIT 30")
        return content + paging + _panel("Latest 30 event headers", _table(["Sequence", "Kind", "Recorded", "Acknowledged"], [
            [str(r["seq"]), _escape(r["kind"]), _timestamp(r["created"]),
             _link("/turn", r["acknowledged_by"], id=r["acknowledged_by"]) if r["acknowledged_by"] else "Awaiting acknowledgement"] for r in events]))

    def _turn(self):
        rows = self._rows("SELECT * FROM turns WHERE id=?", (self.params.get("id", ""),))
        if not rows:
            raise DashboardError(404, "Unknown agent turn.")
        turn = rows[0]
        self.context.update(turn_id=turn["id"])
        invocations = self._rows("SELECT * FROM invocations WHERE turn_id=? ORDER BY bundle_position", (turn["id"],))
        content = _panel("Turn decision", self._turn_summary(turn))
        content += _panel("Invocation accounting", _table(["Kind", "State", "Usage", "Reason"], [
            [_escape(r["kind"]), _badge(r["state"]), _escape(r["usage"] or "Not reported (not zero)"), _escape(r["reason"])]
            for r in invocations]))
        handoffs = self._rows("SELECT source_id FROM handoffs WHERE turn_id=?", (turn["id"],))
        if handoffs:
            content += self._source_article(handoffs[0]["source_id"], focused=True)
        for row in invocations:
            content += self._work_logs(row["id"])
        return content

    def _work_logs(self, work_id):
        from labgoblin.protocol import LaunchEnvelope
        launches = self._rows("SELECT envelope FROM launches WHERE work_id=? ORDER BY created DESC LIMIT 1", (work_id,))
        if not launches:
            return _panel("Retained diagnostics", '<p class="muted">No owned launch or retained stream is registered for this work. '
                          'Missing logs do not establish process death.</p>')
        envelope = LaunchEnvelope.parse(json.loads(launches[0]["envelope"]))
        directory = launch_directory(envelope)
        content = ""
        for stage in ("main", "supervisor"):
            for name in ("stdout", "stderr"):
                path = directory / stage / f"{name}.log"
                if path.exists():
                    value = tail(path, limit=LOG_LIMIT)
                    content += _details(f"{stage} {name}", '<p class="muted">Bounded retained tail; earlier output may be omitted.</p>'
                                        f'<pre>{_escape(value["text"])}</pre>')
        return _panel("Captured logs", content or '<p class="muted">No retained main/supervisor streams are available.</p>')

    def _observations(self, where="", params=(), limit=PAGE_SIZE, offset=0):
        return self._rows(f"""SELECT o.id,o.attempt_id,o.path,o.kind,o.size,o.digest,o.assurance,o.created,
            o.metadata,o.assurance='captured' AS captured,a.experiment_id,a.status,a.validation,a.collection,a.reason,
            a.collection_reason FROM observations o JOIN attempts a ON a.id=o.attempt_id
            {where} ORDER BY o.created DESC,o.id DESC LIMIT ? OFFSET ?""", (*params, limit, offset))

    def _artifact_table(self, rows):
        if not rows:
            return _empty("No registered evidence in this scope", "A missing capture is not a zero result or proof of successful research.")
        cards = []
        for row in rows:
            metrics, total = numeric_metrics(row["metadata"], count=4, byte_limit=1024)
            cards.append('<article class="measurement-card"><h3>' + _link("/observation", row["path"], id=row["id"])
                         + "</h3>" + _link("/job", row["experiment_id"], id=row["attempt_id"])
                         + self._validity(row)
                         + _facts([(k, _escape(v)) for k, v in metrics.items()])
                         + f'<p class="muted">{len(metrics)} of {total} numeric metrics; exact recorded keys, units are not inferred.</p>'
                         + '<div class="actions">' + _link("/observation", "Inspect exact observation", id=row["id"])
                         + f'<label class="compare-choice"><input type="checkbox" name="id" value="{_escape(row["id"])}"> Compare</label></div></article>')
        return ('<form action="/compare" class="measurement-selection"><p class="muted">Choose up to four observations on this page. '
                'Selection does not imply comparability.</p><div class="measurement-grid">'
                + "".join(cards) + '</div><button type="submit">Compare selected observations</button></form>')

    def _validity(self, row):
        warning = ""
        if row["validation"] == "invalid" or row["collection"] == "failed":
            warning = '<p class="validation-warning">' + _escape(row["reason"] or row["collection_reason"] or "Validation/collection failed; no reason was recorded.") + "</p>"
        return ('<div class="evidence-validity"><span>Execution: ' + _badge(row["status"])
                + "</span><span>Collection: " + _badge(row["collection"]) + "</span><span>Validation: "
                + _badge(row["validation"]) + "</span></div>" + warning)

    def _artifacts(self):
        needle = self.params.get("q", "")
        condition = "WHERE instr(lower(o.path),lower(?))>0 OR instr(lower(a.experiment_id),lower(?))>0"
        params = (needle, needle)
        count = self._rows(f"SELECT COUNT(*) n FROM observations o JOIN attempts a ON a.id=o.attempt_id {condition}", params)[0]["n"]
        offset, paging = self._pagination(count)
        filters = f'<form class="filters"><label>Find evidence<input name="q" value="{_escape(needle)}"></label><button>Search</button></form>'
        return filters + _panel("Evidence bank", self._artifact_table(self._observations(condition, params, offset=offset))) + paging

    def _observation(self):
        oid = self.params.get("id", "")
        if not self._rows("SELECT id FROM observations WHERE id=?", (oid,)):
            raise DashboardError(404, "Unknown evidence revision.")
        view_id = self.params.get("view", "")
        row = observation_context(self.connection, oid, view_id=view_id)
        self.context.update(observation_id=oid, view_id=view_id, label=row["experiment_id"] + ": " + row["path"])
        metrics = row["metrics"]
        content = self._historical_scope(view_id) if view_id else '<p class="intro">Exact retained bytes; validation below is current recorded attempt context, not a live check.</p>'
        content += _panel(row["experiment_id"] + " / " + row["path"], self._validity(row) + _facts([
            ("Byte assurance", _escape(row["assurance"]) + "; describes retained bytes, not scientific acceptance"),
            ("Captured at", _timestamp(row["created"])),
        ]))
        content += _panel("Recorded numeric metrics", _table(["Metric", "Value"], [
            [_escape(k), _escape(v)] for k, v in metrics.items()]))
        content += (f'<p class="muted">Showing {len(metrics)} of {row["metric_count"]} metrics. Names are recorded keys; '
                    'units, comparator pairing and scientific support are not inferred.</p>')
        if view_id:
            content += '<p>' + _link("/view", "Attempt as recorded in this inventory", id=view_id, attempt=row["attempt_id"]) + "</p>"
        else:
            assessments = self._rows("""SELECT d.disposition,d.reason,h.source_id,h.created FROM dispositions d
                JOIN handoffs h ON h.turn_id=d.turn_id JOIN turns t ON t.id=h.turn_id AND t.state='accepted'
                WHERE EXISTS(SELECT 1 FROM json_each(d.reference_ids) j WHERE j.value=?)
                ORDER BY h.created DESC LIMIT 10""", (oid,))
            content += _panel("Recorded assessment references (latest 10)", _table(["Treatment", "Reason", "Source"], [
                [_escape(r["disposition"]), _escape(r["reason"]), _link("/journal", "Exact assessment", entry=r["source_id"])]
                for r in assessments], empty="No assessment reference recorded", explanation="Captured or validated evidence is not automatically assessed."))
        content += '<div class="actions">' + _link("/job", "Current experiment", id=row["attempt_id"])
        if row["assurance"] == "captured" and row["size"] <= 1024 * 1024:
            content += _link("/artifact", "Captured download", id=oid)
        content += "</div>"
        return content + _details("Integrity metadata", _facts([("SHA-256", _escape(row["digest"])), ("Size", _size(row["size"])), ("Observation ID", _escape(oid))]))

    def _compare(self):
        ids = list(dict.fromkeys(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id", [])))
        if len(ids) > 4:
            raise DashboardError(400, "Select at most four exact observations for comparison.")
        if not ids:
            return _empty("Select observations to compare", "Choose up to four records from Measurements. No research state is changed.") + _link("/artifacts", "Browse measurements")
        content = ('<p class="notice">Not comparable as a paired scientific estimate: explicit comparator, condition, protocol and unit semantics '
                   'are not established by this selection. Values below are exact individual observations, not a ranking or aggregated effect. '
                   'Invalid measurements must not enter a primary estimate.</p><div class="comparison-grid">')
        for oid in ids:
            value = observation_context(self.connection, oid)
            content += _panel(value["experiment_id"], self._validity(value) + _facts([
                (k, _escape(v)) for k, v in value["metrics"].items()])
                + f'<p class="muted">{len(value["metrics"])} of {value["metric_count"]} metrics; no unit conversion applied.</p>',
                _link("/observation", "Exact source", id=oid))
        return content + "</div>"

    def _download(self):
        oid = self.params.get("id", "")
        rows = self._rows("SELECT assurance='captured' AS captured,size FROM observations WHERE id=?", (oid,))
        if not rows:
            raise DashboardError(404, "Unknown evidence revision.")
        if not rows[0]["captured"] or rows[0]["size"] > 1024 * 1024:
            raise DashboardError(409, "No bounded exact capture is available. A mutable current file is not a historical download.")
        value = observation(self.state.db, oid)
        body = value["body"]
        self._headers(200, "application/octet-stream", len(body))
        self.send_header("Content-Disposition", "attachment")
        self.send_header("X-Content-SHA256", value["digest"])
        self.end_headers()
        self.wfile.write(body)

    def _resources(self):
        budget = self.budget
        content = _panel("Campaign admission limits", self._budget_summary()
                         + _facts([("Committed CPUs", str(budget["resources"]["committed_cpus"])),
                                   ("Committed memory", f'{budget["resources"]["committed_memory_mb"]} MiB'),
                                   ("Experiment slots", f'{budget["active_experiments"]} / {budget["maximum_experiments"]}')])
                         + '<p class="muted">Includes managed reasoning and experiments. CPU affinity is placement, not a native CPU-time quota.</p>')
        technical = _details("Technical accounting and configured storage", f'<pre>{_escape(json.dumps(budget, indent=2))}</pre>'
                             + f'<pre>{_escape(json.dumps(self.settings["storage"], indent=2))}</pre>'
                             '<p class="muted">Soft monitoring, not a hard disk quota. Use storage inventory for owned sizes and reference reachability.</p>')
        path, identity = self.state.ledger_identity()
        if not path.exists():
            return content + _panel("Shared machine capacity", _empty("Not configured", "Recorded ledger is missing; it has not been created.")) + technical
        ledger = ResourceLedger(path, expected_id=identity or None)
        offset = self._int("ledger_offset")
        with ledger.read() as conn:
            capacity = ledger._capacity(conn)
            totals = conn.execute("""SELECT COUNT(*) AS records,
                COALESCE(SUM(CASE WHEN state='granted' THEN cpus ELSE 0 END),0) AS cpus,
                COALESCE(SUM(CASE WHEN state='granted' THEN memory_mb ELSE 0 END),0) AS memory
                FROM grants WHERE state IN ('pending','granted','rejected')""").fetchone()
            if offset and offset >= totals["records"]:
                raise DashboardError(404, "This machine-ledger page does not exist.")
            rows = [dict(row) for row in conn.execute("""SELECT work_id,kind,state,cpus,memory_mb,native_cpus,reason
                FROM grants WHERE state IN ('pending','granted','rejected') ORDER BY sequence LIMIT 100 OFFSET ?""", (offset,))]
        content += _panel("Shared machine capacity", _meter("Reserved CPUs", totals["cpus"], capacity["cpus"],
                          f'{totals["cpus"]} / {capacity["cpus"]}')
                          + _meter("Reserved memory (MiB)", totals["memory"], capacity["memory_mb"],
                                   f'{totals["memory"]} / {capacity["memory_mb"]}')
                          + _facts([("Headroom", f'{capacity["headroom_mb"]} MiB'), ("Capacity revision", str(capacity["revision"]))]))
        content += _panel(f'Machine requests and grants ({offset + 1 if rows else 0}-{offset + len(rows)} of {totals["records"]})',
                          '<p class="muted">Capacity totals include every granted reservation, not just this page. Shared-ledger read is separate from the campaign snapshot; '
                          'a grant is a commitment, not a live process probe.</p>'
                          + _table(["Work", "Consumer", "State", "CPU / RAM", "Placement", "Queue reason"], [
            [_escape(r["work_id"]), _escape(r["kind"]), _badge(r["state"]), f'{r["cpus"]} / {r["memory_mb"]} MiB',
             _escape(r["native_cpus"]), _escape(r["reason"])] for r in rows]))
        if offset:
            content += _link("/resources", "Previous machine page", ledger_offset=max(0, offset - 100))
        if offset + len(rows) < totals["records"]:
            content += _link("/resources", "Next machine page", ledger_offset=offset + len(rows))
        return content + technical

    def _source_article(self, source_id, *, focused=False):
        rows = self._rows("SELECT id,seq,kind,created,length(body) bytes FROM sources WHERE id=?", (source_id,))
        if not rows or rows[0]["kind"] not in RESEARCH_SOURCES:
            raise DashboardError(404, "Exact retained research source is unavailable.")
        row = rows[0]
        limit = DOCUMENT_LIMIT if focused else JOURNAL_PREVIEW_LIMIT
        if row["bytes"] <= limit:
            value = journal.entry(self.state.db, source_id, limit=limit)
            text = value["markdown"]
            more = (self._reference_links([ref for item in value["handoff"]["evidence"] for ref in item["references"]])
                    if value.get("handoff") else "")
        else:
            value = journal.entry_page(self.state.db, source_id, offset=self._int("byte_offset") if focused else 0, limit=16384)
            text = value["text"]
            more = '<div class="notice">Bounded byte-page preview; the full original remains retained.</div>'
            if value["has_more"]:
                more += _link("/journal", "Next byte page", entry=source_id, byte_offset=value["offset"] + value["returned_bytes"])
        title = text.splitlines()[0].lstrip("# ")[:160] if text else row["kind"]
        return (f'<details class="journal-entry" id="entry-{source_id}" data-key="journal-{source_id}"'
                + (" open" if focused else "") + f'><summary><span><span class="journal-entry-meta">Entry {row["seq"]} &middot; '
                f'{_timestamp(row["created"])}</span><strong class="journal-entry-title">{_escape(title)}</strong></span></summary>'
                f'<div class="journal-entry-body">{more}<article class="markdown">{_markdown(text)}</article>'
                f'<details class="raw-source"><summary>View Markdown source</summary><pre>{_escape(text)}</pre></details>'
                f'<p class="muted">Revision {_escape(source_id)} &middot; {_escape(value["digest"])}</p>'
                '<div class="journal-entry-links">' + _link("/journal", "Link to this entry", entry=source_id) + "</div></div></details>")

    def _search_results(self, value):
        content = f'<p class="muted">{_escape(value["coverage"])}. Cutoff {value["cutoff"]}; searched {len(value["searched"])} source prefixes.</p>'
        for row in value["matches"]:
            content += _panel(row["kind"], f'<div class="markdown">{_markdown(row["snippet"])}</div>',
                              _link("/journal", "Exact retained entry", entry=row["id"]))
        if not value["matches"]:
            content += _empty("No matches in this searched scope", "This is not evidence that the archive has no relevant finding.")
        if value["has_more"]:
            content += _link("/journal", "Search next retained scope", q=value["query"], after=value["next_after"], cutoff=value["cutoff"])
        return content

    def _journal(self):
        query = self.params.get("q", "").strip()
        if len(query.encode()) > 256:
            raise DashboardError(400, "Journal search is limited to 256 bytes.")
        toolbar = (f'<section class="journal-toolbar" aria-label="Journal controls"><form class="filters" action="/journal">'
                   f'<label for="journal-search">Search journal<input id="journal-search" name="q" value="{_escape(query)}" type="search"></label>'
                   '<button type="submit">Search</button></form><div class="journal-controls">'
                   + _link("/journal", "Jump to latest") + '<div class="journal-fold-controls" hidden>'
                   '<button type="button" data-journal-action="expand">Expand page</button>'
                   '<button type="button" data-journal-action="collapse">Collapse page</button></div></div></section>')
        if self.params.get("entry"):
            source_id = self.params["entry"]
            self.context.update(source_id=source_id, label="Exact research source")
            article = self._source_article(source_id, focused=True)
            if self.current["generation_state"] != "open":
                article = '<p class="notice">Historical research entry. Earlier next steps do not reopen sealed admission.</p>' + article
            sequence = self._rows("SELECT seq FROM sources WHERE id=?", (source_id,))[0]["seq"]
            for operator, order, label in ((">", "ASC", "Newer entry"), ("<", "DESC", "Older entry")):
                rows = self._rows(f"SELECT id FROM sources WHERE seq{operator}? AND kind IN ('handoff','journal_import','summary','directive') ORDER BY seq {order} LIMIT 1", (sequence,))
                if rows:
                    article += _link("/journal", label, entry=rows[0]["id"])
            return toolbar + article
        cutoff = self._int("cutoff") if "cutoff" in self.params else None
        if query:
            return toolbar + self._search_results(journal.search(self.state.db, query, after=self._int("after"), cutoff=cutoff))
        page = journal.page(self.state.db, limit=1, cutoff=cutoff)
        self.params["cutoff"] = str(page["cutoff"])
        offset, paging = self._pagination(page["total"], page_size=JOURNAL_PAGE_SIZE)
        page = journal.page(self.state.db, limit=JOURNAL_PAGE_SIZE, offset=offset, cutoff=page["cutoff"])
        intro = f'<p class="intro">Newest retained entries first. Stable source cutoff {page["cutoff"]}. Folding does not hide entries from the archive.</p>'
        if not page["entries"]:
            return toolbar + _empty("The research journal is empty", "Accepted handoffs and imported notes appear here.")
        return toolbar + intro + paging + "".join(
            self._source_article(row["id"], focused=index == 0) for index, row in enumerate(page["entries"])) + paging

    def _goal(self):
        rows = self._rows("SELECT name,source_id FROM source_heads WHERE name IN ('goal','protocol') ORDER BY name")
        content = '<p class="intro">Recorded criteria and active operator constraints. The dashboard does not infer a statistical test or unit conversion from prose.</p>'
        if not rows:
            content += _empty("No research goal yet", "No retained goal or protocol revision exists.")
        for row in rows:
            content += self._source_article(row["source_id"], focused=True)
        directives = self._rows("SELECT source_id FROM directives WHERE active=1 ORDER BY created DESC LIMIT 20")
        if directives:
            content += _panel("Active operator constraints (latest 20)", "".join(
                self._source_excerpt(row["source_id"], 1600) for row in directives))
        return content

    def _debug(self):
        content = self._recovery_summary(full=True)
        rows = self._rows("SELECT token,kind,state,reason FROM allocations WHERE state!='released' ORDER BY created LIMIT 100")
        return content + _details("Technical details: pending allocation effects", _table(["Token", "Kind", "State", "Reason"], [
            [_escape(r["token"]), _escape(r["kind"]), _badge(r["state"]), _escape(r["reason"])] for r in rows]))

    def _recovery_summary(self, *, full=False):
        blockers = self.current["blockers"]
        if not blockers:
            return _panel("No unresolved recovery blockers recorded",
                          '<p>No blocker is recorded for this generation. This is not a live ownership check.</p>')
        offset, paging = self._pagination(len(blockers)) if full else (0, "")
        content = ""
        for row in blockers[offset:offset + (PAGE_SIZE if full else 3)]:
            work = self._rows("SELECT experiment_id FROM attempts WHERE id=?", (row["work_id"],)) if row["work_id"] else []
            link = (_link("/job", work[0]["experiment_id"], id=row["work_id"]) if work else
                    _escape(row["category"].replace("_", " ").capitalize()))
            content += (f'<article class="recovery-card"><h3>{link}</h3><p>{_escape(row["detail"])}</p>'
                        f'<p class="source-line">Blocker recorded {_timestamp(row["created"])}. Responsible actor not recorded.</p>')
            if row["category"] in ("ownership", "recovery", "receipt"):
                content += '<p><strong>Do not duplicate uncertain work or release its reservation without ownership proof.</strong></p>'
            if full:
                content += self._inspection(row["work_id"], has_attempt=bool(work))
            content += "</article>"
        return _panel(f'{len(blockers)} unresolved recovery blockers', content, _link("/debug", "Inspect recovery")) + paging + (
            '<p class="muted">Showing a bounded preview of blockers; inspect recovery details for more.</p>'
            if not full and len(blockers) > 3 else "")

    def _inspection(self, work_id, *, has_attempt=True):
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'" if sys.platform == "win32" else shlex.quote(str(value))
        executable = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin") / (
            "labgoblin.exe" if sys.platform == "win32" else "labgoblin")
        prefix = "& " + quote(executable) if sys.platform == "win32" else quote(executable)
        project = quote(self.root.parent)
        commands = [f"{prefix} status --project {project} --json"]
        if has_attempt and work_id:
            commands.append(f"{prefix} logs --project {project} --id {quote(work_id)} --stage supervisor --stream stderr --bytes 8192 --json")
        return ('<div class="inspection"><p>Inspect recorded status and bounded owned diagnostics. These commands do not reconcile, restart, or release work. '
                'Missing terminal receipts or logs do not prove the payload is dead.</p>'
                '<pre class="copy-source">' + _escape("\n".join(commands)) + '</pre>'
                '<button type="button" data-copy-command>Copy read-only inspection commands</button><span class="copy-status" role="status"></span></div>')

    def _reports(self, limit=None):
        count = self._rows("SELECT COUNT(*) n FROM reports")[0]["n"]
        offset, paging = self._pagination(count) if limit is None else (0, "")
        rows = self._rows("""SELECT r.id,r.view_id,r.created,r.outputs,v.kind,v.generation FROM reports r
            JOIN views v ON v.id=r.view_id ORDER BY r.created DESC,r.id DESC LIMIT ? OFFSET ?""", (limit or PAGE_SIZE, offset))
        cards = []
        for row in rows:
            cards.append('<article class="report-card"><h3>' + _link("/report", "Open retained report", id=row["id"]) + "</h3>"
                         + f'<p>Generation {row["generation"]} &middot; {_timestamp(row["created"])}</p>'
                         + '<p class="muted">Historical cutoff report, not by itself a closure assessment.</p>'
                         + _link("/view", "Immutable source inventory", id=row["view_id"])
                         + _details("Output manifest and integrity", f'<pre>{_escape(json.dumps(json.loads(row["outputs"]), indent=2))}</pre>', row["id"])
                         + "</article>")
        return _panel("Retained reports", "".join(cards) if cards else _empty(
            "No report output recorded", "An empty report list is not a research conclusion. Accepted rationale remains in the journal."),
            _link("/reports", "All reports") if limit is not None else "") + paging

    def _report(self):
        report_id = self.params.get("id", "")
        rows = self._rows("SELECT view_id FROM reports WHERE id=?", (report_id,))
        if not rows:
            raise DashboardError(404, "Unknown retained report.")
        self.context.update(view_id=rows[0]["view_id"], label="Retained report")
        scope = self._historical_scope(rows[0]["view_id"])
        try:
            value = report_text(self.state, report_id, offset=self._int("byte_offset"))
        except (OSError, ValueError) as error:
            logging.getLogger(__name__).warning("Retained report unavailable: %s", error)
            return scope + '<div class="notice danger">Retained report unavailable: ' + _escape(error) + "</div>"
        paging = f'<p class="muted">Verified retained Markdown, bytes {value["offset"]}-{value["offset"] + value["returned_bytes"]} of {value["bytes"]}. '
        paging += "HTML is not executed by this reader.</p>"
        if value["has_more"] or value["offset"]:
            paging += '<p class="notice">Bounded byte page; a Markdown table or paragraph may continue on the next page.</p>'
        if value["offset"]:
            paging += _link("/report", "Previous report byte page", id=report_id, byte_offset=max(0, value["offset"] - DOCUMENT_LIMIT))
        if value["has_more"]:
            paging += _link("/report", "Next report byte page", id=report_id, byte_offset=value["offset"] + value["returned_bytes"])
        return scope + paging + _panel("Readable retained report", f'<article class="markdown">{_markdown(value["text"])}</article>') + paging

    def _historical_scope(self, view_id):
        rows = self._rows("SELECT id,kind,generation,created,source_ids,metadata FROM views WHERE id=?", (view_id,))
        if not rows:
            raise DashboardError(404, "Unknown historical source view.")
        view = rows[0]
        metadata = json.loads(view["metadata"])
        content = (f'<div class="notice historical-scope"><strong>Historical source scope &middot; generation {view["generation"]}</strong>'
                   f'<p>As recorded {_timestamp(view["created"])}. {metadata["attempts"]} attempts in the complete inventory. '
                   'This cutoff does not change when current work changes. A retained report is not itself a closure assessment.</p>'
                   + _link("/view", "Return to this source inventory", id=view_id) + "</div>")
        return content

    def _view(self):
        view_id = self.params.get("id", "")
        self.context.update(view_id=view_id, label="Historical source inventory")
        scope = self._historical_scope(view_id)
        if self.params.get("attempt"):
            aid = self.params["attempt"]
            value = reporting.member(self.state.db, view_id, aid, offset=self._int("byte_offset"))
            row = value["content"]
            if row is None:
                import base64
                content = scope + '<p class="notice">Bounded exact inventory-row byte page. No current attempt state is substituted.</p><pre>' + _escape(
                    base64.b64decode(value["base64"]).decode("utf-8", errors="replace")) + "</pre>"
                if value["has_more"]:
                    content += _link("/view", "Next row byte page", id=view_id, attempt=aid, byte_offset=value["offset"] + 65536)
                return content
            content = scope + _panel(row["experiment_id"] + " (historical)", self._validity(row)
                                     + _facts([("Selected", str(row["selected"])), ("Admitted", str(row["admitted"])),
                                               ("Reason", _escape(row["reason"]) or "Not recorded")]))
            content += self._reference_links(row["observation_ids"], view_id=view_id)
            content += self._reference_links(row["source_refs"].values())
            return content + '<p>' + _link("/job", "Switch to current experiment state", id=aid) + "</p>" + _details(
                "Exact historical row", f'<pre>{_escape(json.dumps(row, indent=2))}</pre>')
        value = reporting._page(self.connection, view_id, offset=self._int("offset"), limit=20)
        coverage = value["coverage"]
        content = scope + _panel("Pinned research sources", self._reference_links(value["source_ids"].values()))
        content += f'<p class="muted">Showing inventory members {coverage["offset"] + 1 if coverage["total"] else 0}-{coverage["end"]} of {coverage["total"]}. '
        content += 'Failed, invalid, unselected and unperformed work remain in the denominator.</p>'
        content += _table(["Attempt", "Selected", "Admitted", "Execution", "Validation", "Observation denominator"], [
            [_link("/view", r["experiment_id"], id=view_id, attempt=r["id"]), str(r["selected"]), str(r["admitted"]),
             _badge(r["status"]), _badge(r["validation"]), str(r["observation_count"])] for r in value["attempts"]])
        if coverage["offset"]:
            content += _link("/view", "Previous inventory page", id=value["id"], offset=max(0, coverage["offset"] - 20))
        if coverage["has_more"]:
            content += _link("/view", "Next inventory page", id=value["id"], offset=coverage["end"])
        return content + _details("Technical source scope", f'<pre>{_escape(json.dumps({k: v for k, v in value.items() if k != "attempts"}, indent=2))}</pre>')


def run_dashboard(config_path=".", port=8765, *, chat=None, open_browser=False, json_output=False):
    with DashboardServer(("127.0.0.1", port), config_path, chat=chat) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(json.dumps({"url": url, "read_only": True}) if json_output else f"LabGoblin dashboard: {url}", flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return
