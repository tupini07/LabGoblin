import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from labgoblin import cli, consultation, credits, guidance, provider, runtime
from labgoblin.config import Config
from labgoblin.storage import atomic_json, hash_file, read_json
from tests.conftest import arm
from tests.test_simple_provider import FakeClient, FakeSession, exercise
from tests.test_simple_runtime import fixture_provider


def enabled(project, ledger, *, allowance=300 * credits.NANO):
    root, config, record = project
    config = replace(config, coach=True, consultant=True, closure_critic=True,
                     max_ai_credits=allowance / credits.NANO if allowance is not None else "unlimited")
    return arm((root, config, record), ledger, coach=True, consultant=True, allowance=allowance)


def adjudicator(choice="accept", **kwargs):
    return {"decision": choice, "critique_response":
         "The critique identifies a control gap affecting the original goal; checked retained evidence, not reviewer agreement.",
         **kwargs}


def closing(critique=None, choice="accept", **kwargs):
    return [{"decision": "accept", "reason": "SECRET PRELIMINARY COACH APPROVAL"},
            critique or {}, adjudicator(choice, **kwargs)]


@pytest.mark.parametrize("seconds, critic_seconds", [(3600, 2400), (86400, 7200)])
def test_critic_deadline_is_enforced_without_routine_model_countdowns(project, ledger, monkeypatch, seconds, critic_seconds):
    envelope = enabled(project, ledger)
    ask = provider.Session.ask
    deadlines = {}

    async def observe(session, prompt, checkpoint, **kwargs):
        deadlines[session.role] = session.deadline - time.monotonic()
        if session.role.startswith("consult-"):
            audit = session.client.session.options["hooks"]["on_post_tool_use"].__self__
            assert not audit.count_limited
        return await ask(session, prompt, checkpoint, **kwargs)

    monkeypatch.setattr(provider.Session, "ask", observe)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing()], seconds=seconds)
    assert result == 0, outcome
    assert critic_seconds - 5 < deadlines["consult-0"] <= critic_seconds
    assert seconds - 5 < deadlines["coach-1"] <= seconds
    request = read_json(Path(envelope.root) / "sessions" / "consult-0" / "request.json")
    assert critic_seconds - 5 < request["review_seconds_remaining_including_shutdown"] <= critic_seconds
    assert request["shutdown_reserve_seconds"] == 30
    assert request["adjudication_reserve_seconds"] == 600
    assert request["researcher_response_reserve_seconds"] == 600
    assert request["response_deadline_unix_seconds"] == request["review_deadline_unix_seconds"] - 30
    for client in clients:
        for prompt in client.session.prompts:
            assert "seconds_remaining" not in prompt
            assert "Host response seconds" not in prompt
            assert "shutdown_reserve_seconds" not in prompt
            assert "review_deadline_unix_seconds" not in prompt
    assert seconds - 605 < deadlines["researcher"] <= seconds - 600
    assert seconds - 5 < deadlines["adjudicator-1"] <= seconds


def test_critic_uses_shared_credits_and_returns_unused_allowance(project, ledger):
    envelope = enabled(project, ledger, allowance=10000 * credits.NANO)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing({"cost": 500})], seconds=86400)
    assert result == 0, outcome
    assert clients[3].session.options["session_limits"]["max_ai_credits"] == 9900
    assert clients[2].session.updates == [40]
    root = Path(envelope.root) / "sessions" / "coach-1"
    assert read_json(root / "closure-allocation.json")["critic_allowance_nano"] == 9900 * credits.NANO
    assert read_json(root / "closure-settlement.json")["remaining_campaign_nano"] == 9460 * credits.NANO
    assert len(list(root.glob("allowance-*.json"))) == 1
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 560 * credits.NANO


