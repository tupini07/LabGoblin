"""Owned, optional supervisor communication without new research or authority."""

import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from labgoblin import agent_policy, agent_worker, briefing, cli, coaching, research_tools, supervisor_memos as memos, worker
from labgoblin.protocol import Handoff, LaunchReceipt, canonical, fingerprint
from labgoblin.paths import environment_value
from labgoblin.state import _source_revision
from tests.test_dashboard import dump, get, serve
from tests.test_research_loop import campaign, charter, finish, owned, publish, tasks
from tests.test_state import receipt


def memo(packet, **changes):
    return {"turn_id": packet["turn_id"], "packet_id": packet["id"],
            "bottom_line": "The comparison is promising, but the research goal is not yet established.",
            "changes": "The candidate improved on the development comparison.",
            "uncertainty": "An independent held-out comparison is still needed.",
            "next_step": "Assess the next owned comparison.",
            "decision_needed": "None; research continues within existing permissions.",
            "references": [packet["content"]["sources"]["goal"]["id"]], **changes}


def accept(state, packet, envelope, value=None, *, handoff_changes=None, captured=None):
    handoff = Handoff.parse(json.loads(canonical({**asdict(Handoff(packet["turn_id"], packet["id"], "Internal finding",
                                            "Detailed internal rationale remains intact.", "Continue investigating",
                                            "continue", "Goal remains unresolved")), **(handoff_changes or {})})))
    result = canonical(asdict(handoff))
    Path(envelope.root).mkdir(parents=True, exist_ok=True)
    Path(envelope.metadata["result_path"]).write_bytes(result)
    path = Path(envelope.metadata["supervisor_memo_path"])
    if value is not None:
        path.write_bytes(value if isinstance(value, bytes) else canonical(value))
    captured = memos.capture(envelope) if captured is None else captured
    state.finish_launch(envelope.key.nonce, receipt(envelope, usage={"totalNanoAiu": 1_000_000_000},
        metadata={"result_capture": {"digest": fingerprint(asdict(handoff)), "bytes": len(result),
                                    "handoff_digest": fingerprint(asdict(handoff))},
                  "supervisor_memo_capture": captured}))
    state.released(envelope.key.grant_id)
    agent_worker.accept_result(state, packet["turn_id"])
    return handoff


def status(state):
    with state.db.read() as conn:
        return memos.status(conn)


def test_owned_memo_is_durable_idempotent_and_not_internal_memory(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    assert packet["content"]["supervisor_memo"]["due"]
    assert "supervisor_memo" not in packet["content"]["result_protocol"]["optional"]
    value = memo(packet)
    handoff = accept(state, packet, envelope, value)
    current = status(state)
    assert current["latest"]["content"] == value and not current["pending"]
    assert current["latest"]["metadata"]["turn_id"] == handoff.turn_id
    with state.db.read() as conn:
        rationale = conn.execute("SELECT body FROM sources WHERE id=(SELECT source_id FROM source_heads WHERE name='rationale')").fetchone()[0]
        assert json.loads(rationale)["rationale"] == handoff.rationale
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 0
    before = dump(state)
    agent_worker.accept_result(state, packet["turn_id"])
    Path(envelope.metadata["supervisor_memo_path"]).write_text("later edits", encoding="utf-8")
    assert status(state)["latest"]["content"] == value
    assert dump(state) == before
    next_packet = briefing.prepare(state)
    assert not next_packet["content"]["supervisor_memo"]["due"]
    assert next_packet["content"]["supervisor_memo"]["previous"]["source_id"] == current["latest"]["id"]


@pytest.mark.parametrize("problem", ["missing", "json", "shape", "oversize", "words", "foreign", "unknown-reference", "capture"])
def test_bad_memo_never_rejects_valid_science_and_remains_pending(tmp_path, problem):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = memo(packet)
    if problem == "missing":
        value = None
    elif problem == "json":
        value = b"{"
    elif problem == "shape":
        value = {"bottom_line": []}
    elif problem == "oversize":
        value["bottom_line"] = "x" * (memos.MAX_BYTES + 1)
    elif problem == "words":
        value["bottom_line"] = "word " * 301
    elif problem == "foreign":
        value["turn_id"] = "different-turn"
    elif problem == "unknown-reference":
        value["references"] = ["not-delivered"]
    accept(state, packet, envelope, value, captured={"status": "captured"} if problem == "capture" else None)
    current = status(state)
    assert current["latest"] is None and current["pending"]
    assert current["last_result"]["content"]["error"]
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM turns WHERE id=?", (packet["turn_id"],)).fetchone()[0] == "accepted"
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1
    assert state.credit_budget()["used"] == 1


def test_bad_science_never_publishes_a_good_memo(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    with pytest.raises(ValueError, match="evidence references"):
        accept(state, packet, envelope, memo(packet), handoff_changes={"research_updates": [{
            "id": "bad", "request_id": "bad", "kind": "hypothesis", "title": "Invalid assessment",
            "body": "Unsupported scientific finding", "outcome": "supported"}]})
    assert status(state)["latest"] is None


def test_assessments_retirement_strategy_and_blockers_coalesce_but_notes_do_not(tmp_path):
    config, state = campaign(tmp_path)
    note = publish(state)
    publish(state, "question", kind="question")
    assert status(state)["pending_count"] == 0
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, handoff_changes={"research_updates": [{
        "id": "claim", "request_id": "claim-v1", "kind": "hypothesis", "title": "Candidate",
        "body": "This branch is retired but not refuted.", "activity": "retired",
        "links": [{"target": note["source_id"], "relation": "context"}]}, {
        "id": "assessment", "request_id": "assessment-v1", "kind": "knowledge", "title": "Scoped result",
        "body": "Synthetic evidence remains inconclusive.", "outcome": "inconclusive",
        "links": [{"target": note["source_id"], "relation": "informs"}]}]})
    publish(state, "strategy", kind="reflection", body="A revised research strategy.")
    state.blocker("operational", "input", "An input is unavailable")
    state.blocker("operational", "input", "An input is unavailable")
    current = status(state)
    assert current["pending_count"] == 4
    assert any("not necessarily refutation" in row["content"]["reason"] for row in current["requests"])
    assert state.credit_budget()["used"] == 1


def test_memo_covers_its_own_assessment_but_not_a_late_operator_request(tmp_path):
    config, state = campaign(tmp_path)
    note = publish(state)
    packet, envelope = owned(config, state)
    late = memos.request(state, "Please explain the remaining uncertainty", request_id="late")
    accept(state, packet, envelope, memo(packet), handoff_changes={"research_updates": [{
        "id": "claim", "request_id": "claim-v1", "kind": "hypothesis", "title": "Assessed candidate",
        "body": "A scoped negative result.", "outcome": "refuted",
        "links": [{"target": note["source_id"], "relation": "refutes"}]}]})
    current = status(state)
    assert current["pending_count"] == 1 and current["requests"][0]["id"] == late["request_id"]
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, memo(packet))
    assert not status(state)["pending"]


