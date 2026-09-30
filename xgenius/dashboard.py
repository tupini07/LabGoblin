"""Read-only, loopback research dashboard with locally rendered Markdown."""

from datetime import datetime, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import shutil
import secrets
import sqlite3
import time
import urllib.parse

from markdown_it import MarkdownIt

from xgenius.config import get_xgenius_dir, load_config
from xgenius.db import ACTIVE_STATUSES, hypothesis_statement
from xgenius.dashboard_data import index_journal, query as _query, read_text as _read_text
from xgenius.dashboard_chat import ChatError, ObserverService, load_chat_settings
from xgenius.scheduler import ledger_path
from xgenius.workspace import contained


PAGE_SIZE = 50
LOG_LIMIT = 64 * 1024
DOCUMENT_LIMIT = 128 * 1024
JOURNAL_PAGE_SIZE = 20
JOURNAL_PREVIEW_LIMIT = 16 * 1024
STATIC = Path(__file__).with_name("static")
FAILED = ("failed", "timed_out", "timeout", "oom", "interrupted", "disappeared")
NAV = (
    ("/", "Overview"), ("/jobs", "Experiments"), ("/hypotheses", "Hypotheses"),
    ("/activity", "Agent activity"), ("/artifacts", "Artifacts"),
    ("/resources", "Resources"), ("/goal", "Research goal"),
    ("/journal", "Journal"), ("/debug", "Debug log"),
)
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
       "img-src 'self'; connect-src 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


