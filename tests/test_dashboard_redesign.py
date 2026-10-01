"""Owner journeys over synthetic records, without research or observer inference."""

from dataclasses import asdict
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import time
import urllib.parse
import uuid

import pytest

from labgoblin.paths import environment_value

from tests.test_dashboard import campaign, complete_job, dump, get, job, notes, serve
from tests.test_dashboard_chat import FakeObserver, finished, post, service, token
from labgoblin import reporting
from labgoblin.dashboard_data import observation_context, question_context, report_text, snapshot
from labgoblin.protocol import EvidenceDisposition, Handoff, Resources, canonical


class Document(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.attrs = []
        self.text = []
        self.feed(body.decode())

    def handle_starttag(self, tag, attrs):
        self.attrs.append((tag, dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)

    def snapshot(self):
        return json.loads(next(attrs["data-snapshot"] for tag, attrs in self.attrs if attrs.get("id") == "main"))

    def context(self):
        return json.loads(next(attrs["data-chat-context"] for tag, attrs in self.attrs if attrs.get("id") == "main"))


def handoff(campaign, *, references=(), summary="The early speedup is not established."):
    tid = uuid.uuid4().hex
    aid = references[0][0] if references else None
    evidence = ()
    if aid:
        event = campaign.state.attempt(aid)["collection_event"]
        evidence = (EvidenceDisposition(event, "excluded", "Unequal warm-up invalidates this comparison.",
                                        tuple(oid for _, oid in references)),)
    value = Handoff(tid, uuid.uuid4().hex, summary,
                    "An adverse seed contradicted early promise; two comparisons used unequal warm-up.",
                    "Run three paired-seed replications with equal warm-up.", "wait", "Paired evidence is outstanding",
                    evidence=evidence)
    encoded = canonical(asdict(value))
    source = campaign.state.source("handoff", encoded, origin="researcher")
    with campaign.state.db.write() as conn:
        conn.execute("""INSERT INTO turns(id,generation,kind,state,created,ended,result,revision)
            VALUES(?,1,'research','accepted',?,?,?,0)""", (tid, time.time(), time.time(), encoded.decode()))
        conn.execute("INSERT INTO handoffs(turn_id,content,digest,source_id,created) VALUES(?,?,?,?,?)",
                     (tid, encoded.decode(), hashlib.sha256(encoded).hexdigest(), source, time.time()))
        conn.execute("""INSERT INTO source_heads(name,source_id,revision) VALUES('rationale',?,1)
            ON CONFLICT(name) DO UPDATE SET source_id=excluded.source_id,revision=revision+1""", (source,))
        for item in evidence:
            conn.execute("INSERT INTO dispositions VALUES(?,?,?,?,?,?)",
                         (tid, item.event_id, item.disposition, item.reason, "", json.dumps(item.references)))
        conn.execute("UPDATE campaign SET progress='wait',reason=?", (value.reason,))
    return source


def study(campaign):
    campaign.state.source("goal", b"Reduce p95 latency by at least 15% versus LRU across five held-out seeds; memory <=512 MiB.",
                          origin="operator", head="goal")
    campaign.state.source("protocol", b"Compare equal warm-up. Never pair observations just by experiment names.",
                          origin="operator", head="protocol")
    campaign.state.hypothesis("adaptive", "Adaptive admission improves latency without exceeding the memory cap.")
    attempts, observations = [], []
    for index, score in enumerate((8.3, 9.5, 11.4, 9.3)):
        aid = job(campaign, key=f"seed-{index}", experiment_id=f"policy-seed-{index}")
        observations.append(complete_job(campaign, aid, {"p95_latency_ms": score, "peak_memory_mb": 479})["observation_ids"][0])
        attempts.append(aid)
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE attempts SET validation='invalid',reason='Unequal warm-up' WHERE id=?", (attempts[-1],))
        conn.execute("UPDATE attempts SET hypothesis_id='adaptive' WHERE id=?", (attempts[-1],))
    source = handoff(campaign, references=[(attempts[-1], observations[-1])])
    return attempts, observations, source


def test_brief_four_destinations_and_reference_chain_without_inference(campaign):
    attempts, observations, source = study(campaign)
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        body = get(base)[2]
        text = "".join(Document(body).text)
        assert "The early speedup is not established." in text
        assert "five held-out seeds" in text and "equal warm-up" in text
        assert "Recorded researcher interpretation" in text and "not a live process probe" in text.lower()
        assert b"unacknowledged events" not in body
        assert b'href="/journal?entry=' + source.encode() in body
        for path, label in (("/", "Brief"), ("/evidence", "Evidence"), ("/work", "Work"), ("/history", "History")):
            assert f">{label}</a>".encode() in body
            assert get(base, path)[0] == 200
        hypothesis = get(base, "/hypothesis?id=adaptive")[2]
        assert b"Exact recorded assessment" in hypothesis
        assert source.encode() in hypothesis
        source_body = get(base, "/journal?entry=" + source)[2]
        assert observations[-1].encode() in source_body
    assert dump(campaign.state) == before


@pytest.mark.parametrize("status", ["active", "attention", "invalid", "", "cancelled", "' OR 1=1"])
def test_count_and_filter_share_scope_with_invalid_and_cancelled_attempts(campaign, status):
    from labgoblin.dashboard_data import attempt_filter

    attempts, _, _ = study(campaign)
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE attempts SET status='cancelled',collection='failed',validation='invalid' WHERE id=?", (attempts[0],))
        conn.execute("UPDATE attempts SET status='recovery_required' WHERE id=?", (attempts[1],))
        condition, args = attempt_filter(status)
        expected = conn.execute(f"SELECT COUNT(*) FROM attempts a WHERE {condition}", args).fetchone()[0]
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        page = get(base, "/jobs?" + urllib.parse.urlencode({"status": status}))[2]
        assert f"{expected} records".encode() in page
        assert page.count(b'href="/job?id=') == expected
        if status in ("active", "attention", ""):
            work = get(base, "/work")[2].decode()
            target = "/jobs" + ("?status=" + status if status else "")
            assert f'href="{target}"><span>' in work
            tail = work.split(f'href="{target}"><span>', 1)[1]
            assert f"<strong>{expected}</strong>" in tail.split("</a>", 1)[0]
    assert dump(campaign.state) == before


def test_observation_keeps_invalidity_and_scoped_history_including_observer(campaign):
    attempts, observations, _ = study(campaign)
    view = reporting.seal_view(campaign.state)
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE attempts SET validation='valid',reason='Later current context' WHERE id=?", (attempts[-1],))
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        old = get(base, f"/observation?id={observations[-1]}&view={view['id']}")[2]
        assert b"Unequal warm-up" in old and b"Historical source scope" in old
        assert b"9.3" in old and b"scientific acceptance" in old
        current = get(base, "/observation?id=" + observations[-1])[2]
        assert b"current recorded attempt context" in current
        member = get(base, f"/view?id={view['id']}&attempt={attempts[-1]}")[2]
        assert b"Unequal warm-up" in member and b"Switch to current experiment state" in member
        assert f"view={view['id']}".encode() in member
    from labgoblin.dashboard_data import EvidenceReader
    reader = EvidenceReader(campaign.config.config_path)
    data = reader.read("evidence_observation", {"id": observations[-1], "view_id": view["id"]})
    assert data["data"]["validation"] == "invalid"
    assert data["data"]["reason"] == "Unequal warm-up"
    assert "view=" in data["sources"][0]["url"]
    assert dump(campaign.state) == before
    other = job(campaign, key="outside-view")
    oid = complete_job(campaign, other)["observation_ids"][0]
    with campaign.state.db.read() as conn, pytest.raises(ValueError, match="membership"):
        observation_context(conn, oid, view_id=view["id"])


def test_large_historical_context_is_stream_verified_and_corruption_is_not_hidden(campaign):
    attempts, observations, _ = study(campaign)
    view = reporting.seal_view(campaign.state)
    with campaign.state.db.write() as conn:
        row = conn.execute("SELECT outcome FROM view_members WHERE view_id=? AND attempt_id=?",
                           (view["id"], attempts[-1])).fetchone()
        value = json.loads(row[0])
        value["fixture_padding"] = "X" * 300000
        body = canonical(value)
        conn.execute("UPDATE view_members SET outcome=?,digest=? WHERE view_id=? AND attempt_id=?",
                     (body.decode(), hashlib.sha256(body).hexdigest(), view["id"], attempts[-1]))
    with campaign.state.db.read() as conn:
        value = observation_context(conn, observations[-1], view_id=view["id"])
        assert value["validation"] == "invalid" and len(json.dumps(value)) < 4096
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE view_members SET outcome=replace(outcome,'XXXX','YYYY') WHERE view_id=?", (view["id"],))
    with campaign.state.db.read() as conn, pytest.raises(ValueError, match="integrity failed"):
        observation_context(conn, observations[-1], view_id=view["id"])


def test_raw_comparison_does_not_fabricate_pairing_units_or_aggregation(campaign):
    _, observations, _ = study(campaign)
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        path = "/compare?" + urllib.parse.urlencode({"id": observations}, doseq=True)
        body = get(base, path)[2]
        for value in (b"8.3", b"9.5", b"11.4", b"9.3", b"Unequal warm-up"):
            assert value in body
        assert b"Not comparable as a paired scientific estimate" in body
        assert b"no unit conversion applied" in body
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(Document(body).context()["url"]).query)["id"] == observations
        assert get(base, path + "&id=fifth")[0] == 400
        assert b"Select observations to compare" in get(base, "/compare")[2]
    assert dump(campaign.state) == before


def test_recovery_named_guidance_and_closed_historical_next_steps(campaign):
    attempts, _, _ = study(campaign)
    campaign.state.blocker("lost-owner", "ownership", "Missing terminal receipt; backend liveness is unknown.", work_id=attempts[0])
    with serve(campaign.config.config_path, chat=False) as base:
        brief = get(base)[2]
        assert brief.index(b"unresolved recovery blockers") < brief.index(b"The research question")
        assert b"policy-seed-0" in brief and b"Do not duplicate" in brief
        recovery = get(base, "/debug")[2]
        assert b"Copy read-only inspection commands" in recovery
        assert b"status --project" in recovery and b"--stage supervisor" in recovery
        assert b"Responsible actor not recorded" in recovery
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE generations SET state='closed',outcome='unassessed',reason='No final assessment available',sealed=1")
        conn.execute("UPDATE campaign SET progress='closed'")
        conn.execute("UPDATE blockers SET resolved=1")
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        brief = get(base)[2]
        assert b"Unassessed" in brief and b"No assessed demonstration" in brief
        assert b"Earlier next-step instructions are historical" in brief
        assert b"Run three paired-seed replications" not in brief
    assert dump(campaign.state) == before


def test_report_reader_verifies_same_bytes_and_preserves_source_scope(campaign):
    study(campaign)
    view = reporting.seal_view(campaign.state)
    report = reporting.publish_report(campaign.state, view["id"])
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        assert b"Open retained report" in get(base, "/reports")[2]
        body = get(base, "/report?id=" + report["id"])[2]
        assert b"Readable retained report" in body and b"Deterministic retained inventory" in body
        assert b"Historical source scope" in body and b"not a closure assessment" in body
        assert get(base, "/report?id=missing")[0] == 404
        file = Path(report["outputs"]["markdown"]["path"])
        original = file.read_bytes()
        file.write_bytes(original.replace(b"Research inventory", b"Research invent0ry"))
        changed = get(base, "/report?id=" + report["id"])[2]
        assert b"Retained report unavailable" in changed and b"integrity failed" in changed
        assert b"invent0ry" not in changed
    assert dump(campaign.state) == before


@pytest.mark.parametrize("kind", ["missing", "escape", "markup", "large"])
def test_report_output_absence_containment_safety_and_bounded_paging(campaign, kind):
    view = reporting.seal_view(campaign.state)
    report = reporting.publish_report(campaign.state, view["id"])
    output = report["outputs"]["markdown"]
    path = Path(output["path"])
    if kind == "missing":
        path.unlink()
    elif kind == "escape":
        outside = campaign.project / "not-a-report.md"
        outside.write_text("PRIVATE OUTSIDE FILE", encoding="utf-8")
        output["path"] = str(outside)
    else:
        body = (b'<script>window.injected=true</script>\n\n![remote](https://example.invalid/pixel)\n\n'
                b'[bad](javascript:alert(1))\n\n')
        if kind == "large":
            body += b"A" * (150 * 1024)
        path.write_bytes(body)
        output.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE reports SET outputs=? WHERE id=?", (json.dumps(report["outputs"]), report["id"]))
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        body = get(base, "/report?id=" + report["id"])[2]
        if kind in ("missing", "escape"):
            assert b"Retained report unavailable" in body
            assert b"PRIVATE OUTSIDE FILE" not in body
        else:
            assert b"<script>window.injected" not in body and b"<img" not in body
            assert b'href="javascript:' not in body
        if kind == "large":
            assert b"Next report byte page" in body and len(body) < 160000
            assert get(base, "/report?id=" + report["id"] + "&byte_offset=131072")[0] == 200
            value = report_text(campaign.state, report["id"], offset=131072)
            assert value["has_more"] is False and value["returned_bytes"] < 32768
    assert dump(campaign.state) == before


def test_change_interval_is_sequence_scoped_and_rejects_wrong_checkpoint(campaign):
    first = notes(campaign, "First revision")
    with campaign.state.db.read() as conn:
        prior = snapshot(campaign.state, conn)
    later = notes(campaign, "Late arrival with an old authoring timestamp")
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE sources SET created=1 WHERE id=?", (later,))
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        frame = Document(get(base)[2]).snapshot()
        query = {"after_source": prior["source_cutoff"], "after_event": prior["event_cutoff"],
                 "source_cutoff": frame["source_cutoff"], "event_cutoff": frame["event_cutoff"],
                 "campaign": campaign.state.id, "generation": 1}
        body = get(base, "/changes?" + urllib.parse.urlencode(query))[2]
        assert later.encode() in body and first.encode() not in body
        for change in ({"campaign": "other"}, {"generation": 2}, {"source_cutoff": frame["source_cutoff"] + 1}):
            assert get(base, "/changes?" + urllib.parse.urlencode({**query, **change}))[0] == 409
    assert dump(campaign.state) == before


def test_snapshot_shares_one_read_transaction_during_concurrent_change(campaign):
    with campaign.state.db.read() as conn:
        original = snapshot(campaign.state, conn)
        notes(campaign, "Concurrent new source")
        pinned = snapshot(campaign.state, conn)
        assert pinned["source_cutoff"] == original["source_cutoff"]
        assert pinned["campaign"]["revision"] == original["campaign"]["revision"]
    with campaign.state.db.read() as conn:
        assert snapshot(campaign.state, conn)["source_cutoff"] > original["source_cutoff"]


def test_update_token_catches_control_recovery_and_work_without_clock_noise(campaign):
    def read():
        with campaign.state.db.read() as conn:
            return snapshot(campaign.state, conn)
    first, second = read(), read()
    assert first["read_at"] < second["read_at"] and first["change_token"] == second["change_token"]
    campaign.state.blocker("inspect", "ownership", "No liveness proof")
    blocked = read()
    assert blocked["event_cutoff"] == second["event_cutoff"]
    assert blocked["source_cutoff"] == second["source_cutoff"]
    assert blocked["change_token"] != second["change_token"]
    campaign.state.control("pause", "synthetic-pause", blocked["campaign"]["revision"])
    assert read()["change_token"] != blocked["change_token"]


def test_recovery_and_change_lists_include_all_pages(campaign):
    with campaign.state.db.write() as conn:
        for index in range(55):
            conn.execute("INSERT INTO blockers VALUES(?,1,'ownership',NULL,?,?,NULL)",
                         (f"block-{index}", f"Ownership gap {index}", index))
    for index in range(55):
        notes(campaign, f"Research note {index}")
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        first, second = get(base, "/debug")[2], get(base, "/debug?page=2")[2]
        assert first.count(b'class="recovery-card"') == 50 and second.count(b'class="recovery-card"') == 5
        assert b"55 records" in first and b"Ownership gap 54" in second
        assert get(base, "/debug?page=3")[0] == 404
        changes = get(base, "/changes")[2]
        assert b"56 research sources" in changes and b"Research note 54" in changes
        last = get(base, "/changes?page=3")[2]
        assert b"Page 3 of 3" in last and b"Research note 0" in last and b"Research note 54" not in last
    assert dump(campaign.state) == before


def test_reopened_brief_does_not_reissue_prior_generation_plan(campaign):
    study(campaign)
    with campaign.state.db.write() as conn:
        conn.execute("UPDATE generations SET state='closed',outcome='unassessed'")
        conn.execute("UPDATE campaign SET progress='closed'")
    campaign.state.control("reopen", "reopen-fixture", campaign.state.campaign()["revision"])
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        body = get(base)[2]
        assert b"Historical rationale from generation 1" in body
        assert b"No accepted next step is recorded for this generation" in body
        assert b"Run three paired-seed replications" not in body
    assert dump(campaign.state) == before


def test_empty_source_and_long_generation_history_remain_readable(campaign):
    notes(campaign, "")
    with campaign.state.db.write() as conn:
        for generation in range(2, 24):
            conn.execute("INSERT INTO generations(id,state,created,reason) VALUES(?,'closed',1,?)",
                         (generation, f"Historical boundary {generation}"))
    before = dump(campaign.state)
    with serve(campaign.config.config_path, chat=False) as base:
        assert get(base)[0] == 200 and get(base, "/changes")[0] == 200
        first, last = get(base, "/history")[2], get(base, "/history?page=2")[2]
        assert b"23 records" in first and b"Historical boundary 23" in first
        assert b"Page 2 of 2" in last and b"Historical boundary 2" in last and b"Historical boundary 23" not in last
    assert dump(campaign.state) == before


def test_machine_capacity_totals_do_not_lose_grants_beyond_first_page(campaign):
    owner = {"kind": "campaign", "state_dir": str(campaign.state.root), "generation": 1, "revision": 0}
    for index in range(101):
        campaign.ledger.request(f"request-{index}", campaign.state.id, f"work-{index}", "experiment",
                                Resources(1, 256), owner, native=True)
    with campaign.ledger.write() as conn:
        conn.execute("UPDATE grants SET state='granted' WHERE token='request-100'")
    with campaign.ledger.read() as conn:
        before = list(conn.iterdump())
    with serve(campaign.config.config_path, chat=False) as base:
        first, second = get(base, "/resources")[2], get(base, "/resources?ledger_offset=100")[2]
        assert b"(1-100 of 101)" in first and b"work-100" not in first
        assert b"<strong>Reserved CPUs</strong><span>1 / 2</span>" in first
        assert b"(101-101 of 101)" in second and b"work-100" in second
        assert b"Previous machine page" in second and b"Next machine page" in first
    with campaign.ledger.read() as conn:
        assert list(conn.iterdump()) == before


def test_generated_context_stays_valid_on_error_and_long_filter_pages(campaign):
    with serve(campaign.config.config_path, chat=False) as base:
        for path in ("/not-a-route", "/observation?id=missing", "/jobs?q=" + "x" * 3000):
            context = Document(get(base, path)[2]).context()
            assert question_context(context) == context
        assert "omitted" in context["label"] and "url" not in context


def test_contextual_questions_are_bounded_idempotent_and_not_authority(campaign):
    _, observations, source = study(campaign)
    driver = FakeObserver()
    observer = service(campaign, driver)
    before = dump(campaign.state)
    with serve(campaign.config.config_path, observer=observer) as base:
        csrf = token(base)
        context = {"label": "Exact invalid observation", "url": "/observation?id=" + observations[-1],
                   "observation_id": observations[-1], "source_id": source, "read_at": 123}
        payload = {"message": "Why excluded?", "request_id": uuid.uuid4().hex, "context": context}
        code, response = post(base, "/chat/message", payload, csrf)
        assert code == 202
        done = finished(observer, response["conversation_id"])
        assert done["messages"][0]["context"] == context
        assert "untrusted source hints" in driver.calls[0] and source in driver.calls[0]
        assert post(base, "/chat/message", payload, csrf)[0] == 202 and len(driver.calls) == 1
        assert post(base, "/chat/message", {**payload, "context": {**context, "source_id": "different"}}, csrf)[0] == 409
        for invalid in ({"url": "https://example.invalid"}, {"url": "/artifact?id=x"}, {"shell": "whoami"},
                        {"read_at": True}, {"read_at": float("nan")}, {"label": "x" * 241},
                        {"generation": 1.5}, {"source_cutoff": 2.5}, {"read_at": 1e100}):
            assert post(base, "/chat/message", {**payload, "request_id": uuid.uuid4().hex, "context": invalid}, csrf)[0] == 400
        assert len(driver.calls) == 1
    assert dump(campaign.state) == before


@pytest.mark.skipif(environment_value("BROWSER_TESTS") != "1", reason="Opt in to prepared Edge")
def test_browser_owner_journeys_checkpoint_context_reports_and_mobile(campaign, tmp_path):
    from playwright.sync_api import sync_playwright, expect

    attempts, observations, source = study(campaign)
    view = reporting.seal_view(campaign.state)
    report = reporting.publish_report(campaign.state, view["id"])
    campaign.state.blocker("lost-owner", "ownership", "Missing terminal receipt; do not duplicate.", work_id=attempts[0])
    driver = FakeObserver()
    observer = service(campaign, driver)
    with serve(campaign.config.config_path, observer=observer) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=environment_value("BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(base)
            expect(page.get_by_role("navigation", name="Main navigation").get_by_role("link")).to_have_count(4)
            expect(page.locator("#catchup-status")).to_contain_text("No saved viewing checkpoint")
            page.get_by_role("button", name="Mark caught up").click()
            saved = page.locator("#catchup-link").get_attribute("href")
            expect(page.locator("#catchup-status")).to_contain_text("Since your saved checkpoint")
            page.reload()
            assert page.locator("#catchup-link").get_attribute("href") == saved
            later = notes(campaign, "New source after explicit catch-up")
            before = dump(campaign.state)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
            assert page.locator("#catchup-link").get_attribute("href") != saved
            page.locator("#catchup-link").click()
            expect(page.locator("main")).to_contain_text("New source after explicit catch-up")
            assert source not in page.locator("main").inner_html()
            page.goto(base + "/observation?id=" + observations[-1])
            expect(page.locator("main")).to_contain_text("Unequal warm-up")
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator("#chat-send")).to_be_enabled()
            expect(page.locator("#chat-context")).to_contain_text("policy-seed-3")
            page.get_by_label("Ask about this campaign").fill("Why is this invalid?")
            page.locator("#chat-send").click()
            expect(page.locator("#chat-send")).to_be_enabled()
            expect(page.locator(".chat-exchange-context")).to_contain_text("policy-seed-3")
            assert observations[-1] in driver.calls[0]
            page.get_by_role("button", name="Close chat").click()
            page.get_by_role("link", name="History", exact=True).click()
            page.get_by_role("link", name="Open retained report").click()
            expect(page.locator("main")).to_contain_text("Readable retained report")
            expect(page.locator("main")).to_contain_text("not a closure assessment")
            page.screenshot(path=str(tmp_path / "report-desktop.png"), full_page=True)
            routes = ["/", "/evidence", "/work", "/history", "/debug", "/resources", "/artifacts",
                      "/observation?id=" + observations[-1], "/report?id=" + report["id"],
                      "/compare?" + urllib.parse.urlencode({"id": observations}, doseq=True)]
            for width in (390, 768, 1440):
                page.set_viewport_size({"width": width, "height": 844 if width == 390 else 1000})
                for route in routes:
                    page.goto(base + route)
                    expect(page.locator(".project-name")).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, route)
                    assert not page.locator(".refresh-status.error").count()
                page.goto(base)
                page.screenshot(path=str(tmp_path / f"brief-{width}.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("""() => {
                const sizes = [...document.querySelectorAll('body, body *')].map(element =>
                    [element, parseFloat(getComputedStyle(element).fontSize)]);
                for (const [element, size] of sizes) element.style.fontSize = `${size * 2}px`;
            }""")
            assert page.evaluate("getComputedStyle(document.body).fontSize") == "30px"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / "brief-text-200-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(base + "/debug")
            expect(page.locator(".recovery-card")).to_contain_text("policy-seed-0")
            expect(page.get_by_role("button", name="Copy read-only inspection commands")).to_be_visible()
            page.screenshot(path=str(tmp_path / "recovery-mobile.png"), full_page=True)
            page.goto(base)
            page.get_by_role("button", name="Forget checkpoint").click()
            expect(page.locator("#catchup-status")).to_contain_text("No saved viewing checkpoint")
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator(".chat-question")).to_have_text("Why is this invalid?")
            assert len(driver.calls) == 1 and not errors and all(url.startswith(base) for url in requests)
            assert dump(campaign.state) == before
        finally:
            browser.close()


@pytest.mark.skipif(environment_value("BROWSER_TESTS") != "1", reason="Opt in to prepared Edge")
def test_browser_nonreplacing_updates_and_cross_tab_checkpoints(campaign):
    from playwright.sync_api import sync_playwright, expect

    with serve(campaign.config.config_path, chat=False) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=environment_value("BROWSER_EXECUTABLE"))
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page, other = context.new_page(), context.new_page()
            page.clock.install()
            page.goto(base)
            other.goto(base)
            page.get_by_role("button", name="Mark caught up").click()
            expect(other.locator("#catchup-status")).to_contain_text("Since your saved checkpoint")
            key = f"labgoblin-checkpoint-{campaign.state.id}-1"
            saved = page.evaluate("key => localStorage.getItem(key)", key)
            previous = page.locator("main").inner_html()
            campaign.state.blocker("inspect", "ownership", "Backend unreachable; liveness unknown.")
            before = dump(campaign.state)
            page.get_by_label("Check for updates (15s)").check()
            page.clock.fast_forward(15000)
            expect(page.locator("#refresh-status")).to_contain_text("New recorded data")
            assert page.locator("main").inner_html() == previous
            assert page.evaluate("key => localStorage.getItem(key)", key) == saved
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("main")).to_contain_text("Backend unreachable")
            page.clock.fast_forward(15000)
            expect(page.locator("#refresh-status")).not_to_contain_text("New recorded data")
            assert page.evaluate("key => localStorage.getItem(key)", key) == saved
            other.get_by_role("button", name="Forget checkpoint").click()
            expect(page.locator("#catchup-status")).to_contain_text("No saved viewing checkpoint")
            page.evaluate("""key => localStorage.setItem(key, JSON.stringify({
                campaign: 'different', generation: 2, read_at: 1, source_cutoff: 0, event_cutoff: 0
            }))""", key)
            page.reload()
            expect(page.locator("#catchup-status")).to_contain_text("checkpoint is incompatible")
            expect(page.get_by_role("button", name="Forget checkpoint")).to_be_enabled()
            page.get_by_role("button", name="Forget checkpoint").click()
            expect(page.locator("#catchup-status")).to_contain_text("No saved viewing checkpoint")
            assert dump(campaign.state) == before
        finally:
            browser.close()