def test_critic_has_no_evidence_operation_cap_but_retains_per_read_bounds(project, tmp_path):
    root, _, _ = project
    (root / "view.png").write_bytes(PNG)
    audit = consultation.EvidenceAudit(tmp_path, count_limited=False)
    tools = {tool.name: tool for tool in consultation.evidence_tools(root, audit=audit, images=True)}
    for index in range(consultation.MAX_READS + 1):
        audit.record("labgoblin_read_evidence", {"path": "notes"}, {"text": str(index)})
    image = tools["labgoblin_read_image"]
    for _ in range(9):
        result = asyncio.run(image.handler(SimpleNamespace(
            tool_name=image.name, arguments={"path": "view.png"})))
        assert result.result_type == "success"
        assert "response_seconds_remaining" not in json.loads(result.text_result_for_llm)
    assert audit.images == 9 and audit.count == consultation.MAX_READS + 10
    assert "eight" not in image.description
    request = {"toolName": "web_fetch", "toolArgs": {"url": "https://example.org"},
               "toolResult": {"text": "Retained public evidence"}}
    assert audit.before_web(request, {}) == {"permissionDecision": "allow"}
    assert audit.after_web(request, {}) == {}
    assert audit.pending_web == 0
    with pytest.raises(ValueError, match="retention bound"):
        audit.record("labgoblin_read_evidence", {}, {"text": "x" * (128 * 1024)})


def test_critic_timeout_is_not_the_campaign_deadline_and_never_accepts(project, ledger, monkeypatch):
    envelope = enabled(project, ledger)
    ask = provider.Session.ask

    async def time_out_critic(session, prompt, checkpoint, **kwargs):
        if session.role != "consult-0":
            return await ask(session, prompt, checkpoint, **kwargs)
        assert session.deadline - time.monotonic() > 7100

        def expire():
            checkpoint()
            session.deadline = time.monotonic() + provider.GRACE_SECONDS - 1

        return await ask(session, prompt, expire)

    monkeypatch.setattr(provider.Session, "ask", time_out_critic)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing({"wait": True})], seconds=86400)
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert "consult-0 session deadline" in outcome["reason"]
    assert clients[-1].session.aborted and all(client.stopped for client in clients)
    assert len(clients[2].session.prompts) == 1
    assert not (Path(envelope.root) / "review-1.json").exists()
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 50 * credits.NANO


def test_failed_allowance_return_never_starts_adjudication(project, ledger, monkeypatch):
    envelope = enabled(project, ledger)
    update = provider.Session.set_allowance

    async def reject_return(session, allowance):
        if session.role == "researcher" and session.turns == 2:
            session.client.session.script["update_error"] = True
        return await update(session, allowance)

    monkeypatch.setattr(provider.Session, "set_allowance", reject_return)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing()], seconds=1800)
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert "did not apply" in outcome["reason"]
    assert len(clients[2].session.prompts) == 1 and all(client.stopped for client in clients)
    assert not (Path(envelope.root) / "review-1.json").exists()
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 50 * credits.NANO


def test_opt_in_configuration_and_init_surface():
    assert Config().closure_critic is False
    for values in ({}, {"coach": True}, {"consultant": True}):
        with pytest.raises(ValueError, match="requires both"):
            Config(closure_critic=True, **values)
    config = Config(coach=True, consultant=True, closure_critic=True)
    assert Config.parse(config.to_dict()) == config
    assert "closure_critic = true" in config.toml()
    assert cli.parser().parse_args(["init", "--coach", "--consultant", "--closure-critic"]).closure_critic
    with pytest.raises(ValueError):
        Config(coach=True, consultant=True, closure_critic="yes")


