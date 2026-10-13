"""Strategic context and advice preserve researcher choice and accepted evidence."""

from dataclasses import asdict
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from labgoblin import briefing, coaching, research_tasks, research_tools
from labgoblin.campaign import Campaign
from labgoblin.config import parse_config
from labgoblin.protocol import AdmissionClosed, BudgetExhausted, Handoff, PACKET_BYTES, canonical, fingerprint, handoff_schema
from labgoblin.state import _source_revision
from tests.test_controller import fixture
from tests.test_research_loop import campaign, charter, finish, owned, publish
from tests.test_state import receipt


def progress(reference):
    return {"coverage": "Synthetic goal assessment, not independent certification.", "items": [
        {"deliverable": "A useful candidate", "status": "partial", "assessment": "Development evidence only.",
         "references": [reference]},
        {"deliverable": "Independent generalization", "status": "unresolved",
         "assessment": "Still untested.", "references": []}]}


def commitment(boundary="refinement", branch="candidate"):
    return {"branch": branch, "boundary": boundary, "goal_connection": "Independent generalization remains unresolved.",
            "decision_value": "Enable the independent comparison; retain this prerequisite only if it works.",
            "revisit_when": "The prerequisite works, or evidence shows another route is necessary."}


def advance(config, state, index, *, boundary="refinement", branch="candidate", evidence=True, **changes):
    event = state.event("evidence", {"observation_ids": []}, f"synthetic-result-{index}") if evidence else None
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, commitment=commitment(boundary, branch),
           evidence=[{"event_id": event, "disposition": "assessed", "reason": "Synthetic fixture result assessed."}]
           if event else [], **changes)
    return packet


def review_ready(config, state):
    advance(config, state, 0, boundary="branch")
    for index in range(1, 4):
        advance(config, state, index)
    with state.db.read() as conn:
        assert coaching.is_strategy(coaching.pending(conn))


def verdict(packet, **changes):
    return {"turn_id": packet["turn_id"], "packet_id": packet["id"],
            "episode_id": packet["content"]["coaching"]["id"], "decision": "advise",
            "reason": "The prerequisite is useful; maintain the original independent endpoint.",
            "questions": [], "references": [packet["content"]["coaching"]["proposal"]["trajectory"][0]["source_id"]],
            "limitations": "Accepted researcher claims, not independent recertification.", **changes}


def settle_advice(state, packet, envelope, value):
    state.finish_launch(envelope.key.nonce, receipt(envelope, usage={"totalNanoAiu": 1_000_000_000},
        metadata={"result_capture": {"result_digest": fingerprint(value)}}))
    state.released(envelope.key.grant_id)
    coaching.accept(state, value, invocation_id=envelope.key.work_id)


def test_goal_view_carries_accepted_assessment_without_repeated_paperwork(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    goal = packet["content"]["sources"]["goal"]["id"]
    finish(state, packet, envelope, goal_progress=progress(goal))
    second, envelope = owned(config, state)
    assert second["content"]["goal_progress"]["assessment"] == progress(goal)
    source_id = second["content"]["goal_progress"]["source_id"]
    finish(state, second, envelope)
    publish(state, "not-accepted", kind="reflection", body="All goals supposedly solved.")
    third = briefing.prepare(state)
    assert third["content"]["goal_progress"]["source_id"] == source_id
    assert third["content"]["goal_progress"]["assessment"]["items"][1]["status"] == "unresolved"
    assert third["content"]["goal_progress"]["applicability"] == "current"
    assert source_id in third["content"]["references"]


def test_goal_view_does_not_adopt_rejected_or_stale_assessments(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    goal = packet["content"]["sources"]["goal"]["id"]
    finish(state, packet, envelope, goal_progress=progress(goal))
    invalid, envelope = owned(config, state)
    with pytest.raises(ValueError, match="undelivered"):
        finish(state, invalid, envelope, goal_progress=progress("invented"))
    state.finish_turn(invalid["turn_id"], "Invalid fixture")
    stale, envelope = owned(config, state)
    state.source("goal", b"Changed operator objective", origin="operator", head="goal", notify=True)
    finish(state, stale, envelope, goal_progress=progress(goal))
    new = briefing.prepare(state)
    assert new["content"]["goal_progress"]["turn_id"] == packet["turn_id"]
    assert new["content"]["goal_progress"]["applicability"] == "historical"


def test_goal_view_is_bounded_and_never_displaces_authority(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    goal = packet["content"]["sources"]["goal"]["id"]
    value = progress(goal)
    value["items"][0]["assessment"] = "\u00e9" * 1000
    finish(state, packet, envelope, goal_progress=value, rationale="R" * 7000)
    new = briefing.prepare(state)
    assert len(canonical(new["content"])) <= PACKET_BYTES
    assert new["content"]["sources"]["goal"]["id"] == goal
    assert new["content"]["goal_progress"]["assessment"] == value
    assert new["content"]["sources"]["rationale"]["field_coverage"]["rationale"]["truncated"]


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value["items"][0].update(references=[]), "references"),
    (lambda value: value["items"][0].update(status="complete"), "status"),
    (lambda value: value.update(coverage="x" * 4096), "4096"),
    (lambda value: value.update(items=[]), "one to eight"),
])
def test_goal_view_shape_is_checked(mutation, match):
    value = progress("retained")
    mutation(value)
    with pytest.raises(ValueError, match=match):
        Handoff("turn", "packet", "summary", "rationale", "next", "continue", "reason", goal_progress=value)