@pytest.mark.parametrize("retrieve_rest", [False, True])
def test_unread_request_pages_cannot_be_silently_covered(tmp_path, retrieve_rest):
    config, state = campaign(tmp_path)
    for index in range(12):
        memos.request(state, f"Question {index}", request_id=str(index))
    packet, envelope = owned(config, state)
    assert packet["content"]["supervisor_memo"]["has_more"]
    if retrieve_rest:
        value = research_tools.read(state, {"operation": "memos", "offset": 8}, turn_id=packet["turn_id"])
        assert len(value["requests"]) == 4
    accept(state, packet, envelope, memo(packet))
    assert status(state)["pending_count"] == (0 if retrieve_rest else 4)


def test_own_turn_coverage_survives_later_partially_covered_memos(tmp_path):
    config, state = campaign(tmp_path)
    for index in range(18):
        memos.request(state, f"Question {index}", request_id=str(index))
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, memo(packet), handoff_changes={"research_updates": [{
        "id": "strategy", "request_id": "strategy", "kind": "reflection",
        "title": "Strategy changed", "body": "New strategy described by this memo."}]})
    assert status(state)["pending_count"] == 10
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, memo(packet))
    assert status(state)["pending_count"] == 2
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, memo(packet))
    assert not status(state)["pending"]


@pytest.mark.parametrize("retrieve", [False, True])
def test_truncated_human_request_requires_full_retrieval(tmp_path, retrieve):
    config, state = campaign(tmp_path)
    memos.request(state, "Important nuance. " * 60, request_id="long")
    packet, envelope = owned(config, state)
    assert packet["content"]["supervisor_memo"]["requests"][0]["truncated"]
    if retrieve:
        research_tools.read(state, {"operation": "memos"}, turn_id=packet["turn_id"])
    accept(state, packet, envelope, memo(packet))
    assert status(state)["pending"] is not retrieve


def test_reopening_keeps_historical_memo_and_requests_a_new_one(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, memo(packet))
    original = status(state)["latest"]
    state.close_admission("Test sealed lifecycle")
    from labgoblin.campaign import Campaign
    errors = []
    Campaign(config, state=state, ledger=SimpleNamespace())._advance_closure(errors, no_agent=True, admit=True)
    assert not errors
    state.control("reopen", "new-generation", state.campaign()["revision"])
    current = status(state)
    assert current["latest"] == original and current["first_memo_due"]
    assert current["pending"]
    with serve(config.config_path, chat=False) as base:
        assert b"Historical memo from an earlier generation" in get(base)[2]


