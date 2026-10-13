"""Admission uses real commitments; explicit provider limits never buy shrinking retries."""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
import tomllib

from copilot.generated.session_events import SessionEvent
import pytest

from labgoblin import credits
from labgoblin.campaign import Campaign
from labgoblin.config import parse_config
from labgoblin.copilot_worker import drive
from labgoblin.protocol import Handoff, UncertainExecution, canonical, fingerprint
from labgoblin.evidence import read_json
from tests.test_copilot_worker import Client, Session
from tests.test_research_loop import campaign, finish, owned
from tests.test_state import receipt


def test_lone_researcher_is_not_divided_by_idle_parallel_capacity(tmp_path):
    config, state = campaign(tmp_path, max_parallel_agents=6)
    _, envelope = owned(config, state)
    assert envelope.metadata["credit_allowance_nano"] == (2000 - 120) * credits.NANO
    assert state.credit_budget()["finishing"]["sample_count"] == 0
    assert state.credit_budget()["finishing"]["guarantee"] is False


def test_observed_completion_reserve_is_uncertain_not_a_scientific_threshold(tmp_path):
    config, state = campaign(tmp_path)
    for spend in (100, 200, 150):
        packet, envelope = owned(config, state)
        finish(state, packet, envelope, spent=spend)
    policy = state.credit_budget()["finishing"]
    assert policy["sample_count"] == 3 and policy["reserve"] == 150
    packet, envelope = owned(config, state)
    assert envelope.metadata["credit_allowance_nano"] == (2000 - 450 - 90 - 150) * credits.NANO
    assert packet["content"]["budgets"]["ai_credits"]["finishing"] == policy


