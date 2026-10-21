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
from labgoblin.storage import atomic_json, read_json
from tests.conftest import arm
from tests.test_simple_provider import FakeClient, exercise
from tests.test_simple_runtime import fixture_provider


def enabled(project, ledger, *, allowance=300 * credits.NANO):
    root, config, record = project
    config = replace(config, coach=True, consultant=True, closure_critic=True,
                     max_ai_credits=allowance / credits.NANO if allowance is not None else "unlimited")
    return arm((root, config, record), ledger, coach=True, consultant=True, allowance=allowance)


def adjudicator(choice="accept", **kwargs):
    return {"turns": [
        {"decision": "accept", "reason": "SECRET PRELIMINARY COACH APPROVAL"},
        {"decision": choice, "critique_response":
         "The critique identifies a control gap affecting the original goal; checked retained evidence, not reviewer agreement.",
         **kwargs}]}


@pytest.mark.parametrize("seconds, critic_seconds", [(3600, 3000), (86400, 7200)])
def test_critic_has_its_own_visible_two_hour_deadline(project, ledger, monkeypatch, seconds, critic_seconds):
    envelope = enabled(project, ledger)
    ask = provider.Session.ask
    deadlines = {}

    async def observe(session, prompt, checkpoint):
        deadlines[session.role] = session.deadline - time.monotonic()
        if session.role.startswith("consult-"):
            audit = session.client.session.options["hooks"]["on_post_tool_use"].__self__
            assert not audit.count_limited
            assert audit.response_deadline == session.deadline - provider.GRACE_SECONDS
        return await ask(session, prompt, checkpoint)

    monkeypatch.setattr(provider.Session, "ask", observe)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, adjudicator(), {}], seconds=seconds)
    assert result == 0, outcome
    assert critic_seconds - 5 < deadlines["consult-0"] <= critic_seconds
    assert seconds - 5 < deadlines["coach-1"] <= seconds
    request = read_json(Path(envelope.root) / "sessions" / "consult-0" / "request.json")
    assert critic_seconds - 5 < request["review_seconds_remaining_including_shutdown"] <= critic_seconds
    assert request["shutdown_reserve_seconds"] == 30
    assert request["adjudication_reserve_seconds"] == 600
    assert request["response_deadline_unix_seconds"] == request["review_deadline_unix_seconds"] - 30
    assert "review_seconds_remaining_including_shutdown" in clients[-1].session.prompts[0]
    assert "seconds_remaining_including_shutdown" in clients[2].session.prompts[1]


def test_critic_uses_shared_credits_and_returns_unused_allowance(project, ledger):
    envelope = enabled(project, ledger, allowance=10000 * credits.NANO)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, adjudicator(), {"cost": 500}], seconds=86400)
    assert result == 0, outcome
    assert clients[-1].session.options["session_limits"]["max_ai_credits"] == 9870
    assert clients[2].session.updates == [40, 9110]
    root = Path(envelope.root) / "sessions" / "coach-1"
    assert read_json(root / "closure-allocation.json")["critic_allowance_nano"] == 9870 * credits.NANO
    assert read_json(root / "closure-settlement.json")["ordinary_adviser_reserve_nano"] == 300 * credits.NANO
    assert len(list(root.glob("allowance-*.json"))) == 2
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 550 * credits.NANO


def test_critic_has_no_evidence_operation_cap_but_retains_per_read_bounds(project, tmp_path):
    root, _, _ = project
    (root / "view.png").write_bytes(PNG)
    audit = consultation.EvidenceAudit(tmp_path, count_limited=False, response_deadline=time.monotonic() + 120)
    tools = {tool.name: tool for tool in consultation.evidence_tools(root, audit=audit, images=True)}
    for index in range(consultation.MAX_READS + 1):
        audit.record("labgoblin_read_evidence", {"path": "notes"}, {"text": str(index)})
    image = tools["labgoblin_read_image"]
    for _ in range(9):
        result = asyncio.run(image.handler(SimpleNamespace(
            tool_name=image.name, arguments={"path": "view.png"})))
        assert result.result_type == "success"
        assert 0 < json.loads(result.text_result_for_llm)["response_seconds_remaining"] <= 120
    assert audit.images == 9 and audit.count == consultation.MAX_READS + 10
    assert "eight" not in image.description
    request = {"toolName": "web_fetch", "toolArgs": {"url": "https://example.org"},
               "toolResult": {"text": "Retained public evidence"}}
    assert audit.before_web(request, {}) == {"permissionDecision": "allow"}
    assert "Review response time remaining" in audit.after_web(request, {})["additionalContext"]
    assert audit.pending_web == 0
    with pytest.raises(ValueError, match="retention bound"):
        audit.record("labgoblin_read_evidence", {}, {"text": "x" * (128 * 1024)})


