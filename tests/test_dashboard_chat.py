"""Observer tool boundaries, HTTP controls and real SDK/browser opt-in cases."""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import uuid

import pytest

from labgoblin.paths import environment_value

from tests.test_dashboard import campaign, complete_job, get, job, notes, serve
from labgoblin.dashboard import _chat_markdown
from labgoblin.dashboard_chat import ChatError, ChatSettings, ObserverService, SDKObserver, _SDKSession, TOOLS, load_chat_settings
from labgoblin.dashboard_data import EvidenceReader


class FakeObserver:
    def __init__(self, delay=0.03, error=None, response=None):
        self.delay = delay
        self.error = error
        self.response = response
        self.calls = []
        self.stopped = threading.Event()

    async def answer(self, settings, reader, prompt, emit):
        self.calls.append(prompt)
        try:
            result = reader.read("campaign_status", {})
            emit("tool", {"name": "campaign_status"})
            emit("sources", result["sources"])
            emit("delta", {"id": "reply", "text": "**Recorded state:** "})
            await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            state = result["data"]["campaign"]["state"]
            answer = self.response if self.response is not None else f"**Recorded state:** {state}. [Evidence](/activity)"
            emit("message", {"id": "reply", "text": answer})
            emit("usage", {"id": "usage-1", "model": "test-model", "input_tokens": 30, "output_tokens": 10})
            return answer
        finally:
            self.stopped.set()


def service(campaign, driver=None, **options):
    return ObserverService(campaign.config.config_path,
                           ChatSettings(enabled=True, cli_path=sys.executable, **options),
                           driver=driver or FakeObserver())