@pytest.mark.parametrize("answer", [
    "No compelling objection survives the checks; required controls and endpoint are supported.",
    "Speculative objection: another arbitrary seed might improve an already adequate result.",
])
def test_critique_reaches_researcher_before_fresh_adjudication(project, ledger, answer):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "accept"}, *closing({"answer": answer})], seconds=1800)
    assert result == 0, outcome
    assert [len(client.session.prompts) for client in clients] == [3, 1, 1, 1, 1]
    assert clients[3].session.options["system_message"]["content"] == guidance.CLOSURE_CRITIC
    assert "SECRET PRELIMINARY COACH APPROVAL" not in clients[3].session.prompts[0]
    assert "SECRET PRELIMINARY COACH APPROVAL" not in clients[4].session.prompts[0]
    assert answer in clients[0].session.prompts[2] and answer in clients[4].session.prompts[0]
    assert clients[2].session.options["session_id"] != clients[4].session.options["session_id"]
    assert "critique_response" in outcome["reviews"][-1]
    assert len(outcome["reviews"]) == 2 and outcome["reviews"][-1]["stalled_reviews"] == 1
    assert all(client.stopped for client in clients)
    root = Path(envelope.root)
    assert read_json(root / "review-1-proposal.json")["reason"] == "SECRET PRELIMINARY COACH APPROVAL"
    request = read_json(root / "sessions" / "consult-0" / "request.json")
    assert request["purpose"] == "closure_critique" and request["researcher_stopping_response"]
    assert "SECRET PRELIMINARY COACH APPROVAL" not in json.dumps(request)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 70 * credits.NANO
    tools = {tool.name for tool in clients[3].session.options["tools"]}
    assert tools == {"labgoblin_read_evidence", "labgoblin_list_evidence", "labgoblin_read_image"}
    assert {tool.name for tool in clients[4].session.options["tools"]} == tools | {"labgoblin_review_decision"}


def test_objection_returns_to_same_researcher_without_recursive_critic(project, ledger):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"},
        *closing({"answer": "The unused interior-point control can refute the all-real claim."}, "continue"),
        *closing({"answer": "The new exact counterexample answers the original question."}, progress="meaningful"),
    ], seconds=1800)
    assert result == 0, outcome
    assert len(clients[0].session.prompts) == 5
    assert "closure_critique" in clients[0].session.prompts[2]
    assert "unused interior-point" in clients[4].session.prompts[0]
    assert [review["decision"] for review in outcome["reviews"]] == ["continue", "continue", "accept"]
    assert [review["stalled_reviews"] for review in outcome["reviews"]] == [0, 1, 0]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 120 * credits.NANO


@pytest.mark.parametrize("failure", [
    {"answer": ""}, {"answer": "x" * (consultation.MAX_BRIEF + 1)},
    {"startup_error": True}, {"missing_usage": True}, {"cost": 500},
])
def test_failed_critic_never_falls_back_to_preliminary_acceptance(project, ledger, failure):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing(failure)], seconds=1800)
    assert result == 1 and outcome["status"] != "completed"
    assert len(clients[2].session.prompts) == 1
    assert len(outcome["reviews"]) == 1
    assert (Path(envelope.root) / "review-1-proposal.json").exists()
    assert not (Path(envelope.root) / "review-1.json").exists()
    if failure.get("missing_usage"):
        assert not (Path(envelope.root) / "usage.json").exists()
    else:
        assert runtime.usage_record(envelope, complete=True)
    assert all(client.stopped for client in clients)


@pytest.mark.parametrize("turn", [
    {"no_decision": True},
    {"critique_response": "", "decision_result_type": "failure"},
])
def test_missing_adjudication_is_not_completion(project, ledger, turn):
    envelope = enabled(project, ledger)
    result, _, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing(**turn)], seconds=1800)
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 70 * credits.NANO


def test_ordinary_adviser_spending_is_counted_before_idle_closure_allocation(project, ledger):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {"consult": [{"question": "Check a useful assumption."}]}, {"cost": 30},
        {"decision": "continue"}, *closing(),
    ], seconds=1800)
    assert result == 0, outcome
    allocation = read_json(Path(envelope.root) / "sessions" / "coach-1" / "closure-allocation.json")
    assert allocation["critic_allowance_nano"] == 170 * credits.NANO
    assert allocation["protected_nano"] == 30 * credits.NANO
    assert allocation["reported_total_nano"] == 70 * credits.NANO
    assert clients[3].session.updates == [40]
    assert clients[4].session.options["session_limits"]["max_ai_credits"] == 170
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 100 * credits.NANO