def test_critic_timeout_is_not_the_campaign_deadline_and_never_accepts(project, ledger, monkeypatch):
    envelope = enabled(project, ledger)
    ask = provider.Session.ask

    async def time_out_critic(session, prompt, checkpoint):
        if session.role != "consult-0":
            return await ask(session, prompt, checkpoint)
        assert session.deadline - time.monotonic() > 7100

        def expire():
            checkpoint()
            session.deadline = time.monotonic() + provider.GRACE_SECONDS - 1

        return await ask(session, prompt, expire)

    monkeypatch.setattr(provider.Session, "ask", time_out_critic)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, adjudicator(), {"wait": True}], seconds=86400)
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
        if session.role == "coach-1" and session.allowance_revisions:
            session.client.session.script["update_error"] = True
        return await update(session, allowance)

    monkeypatch.setattr(provider.Session, "set_allowance", reject_return)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, adjudicator(), {}], seconds=1800)
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
def test_critic_is_independent_and_same_coach_adjudicates(project, ledger, answer):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "accept"}, adjudicator(), {"answer": answer}], seconds=1800)
    assert result == 0, outcome
    assert [len(client.session.prompts) for client in clients] == [2, 1, 2, 1]
    assert clients[-1].session.options["system_message"]["content"] == guidance.CLOSURE_CRITIC
    assert "SECRET PRELIMINARY COACH APPROVAL" not in clients[-1].session.prompts[0]
    assert answer in clients[2].session.prompts[1]
    assert "critique_response" in outcome["reviews"][-1]
    assert len(outcome["reviews"]) == 2 and outcome["reviews"][-1]["stalled_reviews"] == 1
    assert all(client.stopped for client in clients)
    root = Path(envelope.root)
    assert read_json(root / "review-1-proposal.json")["reason"] == "SECRET PRELIMINARY COACH APPROVAL"
    request = read_json(root / "sessions" / "consult-0" / "request.json")
    assert request["purpose"] == "closure_critique" and request["researcher_stopping_response"]
    assert "SECRET PRELIMINARY COACH APPROVAL" not in json.dumps(request)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 60 * credits.NANO
    tools = {tool.name for tool in clients[-1].session.options["tools"]}
    assert tools == {"labgoblin_read_evidence", "labgoblin_list_evidence", "labgoblin_read_image"}


def test_objection_returns_to_same_researcher_without_recursive_critic(project, ledger):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, adjudicator("continue"),
        {"answer": "The unused interior-point control can refute the all-real claim."},
        adjudicator(progress="meaningful"), {"answer": "The new exact counterexample answers the original question."},
    ], seconds=1800)
    assert result == 0, outcome
    assert len(clients[0].session.prompts) == 3
    assert "closure_critique" in clients[0].session.prompts[2]
    assert "unused interior-point" in clients[2].session.prompts[1]
    assert [review["decision"] for review in outcome["reviews"]] == ["continue", "continue", "accept"]
    assert [review["stalled_reviews"] for review in outcome["reviews"]] == [0, 1, 0]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 100 * credits.NANO


@pytest.mark.parametrize("failure", [
    {"answer": ""}, {"answer": "x" * (consultation.MAX_BRIEF + 1)},
    {"startup_error": True}, {"missing_usage": True}, {"cost": 500},
])
def test_failed_critic_never_falls_back_to_preliminary_acceptance(project, ledger, failure):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, adjudicator(), failure], seconds=1800)
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
        {}, {"decision": "continue"}, adjudicator(**turn), {}], seconds=1800)
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 60 * credits.NANO


def test_exhausted_adviser_reserve_is_repartitioned_only_at_idle_closure(project, ledger):
    envelope = enabled(project, ledger)
    result, clients, outcome = exercise(envelope, [
        {"consult": [{"question": "Check a useful assumption."}]}, {"cost": 30},
        {"decision": "continue"}, adjudicator(), {},
    ], seconds=1800)
    assert result == 0, outcome
    allocation = read_json(Path(envelope.root) / "sessions" / "coach-1" / "closure-allocation.json")
    assert allocation["previous_adviser_budget_nano"] == 30 * credits.NANO
    assert allocation["new_adviser_budget_nano"] == 170 * credits.NANO
    assert allocation["reported_total_nano"] == 70 * credits.NANO
    assert clients[3].session.updates == [40, 170]
    assert clients[4].session.options["session_limits"]["max_ai_credits"] == 140
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 90 * credits.NANO


@pytest.mark.parametrize("failure", ["timeout", "provider_error"])
def test_settled_failed_adviser_does_not_block_later_closure(project, ledger, monkeypatch, failure):
    envelope = enabled(project, ledger)
    ask = provider.Session.ask

    async def interrupted_advice(session, prompt, checkpoint):
        if session.role != "consult-0":
            return await ask(session, prompt, checkpoint)

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
        {"decision": "continue"}, adjudicator(), {},
    ], seconds=1800)
    assert result == 0, outcome
    assert clients[1].session.aborted and all(client.stopped for client in clients)
    failed = Path(envelope.root) / "sessions" / "consult-0"
    assert read_json(failed / "response.json")["status"] == "failed"
    assert not (failed / "settled-prompt-1.json").exists()
    assert read_json(failed / "usage.json")["complete"]
    assert [len(client.session.prompts) for client in clients] == [2, 1, 1, 2, 1]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 70 * credits.NANO


