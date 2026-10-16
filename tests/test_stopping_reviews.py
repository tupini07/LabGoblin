import asyncio
from dataclasses import replace
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import pytest

from labgoblin import credits, guidance, provider, runtime
from labgoblin.storage import atomic_json, read_json
from tests.conftest import arm
from tests.test_simple_provider import FakeSession, exercise
from tests.test_simple_runtime import fixture_provider


def test_progress_resets_only_the_stalled_count_and_preserves_one_challenge(project, ledger):
    root, config, record = project
    envelope = arm((root, replace(config, max_stalled_reviews=2), record), ledger, coach=True)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "accept"}, {"decision": "continue"},
        {"decision": "continue", "progress": "meaningful", "progress_reason": "The inspected new control rules out the suspected cause."},
        {"decision": "continue"}, {"decision": "accept"},
    ])
    assert result == 0
    assert [review["stalled_reviews"] for review in outcome["reviews"]] == [0, 1, 0, 1, 2]
    researcher = clients[0].session
    assert len(researcher.prompts) == 5
    assert sum(guidance.CLOSING_CHALLENGE in prompt for prompt in researcher.prompts) == 1
    assert researcher.updates == [260, 250, 240, 230]
    assert all("Host execution facts" in client.session.prompts[0] for client in clients[1:])
    assert '"previous_review":' in clients[3].session.prompts[0]
    assert '"progress": "meaningful"' in clients[4].session.prompts[0]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 100 * credits.NANO
    assert len({session["session_id"] for session in outcome["sessions"]}) == 6


def test_meaningful_progress_can_exceed_ten_total_reviews_with_unlimited_credits(project, ledger):
    root, config, record = project
    envelope = arm((root, replace(config, max_ai_credits="unlimited"), record), ledger,
                   coach=True, allowance=None)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"},
        *[{"decision": "continue", "progress": "meaningful"} for _ in range(12)],
        {"decision": "accept"},
    ])
    assert result == 0 and len(outcome["reviews"]) == 14
    assert all(review["stalled_reviews"] == 0 for review in outcome["reviews"][:-1])
    assert len(clients[0].session.prompts) == 14 and clients[0].session.updates == []
    assert all(client.session.options["session_limits"] is None for client in clients)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 280 * credits.NANO


@pytest.mark.parametrize("missing", [False, True])
def test_late_review_unknown_usage_or_overshoot_cannot_fund_another_prompt(project, ledger, missing):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, {"decision": "continue", "progress": "meaningful"},
        {"decision": "continue", "progress": "meaningful", **(
            {"missing_usage": True} if missing else {"cost": 220})},
    ])
    assert result == 1 and len(clients) == 4
    assert len(clients[0].session.prompts) == 3
    assert outcome["status"] == ("review_unavailable" if missing else "budget_exhausted")
    if missing:
        assert runtime.usage_record(envelope, complete=True) is None
        assert not runtime.usage_record(envelope, complete=False)["sessions"][-1]["complete"]
    else:
        assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 270 * credits.NANO


def test_clear_repetition_can_checkpoint_before_the_ceiling(project, ledger):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, {"decision": "continue"},
        {"decision": "checkpoint", "checkpoint_reason": "stalled_reviews"},
    ])
    assert result == 1 and outcome["status"] == "checkpointed"
    assert outcome["checkpoint_reason"] == "stalled_reviews"
    assert outcome["reviews"][-1]["stalled_reviews"] == 2
    assert len(clients[0].session.prompts) == 3
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 60 * credits.NANO


def decide(root, args, **context):
    decision = {}
    tool = next(tool for tool in provider.coach_tools(root, decision, **context)
                if tool.name == "labgoblin_review_decision")
    result = asyncio.run(tool.handler(SimpleNamespace(arguments=args)))
    return result, decision


def review(choice="continue", **extra):
    return {"decision": choice, "reason": "The known-answer control is unresolved.",
            "question": "Does a distinct localization method recover the known solution?",
            "coverage": "Inspected report.md and research-notes.md; no independent rerun.", **extra}


@pytest.mark.parametrize("changes", [
    {"question": ""}, {"question": None}, {"dependency": "deadline"}, {"dependency": None},
])
def test_operator_stop_requires_a_concrete_human_question_and_dependency(project, changes):
    args = review("needs_operator", dependency="input")
    args.update(changes)
    result, decision = decide(project[0], args)
    assert result.result_type == "failure" and not decision