@pytest.mark.parametrize("failure", ["timeout", "provider_error"])
def test_settled_failed_adviser_does_not_block_later_closure(project, ledger, monkeypatch, failure):
    envelope = enabled(project, ledger)
    ask = provider.Session.ask

    async def interrupted_advice(session, prompt, checkpoint, **kwargs):
        if session.role != "consult-0":
            return await ask(session, prompt, checkpoint, **kwargs)

        def interrupt():
            checkpoint()
            if failure == "timeout":
                session.deadline = time.monotonic() + provider.GRACE_SECONDS - 1
            else:
                session.failure = "Deliberate adviser failure while waiting"

        return await ask(session, prompt, interrupt)

    monkeypatch.setattr(provider.Session, "ask", interrupted_advice)
    result, clients, outcome = exercise(envelope, [
        {"consult": [{"question": "Check an earlier assumption."}]}, {"wait": True},
        {"decision": "continue"}, *closing(),
    ], seconds=1800)
    assert result == 0, outcome
    assert clients[1].session.aborted and all(client.stopped for client in clients)
    failed = Path(envelope.root) / "sessions" / "consult-0"
    assert read_json(failed / "response.json")["status"] == "failed"
    assert not (failed / "settled-prompt-1.json").exists()
    assert read_json(failed / "usage.json")["complete"]
    assert [len(client.session.prompts) for client in clients] == [3, 1, 1, 1, 1, 1]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 80 * credits.NANO


@pytest.mark.parametrize("failure", ["abort", "metrics", "disconnect", "stop"])
def test_failed_adviser_close_does_not_clear_waiting(project, ledger, monkeypatch, failure):
    envelope = enabled(project, ledger)
    client = FakeClient({"wait": True})

    async def check():
        session = provider.Session(envelope, "consult-0", "interrupted-adviser",
                                   30 * credits.NANO, client, time.monotonic() + 600, purpose="advice")
        await session.start()

        def interrupt():
            raise provider.StopRequested("failed", "Interrupted adviser")

        with pytest.raises(provider.StopRequested, match="Interrupted adviser"):
            await session.ask("Check an earlier assumption.", interrupt)
        assert session.waiting

        async def fail():
            raise RuntimeError(f"Deliberate {failure} failure")

        if failure == "metrics":
            monkeypatch.setattr(client.session.rpc.usage, "get_metrics", fail)
        elif failure == "stop":
            monkeypatch.setattr(client, "stop", fail)
        else:
            monkeypatch.setattr(client.session, failure, fail)
        with pytest.raises(RuntimeError, match=f"Deliberate {failure} failure"):
            await session.close()
        assert session.waiting and session.events.closed and client.stopped
        assert session.complete is (failure in ("disconnect", "stop"))

    asyncio.run(check())


@pytest.mark.parametrize("boundary", ["time", "credits", "operator", "cancel"])
def test_boundaries_do_not_start_or_retry_a_critic(project, ledger, boundary):
    envelope = enabled(project, ledger, allowance=(90 if boundary == "credits" else 300) * credits.NANO)
    scripts = [{}, {"decision": "continue"}, closing()[0]]
    if boundary == "operator":
        scripts[2] = {"decision": "needs_operator"}
    if boundary == "cancel":
        scripts[2] = {"wait": True, "cancel": str(Path(envelope.root) / "cancel")}
    result, clients, outcome = exercise(envelope, scripts, seconds=600 if boundary == "time" else 1800)
    assert result != 0 and outcome["status"] != "completed"
    assert len(clients) <= 3 and not list((Path(envelope.root) / "sessions").glob("consult-*"))
    if boundary == "time":
        assert "Insufficient time for closure criticism" in outcome["reason"]
    assert runtime.usage_record(envelope, complete=True)