def test_old_capture_digest_is_accepted_only_for_absent_added_fields():
    handoff = Handoff("turn", "packet", "summary", "rationale", "next", "continue", "reason")
    predecessor = {key: value for key, value in asdict(handoff).items()
                   if key not in ("stop_basis", "goal_progress", "commitment")}
    assert fingerprint(predecessor) in handoff.capture_digests()
    changed = Handoff.parse(json.loads(canonical({**predecessor, "goal_progress": progress("evidence")})))
    assert fingerprint(predecessor) not in changed.capture_digests()


def test_repeated_refinement_advice_can_agree_without_pivot_or_reflection(tmp_path):
    config, state = campaign(tmp_path)
    review_ready(config, state)
    packet, envelope = owned(config, state, "coach")
    assert packet["content"]["coaching"]["purpose"] == "strategy"
    assert packet["content"]["coaching"]["proposal"]["trigger"] == "repeated refinement"
    assert "necessary engineering" in packet["content"]["instructions"].lower()
    with state.db.read() as conn:
        before = _source_revision(conn)
    result = verdict(packet)
    coaching.preflight(state, result, invocation_id=envelope.key.work_id)
    settle_advice(state, packet, envelope, result)
    with state.db.read() as conn:
        assert coaching.pending(conn) is None
        assert _source_revision(conn) == before
        assert not conn.execute("SELECT 1 FROM events WHERE kind='reflection'").fetchone()
    assert state.campaign()["progress"] == "research"
    assert state.reasoning_due()
    next_packet = briefing.prepare(state)
    assert any(row["kind"] == "strategy_advice" for row in next_packet["content"]["events"])


@pytest.mark.parametrize("decision", ["accept_stop", "needs_operator", "request_reflection"])
def test_advisory_review_cannot_use_stopping_authority(tmp_path, decision):
    config, state = campaign(tmp_path)
    review_ready(config, state)
    packet, envelope = owned(config, state, "coach")
    with pytest.raises(ValueError, match="only advise"):
        coaching.preflight(state, verdict(packet, decision=decision, questions=["Which uncertainty remains?"]),
                           invocation_id=envelope.key.work_id)
    tool = next(tool for tool in research_tools.sdk_tools(state, envelope) if tool.name == "coach_decide")
    assert tool.parameters["properties"]["decision"]["enum"] == ["advise"]
    assert state.campaign()["progress"] == "research"


def test_advisory_failure_does_not_block_research_or_repeat_unchanged_review(tmp_path):
    config, state = campaign(tmp_path)
    review_ready(config, state)
    packet, envelope = owned(config, state, "coach")
    state.finish_launch(envelope.key.nonce, receipt(envelope, status="failed", returncode=1, reason="Adviser unavailable",
                                                  usage={"totalNanoAiu": 1_000_000_000}))
    state.released(envelope.key.grant_id)
    state.finish_turn(packet["turn_id"], "Synthetic unavailable adviser")
    with state.db.read() as conn:
        assert coaching.pending(conn) is None
        assert conn.execute("SELECT state FROM stop_episodes").fetchone()[0] == "unavailable"
    assert state.reasoning_due() and not state.campaign()["blockers"]
    advance(config, state, 4, evidence=False, boundary="milestone")
    advance(config, state, 5, boundary="milestone")
    with state.db.read() as conn:
        assert coaching.pending(conn) is None
    advance(config, state, 6, boundary="milestone")
    with state.db.read() as conn:
        assert coaching.is_strategy(coaching.pending(conn))


def test_stale_strategy_advice_cannot_block_or_steer_new_authority(tmp_path):
    config, state = campaign(tmp_path)
    review_ready(config, state)
    packet, envelope = owned(config, state, "coach")
    state.source("goal", b"New governing goal", origin="operator", head="goal", notify=True)
    with pytest.raises(AdmissionClosed, match="snapshot or authority"):
        coaching.preflight(state, verdict(packet), invocation_id=envelope.key.work_id)
    settle_advice(state, packet, envelope, verdict(packet, questions=["Stale question"]))
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM stop_episodes").fetchone()[0] == "superseded"
        event = json.loads(conn.execute("SELECT payload FROM events WHERE kind='strategy_advice'").fetchone()[0])
        assert event["stale"] and event["questions"] == []
    assert state.reasoning_due() and not state.campaign()["blockers"]


