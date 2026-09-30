"""Read-only loopback views of retained research records and owned evidence."""

from datetime import datetime, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import sqlite3
import urllib.parse

from markdown_it import MarkdownIt

from xgenius import journal, reporting
from xgenius.config import ChatSettings
from xgenius.dashboard_chat import ChatError, ObserverService, load_chat_settings
from xgenius.dashboard_data import RESEARCH_SOURCES, query as _query, read_text as _read_text
from xgenius.evidence import observation, tail
from xgenius.protocol import ACTIVE
from xgenius.processes import CampaignLease
from xgenius.scheduler import ResourceLedger
from xgenius.state import State
from xgenius.worker import launch_directory


PAGE_SIZE = 50
LOG_LIMIT = 64 * 1024
DOCUMENT_LIMIT = 128 * 1024
JOURNAL_PAGE_SIZE = 20
JOURNAL_PREVIEW_LIMIT = 16 * 1024
STATIC = Path(__file__).with_name("static")
FAILED = ("failed", "timed_out", "interrupted", "not_started")
NAV = (
    ("/", "Overview"), ("/jobs", "Experiments"), ("/hypotheses", "Hypotheses"),
    ("/activity", "Agent activity"), ("/artifacts", "Artifacts"), ("/reports", "Reports"),
    ("/resources", "Resources"), ("/goal", "Research goal"), ("/journal", "Journal"), ("/debug", "Recovery"),
)
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
       "img-src 'self'; connect-src 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


def _escape(value):
    return html.escape(str(value)) if value is not None else ""


def _url(path, **params):
    value = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
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
    allowed = {path for path, _ in NAV} | {"/job", "/hypothesis", "/turn", "/view", "/observation"}
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


