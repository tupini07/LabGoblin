"""Read-only HTTP/browser coverage over retained schema-3 campaign records."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
import tomli_w

from tests.test_controller import fixture
from tests.test_workspace import request
from xgenius import journal, reporting, workspace
from xgenius.dashboard import DashboardServer, JOURNAL_PAGE_SIZE, LOG_LIMIT, _markdown, _read_text


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
def campaign(tmp_path):
    config, state, ledger, raw = fixture(tmp_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    return SimpleNamespace(config=config, state=state, ledger=ledger, project=config.root)


def job(campaign, **overrides):
    return workspace.submit(campaign.state, campaign.config, request(**overrides))["id"]


def complete_job(campaign, attempt_id, metrics=None):
    spec = json.loads(campaign.state.attempt(attempt_id)["spec"])
    Path(spec["output"], "metrics.json").write_text(json.dumps(metrics or {"score": 42}), encoding="utf-8")
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE attempts SET status='completed',started=1,ended=2,elapsed=1,exit_code=0 WHERE id=?", (attempt_id,))
    return workspace.collect_artifacts(campaign.state, attempt_id)


def notes(campaign, text):
    return campaign.state.source("journal_import", text.encode("utf-8"), origin="operator")


def dump(state):
    with state.db.read() as conn:
        return list(conn.iterdump())


def test_markdown_formats_and_rejects_active_content():
    result = _markdown('# Journal\n\n**Measured** and `code`.\n\n'
                       '| Method | Score |\n| --- | --- |\n| baseline | 0.4 |\n\n'
                       '```python\nprint("<script>")\n```\n\n<script>alert(1)</script>\n\n'
                       '[unsafe](javascript:alert(1))\n\n![remote](https://untrusted.example/x.png)')
    assert '<h1>Journal</h1>' in result and '<strong>Measured</strong>' in result
    assert '<table>' in result and '<code class="language-python">' in result
    assert '<script>' not in result and '<img' not in result and 'href="javascript:' not in result


def test_log_tail_preserves_long_single_lines(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(b"prefix-" * LOG_LIMIT + b"TAIL\n")
    text, truncated = _read_text(path, LOG_LIMIT, tail=True)
    assert truncated and text.endswith("TAIL\n") and len(text) == LOG_LIMIT


def test_all_empty_routes_local_assets_and_broken_current_config_are_read_only(campaign):
    before = dump(campaign.state)
    with serve(campaign.config.config_path) as base:
        for path in ("/", "/jobs", "/hypotheses", "/activity", "/artifacts", "/reports", "/resources", "/journal", "/goal", "/debug"):
            status, headers, body = get(base, path)
            assert status == 200, (path, body.decode())
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
            assert headers["Cache-Control"] == "no-store" and b"Read-only dashboard" in body
        for name in ("dashboard.css", "dashboard.js", "dashboard-chat.js"):
            assert get(base, "/static/" + name)[0] == 200
        assert b"No agent turns yet" in get(base)[2]
        assert b"The research journal is empty" in get(base, "/journal")[2]
    Path(campaign.config.config_path).write_text("broken TOML [", encoding="utf-8")
    with serve(campaign.config.config_path) as base:
        assert get(base)[0] == 200 and b"Current configuration unavailable" in get(base)[2]
        assert not json.loads(get(base, "/chat/status")[2])["enabled"]
    assert dump(campaign.state) == before


def test_exact_hypotheses_sources_and_markdown_survive_current_edits(campaign):
    campaign.state.hypothesis("h001", "Adding **constraints** improves coverage.")
    original = notes(campaign, "# Evidence\n\n**Result:** caf\u00e9 \u03b1.\n\nh001 had a negative finding.\n\n- Measured\n")
    old_goal = campaign.state.source("goal", b"Historical stopping criterion", origin="operator", head="goal")
    campaign.state.source("goal", b"New scope", origin="operator", head="goal")
    before = dump(campaign.state)
    with serve(campaign.config.config_path) as base:
        detail = get(base, "/hypothesis?id=h001")[2].decode()
        assert "<strong>constraints</strong>" in detail and "negative finding" in detail
        assert "<strong>Result:</strong>" in get(base, "/journal?entry=" + original)[2].decode()
        assert b"Historical stopping criterion" in get(base, "/journal?entry=" + old_goal)[2]
        assert b"New scope" in get(base, "/goal")[2]
    assert dump(campaign.state) == before


def test_captured_download_serves_historical_bytes_not_modified_outputs(campaign):
    aid = job(campaign)
    collection = complete_job(campaign, aid)
    oid = collection["observation_ids"][0]
    spec = json.loads(campaign.state.attempt(aid)["spec"])
    Path(spec["output"], "metrics.json").write_text('{"score":999}', encoding="utf-8")
    with serve(campaign.config.config_path) as base:
        assert get(base, "/job?id=" + aid)[0] == 200
        assert b"score" in get(base, "/observation?id=" + oid)[2]
        code, headers, body = get(base, "/artifact?id=" + oid)
        assert code == 200 and json.loads(body)["score"] == 42
        assert headers["Content-Disposition"] == "attachment" and headers["X-Content-SHA256"]
        assert get(base, "/artifact?id=missing")[0] == 404
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE observations SET assurance='unmanaged' WHERE id=?", (oid,))
    with serve(campaign.config.config_path) as base:
        assert get(base, "/artifact?id=" + oid)[0] == 409


def test_filters_pagination_and_escaped_user_content(campaign):
    for index in range(53):
        job(campaign, key=f"experiment-{index:03d}", experiment_id=f"experiment-{index:03d}")
    job(campaign, key="markup", experiment_id="<script>alert(1)</script>")
    with serve(campaign.config.config_path) as base:
        first = get(base, "/jobs")[2].decode()
        assert "54 records" in first and "Page 1 of 2" in first
        assert first.count('href="/job?id=') == 50
        assert get(base, "/jobs?page=2")[2].count(b'href="/job?id=') == 4
        assert b"experiment-052" in get(base, "/jobs?q=experiment-052")[2]
        assert "<script>alert(1)</script>" not in first and "&lt;script&gt;" in first
        assert get(base, "/jobs?page=0")[0] == 400
        assert get(base, "/jobs?page=oops")[0] == 400
        assert get(base, "/jobs?page=3")[0] == 404
        assert b"0 records" in get(base, "/jobs?status=%27%20OR%201%3D1")[2]


def test_journal_pagination_search_and_revision_links_survive_append(campaign):
    ids = [notes(campaign, f"## Comparison {i:02d}\n\nControlled result {i}.") for i in range(23)]
    before = dump(campaign.state)
    with serve(campaign.config.config_path) as base:
        first = get(base, "/journal")[2]
        assert first.count(b'class="journal-entry"') == JOURNAL_PAGE_SIZE and b"Comparison 22" in first
        assert get(base, "/journal?page=2")[2].count(b'class="journal-entry"') == 3
        assert b"Controlled result 0" in get(base, "/journal?q=Controlled%20result%200")[2]
        assert b"not evidence that the archive has no" in get(base, "/journal?q=absent")[2]
        assert b"Comparison 00" in get(base, "/journal?entry=" + ids[0])[2]
    assert dump(campaign.state) == before
    notes(campaign, "## Later entry")
    with serve(campaign.config.config_path) as base:
        assert b"Comparison 00" in get(base, "/journal?entry=" + ids[0])[2]
        assert get(base, "/journal?entry=gone")[0] == 404


def test_large_utf8_source_is_bounded_and_byte_page_is_explicit(campaign):
    sid = notes(campaign, "## Long entry\n\n" + "\u03b1" * (150 * 1024))
    with serve(campaign.config.config_path) as base:
        body = get(base, "/journal?entry=" + sid)[2]
        assert b"Bounded byte-page preview" in body and b"Next byte page" in body
        assert len(body) < 65536
        assert get(base, "/journal?entry=" + sid + "&byte_offset=16384")[0] == 200


def test_immutable_report_view_and_missing_ledger_do_not_create_state(campaign):
    aid = job(campaign)
    complete_job(campaign, aid)
    view = reporting.seal_view(campaign.state)
    reporting.publish_report(campaign.state, view["id"])
    before = dump(campaign.state)
    with serve(campaign.config.config_path) as base:
        assert get(base, "/reports")[0] == 200
        assert b"baseline" in get(base, "/view?id=" + view["id"])[2]
        assert get(base, "/resources")[0] == 200
    campaign.ledger.path.unlink()
    with serve(campaign.config.config_path) as base:
        assert b"Recorded ledger is missing" in get(base, "/resources")[2]
        assert get(base, "/job?id=missing")[0] == 404
        assert get(base, "/hypothesis?id=missing")[0] == 404
        assert get(base, "/turn?id=missing")[0] == 404
        assert get(base, "/no-such-route")[0] == 404
    assert not campaign.ledger.path.exists() and dump(campaign.state) == before


def test_host_and_loopback_boundary(campaign):
    with pytest.raises(ValueError, match="loopback"):
        DashboardServer(("0.0.0.0", 0), campaign.config.config_path)
    with serve(campaign.config.config_path) as base:
        request = urllib.request.Request(base, headers={"Host": "untrusted.example"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403


@pytest.mark.skipif(os.environ.get("XGENIUS_BROWSER_TESTS") != "1", reason="Opt in to prepared Playwright/Chromium")
def test_browser_navigation_journal_refresh_and_mobile(campaign, tmp_path):
    from playwright.sync_api import sync_playwright, expect
    campaign.state.hypothesis("h001", "Controls improve coverage")
    for i in range(23):
        notes(campaign, f"## Comparison {i:02d}\n\nA **measured** h001 comparison.\n\n"
                        "| Score | Meaning |\n| --- | --- |\n| 42 | Synthetic |\n\n<script>window.injected=true</script>")
    before = dump(campaign.state)
    with serve(campaign.config.config_path) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("XGENIUS_BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, requests = [], []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: requests.append(r.url))
            page.goto(base + "/journal")
            expect(page.locator(".journal-entry")).to_have_count(20)
            expect(page.locator(".journal-entry[open]")).to_have_count(1)
            expect(page.locator(".journal-entry[open] table")).to_be_visible()
            assert page.evaluate("window.injected") is None
            page.get_by_role("button", name="Collapse page").click()
            expect(page.locator(".journal-entry[open]")).to_have_count(0)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
            expect(page.locator(".journal-entry[open]")).to_have_count(0)
            page.get_by_role("button", name="Expand page").click()
            expect(page.locator(".journal-entry[open]")).to_have_count(20)
            page.get_by_role("link", name="Next", exact=True).first.click()
            expect(page.locator(".journal-entry")).to_have_count(3)
            page.get_by_role("link", name="Link to this entry", exact=True).first.click()
            expect(page.locator(".journal-entry")).to_have_count(1)
            page.reload()
            expect(page.locator(".journal-entry[open]")).to_have_count(1)
            page.get_by_role("link", name="Older entry", exact=True).click()
            expect(page.locator(".journal-entry[open]")).to_have_count(1)
            page.get_by_label("Search journal", exact=True).fill("Comparison 00")
            page.get_by_role("button", name="Search", exact=True).click()
            expect(page.locator("main")).to_contain_text("Comparison 00")
            page.get_by_role("link", name="Exact retained entry").click()
            expect(page.locator(".journal-entry-title")).to_have_text("Comparison 00")
            page.screenshot(path=str(tmp_path / "journal-desktop.png"), full_page=True)
            for route in ("/", "/journal", "/jobs", "/resources", "/activity", "/hypotheses", "/hypothesis?id=h001"):
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(base + route)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), route
            page.screenshot(path=str(tmp_path / "hypothesis-mobile.png"), full_page=True)
            assert not errors and all(url.startswith(base) for url in requests)
        finally:
            browser.close()
    assert dump(campaign.state) == before
