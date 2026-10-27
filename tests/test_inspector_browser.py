import os

import pytest

from labgoblin import runtime
from tests.test_inspector import command, observer, server


pytestmark = pytest.mark.skipif(os.environ.get("LABGOBLIN_BROWSER_TESTS") != "1",
                                reason="Prepared browser qualification is explicitly opt-in")


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("LABGOBLIN_BROWSER_EXECUTABLE") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            yield page
            assert not errors
        finally:
            context.close()
            browser.close()


def ready(page, server, observer):
    from playwright.sync_api import expect
    page.goto(server.url)
    expect(page.locator("#project")).to_have_text(str(observer.project))


def test_read_only_browser_refresh_images_paging_and_mobile(page, server, observer, tmp_path):
    from playwright.sync_api import expect
    text = "<script>window.injected = true</script>\n" + "a" * 17000 + "\nlast page"
    (observer.project / "report.md").write_text(text)
    ready(page, server, observer)
    expect(page.locator("#ask")).to_be_disabled()
    assert page.url == server.origin + "/"
    page.reload()
    expect(page.locator("#project")).to_have_text(str(observer.project))
    page.locator("#read").click()
    expect(page.locator("#evidence pre")).to_contain_text("<script>")
    assert page.evaluate("window.injected") is None
    page.locator("#path").fill("nonexistent.txt")
    page.locator("#next").click()
    expect(page.locator("#evidence pre")).to_contain_text("last page")
    page.locator("#path").fill("report.md")
    page.locator("#read").click()
    expect(page.locator("#next")).to_be_visible()
    (observer.project / "report.md").write_text("Changed live report")
    page.locator("#next").click()
    expect(page.locator("#error")).to_contain_text("changed between pages")
    page.locator("#path").fill("comparison.png")
    page.locator("#image").click()
    expect(page.locator("#evidence img")).to_be_visible()
    assert page.locator("#evidence img").evaluate("(img) => img.complete && img.naturalWidth > 0")
    for width in (1440, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"inspector-{width}.png"), full_page=True)
    assert observer.history()["total"] == 0


def test_browser_owned_answer_citations_followup_and_explicit_failure(page, server, observer, monkeypatch):
    from playwright.sync_api import expect
    observer.approve(100, model="model-free")
    argv = command(observer, image="comparison.png")
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: {"command": argv})
    server.chat = True
    ready(page, server, observer)
    page.locator("#question").fill("Explain the control and show its image.")
    page.locator("#ask").click()
    expect(page.locator(".references button").filter(has_text="[E3]")).to_be_visible(timeout=30000)
    page.locator(".references button").filter(has_text="[E3]").click()
    expect(page.locator("article img")).to_be_visible()
    expect(page.locator("#evidence img")).to_be_visible()
    expect(page.locator("#ask")).to_be_enabled()
    (observer.project / "report.md").write_text("New live evidence for the next question.")
    page.locator("#question").fill("What changed?")
    page.locator("#ask").click()
    expect(page.locator("article")).to_have_count(2, timeout=30000)
    expect(page.locator("article").last.locator(".references button").filter(has_text="[E2]")).to_be_visible(timeout=30000)
    page.locator("article").last.locator(".references button").filter(has_text="[E2]").click()
    expect(page.locator("#evidence pre")).to_contain_text("New live evidence")
    expect(page.locator("#ask")).to_be_enabled()
    server.chat = False
    page.locator("#question").fill("This server now denies new questions")
    page.locator("#ask").click()
    expect(page.locator("#error")).to_contain_text("view-only")
    assert observer.status()["inspection"]["budget"]["used"] == 20


def test_browser_recent_history_and_older_pages(page, server, observer, monkeypatch):
    from playwright.sync_api import expect
    rows = [{"question": f"Question {index}", "answer": f"Answer {index}", "created": index,
             "status": "completed", "history_omitted": 0, "sources": []} for index in range(45)]

    def history(*, offset=None, limit=40):
        start = max(0, len(rows) - limit) if offset is None else offset
        return {"messages": rows[start:start + limit], "offset": start, "total": len(rows),
                "next_offset": start + limit if start + limit < len(rows) else None}

    monkeypatch.setattr(observer, "history", history)
    ready(page, server, observer)
    expect(page.locator("article")).to_have_count(40)
    expect(page.locator("article h3").first).to_have_text("Question 5")
    expect(page.locator("article h3").last).to_have_text("Question 44")
    page.locator("#more-history").click()
    expect(page.locator("article")).to_have_count(45)
    expect(page.locator("article h3").first).to_have_text("Question 0")
    expect(page.locator("#more-history")).to_be_hidden()


def test_browser_chat_idea_requires_review_and_publish(page, server, observer, monkeypatch):
    from playwright.sync_api import expect
    from labgoblin.ideas import Inbox
    observer.approve(100, model="model-free")
    argv = command(observer, idea={
        "title": "Try another control", "message": "<script>window.injected=true</script>\nCompare the positive control.",
        "sources": [{"scope": "workspace", "path": "report.md"}]})
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: {"command": argv})
    server.chat = True
    ready(page, server, observer)
    page.locator("#question").fill("Prepare my control suggestion for publication.")
    page.locator("#ask").click()
    review = page.locator(".idea-draft button").filter(has_text="Review idea draft")
    expect(review).to_be_visible(timeout=30000)
    assert Inbox(observer.project).status()["pending"] == 0
    review.click()
    expect(page.locator(".idea-draft pre")).to_contain_text("<script>")
    assert page.evaluate("window.injected") is None
    page.locator(".idea-draft button").filter(has_text="report.md").click()
    expect(page.locator("#evidence pre")).to_contain_text("failed positive control")
    assert Inbox(observer.project).status()["pending"] == 0
    page.locator(".idea-draft").get_by_role("button", name="Publish idea", exact=True).click()
    expect(page.locator("#ideas")).to_contain_text("1 pending / 1 idea identities")
    assert Inbox(observer.project).status()["pending"] == 1
    assert observer.status()["inspection"]["budget"]["used"] == 10