def finished(observer, cid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = observer.snapshot(cid)
        if not state["busy"]:
            return state
        time.sleep(0.01)
    raise AssertionError("Observer did not finish")


def token(base):
    return re.search(rb'name="labgoblin-chat-token" content="([^"]+)"', get(base)[2])[1].decode()


def post(base, path, payload, csrf="", **headers):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=body, headers={
        "Content-Type": "application/json", "X-LabGoblin-Token": csrf, **headers})
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def test_settings_are_enabled_by_default_and_validated(campaign):
    path = Path(campaign.config.config_path)
    assert load_chat_settings(str(path)).enabled
    assert load_chat_settings(str(path), enabled=True).enabled
    assert not load_chat_settings(str(path), enabled=False).enabled
    original = path.read_text(encoding="utf-8")
    path.write_text(original + '\n[dashboard.chat]\nenabled = true\nmodel = "test-model"\nreasoning_effort = "high"\n',
                    encoding="utf-8")
    settings = load_chat_settings(str(path))
    assert settings.enabled and settings.model == "test-model" and settings.reasoning_effort == "high"
    for invalid in ('timeout_seconds = 0', 'timeout_seconds = 601', 'enabled = "yes"', 'unknown = true',
                    'model = ""', 'reasoning_effort = "extreme"'):
        path.write_text(original + "\n[dashboard.chat]\n" + invalid + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_chat_settings(str(path))


@pytest.mark.parametrize("configured", [None, False, True])
@pytest.mark.parametrize("override", [None, False, True])
def test_chat_flag_overrides_configured_choice(campaign, configured, override):
    path = Path(campaign.config.config_path)
    if configured is not None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n[dashboard.chat]\nenabled = " + str(configured).lower() + "\n")
    expected = override if override is not None else configured if configured is not None else True
    assert load_chat_settings(str(path), enabled=override).enabled is expected


def test_broken_sdk_installation_does_not_block_readonly_dashboard(campaign, monkeypatch):
    import importlib.metadata
    from tests.test_dashboard import dump

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr("labgoblin.dashboard_chat.importlib.metadata.version", missing)
    before = dump(campaign.state)
    with serve(campaign.config.config_path) as base:
        assert get(base)[0] == 200
        code, _, body = get(base, "/chat/status")
        status = json.loads(body)
        assert code == 200 and status["enabled"] and not status["ready"]
        assert "Repair the LabGoblin installation" in status["reason"]
    assert dump(campaign.state) == before


def test_reader_only_exposes_curated_evidence(campaign):
    attempt = job(campaign, environment={"SECRET_TOKEN": "do-not-expose"}, argv=["python", "-c", "SECRET_COMMAND"])
    spec = json.loads(campaign.state.attempt(attempt)["spec"])
    Path(spec["output"]).mkdir(parents=True, exist_ok=True)
    (Path(spec["output"]) / "stdout.log").write_text("PRIVATE_LOG_BODY", encoding="utf-8")
    (Path(spec["output"]) / "private-target.json").write_text("PRIVATE_TARGET_BODY", encoding="utf-8")
    (Path(spec["output"]) / "metrics.json").write_text('{"score":42}', encoding="utf-8")
    complete_job(campaign, attempt)
    notes(campaign, "# Current finding\n\nContinue measuring.")
    reader = EvidenceReader(campaign.config.config_path)
    with campaign.state.db.read() as db:
        before = list(db.iterdump())
    result = reader.read("get_experiment", {"id": attempt})
    text = json.dumps(result)
    for forbidden in ("do-not-expose", "SECRET_COMMAND", "PRIVATE_LOG_BODY", "PRIVATE_TARGET_BODY", "SECRET_TOKEN"):
        assert forbidden not in text
    assert result["data"]["experiments"][0]["metrics"] == {"score": 42}
    assert result["observed_at"] and result["sources"]
    assert "campaign" in reader.read("campaign_status", {})["data"]
    assert reader.read("agent_activity", {})["data"]["events"]
    assert "Current finding" in reader.read("research_document", {"document": "journal"})["data"]["entries"][0]["preview"]
    for name, arguments in (
        ("steer", {}), ("get_experiment", {"id": attempt, "path": "private-target.json"}),
        ("get_experiment", {"id": "../private"}), ("research_document", {"document": "../secret"}),
        ("list_experiments", {"query": 42}), ("campaign_status", {"sql": "DELETE FROM jobs"}),
    ):
        with pytest.raises(ValueError):
            reader.read(name, arguments)
    with campaign.state.db.read() as db:
        assert list(db.iterdump()) == before


def test_observer_presents_explicit_statements_not_inferred_claims(campaign):
    campaign.state.hypothesis("h002", "Constraints improve coverage.")
    reader = EvidenceReader(campaign.config.config_path)
    hypotheses = {row["id"]: row for row in reader.read("campaign_status", {})["data"]["hypotheses"]}
    assert hypotheses["h002"]["statement"] == "Constraints improve coverage."
    assert hypotheses["h002"]["frozen"] == 0


def test_conversation_freshness_idempotency_and_separate_accounting(campaign):
    driver = FakeObserver()
    observer = service(campaign, driver)
    try:
        nonce = uuid.uuid4().hex
        first = observer.send("", nonce, "What is happening?")
        assert observer.send("", nonce, "What is happening?")["conversation_id"] == first["conversation_id"]
        done = finished(observer, first["conversation_id"])
        assert done["messages"][-1]["state"] == "completed"
        assert len(driver.calls) == 1 and done["messages"][-1]["tools_used"] == ["campaign_status"]
        assert done["messages"][-1]["usage"][0]["input_tokens"] == 30
        campaign.state.control("pause", uuid.uuid4().hex, 0)
        observer.send(first["conversation_id"], uuid.uuid4().hex, "And now?")
        second = finished(observer, first["conversation_id"])
        assert "paused" in second["messages"][-1]["answer"]
        assert "What is happening?" in driver.calls[-1]
        with campaign.state.db.read() as db:
            assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM events WHERE acknowledged_by IS NULL").fetchone()[0] >= 1
        observer.clear(first["conversation_id"])
        with pytest.raises(ChatError, match="not found"):
            observer.snapshot(first["conversation_id"])
    finally:
        observer.close()


@pytest.mark.parametrize("action,expected", [("cancel", "cancelled"), ("timeout", "timed_out"), ("close", "cancelled")])
def test_cancel_timeout_and_shutdown_stop_owned_work(campaign, action, expected):
    driver = FakeObserver(delay=30)
    observer = service(campaign, driver, timeout_seconds=0.04 if action == "timeout" else 10)
    state = observer.send("", uuid.uuid4().hex, "Explain progress")
    if action == "cancel":
        observer.cancel(state["conversation_id"])
    elif action == "close":
        observer.close()
    done = finished(observer, state["conversation_id"])
    assert done["messages"][-1]["state"] == expected
    assert driver.stopped.is_set()
    observer.close()


def test_failures_busy_and_missing_dependency_are_explicit(campaign, monkeypatch):
    observer = service(campaign, FakeObserver(delay=0.1, error=RuntimeError("Backend unavailable")))
    try:
        state = observer.send("", uuid.uuid4().hex, "Why?")
        with pytest.raises(ChatError, match="Another"):
            observer.send("", uuid.uuid4().hex, "Also?")
        with pytest.raises(ChatError, match="Cancel"):
            observer.clear(state["conversation_id"])
        done = finished(observer, state["conversation_id"])
        assert done["messages"][-1]["state"] == "failed" and "Backend unavailable" in done["messages"][-1]["error"]
    finally:
        observer.close()
    monkeypatch.setattr("labgoblin.dashboard_chat.importlib.metadata.version", lambda _: "0.0.1")
    observer = ObserverService(campaign.config.config_path, ChatSettings(enabled=True))
    assert not observer.availability()["ready"]
    with pytest.raises(ChatError, match="Repair"):
        observer.send("", uuid.uuid4().hex, "Why?")
    assert observer.thread is None
    observer.close()


def test_chat_http_auth_rendering_and_dashboard_responsiveness(campaign):
    driver = FakeObserver(delay=0.2)
    observer = service(campaign, driver)
    with serve(campaign.config.config_path, observer=observer) as base:
        csrf = token(base)
        assert json.loads(get(base, "/chat/status")[2])["ready"]
        assert driver.calls == []
        payload = {"request_id": uuid.uuid4().hex, "message": "What is running?"}
        assert post(base, "/chat/message", payload)[0] == 403
        assert post(base, "/chat/message", payload, "\xff")[0] == 403
        assert post(base, "/chat/message", payload, csrf, Origin="https://untrusted.example")[0] == 403
        assert post(base, "/chat/message", {**payload, "file": "../secret"}, csrf)[0] == 400
        assert post(base, "/chat/message", payload, csrf, **{"Content-Type": "text/plain"})[0] == 415
        assert post(base, "/chat/message", {"message": "x" * 20001}, csrf)[0] == 413
        assert post(base, "/chat/message", [], csrf)[0] == 400
        invalid = urllib.request.Request(base + "/chat/message", data=b"{broken",
                                         headers={"Content-Type": "application/json", "X-LabGoblin-Token": csrf})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(invalid)
        assert caught.value.code == 400
        code, state = post(base, "/chat/message", payload, csrf, Origin=base)
        assert code == 202
        cid = state["conversation_id"]
        assert get(base, "/jobs")[0] == 200
        assert post(base, "/stop", {}, csrf)[0] == 404
        done = finished(observer, cid)
        result = json.loads(get(base, "/chat/status?conversation_id=" + cid)[2])["conversation"]
        assert "<strong>Recorded state:</strong>" in result["messages"][-1]["html"]
        assert result["messages"][-1]["answer"] == done["messages"][-1]["answer"]
        assert post(base, "/chat/clear", {"conversation_id": cid}, csrf)[0] == 200
        assert get(base, "/chat/status?conversation_id=" + cid)[0] == 404


def test_chat_markdown_restricts_links_images_and_html():
    rendered = _chat_markdown(
        '**Hello** <script>alert(1)</script>\n\n[Local](/job?id=abc)\n\n'
        '[Remote](https://untrusted.example/?secret=data)\n\n![Image](https://untrusted.example/a.png)')
    assert '<strong>Hello</strong>' in rendered and 'href="/job?id=abc"' in rendered
    assert "<script>" not in rendered and "<img" not in rendered
    assert 'href="https:' not in rendered and 'href="#"' in rendered


@pytest.mark.parametrize("target", [
    "//untrusted.example", "/\\untrusted.example", "https://untrusted.example", "/%2f%2funtrusted.example",
    "javascript:alert%281%29", "data:text/html,bad", "/artifact?id=anything",
])
def test_chat_links_cannot_navigate_outside_evidence(target):
    rendered = _chat_markdown(f"[Link]({target})")
    assert 'href="' not in rendered or 'href="#"' in rendered


@pytest.mark.parametrize("limit", ["tool", "output"])
def test_limits_cannot_be_reported_as_success(campaign, limit):
    from labgoblin.dashboard_chat import MAX_RESPONSE

    class LimitedObserver:
        async def answer(self, settings, reader, prompt, emit):
            if limit == "tool":
                emit("limit", {"error": "Evidence-tool budget exhausted"})
            else:
                emit("delta", {"id": "x", "text": "x" * (MAX_RESPONSE + 1)})
            return "This must not be reported as success"

    observer = service(campaign, LimitedObserver())
    try:
        state = observer.send("", uuid.uuid4().hex, "Read everything")
        message = finished(observer, state["conversation_id"])["messages"][-1]
        assert message["state"] == "failed" and message["error"]
        assert len(message["answer"]) <= MAX_RESPONSE
    finally:
        observer.close()


def test_conversation_limits_preserve_history_until_explicit_clear(campaign, monkeypatch):
    from labgoblin import dashboard_chat

    monkeypatch.setattr(dashboard_chat, "MAX_CONVERSATIONS", 1)
    monkeypatch.setattr(dashboard_chat, "MAX_QUESTIONS", 2)
    monkeypatch.setattr(dashboard_chat, "HISTORY_CHARS", 10)
    driver = FakeObserver()
    observer = service(campaign, driver)
    try:
        nonce = uuid.uuid4().hex
        first = observer.send("", nonce, "Old question")
        cid = first["conversation_id"]
        finished(observer, cid)
        with pytest.raises(ChatError, match="already used"):
            observer.send(cid, nonce, "Different question")
        with pytest.raises(ChatError, match="Too many"):
            observer.send("", uuid.uuid4().hex, "Another chat")
        observer.send(cid, uuid.uuid4().hex, "Current question")
        finished(observer, cid)
        assert "Old question" not in driver.calls[-1]
        with pytest.raises(ChatError, match="question limit"):
            observer.send(cid, uuid.uuid4().hex, "Third question")
        monotonic = time.monotonic
        with monkeypatch.context() as clock:
            clock.setattr(time, "monotonic", lambda: monotonic() + 3600)
            with pytest.raises(ChatError, match="Too many"):
                observer.send("", uuid.uuid4().hex, "Fresh conversation")
        assert [message["question"] for message in observer.snapshot(cid)["messages"]] == [
            "Old question", "Current question"]
        observer.clear(cid)
        second = observer.send("", uuid.uuid4().hex, "Fresh conversation")
        finished(observer, second["conversation_id"])
        with pytest.raises(ChatError, match="not found"):
            observer.snapshot(cid)
    finally:
        observer.close()


@pytest.mark.skipif(environment_value("BROWSER_TESTS") != "1", reason="Opt in to prepared Playwright/Chromium")
def test_browser_chat_streaming_navigation_cancellation_and_retry(campaign, tmp_path):
    from playwright.sync_api import sync_playwright, expect

    driver = FakeObserver(delay=1)
    observer = service(campaign, driver)
    with campaign.state.db.write() as db:
        db.execute("UPDATE campaign SET progress='wait',reason='Measuring the next controlled comparison'")
    job(campaign)
    with serve(campaign.config.config_path, observer=observer) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=environment_value("BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator("#chat-send")).to_be_enabled()
            assert not driver.calls
            page.get_by_label("Ask about this campaign").fill("What is happening?")
            page.locator("#chat-question").press("Enter")
            expect(page.locator(".chat-answer")).to_contain_text("Recorded state:")
            expect(page.locator("#chat-send")).to_be_enabled()
            expect(page.locator(".chat-answer")).to_contain_text("wait")
            expect(page.locator(".chat-usage")).to_contain_text("30 in / 10 out")
            assert page.locator(".chat-sources a").first.get_attribute("target") == "_blank"
            page.screenshot(path=str(tmp_path / "chat-desktop.png"), full_page=True)
            cid = next(iter(observer.conversations))
            page.get_by_role("navigation", name="Main navigation").get_by_role("link", name="History", exact=True).click()
            page.get_by_role("navigation", name="Section navigation").get_by_role("link", name="Journal", exact=True).click()
            expect(page.locator("#chat-panel")).to_be_visible()
            expect(page.locator("#chat-open")).to_have_attribute("aria-expanded", "true")
            expect(page.locator(".chat-question")).to_have_text("What is happening?")
            assert len(driver.calls) == 1
            page.locator("#chat-question").press("Escape")
            expect(page.locator("#chat-panel")).to_be_visible()
            page.reload()
            expect(page.locator("#chat-panel")).to_be_visible()
            expect(page.locator(".chat-question")).to_have_text("What is happening?")
            assert list(observer.conversations) == [cid] and len(driver.calls) == 1
            page.get_by_role("button", name="Close chat").click()
            expect(page.locator("#chat-panel")).to_be_hidden()
            expect(page.locator("#chat-open")).to_be_focused()
            page.reload()
            expect(page.locator("#chat-panel")).to_be_hidden()
            expect(page.locator("#chat-open")).to_have_attribute("aria-expanded", "false")
            page.get_by_role("navigation", name="Main navigation").get_by_role("link", name="Brief", exact=True).click()
            expect(page.locator("#chat-panel")).to_be_hidden()
            page.go_back()
            expect(page.locator("#chat-panel")).to_be_hidden()
            page.go_forward()
            expect(page.locator("#chat-panel")).to_be_hidden()
            page.get_by_role("button", name="Refresh", exact=True).click()
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator(".chat-answer")).to_contain_text("wait")
            driver.delay = 30
            page.get_by_label("Ask about this campaign").fill("Please explain more")
            page.locator("#chat-send").click()
            expect(page.locator(".chat-question")).to_have_count(2)
            page.get_by_role("button", name="Maximize chat").click()
            expect(page.locator("#chat-cancel")).to_be_enabled()
            page.get_by_role("button", name="Restore sidebar").click()
            page.reload()
            expect(page.locator("#chat-panel")).to_be_visible()
            expect(page.locator(".chat-question")).to_have_count(2)
            expect(page.locator("#chat-cancel")).to_be_enabled()
            assert list(observer.conversations) == [cid] and len(driver.calls) == 2
            page.get_by_role("button", name="Close chat").click()
            page.reload()
            expect(page.locator("#chat-panel")).to_be_hidden()
            assert observer.snapshot(cid)["busy"]
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator(".chat-question")).to_have_count(2)
            page.locator("#chat-cancel").click()
            expect(page.locator(".chat-error").last).to_contain_text("cancelled")
            expect(page.locator("#chat-send")).to_be_enabled()
            page.locator("#chat-clear").click()
            expect(page.locator(".chat-exchange")).to_have_count(0)
            expect(page.locator("#chat-panel")).to_be_visible()
            page.reload()
            expect(page.locator("#chat-panel")).to_be_visible()
            expect(page.locator(".chat-exchange")).to_have_count(0)
            expect(page.locator("#chat-send")).to_be_enabled()
            assert not observer.conversations
            driver.delay = 0.03

            def drop_response(route):
                route.fetch()
                route.abort()

            page.route("**/chat/message", drop_response, times=1)
            page.get_by_label("Ask about this campaign").fill("Did my question reach you?")
            page.locator("#chat-send").click()
            expect(page.locator("#chat-status")).to_contain_text("reuses its request ID")
            count = len(driver.calls)
            page.locator("#chat-send").click()
            expect(page.locator(".chat-answer")).to_contain_text("wait")
            expect(page.locator("#chat-send")).to_be_enabled()
            assert len(driver.calls) == count
            assert len(observer.conversations) == 1 and cid not in observer.conversations
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(tmp_path / "chat-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator("#chat-send").bounding_box()["y"] < 844
            assert not errors
        finally:
            browser.close()


@pytest.mark.skipif(environment_value("BROWSER_TESTS") != "1", reason="Opt in to prepared Playwright/Chromium")
def test_browser_chat_maximize_preserves_reading_and_conversation(campaign, tmp_path):
    from playwright.sync_api import sync_playwright, expect

    response = "\n\n".join(
        f"## Measurement {number}\n\n"
        + "The controlled comparison separates a measured result from an untested explanation. " * 8
        for number in range(1, 26)
    )
    driver = FakeObserver(response=response)
    observer = service(campaign, driver)
    with serve(campaign.config.config_path, observer=observer) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=environment_value("BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator("#chat-send")).to_be_enabled()
            panel = page.locator("#chat-panel")
            messages = page.get_by_role("region", name="Chat conversation")
            assert panel.bounding_box()["width"] == 500
            page.get_by_label("Ask about this campaign").fill("Explain the evidence in detail.")
            page.locator("#chat-send").click()
            expect(page.locator(".chat-answer")).to_contain_text("Measurement 25")
            expect(page.locator("#chat-send")).to_be_enabled()
            cid = next(iter(observer.conversations))
            page.get_by_label("Ask about this campaign").fill("Draft a follow-up question")
            paragraph = page.locator(".chat-answer > p").nth(12)
            paragraph.evaluate("""element => {
                const messages = document.getElementById("chat-messages");
                const bounds = element.getBoundingClientRect();
                messages.scrollTop += bounds.top - messages.getBoundingClientRect().top + bounds.height / 3;
            }""")

            def reading_fraction():
                return paragraph.evaluate("""element => {
                    const bounds = element.getBoundingClientRect();
                    return (document.getElementById("chat-messages").getBoundingClientRect().top - bounds.top) / bounds.height;
                }""")

            before = reading_fraction()
            assert 0.30 < before < 0.37
            sidebar_text_width = paragraph.bounding_box()["width"]
            page.get_by_role("button", name="Maximize chat").click()
            expect(panel).to_have_attribute("aria-modal", "true")
            assert panel.bounding_box() == {"x": 0, "y": 0, "width": 1440, "height": 1000}
            assert paragraph.bounding_box()["width"] > sidebar_text_width * 1.4
            assert paragraph.evaluate("element => getComputedStyle(element).fontSize") == "15px"
            assert abs(reading_fraction() - before) < 0.03
            expect(page.locator("#chat-question")).to_have_value("Draft a follow-up question")
            assert page.evaluate("document.querySelector('.workspace').inert && document.querySelector('.sidebar').inert")
            assert page.evaluate("getComputedStyle(document.body).overflow") == "hidden"
            page.locator("#chat-clear").focus()
            page.keyboard.press("Tab")
            expect(page.get_by_role("button", name="Restore sidebar")).to_be_focused()
            page.keyboard.press("Shift+Tab")
            expect(page.locator("#chat-clear")).to_be_focused()
            page.locator("#chat-question").press("Escape")
            expect(panel).to_be_visible()
            expect(panel).to_have_attribute("aria-modal", "false")
            expect(page.get_by_role("button", name="Maximize chat")).to_be_focused()
            assert panel.bounding_box()["width"] == 500
            assert abs(reading_fraction() - before) < 0.03
            expect(page.locator("#chat-question")).to_have_value("Draft a follow-up question")
            assert not page.evaluate("document.querySelector('.workspace').inert")

            messages.evaluate("element => element.scrollTop = element.scrollHeight")
            page.get_by_role("button", name="Maximize chat").click()
            assert messages.evaluate("element => element.scrollHeight - element.scrollTop - element.clientHeight") <= 4
            page.get_by_role("button", name="Close chat").click()
            expect(panel).to_be_hidden()
            assert not page.evaluate("document.querySelector('.workspace').inert")
            assert page.evaluate("getComputedStyle(document.body).overflow") != "hidden"
            page.get_by_role("button", name="Ask Copilot").click()
            expect(panel).to_have_attribute("aria-modal", "true")
            expect(page.locator("#chat-question")).to_have_value("Draft a follow-up question")
            page.goto(base + "/journal")
            expect(panel).to_have_attribute("aria-modal", "true")
            expect(page.locator(".chat-answer")).to_contain_text("Measurement 25")
            expect(page.get_by_role("button", name="Restore sidebar")).to_be_focused()
            page.go_back()
            expect(panel).to_have_attribute("aria-modal", "true")
            page.reload()
            expect(panel).to_have_attribute("aria-modal", "true")
            expect(page.locator(".chat-answer")).to_contain_text("Measurement 25")
            page.get_by_role("button", name="Restore sidebar").click()
            page.reload()
            expect(panel).to_have_attribute("aria-modal", "false")
            expect(panel).to_be_visible()
            assert panel.bounding_box()["width"] == 500
            expect(page.locator(".chat-answer")).to_contain_text("Measurement 25")
            assert list(observer.conversations) == [cid] and len(driver.calls) == 1

            page.get_by_role("button", name="Maximize chat").click()
            messages.evaluate("element => element.scrollTop = 0")
            messages.focus()
            page.keyboard.press("PageDown")
            expect(messages).to_be_focused()
            page.wait_for_function("document.getElementById('chat-messages').scrollTop > 0")
            messages.evaluate("element => element.scrollTop = 0")
            page.screenshot(path=str(tmp_path / "chat-maximized-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(tmp_path / "chat-maximized-mobile.png"))
            assert panel.bounding_box() == {"x": 0, "y": 0, "width": 390, "height": 844}
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert panel.evaluate("element => element.scrollWidth <= element.clientWidth")
            for selector in ("#chat-close", "#chat-maximize", "#chat-send"):
                bounds = page.locator(selector).bounding_box()
                assert 0 <= bounds["y"] and bounds["y"] + bounds["height"] <= 844
            page.set_viewport_size({"width": 844, "height": 390})
            page.locator("#chat-send").scroll_into_view_if_needed()
            bounds = page.locator("#chat-send").bounding_box()
            assert 0 <= bounds["y"] and bounds["y"] + bounds["height"] <= 390
            page.locator("#chat-clear").click()
            expect(page.locator(".chat-exchange")).to_have_count(0)
            expect(panel).to_have_attribute("aria-modal", "true")
            assert len(driver.calls) == 1
            page.get_by_role("button", name="Close chat").click()
            page.reload()
            expect(panel).to_be_hidden()
            page.get_by_role("button", name="Ask Copilot").click()
            expect(panel).to_have_attribute("aria-modal", "true")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("failure", ["", "startup", "answer", "abort", "disconnect", "delete", "stop"])
def test_sdk_session_policy_and_cleanup(campaign, monkeypatch, failure):
    copilot = pytest.importorskip("copilot")
    from copilot.generated.rpc import PermissionDecisionDeniedByRules
    captures = {}

    class Session:
        session_id = "owned-observer-session"

        def on(self, callback):
            captures["callback"] = callback

        async def send_and_wait(self, prompt, **kwargs):
            assert "Fresh read-only campaign snapshot" in prompt
            if failure in ("answer", "abort"):
                raise RuntimeError("Answer failed")
            captures["callback"](SimpleNamespace(
                type=SimpleNamespace(value="assistant.usage"), id=uuid.uuid4(),
                data=SimpleNamespace(model="test", input_tokens=12, output_tokens=4)))
            return SimpleNamespace(data=SimpleNamespace(content="Read-only answer"))

        async def abort(self):
            captures["aborted"] = True
            if failure == "abort":
                raise RuntimeError("Abort failed")

        async def disconnect(self):
            captures["disconnected"] = True
            if failure == "disconnect":
                raise RuntimeError("Disconnect failed")

    class Client:
        def __init__(self, **kwargs):
            captures["client"] = kwargs

        async def start(self):
            if failure == "startup":
                raise RuntimeError("Startup failed")

        async def get_auth_status(self):
            return SimpleNamespace(isAuthenticated=True)

        async def create_session(self, **kwargs):
            captures["session"] = kwargs
            return Session()

        async def delete_session(self, sid):
            captures["deleted"] = sid
            if failure == "delete":
                raise RuntimeError("Delete failed")

        async def stop(self):
            captures["stopped"] = True
            if failure == "stop":
                raise ExceptionGroup("Stop failed", [OSError("Transport failed")])

        async def force_stop(self):
            captures["forced"] = True

    monkeypatch.setattr(copilot, "CopilotClient", Client)
    events = []
    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(_SDKSession().answer(
                ChatSettings(cli_path=sys.executable), EvidenceReader(campaign.config.config_path),
                "Why?", lambda *args: None))
        assert captures["stopped"]
        if failure != "startup":
            assert captures["disconnected"] and captures["deleted"] == Session.session_id
        if failure == "stop":
            assert captures["forced"]
        return
    result = asyncio.run(_SDKSession().answer(
        ChatSettings(cli_path=sys.executable), EvidenceReader(campaign.config.config_path), "Why?",
        lambda kind, value: events.append((kind, value))))
    json.dumps(events)
    assert result == "Read-only answer"
    options = captures["session"]
    assert captures["client"]["mode"] == "empty"
    assert set(options["available_tools"].to_list()) == {"custom:" + name for name in TOOLS}
    for name in ("enable_config_discovery", "enable_file_hooks", "enable_host_git_operations",
                 "enable_skills", "enable_session_store", "enable_on_demand_instruction_discovery",
                 "manage_schedule_enabled", "request_extensions"):
        assert options[name] is False, name
    assert options["enable_managed_settings"] is True
    assert options["skip_custom_instructions"] is True
    assert options["system_message"]["mode"] == "replace"
    assert isinstance(options["on_permission_request"]({}, {}), PermissionDecisionDeniedByRules)
    guard = options["hooks"]["on_pre_tool_use"]
    assert guard({"toolName": "powershell"}, {})["permissionDecision"] == "deny"
    assert guard({"toolName": "campaign_status"}, {})["permissionDecision"] == "allow"
    assert captures["disconnected"] and captures["deleted"] == Session.session_id and captures["stopped"]