@pytest.mark.parametrize("dependency", ["permission", "input", "scope"])
def test_genuine_operator_dependency_is_valid_without_exhausting_reviews(project, dependency):
    result, decision = decide(project[0], review(
        "needs_operator", dependency=dependency, question="Which of the conflicting input-use constraints governs?",
        reason="The two supplied constraints prohibit each other's required source; all remaining work depends on resolving this."))
    assert result.result_type == "success"
    assert decision["dependency"] == dependency


@pytest.mark.parametrize("extra", [{}, {"progress": "yes", "progress_reason": "More files"},
                                  {"progress": "meaningful"}, {"progress": "none", "progress_reason": ""}])
def test_follow_up_requires_explicit_evidence_grounded_progress_assessment(project, extra):
    result, decision = decide(project[0], review(**extra), follow_up=True)
    assert result.result_type == "failure" and not decision


@pytest.mark.parametrize("progress,count", [("none", 0), ("meaningful", 4)])
def test_early_stall_cannot_replace_initial_work_or_override_progress(project, progress, count):
    result, decision = decide(project[0], review(
        "checkpoint", checkpoint_reason="stalled_reviews", progress=progress, progress_reason="Inspected the control."),
        follow_up=True, stalled_reviews=count)
    assert result.result_type == "failure" and not decision


def test_initial_review_cannot_invent_a_progress_reset(project):
    result, decision = decide(project[0], review(progress="meaningful", progress_reason="Some files changed."))
    assert result.result_type == "failure" and not decision


@pytest.mark.parametrize("seconds,accepted", [(61, False), (60, True), (45, True)])
def test_deadline_checkpoint_uses_actual_clock_not_report_claims(project, monkeypatch, seconds, accepted):
    monkeypatch.setattr(provider.time, "monotonic", lambda: 100)
    result, decision = decide(project[0], review("checkpoint", checkpoint_reason="deadline"),
                              deadline=100 + seconds)
    assert (result.result_type == "success") is accepted
    assert bool(decision) is accepted


def test_bad_decision_can_be_corrected_in_the_same_review(project):
    decision = {}
    tool = next(tool for tool in provider.coach_tools(project[0], decision)
                if tool.name == "labgoblin_review_decision")
    invalid = asyncio.run(tool.handler(SimpleNamespace(arguments=review("needs_operator"))))
    assert invalid.result_type == "failure" and not decision
    valid = asyncio.run(tool.handler(SimpleNamespace(arguments=review())))
    assert valid.result_type == "success" and decision["decision"] == "continue"
    assert asyncio.run(tool.handler(SimpleNamespace(arguments=review()))).result_type == "failure"


@pytest.mark.parametrize("boundary", ["quota", "cancelled", "timed_out"])
def test_progress_never_renews_hard_boundaries(project, ledger, monkeypatch, boundary):
    envelope = arm(project, ledger, coach=True)
    send = FakeSession.send
    close = provider.Session.close
    async def research(self, prompt):
        if len(self.prompts) == 2:
            self.script["quota"] = boundary == "quota"
        await send(self, prompt)
    async def finish(self):
        await close(self)
        if self.role == "coach-1":
            if boundary == "cancelled":
                (Path(envelope.root) / "cancel").write_text("operator stop")
            elif boundary == "timed_out":
                monkeypatch.setattr(provider.time, "monotonic", lambda: self.deadline - provider.GRACE_SECONDS)
    monkeypatch.setattr(FakeSession, "send", research)
    monkeypatch.setattr(provider.Session, "close", finish)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, {"decision": "continue", "progress": "meaningful"},
    ])
    assert result != 0
    assert outcome["status"] == {"quota": "budget_exhausted"}.get(boundary, boundary)
    assert len(clients) == 3
    assert outcome["reviews"][-1]["stalled_reviews"] == 0
    assert read_json(Path(envelope.root) / "usage.json")["complete"]


def test_late_review_failure_cannot_use_old_acceptance(project, ledger):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "accept"}, {"decision": "continue", "progress": "meaningful"}, {},
    ])
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert len(clients[0].session.prompts) == 3
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 60 * credits.NANO