def test_unmetered_history_is_disclosed_not_used_as_a_zero_cost_or_final_bill(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = Handoff(packet["turn_id"], packet["id"], "Finding", "Why", "Next", "continue", "More")
    state.finish_launch(envelope.key.nonce, receipt(envelope, usage={"cost": 1},
        metadata={"result_capture": {"handoff_digest": fingerprint(asdict(value))}}))
    state.released(envelope.key.grant_id)
    state.accept_handoff(value, invocation_id=envelope.key.work_id)
    budget = state.credit_budget()
    assert budget["finishing"]["sample_count"] == 0
    assert budget["finishing"]["excluded_history"][0]["invocation_id"] == envelope.key.work_id
    assert budget["uncovered_unknown"] == 1 and budget["remaining"] is None
    with pytest.raises(UncertainExecution):
        credits.available(budget)


def test_even_completion_history_uses_the_median_not_the_upper_middle(tmp_path):
    config, state = campaign(tmp_path)
    for spend in (40, 60, 140, 180):
        packet, envelope = owned(config, state)
        finish(state, packet, envelope, spent=spend)
    policy = state.credit_budget()["finishing"]
    assert policy["sample_count"] == 4 and policy["reserve_nano"] == 100 * credits.NANO


def test_new_configuration_resets_planning_samples_not_reported_accounting(tmp_path):
    config, state = campaign(tmp_path)
    for spend in (100, 200, 150):
        packet, envelope = owned(config, state)
        finish(state, packet, envelope, spent=spend)
    raw = tomllib.loads(Path(config.config_path).read_text(encoding="utf-8"))
    raw["agent"]["timeout_seconds"] += 60
    candidate = parse_config(raw, config.config_path)
    with Campaign(candidate, state=state) as controller:
        state.configure(candidate, controller=controller.handle)
    budget = state.credit_budget()
    assert budget["used"] == 450 and budget["unknown"] == 0
    assert budget["finishing"]["sample_count"] == 0 and budget["finishing"]["reserve"] == 30


@pytest.mark.parametrize("explicit,valid", [(True, False), (False, False), (True, True), (False, True)])
@pytest.mark.parametrize("event_kind", ["session.error", "tool.execution_complete", "session.info"])
def test_explicit_sdk_limit_is_preserved_and_only_missing_completion_blocks(tmp_path, explicit, valid, event_kind):
    config, state = campaign(tmp_path, max_ai_credits=190)
    _, envelope = owned(config, state)
    assert envelope.metadata["credit_allowance_nano"] == 70 * credits.NANO
    handoff = Handoff(envelope.metadata["turn_id"], envelope.metadata["packet_id"], "Finding", "Interpretation",
                      "Next useful action", "continue", "More evidence needed")
    body = canonical(asdict(handoff))
    if valid:
        Path(envelope.metadata["result_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(envelope.metadata["result_path"]).write_bytes(body)
    limit_info = SessionEvent.from_dict({
        "id": envelope.key.nonce, "timestamp": "2026-10-13T19:00:00Z", "parentId": None,
        "type": "session.info", "data": {"infoType": "session_limits", "message": (
            "Session limit reached. Increase or unset the session limit to continue."
            if explicit else "Session limits: 62.06/106.06 AI credits used.")}}) if event_kind == "session.info" else None

    async def scenario(session):
        if event_kind == "session.info":
            session.handler(limit_info)
        else:
            session.event("tool.execution_start", tool_call_id="owned-tool")
            session.event(event_kind, tool_call_id="owned-tool", message="Synthetic provider error",
                          error={"code": "session_limits_exhausted" if explicit else "ordinary_tool_error"})
        session.event("session.idle")

    session = Session(scenario, metrics=[0, 71 * credits.NANO])
    expected_code = 1 if event_kind == "session.error" and (not explicit or not valid) else 0
    assert asyncio.run(drive(envelope, Client(session), time.monotonic() + 60, poll_seconds=0.001)) == expected_code
    result = read_json(Path(envelope.root) / "launches" / envelope.key.nonce / "sdk-result.json")
    assert bool(result["session_limit"]) is explicit
    if explicit and event_kind == "session.info":
        assert result["session_limit"]["event"] == event_kind
        assert result["session_limit"]["tool_call_id"] == ""
    accepted = valid and result["status"] == "completed"
    capture = ({"digest": fingerprint(asdict(handoff)), "bytes": len(body),
                "handoff_digest": fingerprint(asdict(handoff))} if valid else {"error": "Missing owned result"})
    state.finish_launch(envelope.key.nonce, receipt(envelope, status=result["status"], returncode=expected_code,
        reason=result["reason"], usage={"totalNanoAiu": 71 * credits.NANO},
        metadata={"sdk": result, "result_capture": capture, "credit_limit_reached": True}))
    state.released(envelope.key.grant_id)
    errors = []
    Campaign(state=state)._finish_turn(errors)
    with state.db.read() as conn:
        turn = conn.execute("SELECT state FROM turns").fetchone()[0]
        checkpoint = conn.execute("SELECT body FROM sources WHERE kind='operational_checkpoint'").fetchone()
        assert turn == ("accepted" if accepted else "failed")
    if explicit and not valid:
        assert errors and checkpoint
        assert json.loads(checkpoint["body"])["scientifically_accepted"] is False
        assert state.campaign()["blockers"][0]["category"] == "provider_limit"
        assert not state.reasoning_due()
        state.event("evidence", {"observation_ids": []})
        assert not state.reasoning_due()
        with pytest.raises(ValueError, match="recovery blockers"):
            state.control("run", "not-resume", state.campaign()["revision"])
        state.blocker("unrelated-recovery", "recovery", "Independent recovery still required")
        with pytest.raises(ValueError, match="recovery blockers"):
            state.control("resume", "blocked-resume", state.campaign()["revision"])
        assert any(item["category"] == "provider_limit" for item in state.campaign()["blockers"])
        state.resolve_blocker("unrelated-recovery")
        state.control("resume", "operator-resume", state.campaign()["revision"])
        assert not state.campaign()["blockers"]
    else:
        assert not checkpoint and not state.campaign()["blockers"]
    assert state.credit_budget()["used"] == 71
    assert state.credit_budget()["unknown"] == 0


@pytest.mark.parametrize("quota_first", [False, True])
def test_quota_error_cannot_mask_an_unrelated_session_failure(tmp_path, quota_first):
    config, state = campaign(tmp_path)
    _, envelope = owned(config, state)
    handoff = Handoff(envelope.metadata["turn_id"], envelope.metadata["packet_id"], "Finding", "Interpretation",
                      "Next useful action", "continue", "More evidence needed")
    target = Path(envelope.metadata["result_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical(asdict(handoff)))

    async def scenario(session):
        errors = [("Independent provider failure", "provider_failed"),
                  ("Session credits exhausted", "session_limits_exhausted")]
        for message, code in reversed(errors) if quota_first else errors:
            session.event("session.error", message=message, error={"code": code})
        session.event("session.idle")

    session = Session(scenario)
    assert asyncio.run(drive(envelope, Client(session), time.monotonic() + 60, poll_seconds=0.001)) == 1
    result = read_json(Path(envelope.root) / "launches" / envelope.key.nonce / "sdk-result.json")
    assert result["session_limit"]["code"] == "session_limits_exhausted"
    assert result["reason"] == "Independent provider failure"
    assert session.disconnected


@pytest.mark.parametrize("info_type,message", [
    ("other", "Session limit reached. Increase or unset the session limit to continue."),
    ("session_limits", "Session limits: 62.06/106.06 AI credits used."),
    ("session_limits", "Tool calls disabled; one final response remains."),
])
def test_only_qualified_reached_info_is_exhaustion(tmp_path, info_type, message):
    config, state = campaign(tmp_path)
    _, envelope = owned(config, state)

    async def scenario(session):
        session.event("session.info", info_type=info_type, message=message)
        session.event("session.idle")

    assert asyncio.run(drive(envelope, Client(Session(scenario)), time.monotonic() + 60,
                             poll_seconds=0.001)) == 0
    result = read_json(Path(envelope.root) / "launches" / envelope.key.nonce / "sdk-result.json")
    assert not result["session_limit"]