def test_unlimited_project_has_no_critic_credit_cap_or_unlimited_coach_updates(project, ledger):
    envelope = enabled(project, ledger, allowance=None)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing()], seconds=1800)
    assert result == 0, outcome
    assert clients[2].session.updates == []
    assert clients[3].session.options["session_limits"] is None


@pytest.mark.parametrize("cause", ["insufficient_credits", "limit_update"])
def test_failed_idle_allocation_cannot_start_critic(project, ledger, cause):
    envelope = enabled(project, ledger, allowance=150 * credits.NANO)
    coach = closing()[0]
    coach.update({"cost": 70} if cause == "insufficient_credits" else {"update_error": True})
    result, clients, outcome = exercise(envelope, [{}, {"decision": "continue"}, coach], seconds=1800)
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert len(clients) == 3
    assert not (Path(envelope.root) / "sessions" / "coach-1" / "closure-allocation.json").exists()
    assert ("cannot fund closure criticism" if cause == "insufficient_credits" else
            "did not apply") in outcome["reason"]
    assert runtime.usage_record(envelope, complete=True)


def test_cancellation_during_critic_prevents_adjudication(project, ledger):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"},
        *closing({"wait": True, "cancel": str(Path(envelope.root) / "cancel")})], seconds=1800)
    assert result == 130 and outcome["status"] == "cancelled"
    assert len(clients[2].session.prompts) == 1 and clients[3].session.aborted
    assert all(client.stopped for client in clients)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 50 * credits.NANO


def test_critic_does_not_reset_stalled_review_limit(project, ledger):
    root, config, record = project
    envelope = enabled((root, replace(config, max_stalled_reviews=1), record), ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing(choice="continue")], seconds=1800)
    assert result == 1 and outcome["status"] == "checkpointed"
    assert outcome["checkpoint_reason"] == "stalled_reviews"
    assert outcome["reviews"][-1]["stalled_reviews"] == 1
    assert len(clients[0].session.prompts) == 3


@pytest.mark.parametrize("tamper", ["legacy", "initial", "adviser", "proposer", "judge", "reference",
                                  "purpose", "proposal", "response", "submitted", "answer"])
def test_closing_response_and_fresh_judge_require_exact_owned_records(project, ledger, tamper):
    envelope = enabled(project, ledger)
    assert exercise(envelope, [{}, {"decision": "continue"}, *closing()], seconds=1800)[0] == 0
    path = Path(envelope.root) / "usage.json"
    value = read_json(path)
    if tamper == "legacy":
        envelope = replace(envelope, config={key: item for key, item in envelope.config.items() if key != "closure_critic"})
        value["envelope_digest"] = envelope.digest
    elif tamper in ("initial", "adviser", "proposer", "judge"):
        value["sessions"][{"initial": 1, "adviser": 3, "proposer": 2, "judge": 4}[tamper]]["prompts"] = 2
    elif tamper == "answer":
        (Path(envelope.root) / "sessions" / "researcher" / "answer-3.txt").write_text("Changed response")
    else:
        other = Path(envelope.root) / {
            "reference": Path("sessions") / "adjudicator-1" / "closure-adjudication.json",
            "purpose": Path("sessions") / "consult-0" / "request.json",
            "proposal": Path("review-1-proposal.json"),
            "response": Path("closure") / "1" / "response.json",
            "submitted": Path("closure") / "1" / "submitted.json"}[tamper]
        record = read_json(other)
        record[{"reference": "response_sha256", "purpose": "purpose", "proposal": "decision",
                "response": "answer_sha256", "submitted": "prompt_sha256"}[tamper]] = "tampered"
        atomic_json(other, record)
    atomic_json(path, value)
    with pytest.raises(ValueError):
        runtime.usage_record(envelope, complete=True)