@pytest.mark.parametrize("enabled,evidence", [(True, False), (False, True)])
def test_rewording_without_evidence_or_disabled_coaching_never_spawns_adviser(tmp_path, enabled, evidence):
    config, state = campaign(tmp_path, coaching=enabled)
    for index in range(6):
        advance(config, state, index, evidence=evidence)
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stop_episodes").fetchone()[0] == 0


def test_advice_alone_does_not_schedule_research(tmp_path):
    _, state = campaign(tmp_path)
    with state.db.write() as conn:
        conn.execute("UPDATE campaign SET wake_after=(SELECT COALESCE(MAX(seq),0) FROM events)")
    state.event("strategy_advice", {"reason": "Nonbinding only"})
    assert not state.reasoning_due()


def test_failed_advice_alone_cannot_schedule_an_extra_research_turn(tmp_path):
    config, state = campaign(tmp_path)
    review_ready(config, state)
    packet, envelope = owned(config, state, "coach")
    with state.db.write() as conn:
        conn.execute("UPDATE campaign SET wake_after=(SELECT MAX(seq) FROM events)")
    state.finish_launch(envelope.key.nonce, receipt(envelope, status="failed", returncode=1, reason="Unavailable",
                                                  usage={"totalNanoAiu": 1_000_000_000}))
    state.released(envelope.key.grant_id)
    state.finish_turn(packet["turn_id"], "Synthetic advice failure")
    assert not state.reasoning_due() and not state.campaign()["blockers"]


@pytest.mark.parametrize("failure", ["preparation", "allowance"])
def test_controller_skips_unavailable_advice_without_closing_admission(tmp_path, monkeypatch, failure):
    config, state, ledger, raw = fixture(tmp_path)
    raw["campaign"].update(max_invocations=0, max_ai_credits=2000)
    raw["research"].update(coaching=True, max_parallel_agents=3)
    raw["agent"].update(transport="sdk", timeout_seconds=60, resources={"cpus": 1, "memory_mb": 128})
    config = parse_config(raw, config.config_path)
    with Campaign(config, state=state, ledger=ledger) as setup_controller:
        assert not setup_controller.step(no_agent=True, admit=False)["errors"]
    review_ready(config, state)
    prepare = briefing.prepare

    def guarded_prepare(state, kind="research", **kwargs):
        if kind == "coach" and failure == "preparation":
            raise ValueError("Synthetic advisory packet failure")
        return prepare(state, kind, **kwargs)

    def no_launch(self, turn):
        if turn["kind"] == "coach" and failure == "allowance":
            raise BudgetExhausted("ai_credits", "Synthetic optional-advice allowance failure")
        return None

    monkeypatch.setattr(briefing, "prepare", guarded_prepare)
    monkeypatch.setattr(Campaign, "_launch_turn", no_launch)
    with Campaign(config, state=state, ledger=ledger) as controller:
        result = controller.step()
        assert not result["errors"]
        assert any("Optional strategy" in item["reason"] for item in result["waiting"])
        with state.db.read() as conn:
            assert conn.execute("SELECT state FROM stop_episodes").fetchone()[0] == "unavailable"
        assert state.campaign()["generation_state"] == "open"
        assert not state.campaign()["blockers"]
        assert not controller.step()["errors"]
    assert not ledger.rows()


@pytest.mark.parametrize("reviewed", [False, True])
def test_stopping_supersedes_advice_and_keeps_its_separate_round_reserve(tmp_path, reviewed):
    config, state = campaign(tmp_path)
    review_ready(config, state)
    if reviewed:
        packet, envelope = owned(config, state, "coach")
        settle_advice(state, packet, envelope, verdict(packet))
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, disposition="finalize", stop_basis="goal_satisfied",
           stopping_criterion="The exact synthetic goal is assessed.",
           evidence=[{"event_id": packet["content"]["events"][0]["id"], "disposition": "assessed",
                      "reason": "Synthetic completion fixture",
                      "references": [packet["content"]["sources"]["goal"]["id"]]}])
    with state.db.read() as conn:
        episodes = list(conn.execute("SELECT * FROM stop_episodes ORDER BY created"))
        assert len(episodes) == 2 and not coaching.is_strategy(episodes[1])
        assert episodes[0]["state"] == ("continued" if reviewed else "superseded")
        assert episodes[1]["rounds"] == 0
    remaining = state.credit_budget()["remaining_nano"]
    packet, envelope = owned(config, state, "coach")
    assert packet["content"]["coaching"]["purpose"] == "stop"
    assert envelope.metadata["credit_allowance_nano"] == remaining - 90_000_000_000
    with pytest.raises(ValueError, match="Stopping review"):
        coaching.preflight(state, {"turn_id": packet["turn_id"], "packet_id": packet["id"],
                           "episode_id": packet["content"]["coaching"]["id"], "decision": "advise",
                           "reason": "Advice is not a stopping verdict.", "references": [], "questions": [],
                           "limitations": "Synthetic test"},
                           invocation_id=envelope.key.work_id)


