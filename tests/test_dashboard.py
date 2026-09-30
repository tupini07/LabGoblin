"""Read-only HTTP dashboard coverage, without starting agents or workloads."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

from xgenius.campaign import Campaign
from xgenius.config import load_config
from xgenius.dashboard import DashboardServer, DOCUMENT_LIMIT, JOURNAL_PAGE_SIZE, LOG_LIMIT, _markdown, _read_text
from xgenius.dashboard_data import index_journal
from xgenius.db import XGeniusDB, _connect
from xgenius.journal import ResearchJournal
from xgenius.local_cli import initialize
from xgenius.workspace import collect_artifacts


@contextmanager
def serve(config_path, **kwargs):
    with DashboardServer(("127.0.0.1", 0), str(config_path), **kwargs) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def get(base, path="/"):
    try:
        response = urllib.request.urlopen(base + path, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XGENIUS_RESOURCE_DB", str(tmp_path / "machine.db"))
    initialize(argparse.Namespace(force=False, agent="copilot"))
    value = Campaign(load_config(str(tmp_path / "xgenius.toml")))
    value.ledger.configure(1, 256, [], 0)
    return value


def job(campaign, **overrides):
    return campaign.submit({
        "key": "example", "argv": ["python", "-c", "print('hello')"],
        "cpus": 1, "memory_mb": 128, "seconds": 30, **overrides,
    })


def test_markdown_formats_and_rejects_active_content():
    result = _markdown(
        '# Journal\n\n**Measured** and `code`.\n\n'
        '| Method | Score |\n| --- | --- |\n| baseline | 0.4 |\n\n'
        '```python\nprint("<script>")\n```\n\n'
        '<script>alert(1)</script>\n\n[unsafe](javascript:alert(1))\n\n'
        '<img src=x onerror=alert(1)>\n'
    )
    assert '<h1>Journal</h1>' in result
    assert '<strong>Measured</strong>' in result
    assert '<table>' in result and '<th>Method</th>' in result
    assert '<code class="language-python">' in result
    assert '<script>' not in result and '<img src=x' not in result
    assert 'href="javascript:' not in result
    assert '&lt;script&gt;' in result


def test_log_tail_preserves_long_single_lines(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(b"prefix-" * LOG_LIMIT + b"TAIL\n")
    text, truncated = _read_text(path, LOG_LIMIT, tail=True)
    assert truncated and text.endswith("TAIL\n") and len(text) == LOG_LIMIT


def test_all_empty_routes_and_local_assets(campaign):
    with serve(campaign.config.config_path) as base:
        for path in ("/", "/jobs", "/hypotheses", "/activity", "/artifacts",
                     "/resources", "/journal", "/goal", "/debug"):
            status, headers, body = get(base, path)
            assert status == 200, (path, body.decode())
            assert headers["Content-Type"] == "text/html; charset=utf-8"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
            assert "img-src 'self'" in headers["Content-Security-Policy"]
            assert headers["Cache-Control"] == "no-store"
            assert b'aria-label="Main navigation"' in body
            assert b'Read-only dashboard' in body
        for path, mime in (("/static/dashboard.css", "text/css"), ("/static/dashboard.js", "text/javascript")):
            status, headers, body = get(base, path)
            assert status == 200 and headers["Content-Type"].startswith(mime)
            assert len(body) > 500
        assert b'No agent turns yet' in get(base)[2]
        assert b'The research journal is empty' in get(base, "/journal")[2]
        assert b'<h1>Debug Log</h1>' in get(base, "/debug")[2]
        (campaign.state.root / "DEBUG.md").unlink()
        assert b'No errors logged' in get(base, "/debug")[2]


def test_journal_goal_debug_are_utf8_markdown(campaign):
    text = "# Evidence\n\n**Result:** caf\u00e9 \u03b1.\n\n- Measured\n- Repeated\n"
    for name in ("journal.md", "DEBUG.md"):
        (campaign.state.root / name).write_text(text, encoding="utf-8")
    (campaign.project / "research_goal.md").write_text(text, encoding="utf-8")
    with serve(campaign.config.config_path) as base:
        for path in ("/journal", "/goal", "/debug"):
            status, _, body = get(base, path)
            rendered = body.decode("utf-8")
            assert status == 200
            assert "<h1>Evidence</h1>" in rendered and "<strong>Result:</strong>" in rendered
            assert "caf\u00e9 \u03b1" in rendered and "<ul>" in rendered
            assert "View Markdown source" in rendered


def test_hypothesis_statements_and_related_journal_context_are_read_only(campaign):
    statement = "Adding **constraints** improves coverage without increasing violations."
    campaign.state.db.add_hypothesis("h001", statement, motivation="Test whether relational structure helps.",
                                     expected_outcome="Higher coverage at the same error rate.")
    campaign.state.db.update_hypothesis("h001", conclusion="Still measuring.", comment="Replicate independently.")
    attempt = job(campaign, key="constraint-control", hypothesis_id="h001")
    other = job(campaign, key="other-control", hypothesis_id="h010")
    journal = campaign.state.root / "journal.md"
    journal.write_text(
        f"# Research journal\n\n## Why test constraints\n\nHypothesis `h001` tests **feasibility**, not prettier pictures.\n\n"
        f"Attempt `{attempt}` measures coverage against the same baseline.\n\n"
        "## Replication\n\nconstraint-control uses independent groups.\n\n"
        f"## Unrelated\n\nOnly h001-extra and {other} have this unrelated finding.\n", encoding="utf-8")
    with _connect(campaign.state.path) as db:
        before = list(db.iterdump())
    before_journal = journal.read_bytes()
    with serve(campaign.config.config_path) as base:
        index = get(base, "/hypotheses")[2].decode()
        assert f'href="/hypothesis?id=h001">{statement}</a>' in index
        assert '<code class="identifier">h001</code>' in index
        detail = get(base, "/hypothesis?id=h001")[2].decode()
        for text in ("Hypothesis statement", "<strong>constraints</strong>", "Test whether relational structure helps.",
                     "Higher coverage at the same error rate.", "Still measuring.", "Replicate independently.",
                     "Related journal context", "Why test constraints", "<strong>feasibility</strong>",
                     "independent groups", "recorded hypothesis statement"):
            assert text in detail
        assert "unrelated finding" not in detail
        assert 'href="/jobs?hypothesis_id=h001"' in detail
        assert 'href="/journal"' in detail
    assert journal.read_bytes() == before_journal
    with _connect(campaign.state.path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("description", ["", "h001", "Auto-created from submit: opaque-experiment"])
def test_hypothesis_placeholders_are_not_presented_as_statements(campaign, description):
    campaign.state.db.add_hypothesis("h001", description)
    with serve(campaign.config.config_path) as base:
        assert b"Hypothesis statement not recorded" in get(base, "/hypotheses")[2]
        detail = get(base, "/hypothesis?id=h001")[2]
        assert b"Hypothesis statement not recorded" in detail and b"No matching journal context" in detail
        assert b"not an inferred definition" in detail
        assert b"Auto-created from submit:" not in detail


def test_hypothesis_context_is_bounded_escaped_and_reference_specific(campaign):
    campaign.state.db.add_hypothesis("h001", "Compare two constraints.")
    journal = campaign.state.root / "journal.md"
    journal.write_text("h001 old finding must be outside the searched portion\n\n" + "x" * DOCUMENT_LIMIT
                       + "\n\n## Context\n\n"
                       + "\n\n".join(f"h001 recorded paragraph {index}" for index in range(8))
                       + "\n\nh001 <script>bad()</script> " + "z" * 3200, encoding="utf-8")
    with serve(campaign.config.config_path) as base:
        detail = get(base, "/hypothesis?id=h001")[2].decode()
        assert "old finding must be outside" not in detail
        assert "latest 128 KiB" in detail and "first three and latest three" in detail
        assert "recorded paragraph 0" in detail and "recorded paragraph 7" in detail
        assert "recorded paragraph 4" not in detail
        assert "&lt;script&gt;bad()" in detail and "<script>bad()" not in detail
        assert "Excerpt truncated" in detail
        assert len(detail) < 16000


def test_job_detail_artifacts_metrics_and_validation_errors(campaign):
    source = campaign.project / "experiment.py"
    source.write_text("print('hello')", encoding="utf-8")
    attempt = job(campaign, source_files=["experiment.py"], hypothesis_id="h001")
    spec = json.loads(campaign.state.attempt(attempt)["spec"])
    output = Path(spec["output"])
    (output / "metrics.json").write_text('{"score": 0.75}', encoding="utf-8")
    collect_artifacts(campaign.state, spec)
    campaign.state.transition(attempt, "completed", reason="Artifact validation failed: expected preview missing",
                              receipt={"returncode": 0, "elapsed": 91, "gpu_hours": 0})
    (Path(spec["root"]) / "stderr.log").write_text("<script>not executable</script>", encoding="utf-8")
    with serve(campaign.config.config_path) as base:
        overview = get(base)[2].decode()
        assert "Review recorded error" in overview
        detail = get(base, "/job?id=" + attempt)[2].decode()
        assert "Artifact validation failed: expected preview missing" in detail
        assert "score" in detail and "0.75" in detail and "1m 31s" in detail
        assert hashlib.sha256(source.read_bytes()).hexdigest() in detail
        assert "&lt;script&gt;not executable&lt;/script&gt;" in detail
        assert "<script>not executable" not in detail
        assert "example" in get(base, "/jobs?status=attention")[2].decode()
        assert "example" not in get(base, "/jobs?status=failed")[2].decode()
        assert b"h001" in get(base, "/hypothesis?id=h001")[2]
        with _connect(campaign.state.path) as db:
            artifact = db.execute("SELECT id FROM artifacts").fetchone()[0]
        status, headers, content = get(base, "/artifact?id=" + artifact)
        assert status == 200 and content == (output / "metrics.json").read_bytes()
        assert headers["Content-Disposition"] == "attachment"
        assert headers["Content-Type"] == "application/octet-stream"
        assert b"score" in get(base, "/artifacts?q=metrics")[2]
        assert b"metrics.json" not in get(base, "/artifacts?q=absent")[2]


def test_download_refuses_missing_and_escaping_artifacts(campaign):
    attempt = job(campaign)
    with _connect(campaign.state.path) as db:
        db.execute("INSERT INTO artifacts VALUES(?,?,?,?)", ("bad", attempt, "../spec.json", "{}"))
        db.execute("INSERT INTO artifacts VALUES(?,?,?,?)", ("missing", attempt, "missing.txt", "{}"))
    with serve(campaign.config.config_path) as base:
        assert get(base, "/artifact?id=bad")[0] == 403
        assert get(base, "/artifact?id=missing")[0] == 404
        assert get(base, "/artifact?id=unknown")[0] == 404


def test_filters_pagination_and_escaped_user_content(campaign):
    for index in range(53):
        campaign.state.db.record_job(
            f"id-{index:03d}", "native", f"experiment-{index:03d}", "", "python", cpus=1, gpus=0)
    campaign.state.db.record_job("zz-danger", "<img src=x>", "<script>alert(1)</script>", "", "python")
    with serve(campaign.config.config_path) as base:
        first = get(base, "/jobs")[2].decode()
        assert "54 records" in first and "Page 1 of 2" in first
        assert 'page=2' in first and len(first.split('href="/job?id=')) - 1 == 50
        second = get(base, "/jobs?page=2")[2].decode()
        assert "Page 2 of 2" in second and len(second.split('href="/job?id=')) - 1 == 4
        filtered = get(base, "/jobs?q=experiment-052")[2].decode()
        assert "1 records" in filtered and "experiment-052" in filtered and "experiment-051" not in filtered
        assert "<script>alert(1)</script>" not in first
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in first
        assert get(base, "/jobs?page=0")[0] == 400
        assert get(base, "/jobs?page=oops")[0] == 400
        assert get(base, "/jobs?page=100000000000")[0] == 400
        assert get(base, "/jobs?page=3")[0] == 404
        assert b"0 records" in get(base, "/jobs?status=%27%20OR%201%3D1")[2]


def test_turn_activity_pending_events_and_elapsed_budget(campaign):
    started = time.time() - 125
    with _connect(campaign.state.path) as db:
        db.execute("UPDATE campaign SET started=?,state='completed',reason=?", (started, "Scope assessed"))
        db.execute("INSERT INTO turns(id,events,started,ended,state,result,kind) VALUES(?,?,?,?,?,?,?)",
                   ("turn-1", '["initial"]', started, started + 60, "completed",
                    json.dumps({"reason": "**Measured** result", "disposition": "complete"}), "research"))
    directory = campaign.state.root / "turns" / "turn-1"
    directory.mkdir(parents=True)
    (directory / "stdout.log").write_bytes(b"x" * (LOG_LIMIT + 100) + b"\nRECENT OUTPUT")
    with serve(campaign.config.config_path) as base:
        overview = get(base)[2].decode()
        assert "Scope assessed" in overview and "1 unacknowledged events" in overview
        assert "1 / 10" in overview and "2m " in overview
        assert "Controller not registered" in overview
        activity = get(base, "/activity")[2].decode()
        assert "turn-1" in activity and "initial" in activity and "Awaiting acknowledgement" in activity
        detail = get(base, "/turn?id=turn-1")[2].decode()
        assert "1m 0s" in detail and "Not reported (not zero)" in detail
        assert "RECENT OUTPUT" in detail and "last 64 KiB" in detail
        assert len(detail) < LOG_LIMIT + 20000


def test_resources_read_shared_reservations_without_mutating_state(campaign):
    attempt = job(campaign)
    spec = json.loads(campaign.state.attempt(attempt)["spec"])
    campaign.ledger.register(campaign.state, spec)
    with campaign.ledger.connect() as db:
        db.execute("UPDATE reservations SET state='reserved',reason='Ownership unknown; retain capacity'")
    with _connect(campaign.state.path) as db:
        before = list(db.iterdump())
    with campaign.ledger.connect() as db:
        ledger_before = list(db.iterdump())
    with serve(campaign.config.config_path) as base:
        status, _, body = get(base, "/resources")
        assert status == 200
        assert b"1 / 1" in body and b"128 / 256" in body
        assert b"Ownership unknown; retain capacity" in body
        for path in ("/", "/activity", "/jobs", "/artifacts"):
            assert get(base, path)[0] == 200
    with _connect(campaign.state.path) as db:
        assert list(db.iterdump()) == before
    with campaign.ledger.connect() as db:
        assert list(db.iterdump()) == ledger_before


def test_missing_resource_ledger_is_not_created(campaign):
    campaign.ledger.path.unlink()
    with serve(campaign.config.config_path) as base:
        assert b"Not configured" in get(base, "/resources")[2]
    assert not campaign.ledger.path.exists()


def test_oversized_journal_is_bounded_and_latest_entries_visible(campaign):
    (campaign.state.root / "journal.md").write_bytes(
        b"old\n" * (DOCUMENT_LIMIT // 4 + 50) + b"\n# Latest finding\n\nPreserve this.\n")
    with serve(campaign.config.config_path) as base:
        body = get(base, "/journal")[2]
        assert b"Showing the latest portion" in body
        assert b"<h1>Latest finding</h1>" in body
        assert len(body) < DOCUMENT_LIMIT * 3


def test_journal_index_respects_entries_fences_unicode_and_append_stability(campaign):
    path = campaign.state.root / "journal.md"
    path.write_text("# Compacted context\n\nEarlier caf\u00e9 decisions.\n", encoding="utf-8")
    journal = ResearchJournal(campaign.config)
    journal.write("## Repeated title\n\nFirst entry.\n\n```markdown\n---\n**[2026-09-29 01:02 UTC]**\n"
                  "\n## Not a separate entry\n```\n\n### A subsection\n\nMore findings.")
    journal.write("## Repeated title\n\nSecond entry with a **measured** result.")
    index = index_journal(path)
    assert [entry.number for entry in index.entries] == [1, 2, 3]
    assert [entry.title for entry in index.entries] == ["Compacted context", "Repeated title", "Repeated title"]
    assert len({entry.key for entry in index.entries}) == 3
    first, _ = _read_text(path, 10000, start=index.entries[1].start, end=index.entries[1].end)
    assert "Not a separate entry" in first and "Second entry" not in first
    assert [entry.number for entry in index_journal(path, "CAF\u00c9").entries if entry.matches] == [1]
    old_keys = [entry.key for entry in index.entries]
    journal.write("## New entry\n\nFresh observation.")
    assert [entry.key for entry in index_journal(path).entries][:3] == old_keys
    with pytest.raises(OSError, match="journal changed"):
        index.verify(path)


def test_journal_search_spans_large_utf8_lines_and_crlf(campaign):
    path = campaign.state.root / "journal.md"
    path.write_bytes((
        "---\r\n**[2026-09-29 01:02 UTC]**\r\n\r\n## Large entry\r\n\r\n"
        + "x" * 65532 + "CAF\u00c9" + "x" * 1000
        + "\r\n\r\n---\r\n**[2026-09-29 01:03 UTC]**\r\n\r\n## Small entry\r\n\r\nOther evidence."
    ).encode("utf-8"))
    entries = index_journal(path, "caf\u00e9").entries
    assert len(entries) == 2
    assert entries[0].matches and not entries[1].matches
    assert entries[0].timestamp == "2026-09-29 01:02 UTC"


def test_journal_pagination_search_and_stable_entry_links_are_read_only(campaign):
    path = campaign.state.root / "journal.md"
    journal = ResearchJournal(campaign.config)
    for index in range(JOURNAL_PAGE_SIZE * 2 + 2):
        journal.write(f"## ENTRY-{index + 1:03d}\n\n"
                      + ("archive-only-first" if index == 0 else "Recorded result")
                      + "\n\n" + "Evidence preserved. " * 500)
    before = path.read_bytes()
    assert len(before) > DOCUMENT_LIMIT
    entries = index_journal(path).entries
    oldest = entries[0]
    with serve(campaign.config.config_path) as base:
        first = get(base, "/journal")[2].decode()
        assert first.count('class="journal-entry"') == JOURNAL_PAGE_SIZE
        assert "ENTRY-042" in first and "ENTRY-001" not in first
        assert "42 entries" in first and "Page 1 of 3" in first
        older = get(base, "/journal?page=3")[2].decode()
        assert older.count('class="journal-entry"') == 2
        assert "ENTRY-001" in older and "ENTRY-042" not in older
        assert "Newer entries" in older
        selected = get(base, "/journal?entry=" + oldest.key)[2].decode()
        assert f'id="entry-{oldest.key}" data-key="journal-{oldest.key}" open' in selected
        assert "Page 3 of 3" in selected
        assert 'href="/journal?page=2"' in selected
        searched = get(base, "/journal?q=archive-only-first")[2].decode()
        assert "1 entry" in searched and "archive-only-first" in searched and "ENTRY-001" in searched
        assert searched.count('class="journal-entry"') == 1
        assert b"No journal entries match this search" in get(base, "/journal?q=absent-phrase")[2]
        assert get(base, "/journal?page=0")[0] == 400
        assert get(base, "/journal?page=oops")[0] == 400
        assert get(base, "/journal?page=4")[0] == 404
        assert get(base, "/journal?entry=missing")[0] == 404
        assert get(base, "/journal?q=" + "x" * 201)[0] == 400
    assert path.read_bytes() == before
    journal.write("## Newer observation\n\nAdded after the bookmark was made.")
    with serve(campaign.config.config_path) as base:
        bookmarked = get(base, "/journal?entry=" + oldest.key)[2].decode()
        assert f'id="entry-{oldest.key}" data-key="journal-{oldest.key}" open' in bookmarked


def test_long_journal_entry_has_explicit_preview_and_detail_bounds(campaign):
    journal = ResearchJournal(campaign.config)
    journal.write("## A long research note\n\n" + "line\n" * (DOCUMENT_LIMIT // 5 + 100))
    key = index_journal(campaign.state.root / "journal.md").entries[0].key
    with serve(campaign.config.config_path) as base:
        overview = get(base, "/journal")[2]
        assert b"16 KiB" in overview and b"Open this entry for a larger excerpt" in overview
        assert len(overview) < 60000
        detail = get(base, "/journal?entry=" + key)[2]
        assert b"128 KiB" in detail and b"full entry remains" in detail
        assert len(detail) < DOCUMENT_LIMIT * 3


def test_journal_long_headlines_and_empty_timestamped_entries(campaign):
    journal = ResearchJournal(campaign.config)
    journal.write("## " + "x" * 30000 + " " * 30000 + "last word ###\n\nRecorded text.")
    journal.write("")
    entries = index_journal(campaign.state.root / "journal.md").entries
    assert len(entries) == 2
    assert entries[0].title == "x" * 160
    assert entries[1].title == "Journal notes"
    with serve(campaign.config.config_path) as base:
        assert b"No entry text recorded yet" in get(base, "/journal")[2]


def test_journal_concurrent_change_returns_an_explicit_error(campaign, monkeypatch):
    import xgenius.dashboard as dashboard

    journal = ResearchJournal(campaign.config)
    journal.write("## Consistent snapshot\n\nOriginal content.")
    read = dashboard._read_text
    changed = False

    def concurrent_read(path, *args, **kwargs):
        nonlocal changed
        result = read(path, *args, **kwargs)
        if not changed:
            changed = True
            journal.write("## Concurrent update\n\nNew evidence.")
        return result

    monkeypatch.setattr(dashboard, "_read_text", concurrent_read)
    with serve(campaign.config.config_path) as base:
        status, _, body = get(base, "/journal")
        assert status == 503 and b"journal changed" in body
        assert b"class=\"journal-entry\"" not in body
        status, _, body = get(base, "/journal")
        assert status == 200 and b"Concurrent update" in body


def test_missing_records_and_unknown_routes_return_errors(campaign):
    with serve(campaign.config.config_path) as base:
        for path in ("/unknown", "/job", "/turn?id=nope", "/hypothesis?id=nope", "/static/../config.py"):
            assert get(base, path)[0] == 404, path
        request = urllib.request.Request(base + "/stop", method="POST", data=b"")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == 404
        request = urllib.request.Request(base, headers={"Host": "untrusted.example"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == 403


def test_database_failure_is_visible_not_empty_success(campaign):
    Path(campaign.state.path).unlink()
    with serve(campaign.config.config_path) as base:
        status, _, body = get(base)
        assert status == 503 and b"Dashboard data unavailable" in body
    assert not Path(campaign.state.path).exists()


def test_legacy_dashboard_and_multiple_servers(tmp_path, campaign):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    path = legacy / "xgenius.toml"
    path.write_text('[project]\nname = "Legacy <safe>"\n', encoding="utf-8")
    config = load_config(str(path))
    db = XGeniusDB(config)
    db.record_job("cluster:42", "cluster", "legacy-experiment", "", "python train.py")
    with serve(path) as old, serve(campaign.config.config_path) as local:
        for endpoint in ("/", "/jobs", "/hypotheses", "/journal", "/debug", "/goal"):
            assert get(old, endpoint)[0] == 200
        body = get(old)[2]
        assert b"Legacy &lt;safe&gt;" in body and b"SLURM research" in body
        assert b'href="/resources"' not in body and b"legacy-experiment" in body
        assert b"legacy-experiment" not in get(local)[2]
        assert get(old, "/resources")[0] == 404
        assert get(old, "/job?id=cluster%3A42")[0] == 200


@pytest.mark.skipif(os.environ.get("XGENIUS_BROWSER_TESTS") != "1",
                    reason="Opt in with XGENIUS_BROWSER_TESTS=1 and install Playwright + Chromium")
def test_browser_navigation_refresh_markdown_and_mobile(campaign, tmp_path):
    from playwright.sync_api import sync_playwright, expect

    campaign.state.db.add_hypothesis("h001", "Relational controls improve arrangement quality")
    for index, status in enumerate(("completed", "running", "failed", "queued", "completed", "cancelled")):
        attempt = job(campaign, key=f"method-{index:02d}", hypothesis_id="h001")
        if status != "queued":
            campaign.state.transition(attempt, status, reason="Missing preview; inspect validator" if index == 2 else "",
                                      receipt={"returncode": 1 if index == 2 else 0, "elapsed": 91})
        if index == 0:
            spec = json.loads(campaign.state.attempt(attempt)["spec"])
            (Path(spec["output"]) / "metrics.json").write_text('{"quality":0.78,"coverage":0.94}', encoding="utf-8")
            collect_artifacts(campaign.state, spec)
    journal = campaign.state.root / "journal.md"
    journal.write_text(
        "# Research journal\n\n## First controlled comparison\n\n"
        "The **relational baseline** improves coverage while preserving the full inventory.\n\n"
        "Hypothesis `h001` asks whether relational controls improve coverage without extra violations.\n\n"
        "| Method | Quality | Coverage |\n| --- | --- | --- |\n| Baseline | 0.63 | 0.87 |\n"
        "| Relational | 0.78 | 0.94 |\n\n"
        "### Next experiments\n\n- Replicate on independent groups\n- Inspect adverse cases\n\n"
        "> A successful process is not evidence of scientific validity.\n\n"
        "```python\nmetrics = {'quality': 0.78, 'coverage': 0.94}\n```\n\n"
        "<script>window.dashboardInjected = true</script>\n", encoding="utf-8")
    with _connect(campaign.state.path) as db:
        db.execute("UPDATE campaign SET state='waiting',started=?,reason=?",
                   (time.time() - 840, "Comparing controls while the next experiment runs."))
        artifact = db.execute("SELECT id FROM artifacts LIMIT 1").fetchone()[0]
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    with serve(campaign.config.config_path) as base, sync_playwright() as playwright:
        executable = os.environ.get("XGENIUS_BROWSER_EXECUTABLE")
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1080}, device_scale_factor=1)
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.clock.install()
            page.goto(base)
            expect(page.locator(".campaign-banner")).to_contain_text("Comparing controls")
            assert page.locator("meter").count() >= 4
            page.screenshot(path=str(screenshots / "overview-desktop.png"), full_page=True)
            page.get_by_label("Auto-refresh (15s)").check()
            campaign.state.set_campaign("waiting", "Fresh evidence available")
            page.clock.fast_forward(15001)
            expect(page.locator(".campaign-banner")).to_contain_text("Fresh evidence available")
            page.get_by_role("navigation", name="Main navigation").get_by_role("link", name="Journal", exact=True).click()
            expect(page.locator("article.markdown h1")).to_have_text("Research journal")
            expect(page.locator("article.markdown table")).to_be_visible()
            expect(page.locator("article.markdown pre code")).to_be_visible()
            assert page.evaluate("window.dashboardInjected") is None
            page.screenshot(path=str(screenshots / "journal-desktop.png"), full_page=True)
            with journal.open("a", encoding="utf-8") as stream:
                stream.write("\n## New measurement\n\nRefresh makes this visible.\n")
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("article.markdown")).to_contain_text("New measurement")
            page.route(base + "/journal", lambda route: route.fulfill(status=503, body="Unavailable"))
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#refresh-status")).to_contain_text("Refresh failed: HTTP 503")
            expect(page.locator("article.markdown")).to_contain_text("New measurement")
            page.unroute(base + "/journal")
            page.goto(base + "/hypotheses")
            page.get_by_role("link", name="Relational controls improve arrangement quality", exact=True).click()
            expect(page.locator("main")).to_contain_text("Hypothesis statement")
            expect(page.locator("main")).to_contain_text("whether relational controls improve coverage")
            page.screenshot(path=str(screenshots / "hypothesis-desktop.png"), full_page=True)
            page.goto(base + "/jobs")
            page.get_by_label("Search experiments").fill("method-03")
            page.get_by_role("button", name="Filter", exact=True).click()
            expect(page.locator("tbody tr")).to_have_count(1)
            page.locator("tbody a").first.click()
            expect(page.locator(".detail-heading")).to_contain_text("method-03")
            page.goto(base + "/artifacts")
            with page.expect_download() as download:
                page.locator(f'a[href="/artifact?id={artifact}"]').click()
            assert Path(download.value.path()).read_text() == '{"quality":0.78,"coverage":0.94}'
            for route in ("/", "/journal", "/jobs", "/resources", "/activity", "/hypotheses", "/hypothesis?id=h001"):
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(base + route)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), route
                if route == "/":
                    page.screenshot(path=str(screenshots / "overview-mobile.png"), full_page=True)
            assert not errors, errors
            assert all(url.startswith(base) for url in requests), requests
            print(f"Dashboard screenshots: {screenshots}")
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("XGENIUS_BROWSER_TESTS") != "1", reason="Opt in to prepared Playwright/Chromium")
def test_browser_journal_folding_search_and_navigation(campaign, tmp_path):
    from playwright.sync_api import sync_playwright, expect

    journal = ResearchJournal(campaign.config)
    for index in range(JOURNAL_PAGE_SIZE + 3):
        journal.write(f"## Comparison {index + 1:02d}\n\n"
                      + ("Distinctive early decision." if index == 0 else "A **measured** comparison, not an assumed result.")
                      + "\n\n### Next step\n\n- Repeat the control\n- Inspect adverse cases\n")
    with serve(campaign.config.config_path) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("XGENIUS_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1080})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/journal")
            expect(page.locator(".journal-entry")).to_have_count(JOURNAL_PAGE_SIZE)
            expect(page.locator(".journal-entry[open]")).to_have_count(1)
            expect(page.locator(".journal-entry").first.locator("summary").first).to_contain_text("Comparison 23")
            page.locator(".journal-entry").nth(1).locator("summary").first.click()
            expect(page.locator(".journal-entry[open]")).to_have_count(2)
            page.get_by_role("button", name="Collapse page").click()
            expect(page.locator(".journal-entry[open]")).to_have_count(0)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
            expect(page.locator(".journal-entry[open]")).to_have_count(0)
            page.get_by_role("button", name="Expand page").click()
            expect(page.locator(".journal-entry[open]")).to_have_count(JOURNAL_PAGE_SIZE)
            reading_id = page.locator(".journal-entry").nth(2).get_attribute("id")
            page.evaluate("(id) => window.scrollTo(0, document.getElementById(id).offsetTop + 100)", reading_id)
            before_y = page.locator("#" + reading_id).bounding_box()["y"]
            journal.write("## Comparison 24\n\nAn appended observation must preserve the visible reading position.")
            page.locator("#refresh").evaluate("button => button.click()")
            expect(page.locator(".journal-entry-title").first).to_have_text("Comparison 24")
            assert abs(page.locator("#" + reading_id).bounding_box()["y"] - before_y) <= 2
            page.get_by_role("button", name="Collapse page").click()
            page.locator(".journal-entry").first.locator("summary").first.click()
            page.screenshot(path=str(tmp_path / "journal-reader-desktop.png"), full_page=True)
            page.get_by_role("link", name="Older entry", exact=True).first.click()
            expect(page.locator(".journal-entry[open] .journal-entry-title")).to_have_text("Comparison 23")
            focused_url = page.url
            journal.write("## Comparison 25\n\nA new result must not steal the reader's place.")
            page.reload()
            assert page.url == focused_url
            expect(page.locator(".journal-entry[open] .journal-entry-title")).to_have_text("Comparison 23")
            page.get_by_label("Search journal", exact=True).fill("Distinctive early decision")
            page.get_by_role("button", name="Search", exact=True).click()
            expect(page.locator(".journal-entry")).to_have_count(1)
            expect(page.locator(".journal-entry-title")).to_have_text("Comparison 01")
            expect(page.locator(".journal-entry article")).to_contain_text("Distinctive early decision")
            page.get_by_role("link", name="Jump to latest", exact=True).click()
            expect(page.locator(".journal-entry[open] .journal-entry-title")).to_have_text("Comparison 25")
            page.get_by_role("link", name="Older entries", exact=True).first.click()
            expect(page.locator(".journal-entry")).to_have_count(5)
            page.get_by_role("link", name="Newer entries", exact=True).first.click()
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator("#chat-panel")).to_be_visible()
            page.reload()
            expect(page.locator("#chat-panel")).to_be_visible()
            page.get_by_role("button", name="Close chat").click()
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="Collapse page").click()
            page.locator(".journal-entry").first.locator("summary").first.click()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / "journal-reader-mobile.png"), full_page=True)
            assert not errors
        finally:
            browser.close()