@pytest.mark.parametrize("corrections", [["Correct the scope in report.md"], "report.md", [""], [{}], None])
def test_acceptance_cannot_carry_unapplied_or_unspecified_corrections(project, tmp_path, corrections):
    decision = {}
    tool = next(tool for tool in provider.coach_tools(project[0], decision, follow_up=True,
                                                     closure={"artifact": "critique.md"},
                                                     audit=consultation.EvidenceAudit(tmp_path / "review"))
                if tool.name == "labgoblin_review_decision")
    args = {"decision": "accept", "reason": "The investigation is sufficient.", "coverage": "Read report.md",
            "progress": "none", "progress_reason": "No new result.", "critique_response": "The claim needs narrowing."}
    if corrections is not None:
        args["required_corrections"] = corrections
    result = asyncio.run(tool.handler(SimpleNamespace(arguments=args)))
    assert result.result_type == "failure" and not decision
    args.update(decision="continue", question="Correct the unsupported report claim.",
                required_corrections=["Correct the scope in report.md"])
    assert asyncio.run(tool.handler(SimpleNamespace(arguments=args))).result_type == "success"
    assert decision["required_corrections"] == ["Correct the scope in report.md"]


def test_required_correction_is_applied_before_later_acceptance(project, ledger, monkeypatch):
    envelope = enabled(project, ledger)
    report = project[0] / "report.md"
    report.write_text("Unsupported claim of exhaustive coverage.", encoding="utf-8")
    original = FakeSession.send
    inspected = []

    async def act(session, prompt):
        system = session.options["system_message"]
        if system["mode"] == "append" and '"required_corrections": [' in prompt:
            report.write_text("Bounded result only; broader coverage remains unverified.", encoding="utf-8")
        if "FRESH closure adjudicator" in system["content"]:
            tool = next(tool for tool in session.options["tools"] if tool.name == "labgoblin_read_evidence")
            result = await tool.handler(SimpleNamespace(arguments={"path": "report.md"}, tool_name=tool.name))
            assert result.result_type == "success"
            inspected.append(result.text_result_for_llm)
        await original(session, prompt)

    monkeypatch.setattr(FakeSession, "send", act)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"},
        *closing(choice="continue", required_corrections=["Replace the exhaustive claim with the measured scope."]),
        *closing(),
    ], seconds=3600)
    assert result == 0, outcome
    assert "Unsupported claim" in inspected[0] and "broader coverage remains unverified" in inspected[1]
    assert "required_corrections" in clients[0].session.prompts[3]
    assert [review["stalled_reviews"] for review in outcome["reviews"]] == [0, 1, 2]
    assert outcome["reviews"][-1]["required_corrections"] == []
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 120 * credits.NANO


@pytest.mark.parametrize("failure", ["cancel", "timeout", "overshoot", "missing_usage", "empty", "error", "quota"])
def test_failed_closing_response_never_starts_judge_or_reuses_proposal(project, ledger, monkeypatch, failure):
    envelope = enabled(project, ledger)
    original = provider.Session.ask

    async def interrupt(session, prompt, checkpoint, **kwargs):
        if session.role == "researcher" and session.turns == 2:
            if failure == "cancel":
                (Path(envelope.root) / "cancel").write_text("Operator stop")
            elif failure == "timeout":
                session.deadline = time.monotonic() + provider.GRACE_SECONDS - 1
            else:
                session.client.session.script.update({
                    "overshoot": {"cost": 230}, "missing_usage": {"missing_usage": True},
                    "empty": {"answer": ""}, "error": {"error": True}, "quota": {"quota": True}}[failure])
        return await original(session, prompt, checkpoint, **kwargs)

    monkeypatch.setattr(provider.Session, "ask", interrupt)
    code, clients, outcome = exercise(envelope, [{}, {"decision": "continue"}, *closing()], seconds=1800)
    assert code != 0 and outcome["status"] != "completed"
    assert len(clients) == 4 and all(client.stopped for client in clients)
    root = Path(envelope.root)
    assert not (root / "review-1.json").exists()
    assert not (root / "sessions" / "adjudicator-1").exists()
    assert (root / "closure" / "1" / "request.json").exists()
    if failure == "missing_usage":
        assert runtime.usage_record(envelope, complete=True) is None
    else:
        assert runtime.usage_record(envelope, complete=True)