@pytest.mark.parametrize("failure", ["abort", "metrics", "disconnect", "stop"])
def test_failed_adviser_close_does_not_clear_waiting(project, ledger, monkeypatch, failure):
    envelope = enabled(project, ledger)
    client = FakeClient({"wait": True})

    async def check():
        session = provider.Session(envelope, "consult-0", "interrupted-adviser",
                                   30 * credits.NANO, client, time.monotonic() + 600)
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
    scripts = [{}, {"decision": "continue"}, adjudicator()]
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
        {}, {"decision": "continue"}, adjudicator(), {}], seconds=1800)
    assert result == 0, outcome
    assert clients[2].session.updates == []
    assert clients[3].session.options["session_limits"] is None


@pytest.mark.parametrize("cause", ["insufficient_credits", "limit_update"])
def test_failed_idle_allocation_cannot_start_critic(project, ledger, cause):
    envelope = enabled(project, ledger, allowance=150 * credits.NANO)
    coach = adjudicator()
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
        {}, {"decision": "continue"}, adjudicator(),
        {"wait": True, "cancel": str(Path(envelope.root) / "cancel")}], seconds=1800)
    assert result == 130 and outcome["status"] == "cancelled"
    assert len(clients[2].session.prompts) == 1 and clients[3].session.aborted
    assert all(client.stopped for client in clients)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 50 * credits.NANO


def test_critic_does_not_reset_stalled_review_limit(project, ledger):
    root, config, record = project
    envelope = enabled((root, replace(config, max_stalled_reviews=1), record), ledger)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "continue"}, adjudicator("continue"), {}], seconds=1800)
    assert result == 1 and outcome["status"] == "checkpointed"
    assert outcome["checkpoint_reason"] == "stalled_reviews"
    assert outcome["reviews"][-1]["stalled_reviews"] == 1
    assert len(clients[0].session.prompts) == 2


@pytest.mark.parametrize("tamper", ["legacy", "initial", "adviser", "third", "reference", "purpose", "proposal"])
def test_extra_coach_prompt_requires_enabled_owned_closure_records(project, ledger, tamper):
    envelope = enabled(project, ledger)
    assert exercise(envelope, [{}, {"decision": "continue"}, adjudicator(), {}], seconds=1800)[0] == 0
    path = Path(envelope.root) / "usage.json"
    value = read_json(path)
    if tamper == "legacy":
        envelope = replace(envelope, config={key: item for key, item in envelope.config.items() if key != "closure_critic"})
        value["envelope_digest"] = envelope.digest
    elif tamper in ("initial", "adviser", "third"):
        value["sessions"][{"initial": 1, "adviser": 3, "third": 2}[tamper]]["prompts"] = 3 if tamper == "third" else 2
    else:
        other = Path(envelope.root) / {
            "reference": Path("sessions") / "coach-1" / "closure-adjudication.json",
            "purpose": Path("sessions") / "consult-0" / "request.json",
            "proposal": Path("review-1-proposal.json")}[tamper]
        record = read_json(other)
        record[{"reference": "artifact_sha256", "purpose": "purpose", "proposal": "decision"}[tamper]] = "tampered"
        atomic_json(other, record)
    atomic_json(path, value)
    with pytest.raises(ValueError):
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
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(
        config, coach=True, consultant=True, closure_critic=True, timeout_seconds=1800).toml(), encoding="utf-8")
    (root / "view.png").write_bytes(PNG)
    fixture_provider(root, monkeypatch, "closure_critic")
    atomic_json(root / "fixture-provider.json", {
        "model_free": True, "root": str(root), "mode": "closure_critic", "image": "view.png"})
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 100 * credits.NANO
    assert [s["prompts"] for s in result["usage"]["sessions"]] == [3, 1, 2, 1, 2, 1]
    directory = root / ".labgoblin" / "runs" / result["id"]
    assert read_json(directory / "snapshot" / "artifacts" / "closure-counterexample.json") == {"x": "1/2", "value": "-1/4"}
    assert [read_json(directory / f"review-{i}.json")["decision"] for i in range(3)] == ["accept", "continue", "accept"]
    for role in ("consult-0", "consult-1"):
        capture = read_json(directory / "sessions" / role / "evidence-1.json")["result"]
        assert base64.b64decode(capture["image"]["data"]) == PNG
    researcher = result["usage"]["sessions"][0]["session_id"]
    follow_up = (directory / "snapshot" / f"fixture-prompt-{researcher}-3.txt").read_text()
    assert "interior-point control" in follow_up and "closure_critique" in follow_up
    assert runtime.status(root)["budget"]["remaining"] == 200
    assert not ledger.rows()