def _escape(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _url(path: str, **params) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
    return path + ("?" + query if query else "")


def _link(path: str, label, **params) -> str:
    return f'<a href="{_escape(_url(path, **params))}">{_escape(label)}</a>'


def _badge(status: str) -> str:
    tone = "neutral"
    if status in ("completed", "promising", "accepted"):
        tone = "success"
    elif status in (*FAILED, "recovery_required", "blocked"):
        tone = "danger"
    elif status in ("running", "open", "maintenance"):
        tone = "accent"
    elif status in ("queued", "starting", "pending", "submitted", "waiting", "paused", "proposed"):
        tone = "warning"
    return f'<span class="badge {tone}">{_escape(status.replace("_", " "))}</span>'


def _duration(seconds) -> str:
    if seconds is None:
        return "Not recorded"
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _timestamp(value) -> str:
    if value in (None, ""):
        return '<span class="muted">Not recorded</span>'
    if isinstance(value, (float, int)):
        date = datetime.fromtimestamp(value, timezone.utc)
    else:
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _escape(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        date = date.astimezone(timezone.utc)
    return f'<time datetime="{date.isoformat()}">{date:%Y-%m-%d %H:%M:%S} UTC</time>'


def _size(value) -> str:
    if value is None:
        return "Not recorded"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{value:,} B"
        value /= 1024


def _markdown(text: str) -> str:
    # No raw HTML or automatic remote image requests from research content.
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


def _chat_markdown(text: str) -> str:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    tokens = parser.parse(text)
    for block in tokens:
        for token in block.children or []:
            if token.type == "link_open":
                target = urllib.parse.urlsplit(token.attrGet("href") or "")
                if target.scheme or target.netloc or target.path not in {
                        "/", "/jobs", "/job", "/hypotheses", "/hypothesis", "/activity",
                        "/turn", "/artifacts", "/resources", "/goal", "/journal", "/debug"}:
                    token.attrSet("href", "#")
                    token.attrSet("title", "Only dashboard evidence links are enabled in chat")
    return parser.renderer.render(tokens, parser.options, {})


def _empty(title: str, description: str) -> str:
    return f'<div class="empty"><h3>{_escape(title)}</h3><p>{_escape(description)}</p></div>'


def _panel(title: str, body: str, action: str = "") -> str:
    return (f'<section class="panel"><div class="section-heading"><h2>{_escape(title)}</h2>'
            f'{action}</div>{body}</section>')


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return _empty("Nothing here yet", "New records will appear as the research progresses.")
    return ('<div class="table-scroll" tabindex="0"><table><thead><tr>'
            + "".join(f"<th scope=\"col\">{_escape(h)}</th>" for h in headers)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
            + "</tbody></table></div>")


def _facts(items: list[tuple[str, str]]) -> str:
    return '<dl class="facts">' + "".join(
        f"<div><dt>{_escape(label)}</dt><dd>{value}</dd></div>" for label, value in items) + "</dl>"


def _meter(label: str, used: float, limit: float, detail: str) -> str:
    progress = (f'<meter min="0" max="{limit}" value="{min(used, limit)}" '
                f'aria-label="{_escape(label)}"></meter>') if limit > 0 else ""
    return (f'<div class="budget"><div><strong>{_escape(label)}</strong>'
            f'<span>{_escape(detail)}</span></div>{progress}</div>')


class DashboardError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address, config_path: str, *, chat: bool = False, observer=None):
        self.config_path = str(Path(config_path).resolve())
        self.chat_token = secrets.token_urlsafe(32)
        self.observer = observer or ObserverService(self.config_path, load_chat_settings(self.config_path, enabled=chat))
        super().__init__(address, DashboardHandler)

    def server_close(self):
        try:
            self.observer.close()
        finally:
            super().server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    def _check_host(self):
        host = self.headers.get("Host", "").lower()
        allowed_hosts = {"localhost", "127.0.0.1",
                         f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        if host not in allowed_hosts:
            raise DashboardError(403, "Use the dashboard's loopback address.")
        return host

    def do_GET(self):
        self.config = None
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
            self.config = load_config(self.server.config_path)
            self.root = Path(get_xgenius_dir(self.config))
            self.db = self.root / "xgenius.db"
            routes = {
                "/": ("Overview", self._overview), "/jobs": ("Experiments", self._jobs),
                "/job": ("Experiment details", self._job),
                "/hypotheses": ("Hypotheses", self._hypotheses),
                "/hypothesis": ("Hypothesis details", self._hypothesis),
                "/activity": ("Agent activity", self._activity),
                "/turn": ("Agent turn", self._turn),
                "/artifacts": ("Artifacts", self._artifacts),
                "/resources": ("Resources", self._resources),
                "/journal": ("Research journal", self._journal),
                "/goal": ("Research goal", self._goal),
                "/debug": ("Debug log", self._debug),
            }
            if self.path_name == "/artifact" and self.config.local:
                self._download()
                return
            if self.path_name not in routes:
                raise DashboardError(404, "This dashboard page does not exist.")
            title, render = routes[self.path_name]
            self._send(self._page(title, render()).encode("utf-8"))
        except (DashboardError, ChatError) as error:
            if self.path_name.startswith("/chat/"):
                self._json({"error": str(error)}, status=error.status)
                return
            self._send(self._page(str(error.status), _empty("Page unavailable", str(error))).encode("utf-8"),
                       status=error.status)
        except ConnectionError as error:
            logging.getLogger(__name__).debug("Dashboard client disconnected: %s", error)
        except (OSError, sqlite3.Error, ValueError) as error:
            logging.getLogger(__name__).exception("Unable to read dashboard data")
            content = _empty("Dashboard data unavailable", str(error))
            content += '<p>Check the configuration and database. The dashboard has not changed campaign state.</p>'
            self._send(self._page("Data unavailable", content).encode("utf-8"), status=503)

    def _chat_snapshot(self, conversation_id):
        result = self.server.observer.snapshot(conversation_id)
        for message in result["messages"]:
            message["html"] = _chat_markdown(message["answer"])
        return result

    def _json(self, data: dict, *, status: int = 200):
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status=status)

    def do_POST(self):
        try:
            host = self._check_host()
            path = urllib.parse.urlsplit(self.path).path
            if path not in ("/chat/message", "/chat/cancel", "/chat/clear"):
                raise DashboardError(404, "No dashboard control endpoint exists here.")
            if self.headers.get("Origin") not in (None, f"http://{host}"):
                raise DashboardError(403, "Cross-origin chat requests are not allowed.")
            token = self.headers.get("X-Xgenius-Token", "")
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
            allowed = {"conversation_id", "request_id", "message"} if path == "/chat/message" else {"conversation_id"}
            if set(data) - allowed or not isinstance(data.get("conversation_id", ""), str):
                raise DashboardError(400, "Invalid chat request fields.")
            conversation = data.get("conversation_id", "")
            if path == "/chat/message":
                state = self.server.observer.send(conversation, data.get("request_id"), data.get("message"))
                self._json(self._chat_snapshot(state["conversation_id"]), status=202)
            elif path == "/chat/cancel":
                self.server.observer.cancel(conversation)
                self._json(self._chat_snapshot(conversation))
            else:
                self.server.observer.clear(conversation)
                self._json({"cleared": True})
        except (DashboardError, ChatError) as error:
            self._json({"error": str(error)}, status=error.status)
        except ConnectionError as error:
            logging.getLogger(__name__).debug("Dashboard chat client disconnected: %s", error)
        except (ValueError, UnicodeError, TimeoutError) as error:
            self._json({"error": f"Invalid chat request: {error}"}, status=400)

    def _headers(self, status: int, mime: str, length: int):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send(self, body: bytes, mime: str = "text/html; charset=utf-8", *, status: int = 200):
        self._headers(status, mime, len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return _query(self.db, sql, params)

    def _page(self, title: str, content: str) -> str:
        local = bool(self.config and self.config.local)
        project = self.config.project.name if self.config else "Research dashboard"
        active = {"/job": "/jobs", "/hypothesis": "/hypotheses", "/turn": "/activity"}.get(
            self.path_name, self.path_name)
        nav = "".join(
            f'<a href="{path}"' + (' aria-current="page"' if active == path else "")
            + f">{label}</a>" for path, label in NAV
            if local or path not in ("/activity", "/artifacts", "/resources"))
        live = self.path_name in ("/", "/jobs", "/activity", "/artifacts", "/resources")
        refresh = ('<label class="live-toggle"><input id="live-refresh" type="checkbox"> '
                   'Auto-refresh (15s)</label>') if live else ""
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="xgenius-chat-token" content="{self.server.chat_token}">
<title>{_escape(title)} | {_escape(project)} | xgenius</title>
<link rel="stylesheet" href="/static/dashboard.css">
<script src="/static/dashboard.js" defer></script></head>
<body><a class="skip-link" href="#main">Skip to content</a>
<aside class="sidebar"><a class="brand" href="/"><span class="brand-mark">x</span>xgenius</a>
<div class="project-label">RESEARCH WORKSPACE</div><div class="project-name">{_escape(project)}</div>
<span class="mode">{'Local campaign' if local else 'SLURM research'}</span>
<nav aria-label="Main navigation">{nav}</nav>
<div class="sidebar-note"><span class="status-dot"></span> Read-only dashboard<br>
<span>Local evidence. No remote assets.</span></div></aside>
<div class="workspace"><header class="topbar"><div><span class="eyebrow">RESEARCH OBSERVATORY</span>
<h1>{_escape(title)}</h1></div><div class="toolbar">{refresh}
<button id="chat-open" type="button" aria-controls="chat-panel" aria-expanded="false">Ask Copilot</button>
<button id="refresh" type="button">Refresh</button></div></header>
<div class="refresh-status" id="refresh-status" role="status" aria-live="polite">Updated
{datetime.now(timezone.utc):%H:%M:%S} UTC</div>
<main id="main" tabindex="-1">{content}</main>
<footer>Times in UTC &middot; Recorded state, not a live process probe &middot;
Manage campaigns through the CLI</footer></div>
<aside id="chat-panel" class="chat-panel" role="dialog" aria-modal="false" aria-labelledby="chat-title" hidden>
<div class="chat-heading"><div><span class="eyebrow">READ-ONLY OBSERVER</span><h2 id="chat-title">Dashboard Copilot</h2></div>
<button id="chat-close" type="button" aria-label="Close chat">Close</button></div>
<p class="chat-notice" id="chat-notice">On-demand answers grounded in campaign evidence. No steering or file edits.</p>
<div class="chat-model" id="chat-model"></div>
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

    def _local_only(self):
        if not self.config.local:
            raise DashboardError(404, "This view is available for local campaigns.")

    def _pagination(self, count: int, *, page_size: int = PAGE_SIZE) -> tuple[int, str]:
        try:
            page = int(self.params.get("page", "1"))
        except ValueError:
            raise DashboardError(400, "Page must be a positive integer.") from None
        if page < 1 or page > 1_000_000:
            raise DashboardError(400, "Page must be between 1 and 1000000.")
        pages = max(1, (count + page_size - 1) // page_size)
        if page > pages:
            raise DashboardError(404, "This results page does not exist.")
        journal = self.path_name == "/journal"
        unit = ("entry" if count == 1 else "entries") if journal else "records"
        controls = f'<span>{count:,} {unit} &middot; Page {page} of {pages}</span>'
        params = {k: v for k, v in self.params.items() if k not in ("page", "entry")}
        if page > 1:
            controls += _link(self.path_name, "Newer entries" if journal else "Previous", **params, page=page - 1)
        if page < pages:
            controls += _link(self.path_name, "Older entries" if journal else "Next", **params, page=page + 1)
        return (page - 1) * page_size, f'<nav class="pagination" aria-label="Results pages">{controls}</nav>'

    def _campaign(self) -> dict:
        rows = self._rows("SELECT * FROM campaign")
        if not rows:
            raise DashboardError(503, "No local campaign record is available.")
        return rows[0]

    def _overview(self) -> str:
        counts = {row["status"]: row["n"] for row in self._rows(
            "SELECT status,COUNT(*) AS n FROM jobs GROUP BY status")}
        total = sum(counts.values())
        active = sum(counts.get(s, 0) for s in ACTIVE_STATUSES)
        attention = self._rows(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN (?,?,?,?,?,?,?) OR error_message!=''",
            (*FAILED, "recovery_required"))[0]["n"]
        hypotheses = self._rows("SELECT COUNT(*) AS n FROM hypotheses")[0]["n"]
        content = ""
        if self.config.local:
            campaign = self._campaign()
            pending = self._rows("SELECT COUNT(*) AS n FROM events WHERE acknowledged_by IS NULL")[0]["n"]
            explanations = {
                "ready": "Ready for its first research turn.",
                "running": "The campaign is scheduled to make progress.",
                "waiting": "Waiting for experiment results or new events.",
                "paused": "New dispatch and agent turns are paused.",
                "blocked": "An external constraint or recovery issue needs attention.",
                "completed": "The agent declared the research scope complete.",
                "stopped": "The campaign has stopped; history is preserved.",
                "stopping": "Draining admitted work before stopping.",
                "finishing": "Draining admitted work before completion.",
            }
            content += (f'<section class="campaign-banner"><div><span class="eyebrow">CAMPAIGN</span>'
                        f'<h2>{_badge(campaign["state"])}</h2><p>'
                        f'{_escape(campaign["reason"] or explanations.get(campaign["state"], ""))}</p>'
                        f'<code class="identifier">{_escape(campaign["id"])}</code></div>'
                        '<div class="campaign-signals">'
                        f'<span>{pending} unacknowledged events</span>'
                        f'<span>Controller {"registered" if campaign["controller"] else "not registered"}</span>'
                        f'{_link("/activity", "Inspect agent activity")}</div></section>')
        else:
            content += '<p class="intro">Cluster experiments and research evidence in one place.</p>'
        content += '<div class="stat-grid">' + "".join(
            f'<a class="stat-card" href="{url}"><span>{label}</span><strong>{value:,}</strong>'
            f'<small>{hint}</small></a>' for label, value, hint, url in [
                ("Experiments", total, "All recorded attempts", "/jobs"),
                ("In flight", active, "Queued, active or recovery required", "/jobs?status=active"),
                ("Needs attention", attention, "Failures or validation/recovery errors", "/jobs?status=attention"),
                ("Hypotheses", hypotheses, "Tracked research questions", "/hypotheses")]) + "</div>"
        if self.config.local:
            local = self.config.local
            used_turns = self._rows("SELECT COUNT(*) AS n FROM turns")[0]["n"]
            elapsed = max(0, time.time() - campaign["started"]) if campaign["started"] else 0
            budgets = _meter("Campaign elapsed", elapsed, local.max_seconds,
                             f"{_duration(elapsed)} / {_duration(local.max_seconds)}")
            budgets += _meter("Agent turns", used_turns, local.max_turns,
                              f"{used_turns:,} / {local.max_turns:,}")
            budgets += (f'<p class="muted">Elapsed time includes pauses and completed intervals. '
                        f'Research, report and compact sessions share the turn budget. '
                        f'Per-turn deadline: {_duration(local.turn_timeout)}. Limits reflect the file; '
                        'a running controller may have loaded earlier values.</p>')
            turns = self._rows("SELECT * FROM turns ORDER BY started DESC LIMIT 1")
            latest = self._turn_summary(turns[0]) if turns else _empty(
                "No agent turns yet", "The first turn starts when you run the campaign.")
            content += '<div class="two-column">' + _panel("Campaign limits", budgets, _link(
                "/resources", "Resources")) + _panel("Latest agent turn", latest, _link(
                    "/activity", "All activity")) + "</div>"
        distribution = "".join(
            f'<div class="distribution-row">{_badge(status)}<meter min="0" max="{max(total, 1)}" '
            f'value="{n}" aria-label="{_escape(status)} jobs"></meter><strong>{n:,}</strong></div>'
            for status, n in sorted(counts.items(), key=lambda item: (-item[1], item[0])))
        recent = self._rows("""SELECT job_id,experiment_id,cluster,status,walltime_seconds,
            submitted_at,error_message FROM jobs ORDER BY submitted_at DESC,job_id DESC LIMIT 8""")
        content += _panel("Experiment outcomes", distribution or _empty(
            "No experiments yet", "Submit an experiment through xgenius to start collecting evidence."))
        content += _panel("Recent experiments", self._job_table(recent), _link("/jobs", "All experiments"))
        return content

    def _job_table(self, jobs: list[dict]) -> str:
        return _table(["Experiment", "Runner / cluster", "Status", "Recorded runtime", "Submitted"], [
            [_link("/job", job["experiment_id"], id=job["job_id"])
             + f'<code class="identifier">{_escape(job["job_id"])}</code>',
             _escape(job["cluster"]),
             _badge(job["status"]) + ('<span class="validation-warning">Review recorded error</span>'
                                      if job["error_message"] else ""),
             ("Pending completion" if job["status"] in ACTIVE_STATUSES and not job["walltime_seconds"]
              else _duration(job["walltime_seconds"])),
             _timestamp(job["submitted_at"])] for job in jobs])

    def _jobs(self) -> str:
        where, args = ["1=1"], []
        status = self.params.get("status", "")
        if status in ("active", "attention"):
            states = ACTIVE_STATUSES if status == "active" else (*FAILED, "recovery_required")
            clause = "status IN (" + ",".join("?" for _ in states) + ")"
            where.append("(" + clause + (" OR error_message!='')" if status == "attention" else ")"))
            args.extend(states)
        elif status:
            where.append("status=?")
            args.append(status)
        if self.params.get("hypothesis_id"):
            where.append("hypothesis_id=?")
            args.append(self.params["hypothesis_id"])
        query = self.params.get("q", "")
        if query:
            where.append("(instr(lower(experiment_id),lower(?))>0 OR instr(lower(job_id),lower(?))>0)")
            args.extend([query, query])
        condition = " AND ".join(where)
        count = self._rows(f"SELECT COUNT(*) AS n FROM jobs WHERE {condition}", tuple(args))[0]["n"]
        offset, paging = self._pagination(count)
        jobs = self._rows(f"""SELECT job_id,experiment_id,cluster,status,walltime_seconds,
            submitted_at,error_message FROM jobs WHERE {condition}
            ORDER BY submitted_at DESC,job_id DESC LIMIT ? OFFSET ?""", (*args, PAGE_SIZE, offset))
        statuses = ["", "active", "attention"] + [r["status"] for r in self._rows(
            "SELECT DISTINCT status FROM jobs ORDER BY status")]
        if status not in statuses:
            statuses.append(status)
        options = "".join(f'<option value="{_escape(s)}"'
                          + (' selected' if status == s else "")
                          + f'>{_escape(s.replace("_", " ") if s else "All statuses")}</option>'
                          for s in statuses)
        filters = (f'<form class="filters" action="/jobs" method="get">'
                   f'<label>Search experiments<input type="search" name="q" value="{_escape(query)}" '
                   'placeholder="Name or attempt ID"></label>'
                   f'<label>Status<select name="status">{options}</select></label>'
                   f'<label>Hypothesis<input name="hypothesis_id" '
                   f'value="{_escape(self.params.get("hypothesis_id", ""))}" placeholder="Any hypothesis"></label>'
                   '<button type="submit" class="primary">Filter</button>'
                   '<a href="/jobs">Clear</a></form>')
        return filters + _panel("Experiment history", self._job_table(jobs)) + paging

    def _job(self) -> str:
        rows = self._rows("SELECT * FROM jobs WHERE job_id=?", (self.params.get("id", ""),))
        if not rows:
            raise DashboardError(404, "Unknown experiment.")
        job = rows[0]
        content = '<div class="detail-heading"><h2>' + _escape(job["experiment_id"]) + "</h2>" + _badge(
            job["status"]) + "</div>"
        if job["error_message"]:
            content += f'<div class="notice danger"><strong>Recorded error</strong><p>{_escape(job["error_message"])}</p></div>'
        content += _panel("Execution", _facts([
            ("Attempt ID", f'<code>{_escape(job["job_id"])}</code>'),
            ("Runner / cluster", _escape(job["cluster"])),
            ("Hypothesis", _link("/hypothesis", job["hypothesis_id"], id=job["hypothesis_id"])
             if job["hypothesis_id"] else "Unassigned"),
            ("Submitted", _timestamp(job["submitted_at"])),
            ("Completed", _timestamp(job["completed_at"])),
            ("Recorded runtime", _duration(job["walltime_seconds"])),
            ("Recorded GPU-hours", _escape(job["gpu_hours"])),
            ("Exit code", _escape(job["exit_code"]) if job["exit_code"] is not None else "Not recorded"),
            ("Resources", f'{job["cpus"]} CPUs / {_escape(job["memory"])} / {job["gpus"]} GPUs'),
        ]) + f'<h3>Command</h3><pre>{_escape(job["command"])}</pre>'
            f'<details><summary>Full operational record</summary>'
            f'<pre>{_escape(json.dumps(job, indent=2))}</pre></details>')
        if self.config.local:
            attempts = self._rows("SELECT spec,started,ended FROM attempts WHERE id=?", (job["job_id"],))
            if attempts:
                attempt = attempts[0]
                spec = json.loads(attempt["spec"])
                content += _panel("Reproducibility", _facts([
                    ("Idempotency key", _escape(spec["key"])),
                    ("Started", _timestamp(attempt["started"])),
                    ("Deadline", _duration(spec["seconds"])),
                    ("Output directory", f'<code>{_escape(spec["output"])}</code>'),
                ]) + "<h3>Frozen source files</h3>" + _table(["File", "SHA-256"], [
                    [_escape(name), f'<code>{_escape(digest)}</code>']
                    for name, digest in spec["source_hashes"].items()]))
                content += _panel("Registered evidence", self._artifact_table(self._rows(
                    "SELECT a.*,j.experiment_id FROM artifacts a JOIN jobs j ON a.attempt_id=j.job_id "
                    "WHERE a.attempt_id=? ORDER BY a.path", (job["job_id"],))))
                content += self._logs(contained(self.root, f'attempts/{job["job_id"]}'),
                                      ("stdout.log", "stderr.log", "backend.stderr.log"))
        return content

    def _hypotheses(self) -> str:
        count = self._rows("SELECT COUNT(*) AS n FROM hypotheses")[0]["n"]
        offset, paging = self._pagination(count)
        rows = self._rows("""SELECT h.*,COUNT(j.job_id) AS attempts,
            SUM(CASE WHEN j.status='completed' THEN 1 ELSE 0 END) AS completed
            FROM hypotheses h LEFT JOIN jobs j ON h.hypothesis_id=j.hypothesis_id
            GROUP BY h.hypothesis_id ORDER BY h.created_at DESC,h.hypothesis_id LIMIT ? OFFSET ?""",
                          (PAGE_SIZE, offset))
        table_rows = []
        for h in rows:
            statement = hypothesis_statement(h)
            label = statement[:240] + ("..." if len(statement) > 240 else "") if statement else h["hypothesis_id"]
            title = _link("/hypothesis", label, id=h["hypothesis_id"])
            if statement:
                title += f'<code class="identifier">{_escape(h["hypothesis_id"])}</code>'
            else:
                title += '<span class="validation-warning">Hypothesis statement not recorded</span>'
            table_rows.append([title, _badge(h["status"]), str(h["attempts"]), str(h["completed"]),
                               _timestamp(h["updated_at"])])
        intro = '<p class="intro">Statements describe what is being tested; IDs only identify it. '
        intro += 'Open a hypothesis for its rationale, outcomes, and related journal context.</p>'
        return intro + _panel("Research questions", _table(
            ["Hypothesis statement", "Status", "Experiments", "Completed", "Updated"], table_rows)) + paging

    def _hypothesis(self) -> str:
        hid = self.params.get("id", "")
        rows = self._rows("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hid,))
        if not rows:
            raise DashboardError(404, "Unknown hypothesis.")
        h = rows[0]
        content = f'<div class="detail-heading"><h2>Hypothesis</h2>{_badge(h["status"])}</div>'
        content += _facts([("Hypothesis ID", f'<code>{_escape(hid)}</code>'),
                           ("Created", _timestamp(h["created_at"])), ("Updated", _timestamp(h["updated_at"]))])
        statement = hypothesis_statement(h)
        if statement:
            content += _panel("Hypothesis statement", '<div class="markdown">' + _markdown(statement) + "</div>")
        else:
            content += ('<div class="notice"><strong>Hypothesis statement not recorded</strong>'
                        '<p>This record contains only an identifier or an automatic submission placeholder, '
                        'not a description of the claim being tested. Related journal excerpts below provide '
                        'recorded context, not an inferred definition.</p></div>')
        for name in ("motivation", "expected_outcome", "conclusion", "comment"):
            if h[name]:
                content += _panel(name.replace("_", " ").capitalize(),
                                  '<div class="markdown">' + _markdown(h[name]) + "</div>")
        content += self._hypothesis_context(hid)
        jobs = self._rows("SELECT * FROM jobs WHERE hypothesis_id=? ORDER BY submitted_at DESC LIMIT 10", (hid,))
        return content + _panel("Recent experiments", self._job_table(jobs),
                                _link("/jobs", "All experiments", hypothesis_id=hid))

    def _hypothesis_context(self, hid: str) -> str:
        path = self.root / "journal.md"
        matches = []
        truncated = False
        if path.is_file():
            text, truncated = _read_text(path, DOCUMENT_LIMIT, tail=True)
            jobs = self._rows("SELECT job_id,experiment_id FROM jobs WHERE hypothesis_id=? "
                              "ORDER BY submitted_at DESC,job_id DESC LIMIT 200", (hid,))
            references = {hid}
            for job in jobs:
                references.update((job["job_id"], job["experiment_id"], "completion-" + job["job_id"]))
            pattern = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(ref) for ref in sorted(references) if ref)
                                 + r")(?![\w-])")
            heading = "Journal excerpt"
            for paragraph in re.split(r"\r?\n\s*\r?\n", text):
                paragraph = paragraph.strip()
                if re.match(r"^#{1,6}\s", paragraph):
                    heading = paragraph.splitlines()[0].lstrip("# ").strip()
                if pattern.search(paragraph):
                    matches.append((heading, paragraph))
        body = ('<p class="muted">Exact journal excerpts mentioning this ID or one of its latest 200 experiments. '
                'These are research notes, not a replacement for a recorded hypothesis statement.</p>')
        if truncated:
            body += '<p class="muted">Searched only the latest 128 KiB of the journal; older context may be omitted.</p>'
        if matches:
            selected = matches if len(matches) <= 6 else matches[:3] + matches[-3:]
            if len(matches) > 6:
                body += f'<p class="muted">Showing the first three and latest three of {len(matches)} matching excerpts.</p>'
            for index, (heading, paragraph) in enumerate(selected):
                excerpt = paragraph[:3000]
                note = '<p class="muted">Excerpt truncated; read the journal for the full text.</p>' if len(paragraph) > 3000 else ""
                body += (f'<details{" open" if index == 0 else ""}><summary>{_escape(heading)}</summary>'
                         f'<div class="markdown">{_markdown(excerpt)}</div>{note}</details>')
        else:
            body += _empty("No matching journal context", "No exact reference was found in the searched journal portion.")
        return _panel("Related journal context", body, _link("/journal", "Read journal"))

    def _turn_summary(self, turn: dict) -> str:
        result = json.loads(turn["result"]) if turn["result"] else {}
        summary = '<div class="detail-heading">' + _badge(turn["state"]) + f'<strong>{_escape(turn["kind"])}</strong></div>'
        summary += f'<p>{_escape(result.get("reason", "No decision recorded yet."))}</p>'
        summary += _facts([
            ("Started", _timestamp(turn["started"])),
            ("Duration", _duration((turn["ended"] or time.time()) - turn["started"])),
            ("Disposition", _escape(result.get("disposition", "Not recorded"))),
        ])
        return summary + _link("/turn", "Inspect turn and logs", id=turn["id"])

    def _activity(self) -> str:
        self._local_only()
        count = self._rows("SELECT COUNT(*) AS n FROM turns")[0]["n"]
        offset, paging = self._pagination(count)
        turns = self._rows("SELECT * FROM turns ORDER BY started DESC,id DESC LIMIT ? OFFSET ?", (PAGE_SIZE, offset))
        table = _table(["Turn", "State", "Decision", "Started", "Duration"], [
            [_link("/turn", turn["kind"], id=turn["id"])
             + f'<code class="identifier">{_escape(turn["id"])}</code>',
             _badge(turn["state"]),
             _escape(json.loads(turn["result"]).get("disposition", "")) if turn["result"] else "Pending",
             _timestamp(turn["started"]),
             _duration((turn["ended"] or time.time()) - turn["started"])] for turn in turns])
        events = self._rows("SELECT * FROM events ORDER BY created DESC,id DESC LIMIT 30")
        timeline = '<ol class="timeline">' + "".join(
            f'<li><div class="timeline-heading"><strong>{_escape(event["kind"].replace("_", " "))}</strong>'
            + (_badge("accepted") if event["acknowledged_by"] else _badge("pending"))
            + f'</div>{_timestamp(event["created"])}'
            + (f'<p>Handled by {_link("/turn", event["acknowledged_by"], id=event["acknowledged_by"])}</p>'
               if event["acknowledged_by"] else "<p>Awaiting acknowledgement by an agent turn.</p>")
            + f'<details><summary>Event payload</summary><pre>{_escape(json.dumps(json.loads(event["payload"]), indent=2))}</pre></details></li>'
            for event in events) + "</ol>"
        return _panel("Agent turns", table) + paging + _panel("Latest 30 events", timeline)

    def _turn(self) -> str:
        self._local_only()
        rows = self._rows("SELECT * FROM turns WHERE id=?", (self.params.get("id", ""),))
        if not rows:
            raise DashboardError(404, "Unknown agent turn.")
        turn = rows[0]
        content = _panel("Turn decision", self._turn_summary(turn))
        content += _panel("Turn record", _facts([
            ("Turn ID", f'<code>{_escape(turn["id"])}</code>'),
            ("Assigned events", str(len(json.loads(turn["events"])))),
            ("Provider usage", _escape(turn["usage"]) if turn["usage"] else "Not reported (not zero)"),
        ]))
        return content + self._logs(contained(self.root, f'turns/{turn["id"]}'), ("stdout.log", "stderr.log"))

    def _logs(self, directory: Path, names: tuple[str, ...]) -> str:
        content = ""
        for name in names:
            path = contained(directory, name)
            if not path.is_file():
                content += f'<p class="muted">{_escape(name)}: not present.</p>'
                continue
            text, truncated = _read_text(path, LOG_LIMIT, tail=True)
            note = '<p class="muted">Showing the last 64 KiB; earlier output is omitted.</p>' if truncated else ""
            content += (f'<details data-key="{_escape(name)}"><summary>{_escape(name)}'
                        f' ({_size(path.stat().st_size)})</summary>{note}<pre>{_escape(text) if text else "(empty)"}</pre></details>')
        return _panel("Captured logs", content)

    def _artifact_table(self, artifacts: list[dict]) -> str:
        rows = []
        for artifact in artifacts:
            metadata = json.loads(artifact["metadata"])
            metrics = metadata.get("metrics", {})
            evidence = '<dl class="metrics">' + "".join(
                f'<div><dt>{_escape(name)}</dt><dd>{_escape(value)}</dd></div>'
                for name, value in metrics.items()) + "</dl>" if metrics else '<span class="muted">No numeric metrics</span>'
            rows.append([
                _link("/artifact", artifact["path"], id=artifact["id"])
                + f'<details><summary>SHA-256</summary><code>{_escape(metadata.get("sha256", "Not recorded"))}</code></details>',
                _link("/job", artifact["experiment_id"], id=artifact["attempt_id"]),
                _size(metadata.get("bytes")), evidence,
            ])
        return _table(["Artifact / download", "Experiment", "Size", "Recorded metrics"], rows)

    def _artifacts(self) -> str:
        self._local_only()
        query = self.params.get("q", "")
        condition = "(instr(lower(a.path),lower(?))>0 OR instr(lower(j.experiment_id),lower(?))>0)"
        count = self._rows("SELECT COUNT(*) AS n FROM artifacts a JOIN jobs j ON a.attempt_id=j.job_id "
                           f"WHERE {condition}", (query, query))[0]["n"]
        offset, paging = self._pagination(count)
        rows = self._rows(
            "SELECT a.*,j.experiment_id FROM artifacts a JOIN jobs j ON a.attempt_id=j.job_id "
            f"WHERE {condition} ORDER BY a.rowid DESC LIMIT ? OFFSET ?", (query, query, PAGE_SIZE, offset))
        filters = (f'<form class="filters" action="/artifacts"><label>Find evidence'
                   f'<input type="search" name="q" value="{_escape(query)}" placeholder="Path or experiment"></label>'
                   '<button class="primary" type="submit">Search</button><a href="/artifacts">Clear</a></form>')
        return ('<p class="intro">Registered outputs and measurements. Successful execution and registered '
                'artifacts do not establish scientific validity. Files download rather than execute in this dashboard.</p>'
                + filters + _panel("Evidence bank", self._artifact_table(rows)) + paging)

    def _resources(self) -> str:
        self._local_only()
        local = self.config.local
        content = _panel("Campaign envelope", _facts([
            ("Experiment CPUs", str(local.cpus)), ("Experiment memory", f"{local.memory_mb:,} MiB"),
            ("Concurrent jobs", str(local.max_jobs)), ("Assigned GPUs", _escape(", ".join(local.gpus) or "None (CPU only)")),
            ("GPU-hour limit", str(local.max_gpu_hours)), ("Agent-turn limit", str(local.max_turns)),
            ("Campaign elapsed limit", _duration(local.max_seconds)), ("Agent turn deadline", _duration(local.turn_timeout)),
        ]) + '<p class="muted">Values reflect the current configuration file. An already-running controller '
             'may have loaded earlier limits. Reservations are not measured desktop utilization.</p>')
        path = ledger_path()
        if not path.is_file():
            return content + _panel("Shared machine capacity", _empty(
                "Not configured", "Use xgenius machine configure before running experiments."))
        capacities = _query(path, "SELECT * FROM capacity WHERE id=1")
        reservations = _query(path, "SELECT * FROM reservations WHERE state!='released' ORDER BY created,id")
        if capacities:
            capacity = capacities[0]
            specs = [json.loads(r["spec"]) for r in reservations if r["state"] in ("reserved", "running")]
            body = _meter("Reserved CPUs", sum(s["cpus"] for s in specs), capacity["cpus"],
                          f'{sum(s["cpus"] for s in specs)} / {capacity["cpus"]}')
            body += _meter("Reserved memory (MiB)", sum(s["memory_mb"] for s in specs), capacity["memory_mb"],
                           f'{sum(s["memory_mb"] for s in specs):,} / {capacity["memory_mb"]:,}')
            body += _facts([
                ("Host RAM headroom", f'{capacity["headroom_mb"]:,} MiB'),
                ("Configured GPUs", _escape(", ".join(json.loads(capacity["gpus"])) or "None")),
            ])
            body += '<p class="muted">Shared across this Windows/OS user\'s campaigns. External applications are not included.</p>'
        else:
            body = _empty("Not configured", "Use xgenius machine configure before running experiments.")
        content += _panel("Shared machine capacity", body)
        own = self._campaign()["id"]
        rows = []
        for reservation in reservations:
            spec = json.loads(reservation["spec"])
            rows.append([
                _link("/job", reservation["id"], id=reservation["id"]) if reservation["campaign"] == own else _escape(reservation["id"]),
                "This campaign" if reservation["campaign"] == own else _escape(reservation["campaign"]),
                _badge(reservation["state"]), f'{spec["cpus"]} / {spec["memory_mb"]:,} MiB / {len(spec["gpus"])}',
                _timestamp(reservation["created"]),
                _escape(reservation["reason"]) or "No queue reason recorded",
            ])
        return content + _panel("Unreleased reservations", _table(
            ["Attempt", "Campaign", "State", "CPUs / RAM / GPUs", "Requested", "Queue / recovery reason"], rows))

    def _document(self, path: Path, empty: str, *, tail: bool = False) -> str:
        if not path.is_file():
            return _empty(empty, "This document has not been written yet.")
        text, truncated = _read_text(path, DOCUMENT_LIMIT, tail=tail)
        if not text.strip() and not truncated:
            return _empty(empty, "This document has not been written yet.")
        note = ('<div class="notice">This document is larger than 128 KiB. Showing '
                + ("the latest portion" if tail else "the beginning") + "; the full file remains on disk.</div>") if truncated else ""
        return (f'<div class="document-meta"><span>{_size(path.stat().st_size)}</span>'
                f'<span>Updated {_timestamp(path.stat().st_mtime)}</span></div>{note}'
                f'<article class="panel markdown">{_markdown(text)}</article>'
                f'<details class="raw-source"><summary>View Markdown source</summary><pre>{_escape(text)}</pre></details>')

    def _journal(self) -> str:
        path = self.root / "journal.md"
        if not path.is_file():
            return _empty("The research journal is empty", "New research entries will appear here.")
        query = self.params.get("q", "").strip()
        if len(query) > 200:
            raise DashboardError(400, "Journal search must be at most 200 characters.")
        index = index_journal(path, query)
        entries = [entry for entry in reversed(index.entries) if entry.matches]
        focused = self.params.get("entry", "")
        if focused:
            position = next((i for i, entry in enumerate(entries) if entry.key == focused), None)
            if position is None:
                raise DashboardError(404, "Journal entry not found in this search. It may have been compacted or replaced.")
            self.params["page"] = str(position // JOURNAL_PAGE_SIZE + 1)
        offset, paging = self._pagination(len(entries), page_size=JOURNAL_PAGE_SIZE)
        content = (f'<div class="document-meta"><span>{len(index.entries):,} recorded '
                   f'{"entry" if len(index.entries) == 1 else "entries"}</span>'
                   f'<span>{_size(index.size)}</span><span>Updated {_timestamp(index.modified)}</span></div>'
                   '<p class="intro">Newest entries first. Open a headline to read it; search the whole journal '
                   'or browse older entries. Nothing refreshes automatically while you read.</p>')
        content += (f'<section class="journal-toolbar" aria-label="Journal controls">'
                    f'<form class="filters" action="/journal" method="get"><label for="journal-search">Search journal'
                    f'<input id="journal-search" name="q" value="{_escape(query)}" maxlength="200" '
                    'type="search" placeholder="Find a decision, experiment, or phrase"></label>'
                    '<button type="submit">Search</button></form><div class="journal-controls">'
                    + _link("/journal", "Jump to latest")
                    + '<div class="journal-fold-controls" hidden><button type="button" data-journal-action="expand">'
                    'Expand page</button><button type="button" data-journal-action="collapse">Collapse page</button>'
                    '</div></div></section>')
        content += paging
        if not entries:
            content += _empty("No journal entries match this search" if query else "The research journal is empty",
                              "Try another phrase or jump to the latest entries." if query else "New research entries will appear here.")
        for position, entry in enumerate(entries[offset:offset + JOURNAL_PAGE_SIZE], offset):
            is_focused = entry.key == focused
            limit = DOCUMENT_LIMIT if is_focused or not entry.timestamp else JOURNAL_PREVIEW_LIMIT
            text, truncated = _read_text(path, limit, tail=not entry.timestamp, start=entry.start, end=entry.end)
            open_entry = is_focused or (not focused and position == offset)
            destination = _url("/journal", entry=entry.key) + f"#entry-{entry.key}"
            note = ""
            if truncated:
                portion = "the latest portion" if not entry.timestamp else "the beginning"
                note = f'<div class="notice">Showing {portion} of this long entry ({limit // 1024} KiB). '
                note += (_link("/journal", "Open this entry for a larger excerpt", entry=entry.key, q=query)
                         if limit < DOCUMENT_LIMIT else "The full entry remains in the journal file on disk.")
                note += "</div>"
            if query and query.casefold() not in text.casefold():
                note += '<p class="muted">The search matches entry metadata or text outside this excerpt.</p>'
            rendered = _markdown(text) if text.strip() else '<p class="muted">No entry text recorded yet.</p>'
            steps = []
            for adjacent, label in ((position - 1, "Newer entry"), (position + 1, "Older entry")):
                if 0 <= adjacent < len(entries):
                    target = entries[adjacent]
                    url = _url("/journal", entry=target.key, q=query) + f"#entry-{target.key}"
                    steps.append(f'<a href="{_escape(url)}">{label}</a>')
            content += (
                f'<details class="journal-entry" id="entry-{entry.key}" data-key="journal-{entry.key}"'
                + (" open" if open_entry else "") + ">"
                f'<summary><span><span class="journal-entry-meta">Entry {entry.number} &middot; '
                f'{_escape(entry.timestamp or "Untimestamped notes")}</span>'
                f'<strong class="journal-entry-title">{_escape(entry.title)}</strong></span></summary>'
                f'<div class="journal-entry-body">{note}<article class="markdown">{rendered}</article>'
                f'<details class="raw-source" data-key="journal-source-{entry.key}">'
                f'<summary>View Markdown source</summary><pre>{_escape(text)}</pre></details>'
                f'<div class="journal-entry-links"><a href="{_escape(destination)}">Link to this entry</a>'
                + "".join(steps) + "</div></div></details>")
        index.verify(path)
        return content + paging

    def _goal(self) -> str:
        return self._document(Path(self.config.config_path).parent / self.config.project.research_goal,
                              "No research goal yet")

    def _debug(self) -> str:
        return self._document(self.root / "DEBUG.md", "No errors logged", tail=True)

    def _download(self):
        rows = self._rows(
            "SELECT a.path,t.spec FROM artifacts a JOIN attempts t ON t.id=a.attempt_id WHERE a.id=?",
            (self.params.get("id", ""),))
        if not rows:
            raise DashboardError(404, "Unknown artifact.")
        spec = json.loads(rows[0]["spec"])
        try:
            artifact = contained(Path(spec["output"]), rows[0]["path"])
        except ValueError:
            raise DashboardError(403, "Artifact path escapes its output directory.") from None
        if not artifact.is_file():
            raise DashboardError(404, "Artifact no longer present.")
        with artifact.open("rb") as stream:
            length = stream.seek(0, 2)
            stream.seek(0)
            self._headers(200, "application/octet-stream", length)
            self.send_header("Content-Disposition", "attachment")
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile)


def run_dashboard(config_path: str = "xgenius.toml", port: int = 8765, *, chat: bool = False) -> None:
    """Start the read-only dashboard on the loopback interface."""
    config = load_config(config_path)
    with DashboardServer(("127.0.0.1", port), config_path, chat=chat) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"xgenius dashboard: {url}")
        print(f"DB: {Path(get_xgenius_dir(config)) / 'xgenius.db'}")
        print("Read-only. Press Ctrl+C to stop.")
        import webbrowser
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