def test_memos_and_requests_do_not_advance_research_or_coaching_basis(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, memo(packet))
    with state.db.read() as conn:
        original = (_source_revision(conn), coaching.basis(conn), coaching.snapshot(conn))
    due = state.reasoning_due()
    first = memos.request(state, "A human-readable update", request_id="request")
    assert memos.request(state, "A human-readable update", request_id="request") == first
    with state.db.read() as conn:
        assert (_source_revision(conn), coaching.basis(conn), coaching.snapshot(conn)) == original
        assert conn.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 1
    assert state.reasoning_due() == due


def test_scope_and_stale_authority_do_not_publish_current_memos(tmp_path):
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    child, child_launch = owned(config, state, "worker", task["id"])
    assert "supervisor_memo" not in child["content"]
    assert "supervisor_memo_path" not in child_launch.metadata
    finish(state, child, child_launch)
    assert status(state)["latest"] is None
    packet, envelope = owned(config, state)
    state.source("goal", b"Changed governing goal", origin="operator", head="goal", notify=True)
    accept(state, packet, envelope, memo(packet))
    assert status(state)["latest"] is None
    assert "stale" in status(state)["last_result"]["content"]["error"]


def test_root_assessment_of_a_worker_record_requests_a_supervisor_update(tmp_path):
    from labgoblin import research_memory
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    child, child_launch = owned(config, state, "worker", task["id"])
    record = research_memory.publish(state, {
        "id": "worker-claim", "request_id": "worker-claim", "kind": "hypothesis",
        "title": "Worker claim", "body": "A locally investigated claim."}, turn_id=child["turn_id"])
    finish(state, child, child_launch)
    assert status(state)["pending_count"] == 0
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, handoff_changes={"research_updates": [{
        "id": "worker-claim", "base": record["source_id"], "request_id": "root-assessment",
        "title": "Worker claim", "body": "The root retains uncertainty about the local finding.",
        "outcome": "inconclusive", "links": [
            {"target": packet["content"]["sources"]["goal"]["id"], "relation": "context"}]}]})
    assert status(state)["pending_count"] == 1


def test_provider_policy_captures_sidecar_separately_from_handoff(tmp_path, monkeypatch):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    Path(envelope.root).mkdir(parents=True, exist_ok=True)
    value = Handoff(packet["turn_id"], packet["id"], "Finding", "Rationale", "Next", "continue", "Reason")
    Path(envelope.metadata["result_path"]).write_bytes(canonical(asdict(value)))
    Path(envelope.metadata["supervisor_memo_path"]).write_bytes(canonical(memo(packet)))
    native = LaunchReceipt(envelope.key, envelope.digest, "completed", True, 1, returncode=0, executed=True)
    monkeypatch.setattr(agent_policy, "execute_spec", lambda *args, **kwargs: native)
    result = agent_policy.supervise(envelope)
    assert result.metadata["result_capture"]["handoff_digest"] == fingerprint(asdict(value))
    assert result.metadata["supervisor_memo_capture"]["content"] == memo(packet)
    assert "supervisor_memos.py" in envelope.metadata["runtime"]["files"]
    worker.verify_runtime(state, envelope)