@pytest.mark.parametrize("failure", ["startup_error", "no_decision", "missing_usage", "error", "quota"])
def test_failed_fresh_judge_never_uses_provisional_acceptance(project, ledger, failure):
    envelope = enabled(project, ledger)
    scripts = [{}, {"decision": "continue"}, *closing()]
    scripts[-1][failure] = True
    code, clients, outcome = exercise(envelope, scripts, seconds=1800)
    assert code != 0 and outcome["status"] != "completed"
    assert len(clients) == 5 and all(client.stopped for client in clients)
    root = Path(envelope.root)
    assert not (root / "review-1.json").exists()
    assert (root / "closure" / "1" / "response.json").exists()
    if failure == "missing_usage":
        assert runtime.usage_record(envelope, complete=True) is None
    else:
        assert runtime.usage_record(envelope, complete=True)


@pytest.mark.parametrize("allowance,starts", [(129, False), (130, True)])
def test_critic_cannot_borrow_minimum_response_or_judge_funding(project, ledger, allowance, starts):
    envelope = enabled(project, ledger, allowance=allowance * credits.NANO)
    code, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, *closing({"cost": 30})], seconds=1800)
    assert (code == 0) is starts, outcome
    assert len(clients) == (5 if starts else 3)
    if starts:
        assert clients[3].session.options["session_limits"]["max_ai_credits"] == 30
        assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 90 * credits.NANO
    else:
        assert outcome["status"] == "review_unavailable"


def test_new_driver_does_not_admit_closing_responses_without_frozen_capability(project, ledger):
    envelope = enabled(project, ledger)
    path = Path(envelope.root) / "prompt.json"
    prompt = read_json(path)
    prompt.pop("closure_response_version")
    atomic_json(path, prompt)
    envelope = replace(envelope, prompt_sha256=hash_file(path))
    code, clients, outcome = exercise(envelope, [{}], seconds=1800)
    assert code == 1 and "frozen researcher-response capability" in outcome["reason"]
    assert clients[0].session is None and clients[0].stopped
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 0


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_unknown_closing_capability_is_not_treated_as_legacy(project, ledger, version):
    envelope = enabled(project, ledger)
    path = Path(envelope.root) / "prompt.json"
    prompt = read_json(path)
    prompt["closure_response_version"] = version
    atomic_json(path, prompt)
    envelope = replace(envelope, prompt_sha256=hash_file(path))
    code, clients, outcome = exercise(envelope, [{}], seconds=1800)
    assert code == 1 and "Unsupported frozen closing-response capability" in outcome["reason"]
    assert clients[0].session is None and clients[0].stopped
    with pytest.raises(ValueError, match="Unsupported frozen closing-response capability"):
        runtime.usage_record(envelope, complete=True)


def test_legacy_same_coach_usage_keeps_its_original_prompt_bounds(project, ledger):
    envelope = enabled(project, ledger)
    assert exercise(envelope, [{}, {"decision": "continue"}, *closing()], seconds=1800)[0] == 0
    root = Path(envelope.root)
    prompt = read_json(root / "prompt.json")
    prompt.pop("closure_response_version")
    atomic_json(root / "prompt.json", prompt)
    envelope = replace(envelope, prompt_sha256=hash_file(root / "prompt.json"))
    usage = read_json(root / "usage.json")
    usage["envelope_digest"] = envelope.digest
    judge = usage["sessions"].pop()
    usage["sessions"][0]["prompts"] = 2
    coach = usage["sessions"][2]
    coach["prompts"] = 2
    coach["usage"]["totalNanoAiu"] += judge["usage"]["totalNanoAiu"]
    critique = read_json(root / "sessions" / "consult-0" / "response.json")
    atomic_json(root / "sessions" / "coach-1" / "closure-adjudication.json",
                {key: critique[key] for key in ("artifact", "artifact_sha256", "adviser_role")})
    atomic_json(root / "usage.json", usage)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 70 * credits.NANO
    coach["prompts"] = 3
    atomic_json(root / "usage.json", usage)
    with pytest.raises(ValueError, match="bounded research/review"):
        runtime.usage_record(envelope, complete=True)


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9f8AAAAASUVORK5CYII=")