@pytest.mark.parametrize("field", ["goal_progress", "commitment"])
def test_worker_cannot_publish_root_goal_or_commitment(tmp_path, field):
    config, state = campaign(tmp_path)
    task = research_tasks.create(state, charter())
    packet, envelope = owned(config, state, "worker", task["id"])
    assert "Workers must not publish goal_progress or commitment" in packet["content"]["instructions"]
    assert "In root research/reflection turns" in packet["content"]["instructions"]
    value = progress(packet["content"]["sources"]["goal"]["id"]) if field == "goal_progress" else commitment()
    with pytest.raises(ValueError, match="root"):
        finish(state, packet, envelope, **{field: value})
    with state.db.read() as conn:
        assert conn.execute("SELECT 1 FROM source_heads WHERE name='goal_progress'").fetchone() is None


def test_typed_completion_validates_writes_and_replays_without_accepting(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = {"summary": "A bounded finding", "rationale": "Evidence and limits", "next_step": "Assess next result",
             "disposition": "continue", "reason": "Goal gaps remain",
             "goal_progress": progress(packet["content"]["sources"]["goal"]["id"])}
    with state.db.read() as conn:
        before = list(conn.iterdump())
    tool = next(tool for tool in research_tools.sdk_tools(state, envelope) if tool.name == "research_complete")
    result = asyncio.run(tool.handler(SimpleNamespace(tool_name=tool.name, arguments=value)))
    assert result.result_type == "success"
    recorded = json.loads(result.text_result_for_llm)
    assert recorded["recorded"] and recorded["accepted"] is False
    path = Path(envelope.metadata["result_path"])
    original = path.read_bytes()
    assert json.loads(original)["turn_id"] == packet["turn_id"]
    assert research_tools.complete(state, value, turn_id=packet["turn_id"], invocation_id=envelope.key.work_id)["recorded"]
    with pytest.raises(ValueError, match="undelivered"):
        research_tools.complete(state, {**value, "goal_progress": progress("invented")},
                                turn_id=packet["turn_id"], invocation_id=envelope.key.work_id)
    assert path.read_bytes() == original
    with state.db.read() as conn:
        assert list(conn.iterdump()) == before
    assert tool.parameters["properties"] == handoff_schema(owned=True)["properties"]
    assert "turn_id" not in tool.parameters["required"]
    state.finish_launch(envelope.key.nonce, receipt(envelope, usage={"totalNanoAiu": 1_000_000_000},
        metadata={"result_capture": {"handoff_digest": fingerprint(json.loads(original))}}))
    state.released(envelope.key.grant_id)
    state.accept_handoff(Handoff.parse(json.loads(original)), invocation_id=envelope.key.work_id)
    assert briefing.prepare(state)["content"]["goal_progress"]["assessment"] == value["goal_progress"]


def test_completion_cannot_fix_wrong_identity_or_bypass_paused_owner(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = {"summary": "Finding", "rationale": "Why", "next_step": "Next", "disposition": "continue", "reason": "More"}
    with pytest.raises(ValueError, match="turn_id differs"):
        research_tools.complete(state, {**value, "turn_id": "someone-else"},
                                turn_id=packet["turn_id"], invocation_id=envelope.key.work_id)
    state.control("pause", "pause", state.campaign()["revision"])
    with pytest.raises(AdmissionClosed, match="authority changed"):
        research_tools.complete(state, value, turn_id=packet["turn_id"], invocation_id=envelope.key.work_id)
    assert not Path(envelope.metadata["result_path"]).exists()


def test_completion_preserves_equivalent_manual_bytes_and_rejects_replacement(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = {"turn_id": packet["turn_id"], "packet_id": packet["id"], "summary": "Finding",
             "rationale": "Why", "next_step": "Next", "disposition": "continue", "reason": "More"}
    path = Path(envelope.metadata["result_path"])
    body = json.dumps(value, indent=4).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    assert research_tools.complete(state, value, turn_id=packet["turn_id"],
                                   invocation_id=envelope.key.work_id)["recorded"]
    assert path.read_bytes() == body
    with pytest.raises(ValueError, match="different handoff"):
        research_tools.complete(state, {**value, "summary": "Different finding"}, turn_id=packet["turn_id"],
                                invocation_id=envelope.key.work_id)
    assert path.read_bytes() == body