@pytest.mark.parametrize("tamper", ["gap", "wrong_id", "too_many_prompts", "duplicate", "legacy"])
def test_expanded_usage_retains_exact_role_and_historical_bounds(project, ledger, tamper):
    envelope = arm(project, ledger, coach=True)
    result, _, _ = exercise(envelope, [
        {}, {"decision": "continue"}, {"decision": "continue"}, {"decision": "accept"}])
    assert result == 0
    path = Path(envelope.root) / "usage.json"
    value = read_json(path)
    if tamper == "gap":
        value["sessions"][-1]["role"] = "coach-3"
        value["sessions"][-1]["session_id"] = str(uuid.uuid5(uuid.UUID(envelope.id), "coach-3"))
    elif tamper == "wrong_id":
        value["sessions"][-1]["session_id"] = str(uuid.uuid4())
    elif tamper == "too_many_prompts":
        value["sessions"][0]["prompts"] = 5
    elif tamper == "duplicate":
        value["sessions"][-1] = value["sessions"][-2]
    else:
        envelope = replace(envelope, config={key: item for key, item in envelope.config.items()
                                              if key != "max_stalled_reviews"})
        value["envelope_digest"] = envelope.digest
    atomic_json(path, value)
    with pytest.raises(ValueError):
        runtime.usage_record(envelope, complete=True)


def test_old_two_review_usage_remains_readable_without_rewriting(project, ledger):
    envelope = arm(project, ledger, coach=True)
    _, _, _ = exercise(envelope, [{}, {"decision": "accept"}, {"decision": "accept"}])
    envelope = replace(envelope, config={key: item for key, item in envelope.config.items()
                                        if key != "max_stalled_reviews"})
    path = Path(envelope.root) / "usage.json"
    value = read_json(path)
    value["envelope_digest"] = envelope.digest
    atomic_json(path, value)
    before = path.read_bytes()
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 40 * credits.NANO
    assert path.read_bytes() == before


def test_owned_sdk_progress_reset_keeps_one_researcher_and_retained_computation(project, ledger, monkeypatch):
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(
        config, coach=True, max_stalled_reviews=2, timeout_seconds=180).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "progress_reviews")
    atomic_json(root / "fixture-provider.json", {
        "model_free": True, "root": str(root), "mode": "progress_reviews",
        "review_decisions": ["accept", "continue", "continue", "continue", "accept"],
        "review_progress": ["none", "meaningful", "none", "none"],
    })
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 100 * credits.NANO
    assert [s["prompts"] for s in result["usage"]["sessions"]] == [5, 1, 1, 1, 1, 1]
    directory = root / ".labgoblin" / "runs" / result["id"]
    snapshot = directory / "snapshot"
    assert [read_json(directory / f"review-{i}.json")["stalled_reviews"] for i in range(5)] == [0, 1, 0, 1, 2]
    assert read_json(snapshot / "artifacts" / "review-control-2.json") == {"baseline": 4, "shifted": 5}
    assert read_json(snapshot / "artifacts" / "review-control-3.json") == {"baseline": 4, "shifted": 6}
    researcher = result["usage"]["sessions"][0]["session_id"]
    prompts = [(snapshot / f"fixture-prompt-{researcher}-{i}.txt").read_text() for i in range(1, 6)]
    assert sum(guidance.CLOSING_CHALLENGE in prompt for prompt in prompts) == 1
    assert not ledger.rows() and runtime.status(root)["budget"]["remaining"] == 200


@pytest.mark.parametrize("boundary", ["deadline", "stalled_reviews"])
def test_owned_sdk_checkpoint_is_incomplete_and_visible_in_status(project, ledger, monkeypatch, boundary):
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(config, coach=True).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch)
    atomic_json(root / "fixture-provider.json", {
        "model_free": True, "root": str(root), "checkpoint_reason": boundary,
        "review_decisions": (["checkpoint"] if boundary == "deadline" else ["continue", "continue", "checkpoint"]),
    })
    result = runtime.run(root)
    assert result["status"] == "checkpointed", result
    assert result["metadata"]["provider"]["checkpoint_reason"] == boundary
    view = runtime.status(root)
    assert view["state"] == "checkpointed" and view["runs"][-1]["checkpoint_reason"] == boundary
    assert result["usage"]["totalNanoAiu"] == (20 if boundary == "deadline" else 60) * credits.NANO
    assert not ledger.rows()