def test_cli_sdk_requests_and_read_only_dashboard(tmp_path, monkeypatch, capsys):
    config, state = campaign(tmp_path)
    assert cli.main(["--project", str(tmp_path), "research", "request-memo", "--id", "operator-1",
                     "--reason", "Explain progress", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"]
    packet, envelope = owned(config, state)
    tool = next(t for t in research_tools.sdk_tools(state, envelope) if t.name == "research_read")
    response = asyncio.run(tool.handler(SimpleNamespace(tool_name=tool.name, arguments={"operation": "memos"})))
    assert response.result_type == "success"
    monkeypatch.setenv("LABGOBLIN_TURN_ID", packet["turn_id"])
    assert cli.main(["--project", str(tmp_path), "research", "request-memo", "--id", "bad",
                     "--reason", "Self request", "--json"]) == 1
    assert "Only the operator" in capsys.readouterr().out
    monkeypatch.delenv("LABGOBLIN_TURN_ID")
    accept(state, packet, envelope, memo(packet, changes="<script>not executable</script>"))
    current = status(state)
    before = dump(state)
    with serve(config.config_path, chat=False) as base:
        code, _, body = get(base)
        assert code == 200 and body.index(b"Supervisor update") < body.index(b"The research question")
        assert b"&lt;script&gt;not executable" in body and b"<script>not executable" not in body
        assert b"No recorded controller" in body and b"Detailed internal rationale remains intact" in body
        assert get(base, "/memos")[0] == 200
        assert get(base, "/memos?id=" + current["latest"]["id"])[0] == 200
        assert get(base, "/journal?entry=" + current["latest"]["id"])[0] == 200
    assert dump(state) == before


def test_legacy_frozen_packet_receipt_and_manifest_are_not_rewritten(tmp_path, monkeypatch):
    config, state = campaign(tmp_path)
    build = briefing._content

    def legacy(*args, **kwargs):
        result = build(*args, **kwargs)
        result.pop("supervisor_memo", None)
        return result

    monkeypatch.setattr(briefing, "_content", legacy)
    packet, envelope = owned(config, state)
    runtime = dict(envelope.metadata["runtime"])
    runtime["files"] = {name: digest for name, digest in runtime["files"].items() if name != "supervisor_memos.py"}
    runtime["id"] = fingerprint({key: runtime[key] for key in ("files", "bootstrap", "namespace")})
    previous = replace(envelope, metadata={**envelope.metadata, "runtime": runtime})
    worker.verify_runtime(state, previous)
    packet_bytes = Path(packet["path"]).read_bytes()
    finish(state, packet, envelope)
    assert Path(packet["path"]).read_bytes() == packet_bytes
    assert status(state)["latest"] is None and status(state)["last_result"] is None
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM turns WHERE id=?", (packet["turn_id"],)).fetchone()[0] == "accepted"
    required = replace(previous, metadata={**previous.metadata, "supervisor_memo_path": "required"})
    with pytest.raises(RuntimeError, match="manifest is incomplete"):
        worker.verify_runtime(state, required)


def test_publication_does_not_reset_progress_and_preflight_has_no_memo_side_effect(tmp_path, monkeypatch):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    original = memos.accept_in
    observed = []

    def checked(conn, *args, **kwargs):
        before = (_source_revision(conn), coaching.snapshot(conn))
        original(conn, *args, **kwargs)
        assert (_source_revision(conn), coaching.snapshot(conn)) == before
        observed.append(True)

    monkeypatch.setattr(memos, "accept_in", checked)
    handoff = Handoff(packet["turn_id"], packet["id"], "Finding", "Reason", "Next", "continue", "More research")
    before = dump(state)
    state.preflight_handoff(handoff, invocation_id=envelope.key.work_id)
    assert dump(state) == before and not observed
    accept(state, packet, envelope, memo(packet))
    assert observed == [True]


def test_latest_history_observer_and_request_conflicts(tmp_path):
    from labgoblin.dashboard_data import EvidenceReader, question_context
    config, state = campaign(tmp_path)
    memos.request(state, "Explain the result", request_id="stable")
    with pytest.raises(ValueError, match="different content"):
        memos.request(state, "Changed request", request_id="stable")
    for number in range(2):
        packet, envelope = owned(config, state)
        accept(state, packet, envelope, memo(packet, bottom_line=f"Update {number}"))
    before = dump(state)
    reader = EvidenceReader(config.config_path)
    result = reader.read("supervisor_memos", {})
    assert "Update 1" in json.dumps(result)
    assert question_context({"url": "/memos"})["url"] == "/memos"
    with state.db.read() as conn:
        history = memos.history(conn, limit=1)
        assert history["total"] == 2 and history["has_more"]
        assert memos.get(conn, memos.history(conn, offset=1)["memos"][0]["id"])["content"]["bottom_line"] == "Update 0"
    assert dump(state) == before


@pytest.mark.skipif(environment_value("BROWSER_TESTS") != "1", reason="Opt in to prepared Edge")
def test_browser_memo_first_history_and_mobile_are_read_only(tmp_path):
    from playwright.sync_api import sync_playwright, expect
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    accept(state, packet, envelope, memo(packet))
    before = dump(state)
    with serve(config.config_path, chat=False) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=environment_value("BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for width in (390, 768, 1440):
                page.set_viewport_size({"width": width, "height": 950})
                page.goto(base)
                expect(page.get_by_role("heading", name="Supervisor update", exact=True)).to_be_visible()
                expect(page.get_by_text(memo(packet)["bottom_line"], exact=True)).to_be_visible()
                assert not page.locator('details[data-key="Researcher\'s working context and lifecycle"]').evaluate("(node) => node.open")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.get_by_role("link", name="Memo history", exact=True).click()
                expect(page.get_by_role("heading", name="Memo history", exact=True)).to_be_visible()
                page.get_by_role("link", name=memo(packet)["bottom_line"], exact=True).click()
                expect(page.get_by_role("heading", name="Retained supervisor memo", exact=True)).to_be_visible()
            assert not errors
        finally:
            browser.close()
    assert dump(state) == before