def test_bounded_image_tool_delivers_and_retains_exact_pixels(project, tmp_path):
    root, _, _ = project
    (root / "view.png").write_bytes(PNG)
    audit = consultation.EvidenceAudit(tmp_path)
    tool = next(tool for tool in consultation.evidence_tools(root, audit=audit, images=True)
                if tool.name == "labgoblin_read_image")
    invocation = SimpleNamespace(tool_name=tool.name, arguments={"path": "view.png"})
    for index in range(8):
        result = asyncio.run(tool.handler(invocation))
        assert result.result_type == "success"
        assert base64.b64decode(result.binary_results_for_llm[0].data) == PNG
        saved = read_json(tmp_path / f"evidence-{index}.json")["result"]
        assert base64.b64decode(saved["image"]["data"]) == PNG
        assert saved["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert asyncio.run(tool.handler(invocation)).result_type == "failure"
    assert audit.images == 8 and audit.count == 8
    assert "labgoblin_read_image" not in {tool.name for tool in consultation.evidence_tools(root)}


@pytest.mark.parametrize("kind", ["active", "size", "dimensions", "state", "offset"])
def test_image_boundary_rejects_unsupported_or_inaccessible_content(project, kind):
    root, _, _ = project
    body = {"active": b"<svg></svg>", "size": b"x" * (4 * 1024**2 + 1),
            "dimensions": PNG[:16] + (16385).to_bytes(4, "big") + PNG[20:]}.get(kind, PNG)
    path = str(Path(".labgoblin") / "secret.png") if kind == "state" else "view.png"
    (root / path).write_bytes(body)
    args = {"path": path, **({"offset": 1} if kind == "offset" else {})}
    with pytest.raises(ValueError):
        consultation.read_evidence(root, args, image=True)


def test_owned_sdk_critic_causes_real_same_researcher_computation(project, ledger, monkeypatch):
    from labgoblin.inspection_evidence import Evidence
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(
        config, coach=True, consultant=True, closure_critic=True, timeout_seconds=1800).toml(), encoding="utf-8")
    (root / "view.png").write_bytes(PNG)
    fixture_provider(root, monkeypatch, "closure_critic")
    atomic_json(root / "fixture-provider.json", {
        "model_free": True, "root": str(root), "mode": "closure_critic", "image": "view.png"})
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 70 * credits.NANO
    assert [s["prompts"] for s in result["usage"]["sessions"]] == [3, 1, 1, 1, 1]
    directory = root / ".labgoblin" / "runs" / result["id"]
    assert read_json(directory / "snapshot" / "artifacts" / "closure-counterexample.json") == {"x": "1/2", "value": "-1/4"}
    assert [read_json(directory / f"review-{i}.json")["decision"] for i in range(2)] == ["accept", "accept"]
    for role in ("consult-0",):
        capture = read_json(directory / "sessions" / role / "evidence-1.json")["result"]
        assert base64.b64decode(capture["image"]["data"]) == PNG
    researcher = result["usage"]["sessions"][0]["session_id"]
    follow_up = (directory / "snapshot" / f"fixture-prompt-{researcher}-3.txt").read_text()
    assert "x=1/2" in follow_up and "closure_critique" in follow_up
    assert "all-real claim is false" in (directory / "snapshot" / "report.md").read_text()
    assert runtime.status(root)["budget"]["remaining"] == 230
    view = Evidence(root, project[2]["id"]).session({"run": result["id"], "role": "adjudicator-1"})
    assert view["events"] and not view["live"]
    assert view["role"] == "adjudicator-1"
    assert not ledger.rows()