def _table(headers, rows):
    if not rows:
        return _empty("Nothing here yet", "New records will appear as the research progresses.")
    return ('<div class="table-scroll" tabindex="0"><table><thead><tr>'
            + "".join(f'<th scope="col">{_escape(h)}</th>' for h in headers) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
            + "</tbody></table></div>")


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
    def __init__(self, address, config_path, *, chat=False, observer=None):
        if address[0] not in ("127.0.0.1", "localhost"):
            raise ValueError("The dashboard must bind to loopback")
        self.config_path = str(Path(config_path).resolve())
        self.state = State.open(Path(self.config_path).parent / ".xgenius")
        self.chat_token = secrets.token_urlsafe(32)
        self.configuration_error = ""
        try:
            settings = load_chat_settings(self.config_path, enabled=chat)
        except (OSError, ValueError) as error:
            self.configuration_error = str(error)
            settings = ChatSettings()
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
            self.settings = json.loads(self._rows(
                "SELECT content FROM configs WHERE id=(SELECT config_revision FROM campaign)")[0]["content"])
            routes = {
                "/": ("Overview", self._overview), "/jobs": ("Experiments", self._jobs),
                "/job": ("Experiment details", self._job), "/hypotheses": ("Hypotheses", self._hypotheses),
                "/hypothesis": ("Hypothesis details", self._hypothesis), "/activity": ("Agent activity", self._activity),
                "/turn": ("Agent turn", self._turn), "/artifacts": ("Artifacts", self._artifacts),
                "/observation": ("Evidence revision", self._observation), "/reports": ("Reports", self._reports),
                "/view": ("Historical source view", self._view), "/resources": ("Resources", self._resources),
                "/journal": ("Research journal", self._journal), "/goal": ("Research goal", self._goal),
                "/debug": ("Recovery", self._debug),
            }
            if self.path_name == "/artifact":
                self._download()
                return
            if self.path_name not in routes:
                raise DashboardError(404, "This dashboard page does not exist.")
            title, render = routes[self.path_name]
            self._send(self._page(title, render()).encode("utf-8"))
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
            cid = data.get("conversation_id", "")
            if path == "/chat/message":
                result = self.server.observer.send(cid, data.get("request_id"), data.get("message"))
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
        return _query(self.db, sql, params)

    def _page(self, title, content):
        project = self.settings.get("project", {}).get("name", "Research dashboard")
        active = {"/job": "/jobs", "/hypothesis": "/hypotheses", "/turn": "/activity",
                  "/observation": "/artifacts", "/view": "/reports"}.get(self.path_name, self.path_name)
        nav = "".join(f'<a href="{path}"' + (' aria-current="page"' if active == path else "")
                      + f">{label}</a>" for path, label in NAV)
        refresh = ('<label class="live-toggle"><input id="live-refresh" type="checkbox"> Auto-refresh (15s)</label>'
                   if self.path_name in ("/", "/jobs", "/activity", "/artifacts", "/resources") else "")
        if self.server.configuration_error:
            content = ('<div class="notice danger">Current configuration unavailable; showing retained records. '
                       + _escape(self.server.configuration_error) + "</div>" + content)
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
<span class="mode">Local campaign</span><nav aria-label="Main navigation">{nav}</nav>
<div class="sidebar-note"><span class="status-dot"></span> Read-only dashboard<br>
<span>Local evidence. No remote assets.</span></div></aside>
<div class="workspace"><header class="topbar"><div><span class="eyebrow">RESEARCH OBSERVATORY</span>
<h1>{_escape(title)}</h1></div><div class="toolbar">{refresh}
<button id="chat-open" type="button" aria-controls="chat-panel" aria-expanded="false">Ask Copilot</button>
<button id="refresh" type="button">Refresh</button></div></header>
<div class="refresh-status" id="refresh-status" role="status" aria-live="polite">Updated
{datetime.now(timezone.utc):%H:%M:%S} UTC</div><main id="main" tabindex="-1">{content}</main>
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
        current, budget = self.state.campaign(), self.state.budget()
        counts = {r["status"]: r["n"] for r in self._rows("SELECT status,COUNT(*) n FROM attempts GROUP BY status")}
        pending = self._rows("SELECT COUNT(*) n FROM events WHERE acknowledged_by IS NULL")[0]["n"]
        content = (f'<section class="campaign-banner"><div><span class="eyebrow">GENERATION {current["generation"]}</span>'
                   f'<h2>{_badge(current["state"])}</h2><p>{_escape(current["reason"])}</p>'
                   f'<code class="identifier">{current["id"]}</code></div><div class="campaign-signals">'
                   f'<span>{pending} unacknowledged events</span>'
                   f'<span>Controller {"registered" if current["controller"] else "not registered"}</span></div></section>')
        content += _panel("Independent lifecycle facts", _facts([
            ("Operator intent", _badge(current["operator_mode"])), ("Research progression", _badge(current["progress"])),
            ("Research outcome", _badge(current["research_outcome"])), ("Recovery blockers", str(len(current["blockers"]))),
            ("Assessment scope", "Later authority changed; historical assessment is stale" if current["assessment_scope_stale"] else "No later authority change recorded"),
        ]) + '<p class="muted">Only an owned assessed closure plus operational quiescence is completed research.</p>')
        content += '<div class="stat-grid">' + "".join(
            f'<a class="stat-card" href="{url}"><span>{label}</span><strong>{n}</strong><small>{hint}</small></a>'
            for label, n, hint, url in [
                ("Experiments", sum(counts.values()), "All recorded attempts", "/jobs"),
                ("In flight", sum(counts.get(s, 0) for s in ACTIVE), "Includes unknown ownership", "/jobs?status=active"),
                ("Needs attention", sum(counts.get(s, 0) for s in (*FAILED, "recovery_required")), "Execution, not scientific acceptance", "/jobs?status=attention"),
                ("Hypotheses", self._rows("SELECT COUNT(*) n FROM hypotheses")[0]["n"], "Immutable evaluated claims", "/hypotheses"),
            ]) + "</div>"
        meters = ""
        for name, label in (("elapsed_admission_seconds", "Elapsed admission horizon"), ("managed_invocations", "Provider invocations")):
            item = budget[name]
            limit = item["configured"]
            meters += _meter(label, item["used"], limit, f'{item["used"]:,.1f} / {"Unlimited" if item["unlimited"] else limit}')
        meters += '<p class="muted">Last admitted limits; pauses/restarts count. Armed uncertainty remains charged. Observer usage is separate.</p>'
        turns = self._rows("SELECT * FROM turns ORDER BY created DESC LIMIT 1")
        latest = self._turn_summary(turns[0]) if turns else _empty("No agent turns yet", "Run starts eligible research.")
        content += '<div class="two-column">' + _panel("Campaign limits", meters) + _panel("Latest agent turn", latest) + "</div>"
        if current["closure"]:
            content += _panel("Closure coverage", f'<pre>{_escape(json.dumps(current["closure"], indent=2))}</pre>')
        return content + _panel("Recent experiments", self._job_table(
            self._rows("SELECT * FROM attempts ORDER BY created DESC,id DESC LIMIT 8")))

    def _job_table(self, rows):
        return _table(["Experiment", "Execution", "Collection / validation", "Runtime", "Submitted"], [
            [_link("/job", r["experiment_id"], id=r["id"]) + f'<code class="identifier">{_escape(r["id"])}</code>',
             _badge(r["status"]), _badge(r["collection"]) + " " + _badge(r["validation"]),
             _duration(r["elapsed"]), _timestamp(r["created"])] for r in rows])

    def _jobs(self):
        where, args = ["1=1"], []
        status, query = self.params.get("status", ""), self.params.get("q", "")
        if status in ("active", "attention"):
            choices = ACTIVE if status == "active" else (*FAILED, "recovery_required")
            where.append("(status IN (" + ",".join("?" for _ in choices) + ")"
                         + (" OR validation='invalid' OR collection='failed')" if status == "attention" else ")"))
            args.extend(choices)
        elif status:
            where.append("status=?")
            args.append(status)
        if self.params.get("hypothesis_id"):
            where.append("hypothesis_id=?")
            args.append(self.params["hypothesis_id"])
        if query:
            where.append("(instr(lower(experiment_id),lower(?))>0 OR instr(lower(id),lower(?))>0)")
            args.extend((query, query))
        condition = " AND ".join(where)
        count = self._rows(f"SELECT COUNT(*) n FROM attempts WHERE {condition}", tuple(args))[0]["n"]
        offset, paging = self._pagination(count)
        rows = self._rows(f"SELECT * FROM attempts WHERE {condition} ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                          (*args, PAGE_SIZE, offset))
        statuses = ["", "active", "attention"] + [r["status"] for r in self._rows("SELECT DISTINCT status FROM attempts ORDER BY status")]
        options = "".join(f'<option value="{_escape(s)}"' + (" selected" if s == status else "")
                          + f'>{_escape(s or "All statuses")}</option>' for s in statuses)
        filters = (f'<form class="filters" action="/jobs"><label>Search experiments<input type="search" name="q" value="{_escape(query)}"></label>'
                   f'<label>Status<select name="status">{options}</select></label><label>Hypothesis<input name="hypothesis_id" '
                   f'value="{_escape(self.params.get("hypothesis_id", ""))}"></label><button type="submit">Filter</button></form>')
        return filters + _panel("Experiment history", self._job_table(rows)) + paging

    def _job(self):
        rows = self._rows("SELECT * FROM attempts WHERE id=?", (self.params.get("id", ""),))
        if not rows:
            raise DashboardError(404, "Unknown experiment.")
        row = rows[0]
        spec = json.loads(row.pop("spec"))
        content = _panel(row["experiment_id"], _facts([
            ("Execution", _badge(row["status"])), ("Validation", _badge(row["validation"])),
            ("Collection", _badge(row["collection"])), ("Runtime", _duration(row["elapsed"])),
            ("Exit code", _escape(row["exit_code"])), ("Reason", _escape(row["reason"])),
            ("Collection reason", _escape(row["collection_reason"])), ("Generation", str(row["generation"])),
            ("Hypothesis", _link("/hypothesis", row["hypothesis_id"], id=row["hypothesis_id"]) if row["hypothesis_id"] else "Support work"),
        ]))
        content += _panel("Frozen provenance", f'<pre>{_escape(json.dumps({k: v for k, v in spec.items() if k not in ("environment", "inputs")}, indent=2))}</pre>')
        content += _panel("Registered evidence", self._artifact_table(self._observations("WHERE o.attempt_id=?", (row["id"],))))
        return content + self._work_logs(row["id"])

    def _hypotheses(self):
        count = self._rows("SELECT COUNT(*) n FROM hypotheses")[0]["n"]
        offset, paging = self._pagination(count)
        rows = self._rows("""SELECT h.*,COUNT(a.id) attempts FROM hypotheses h LEFT JOIN attempts a ON a.hypothesis_id=h.id
            GROUP BY h.id ORDER BY h.created DESC,h.id LIMIT ? OFFSET ?""", (PAGE_SIZE, offset))
        return _panel("Research questions", _table(["Hypothesis statement", "Status", "Evaluated", "Experiments", "Updated"], [
            [_link("/hypothesis", r["statement"][:240], id=r["id"]) + f'<code class="identifier">{_escape(r["id"])}</code>',
             _badge(r["status"]), "Frozen" if r["frozen"] else "Not yet admitted", str(r["attempts"]), _timestamp(r["updated"])]
            for r in rows])) + paging

    def _hypothesis(self):
        hid = self.params.get("id", "")
        rows = self._rows("SELECT * FROM hypotheses WHERE id=?", (hid,))
        if not rows:
            raise DashboardError(404, "Unknown hypothesis.")
        row = rows[0]
        content = _panel("Hypothesis statement", f'<div class="markdown">{_markdown(row["statement"])}</div>' + _facts([
            ("Hypothesis ID", _escape(hid)), ("Status", _badge(row["status"])),
            ("Evaluated claim", "Frozen; changed claims require a new ID" if row["frozen"] else "Not yet admitted"),
            ("Supersedes", _link("/hypothesis", row["supersedes"], id=row["supersedes"]) if row["supersedes"] else "None"),
        ]))
        if row["conclusion"]:
            content += _panel("Recorded conclusion", f'<div class="markdown">{_markdown(row["conclusion"])}</div>')
        metadata = json.loads(row["metadata"])
        if metadata:
            content += _panel("Claim context", f'<pre>{_escape(json.dumps(metadata, indent=2))}</pre>')
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
        from xgenius.protocol import LaunchEnvelope
        launches = self._rows("SELECT envelope FROM launches WHERE work_id=? ORDER BY created DESC LIMIT 1", (work_id,))
        if not launches:
            return ""
        envelope = LaunchEnvelope.parse(json.loads(launches[0]["envelope"]))
        directory = launch_directory(envelope)
        content = ""
        for name in ("stdout", "stderr"):
            path = directory / "main" / f"{name}.log"
            if path.exists():
                value = tail(path, limit=LOG_LIMIT)
                content += f'<details><summary>{name}</summary><p class="muted">Bounded retained tail; earlier output may be omitted.</p><pre>{_escape(value["text"])}</pre></details>'
        return _panel("Captured logs", content or '<p class="muted">No retained main streams yet.</p>')

    def _observations(self, where="", params=(), limit=PAGE_SIZE, offset=0):
        return self._rows(f"""SELECT o.id,o.attempt_id,o.path,o.kind,o.size,o.digest,o.assurance,o.created,
            o.assurance='captured' AS captured,a.experiment_id FROM observations o JOIN attempts a ON a.id=o.attempt_id
            {where} ORDER BY o.created DESC,o.id DESC LIMIT ? OFFSET ?""", (*params, limit, offset))

    def _artifact_table(self, rows):
        return _table(["Evidence revision", "Experiment", "Size", "Assurance", "Exact bytes"], [
            [_link("/observation", r["path"], id=r["id"]) + f'<code class="identifier">{r["id"]}</code>',
             _link("/job", r["experiment_id"], id=r["attempt_id"]), _size(r["size"]), _escape(r["assurance"]),
             _link("/artifact", "Captured download", id=r["id"]) if r["captured"] else "Mutable external artifact; not served as exact"]
            for r in rows])

    def _artifacts(self):
        needle = self.params.get("q", "")
        condition = "WHERE instr(lower(o.path),lower(?))>0 OR instr(lower(a.experiment_id),lower(?))>0"
        params = (needle, needle)
        count = self._rows(f"SELECT COUNT(*) n FROM observations o JOIN attempts a ON a.id=o.attempt_id {condition}", params)[0]["n"]
        offset, paging = self._pagination(count)
        filters = f'<form class="filters"><label>Find evidence<input name="q" value="{_escape(needle)}"></label><button>Search</button></form>'
        return filters + _panel("Evidence bank", self._artifact_table(self._observations(condition, params, offset=offset))) + paging

    def _observation(self):
        rows = self._observations("WHERE o.id=?", (self.params.get("id", ""),))
        if not rows:
            raise DashboardError(404, "Unknown evidence revision.")
        row = rows[0]
        metadata = json.loads(self._rows("SELECT metadata FROM observations WHERE id=?", (row["id"],))[0]["metadata"])
        metrics = metadata.get("metrics", {})
        content = self._artifact_table(rows) + _facts([("SHA-256", _escape(row["digest"])), ("Recorded", _timestamp(row["created"]))])
        content += _panel("Recorded numeric metrics", _table(["Metric", "Value"], [
            [_escape(k[:512]), _escape(v)] for k, v in list(metrics.items())[:32]]))
        return content + f'<p class="muted">Showing {min(len(metrics), 32)} of {len(metrics)} metrics. Downloads serve the retained bytes, not current files.</p>'

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
        budget = self.state.budget()
        content = _panel("Campaign envelope", f'<pre>{_escape(json.dumps(budget, indent=2))}</pre>'
                         '<p class="muted">Includes managed reasoning and experiments. CPU affinity is placement, not a native CPU-time quota.</p>')
        path, identity = self.state.ledger_identity()
        if not path.exists():
            return content + _panel("Shared machine capacity", _empty("Not configured", "Recorded ledger is missing; it has not been created."))
        ledger = ResourceLedger(path, expected_id=identity or None)
        capacity, rows = ledger.capacity(), ledger.rows()
        active = [r for r in rows if r["state"] == "granted"]
        content += _panel("Shared machine capacity", _meter("Reserved CPUs", sum(r["cpus"] for r in active), capacity["cpus"],
                          f'{sum(r["cpus"] for r in active)} / {capacity["cpus"]}')
                          + _meter("Reserved memory (MiB)", sum(r["memory_mb"] for r in active), capacity["memory_mb"],
                                   f'{sum(r["memory_mb"] for r in active)} / {capacity["memory_mb"]}')
                          + _facts([("Headroom", f'{capacity["headroom_mb"]} MiB'), ("Capacity revision", str(capacity["revision"]))]))
        content += _panel("Unreleased grants (first 100)", _table(["Work", "Consumer", "State", "CPU / RAM", "Placement", "Queue reason"], [
            [_escape(r["work_id"]), _escape(r["kind"]), _badge(r["state"]), f'{r["cpus"]} / {r["memory_mb"]} MiB',
             _escape(r["native_cpus"]), _escape(r["reason"])] for r in rows]))
        content += _panel("Configured storage watermarks", f'<pre>{_escape(json.dumps(self.settings["storage"], indent=2))}</pre>'
                          '<p class="muted">Soft monitoring, not a hard disk quota. Use storage inventory for owned sizes and reference reachability.</p>')
        return content

    def _source_article(self, source_id, *, focused=False):
        rows = self._rows("SELECT id,seq,kind,created,length(body) bytes FROM sources WHERE id=?", (source_id,))
        if not rows or rows[0]["kind"] not in RESEARCH_SOURCES:
            raise DashboardError(404, "Exact retained research source is unavailable.")
        row = rows[0]
        limit = DOCUMENT_LIMIT if focused else JOURNAL_PREVIEW_LIMIT
        if row["bytes"] <= limit:
            value = journal.entry(self.state.db, source_id, limit=limit)
            text = value["markdown"]
            more = ""
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
            article = self._source_article(source_id, focused=True)
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
        rows = self._rows("SELECT source_id FROM source_heads WHERE name='goal'")
        return self._source_article(rows[0]["source_id"], focused=True) if rows else _empty("No research goal yet", "No retained goal revision exists.")

    def _debug(self):
        current = self.state.campaign()
        content = _panel("Unresolved recovery blockers", _table(["Category", "Work", "Detail", "Recorded"], [
            [_escape(r["category"]), _escape(r["work_id"]), _escape(r["detail"]), _timestamp(r["created"])] for r in current["blockers"]]))
        rows = self._rows("SELECT token,kind,state,reason FROM allocations WHERE state!='released' ORDER BY created LIMIT 100")
        return content + _panel("Pending allocation effects", _table(["Token", "Kind", "State", "Reason"], [
            [_escape(r["token"]), _escape(r["kind"]), _badge(r["state"]), _escape(r["reason"])] for r in rows]))

    def _reports(self):
        rows = self._rows("SELECT id,view_id,created,outputs FROM reports ORDER BY created DESC LIMIT 50")
        return _panel("Historical reports (latest 50)", _table(["Report", "Sources", "Published", "Retained outputs"], [
            [_escape(r["id"]), _link("/view", "Immutable source inventory", id=r["view_id"]), _timestamp(r["created"]),
             f'<pre>{_escape(json.dumps(json.loads(r["outputs"]), indent=2))}</pre>'] for r in rows]))

    def _view(self):
        value = reporting.page(self.state.db, self.params.get("id", ""), offset=self._int("offset"), limit=20)
        coverage = value["coverage"]
        content = _panel("Pinned source scope", f'<pre>{_escape(json.dumps({k: v for k, v in value.items() if k != "attempts"}, indent=2))}</pre>')
        content += _table(["Attempt", "Selected", "Admitted", "Execution", "Validation", "Observation denominator"], [
            [_link("/job", r["experiment_id"], id=r["id"]), str(r["selected"]), str(r["admitted"]),
             _badge(r["status"]), _badge(r["validation"]), str(r["observation_count"])] for r in value["attempts"]])
        if coverage["has_more"]:
            content += _link("/view", "Next inventory page", id=value["id"], offset=coverage["end"])
        return content


def run_dashboard(config_path="xgenius.toml", port=8765, *, chat=False, open_browser=False, json_output=False):
    with DashboardServer(("127.0.0.1", port), config_path, chat=chat) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(json.dumps({"url": url, "read_only": True}) if json_output else f"xgenius dashboard: {url}", flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return
