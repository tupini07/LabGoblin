import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import pytest

from labgoblin import consultation, credits, guidance, provider, runtime
from labgoblin.cli import main
from labgoblin.config import Config
from labgoblin.storage import atomic_json, hash_file, read_json
from tests.conftest import arm
from tests.test_simple_provider import exercise


QUESTION = {"question": "Which assumptions or materially different approaches are worth checking?"}


def test_consultant_is_strict_opt_in_and_preserves_old_configuration(tmp_path, capsys):
    assert Config.parse({"version": 5}).consultant is False
    assert main(["init", str(tmp_path / "plain"), "--credits", "30"]) == 0
    capsys.readouterr()
    assert Config.load(tmp_path / "plain").consultant is False
    assert main(["init", str(tmp_path / "advice"), "--consultant", "--credits", "60"]) == 0
    capsys.readouterr()
    assert Config.load(tmp_path / "advice").consultant is True
    assert main(["init", str(tmp_path / "both"), "--coach", "--consultant", "--credits", "90"]) == 0
    capsys.readouterr()
    for args in ({"consultant": "true"}, {"consultant": True, "max_ai_credits": 59},
                 {"consultant": True, "coach": True, "max_ai_credits": 89}):
        with pytest.raises(ValueError):
            Config(**args)


@pytest.mark.parametrize("cap", [60, 300, 1000, 10000, None])
def test_consultations_have_no_independent_pool(cap):
    summary = credits.summarize([], cap)
    assert credits.available(summary, consultant=True) == (None if cap is None else cap * credits.NANO)


def test_researcher_consults_without_losing_normal_tools_and_retains_evidence(project, ledger):
    root, _, _ = project
    (root / "research-notes.md").write_text("Known positive control failed; investigate the method.", encoding="utf-8")
    envelope = arm(project, ledger, consultant=True)
    request = {**QUESTION, "evidence_paths": ["research-notes.md"]}
    code, clients, outcome = exercise(envelope, [{"consult": [request], "duplicate": True}, {}])
    assert code == 0 and len(clients) == 2
    researcher, adviser = [client.session for client in clients]
    assert "available_tools" not in researcher.options
    assert researcher.options["system_message"]["mode"] == "append"
    assert guidance.CONSULT_CALLER in researcher.options["system_message"]["content"]
    assert researcher.options["session_limits"] == {"max_ai_credits": 300.0}
    assert adviser.options["session_limits"] == {"max_ai_credits": 260.0}
    assert researcher.updates == [40, 290]
    assert adviser.options["system_message"] == {"mode": "replace", "content": guidance.CONSULTANT}
    assert {tool.name for tool in adviser.options["tools"]} == {"labgoblin_read_evidence", "labgoblin_list_evidence"}
    assert len(researcher.prompts) == 1 and len(adviser.prompts) == 1
    assert researcher.tool_results[0] is researcher.tool_results[1]
    response = json.loads(researcher.tool_results[0].text_result_for_llm)
    assert (root / response["artifact"]).is_file()
    assert (root / "research-notes.md").read_text().startswith("Known positive")
    directory = Path(envelope.root) / "sessions" / "consult-0"
    request_record = read_json(directory / "request.json")
    assert request_record["goal"] == "Answer the original question"
    assert request_record["caller_session_id"] == outcome["sessions"][0]["session_id"]
    assert read_json(directory / "evidence-0.json")["result"]["text"].startswith("Known positive")
    assert read_json(directory / "response.json")["status"] == "completed"
    usage = runtime.usage_record(envelope, complete=True)
    assert usage["totalNanoAiu"] == 20 * credits.NANO
    assert [s["role"] for s in usage["sessions"]] == ["researcher", "consult-0"]
    assert all(client.stopped for client in clients)


@pytest.mark.parametrize("first_decision", ["accept", "continue"])
def test_coach_can_consult_and_passes_artifact_to_same_researcher(project, ledger, first_decision):
    root, config, record = project
    config = replace(config, max_ai_credits=1000)
    envelope = arm((root, config, record), ledger, coach=True, consultant=True, allowance=1000 * credits.NANO)
    scripts = [{}, {"consult": [QUESTION], "decision": first_decision}, {}, {"decision": "accept"}]
    code, clients, outcome = exercise(envelope, scripts)
    assert code == 0 and len(clients) == 4
    assert len(clients[0].session.prompts) == 2
    assert "consult-0.md" in clients[0].session.prompts[1]
    assert "Consultation artifacts available to you" in clients[0].session.prompts[1]
    assert [s["role"] for s in outcome["sessions"]] == ["researcher", "coach-0", "consult-0", "coach-1"]
    assert clients[0].session.options["session_limits"]["max_ai_credits"] == 970
    assert clients[1].session.options["session_limits"]["max_ai_credits"] == 930
    assert clients[2].session.options["session_limits"]["max_ai_credits"] == 890
    assert clients[0].session.updates == [950]
    assert clients[1].session.updates == [40, 920]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 50 * credits.NANO


def test_repeated_consultations_are_fresh_and_do_not_renew_the_pool(project, ledger):
    root, config, record = project
    envelope = arm((root, replace(config, max_ai_credits=1000), record), ledger,
                   consultant=True, allowance=1000 * credits.NANO)
    code, clients, outcome = exercise(envelope, [{"consult": [QUESTION, {"question": "Check another assumption."}]}, {}, {}])
    assert code == 0 and len(clients) == 3
    assert [c.session.options["session_limits"]["max_ai_credits"] for c in clients] == [1000, 960, 950]
    assert clients[0].session.updates == [40, 990, 40, 980]
    assert clients[1].session.options["session_id"] != clients[2].session.options["session_id"]
    assert len(clients[0].session.prompts) == 1
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 30 * credits.NANO


@pytest.mark.parametrize("first_decision", ["accept", "continue"])
def test_targeted_claim_review_is_retained_and_available_without_repeating_it(project, ledger, first_decision):
    root, config, record = project
    (root / "report.md").write_text("Two sampled zeros imply f(x)=x*x-x is nonnegative for every real x.")
    envelope = arm((root, replace(config, max_ai_credits=1000), record), ledger,
                   coach=True, consultant=True, allowance=1000 * credits.NANO)
    request = {"purpose": "claim_review", "question": "Does the all-real claim justify stopping?",
               "evidence_paths": ["report.md"]}
    objection = "At x=1/2, f(x)=-1/4. Finite samples do not prove the all-real claim."
    code, clients, outcome = exercise(envelope, [
        {}, {"consult": [request], "duplicate": True, "decision": first_decision},
        {"answer": objection}, {"decision": "accept", "progress": "meaningful"},
    ])
    assert code == 0 and len(clients) == 4
    adviser = clients[2].session
    assert adviser.options["system_message"]["content"] == guidance.CONSULTANT + guidance.CLAIM_REVIEW
    assert guidance.COACH_CLAIM_REVIEW in clients[1].session.options["system_message"]["content"]
    assert {tool.name for tool in adviser.options["tools"]} == {"labgoblin_read_evidence", "labgoblin_list_evidence"}
    guard = adviser.options["hooks"]["on_pre_tool_use"]
    for name in ("powershell", "labgoblin_consult", "labgoblin_review_decision", "write_file"):
        assert guard({"toolName": name}, {})["permissionDecision"] == "deny"
    directory = Path(envelope.root) / "sessions" / "consult-0"
    retained = read_json(directory / "request.json")
    assert retained["purpose"] == "claim_review"
    assert retained["question"] == request["question"] and retained["goal"] == "Answer the original question"
    assert retained["operational_boundaries"] == guidance.CONSULTANT + guidance.CLAIM_REVIEW
    response = read_json(directory / "response.json")
    assert response["purpose"] == "claim_review" and response["brief"] == objection
    artifact = root / response["artifact"]
    assert artifact.read_text().startswith("# Scientific claim review")
    assert response["artifact_sha256"] == hash_file(artifact)
    linked = outcome["reviews"][0]["consultations"]
    assert linked == [{"caller": "coach-0", "purpose": "claim_review", "artifact": response["artifact"],
                       "artifact_sha256": response["artifact_sha256"]}]
    assert outcome["reviews"][1]["consultations"] == []
    assert response["artifact_sha256"] in clients[3].session.prompts[0]
    assert '"available_consultations":' in clients[3].session.prompts[0]
    assert "consult-0.md" in clients[0].session.prompts[1]
    assert "Test, repair or narrow a challenged claim" in clients[0].session.prompts[1]
    assert clients[1].session.tool_results[0] is clients[1].session.tool_results[1]
    assert [client.session.options["session_limits"]["max_ai_credits"] for client in clients] == [970, 930, 890, 900]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 50 * credits.NANO


@pytest.mark.parametrize("failure", [{"startup_error": True}, {"missing_usage": True}, {"cost": 261}])
def test_claim_review_failures_do_not_publish_success_or_relax_accounting(project, ledger, failure):
    root, _, _ = project
    (root / "report.md").write_text("A claim needing review.")
    request = {"purpose": "claim_review", "question": "Check the claim's scope.", "evidence_paths": ["report.md"]}
    envelope = arm(project, ledger, consultant=True)
    code, clients, outcome = exercise(envelope, [{"consult": [request]}, failure])
    response = read_json(Path(envelope.root) / "sessions" / "consult-0" / "response.json")
    assert response["status"] == "failed" and response["purpose"] == "claim_review"
    assert not (root / "artifacts" / "consultations").exists()
    if "startup_error" in failure:
        assert clients[0].session.tool_results[0].result_type == "failure"
    else:
        assert code == 1
        assert clients[0].session.tool_results == []
        assert outcome["status"] == ("failed" if "missing_usage" in failure else "budget_exhausted")
    if "missing_usage" in failure:
        assert runtime.usage_record(envelope, complete=True) is None


def test_decision_waits_for_active_consultation_then_can_be_corrected(project, ledger):
    from labgoblin import provider
    envelope = arm(project, ledger, coach=True, consultant=True)

    async def check():
        pool = provider.Consultations(envelope, {}, [], 0, None, None)
        pool.active = asyncio.get_running_loop().create_future()
        decision = {}
        tool = next(tool for tool in provider.coach_tools(project[0], decision, consultations=pool)
                    if tool.name == "labgoblin_review_decision")
        invocation = SimpleNamespace(arguments={"decision": "accept", "reason": "The scoped result supports stopping.",
                                               "coverage": "Inspected the claim and its evidence."})
        result = await tool.handler(invocation)
        assert result.result_type == "failure" and "settle" in result.error and not decision
        pool.active.set_result(None)
        result = await tool.handler(invocation)
        assert result.result_type == "success" and decision["decision"] == "accept"

    asyncio.run(check())


@pytest.mark.parametrize("status", ["failed", "budget_exhausted"])
def test_parallel_decision_cannot_return_after_fatal_consultation_accounting(project, status):
    from labgoblin import provider
    pool = SimpleNamespace(active=None, fatal=provider.StopRequested(status, "Consultation cannot fund further inference."))
    decision = {}
    tool = next(tool for tool in provider.coach_tools(project[0], decision, consultations=pool)
                if tool.name == "labgoblin_review_decision")
    invocation = SimpleNamespace(arguments={"decision": "accept", "reason": "Scoped conclusion.",
                                           "coverage": "Inspected report."})
    with pytest.raises(asyncio.CancelledError, match="cannot fund"):
        asyncio.run(tool.handler(invocation))
    assert not decision


@pytest.mark.parametrize("arguments", [
    {"question": ""}, {"question": "x" * 4097}, {**QUESTION, "model": "other"},
    {**QUESTION, "purpose": "certify"}, {**QUESTION, "purpose": None},
    {**QUESTION, "purpose": "claim_review"}, {**QUESTION, "purpose": "claim_review", "evidence_paths": []},
    {**QUESTION, "evidence_paths": "../report.md"}, {**QUESTION, "evidence_paths": [".."]},
    {**QUESTION, "evidence_paths": [".labgoblin/project.json"]},
    {**QUESTION, "evidence_paths": ["missing.txt"]},
])
def test_invalid_consultation_is_explicit_and_starts_no_adviser(project, ledger, arguments):
    envelope = arm(project, ledger, consultant=True)
    code, clients, _ = exercise(envelope, [{"consult": [arguments]}])
    assert code == 0 and len(clients) == 1
    assert clients[0].session.tool_results[0].result_type == "failure"
    assert clients[0].session.tool_results[0].text_result_for_llm


@pytest.mark.parametrize("script", [{"startup_error": True}, {"missing_web": True}, {"metadata_unavailable": True}, {"answer": ""},
                                   {"answer": "x" * (consultation.MAX_BRIEF + 1)}])
def test_unavailable_adviser_is_not_a_success_shaped_empty_answer(project, ledger, script):
    envelope = arm(project, ledger, consultant=True)
    code, clients, _ = exercise(envelope, [{"consult": [QUESTION]}, script])
    assert code == 0
    assert clients[0].session.tool_results[0].result_type == "failure"
    assert read_json(Path(envelope.root) / "sessions" / "consult-0" / "response.json")["status"] == "failed"
    assert not (Path(envelope.project) / "artifacts").exists()
    assert runtime.usage_record(envelope, complete=True)["complete"]


@pytest.mark.parametrize("caller", ["researcher", "coach"])
def test_unknown_adviser_usage_blocks_followup_and_is_not_final_billing(project, ledger, caller):
    envelope = arm(project, ledger, coach=True, consultant=True)
    scripts = ([{}] if caller == "coach" else []) + [{"consult": [QUESTION]}, {"missing_usage": True}]
    code, clients, outcome = exercise(envelope, scripts)
    assert code == 1 and outcome["status"] == "failed"
    assert len(clients) == len(scripts)
    assert "unknown" in outcome["reason"]
    assert runtime.usage_record(envelope, complete=True) is None
    partial = runtime.usage_record(envelope, complete=False)
    assert partial["complete"] is False
    assert partial["sessions"][-1]["complete"] is False
    assert clients[-2].session.aborted
    assert clients[-2].session.tool_results == []


@pytest.mark.parametrize("caller", ["researcher", "coach"])
def test_soft_overshoot_cannot_continue_under_overlapping_allowances(project, ledger, caller):
    envelope = arm(project, ledger, coach=True, consultant=True)
    scripts = ([{}] if caller == "coach" else []) + [{"consult": [QUESTION]}, {"cost": 231}]
    code, clients, outcome = exercise(envelope, scripts)
    assert code == 1 and outcome["status"] == "budget_exhausted"
    assert len(clients) == len(scripts)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == (231 + 10 * (len(scripts) - 1)) * credits.NANO
    assert clients[-2].session.aborted
    assert clients[-2].session.tool_results == []


def test_cancelling_a_waiting_consultation_retires_both_sessions(project, ledger):
    envelope = arm(project, ledger, consultant=True)
    code, clients, outcome = exercise(envelope, [
        {"consult": [QUESTION]}, {"wait": True, "cancel": str(Path(envelope.root) / "cancel")}])
    assert code == 130 and outcome["status"] == "cancelled"
    assert all(client.stopped for client in clients)
    assert clients[1].session.aborted
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 20 * credits.NANO


def test_adviser_tool_policy_denies_shell_decisions_recursion_and_invalid_fetch(project, ledger):
    envelope = arm(project, ledger, consultant=True)
    _, clients, _ = exercise(envelope, [{"consult": [QUESTION]}, {}])
    options = clients[1].session.options
    guard = options["hooks"]["on_pre_tool_use"]
    for name in ("powershell", "task", "labgoblin_consult", "labgoblin_review_decision", "write_file"):
        assert guard({"toolName": name}, {})["permissionDecision"] == "deny"
    for args in ({"url": "file:///local"}, {"url": "https://name:password@example.org"},
                 {"url": "https://127.0.0.1"}, {"url": "https://localhost/"},
                 {"url": "https://[::1]/"}, {"url": "https://169.254.169.254/"},
                 {"url": "https://example.org", "max_length": 16385},
                 {"url": "https://example.org", "start_index": -1}):
        assert guard({"toolName": "web_fetch", "toolArgs": args}, {})["permissionDecision"] == "deny"


def test_web_evidence_is_retained_with_real_result_and_bounded_pending_reads(tmp_path):
    audit = consultation.EvidenceAudit(tmp_path)
    request = {"toolName": "web_fetch", "toolArgs": {"url": "https://arxiv.org/abs/1234.56789"},
               "toolResult": {"content": "Synthetic publication body"}}
    assert audit.before_web(request, {})["permissionDecision"] == "allow"
    assert audit.after_web(request, {}) == {}
    assert read_json(tmp_path / "evidence-0.json")["result"] == request["toolResult"]
    audit.count = consultation.MAX_READS - 1
    assert audit.before_web(request, {})["permissionDecision"] == "allow"
    assert audit.before_web(request, {})["permissionDecision"] == "deny"


def test_same_call_id_cannot_purchase_changed_advice(project, ledger):
    envelope = arm(project, ledger, consultant=True)
    code, clients, _ = exercise(envelope, [{"consult": [QUESTION], "changed_duplicate": True}, {}])
    assert code == 0 and len(clients) == 2
    assert clients[0].session.tool_results[0].result_type == "success"
    assert clients[0].session.tool_results[1].result_type == "failure"
    assert "changed its consultation" in clients[0].session.tool_results[1].error


def test_same_call_id_cannot_be_relabelled_as_a_claim_review(project, ledger):
    root, _, _ = project
    (root / "report.md").write_text("Evidence for an ordinary consultation.")
    envelope = arm(project, ledger, consultant=True)
    code, clients, _ = exercise(envelope, [
        {"consult": [{**QUESTION, "evidence_paths": ["report.md"]}], "changed_purpose": True}, {}])
    assert code == 0 and len(clients) == 2
    assert clients[0].session.tool_results[1].result_type == "failure"
    assert "changed its consultation" in clients[0].session.tool_results[1].error


def test_parallel_requests_are_rejected_not_queued(project, ledger):
    envelope = arm(project, ledger, consultant=True)
    code, clients, _ = exercise(envelope, [{"consult": [QUESTION, QUESTION], "concurrent": True}, {}])
    assert code == 0 and len(clients) == 2
    results = clients[0].session.tool_results
    assert [r.result_type for r in results] == ["success", "failure"]
    assert "not queued" in results[1].error


@pytest.mark.parametrize("purpose", ["advice", "claim_review"])
@pytest.mark.parametrize("caller", ["researcher", "coach"])
@pytest.mark.parametrize("seconds, expected", [(86400, 1800), (900, 900)])
def test_ordinary_advisers_get_thirty_minutes_within_campaign_time(
        project, ledger, monkeypatch, purpose, caller, seconds, expected):
    root, _, _ = project
    (root / "research-notes.md").write_text("A retained claim and its evidence.", encoding="utf-8")
    request = {**QUESTION, "purpose": purpose, "evidence_paths": ["research-notes.md"]}
    envelope = arm(project, ledger, coach=caller == "coach", consultant=True)
    deadlines, remaining = {}, {}
    ask = provider.Session.ask

    async def observe(session, prompt, checkpoint):
        deadlines[session.role] = session.deadline
        remaining[session.role] = session.deadline - time.monotonic()
        return await ask(session, prompt, checkpoint)

    monkeypatch.setattr(provider.Session, "ask", observe)
    scripts = ([{}, {"consult": [request], "decision": "continue"}, {}, {"decision": "accept"}]
               if caller == "coach" else [{"consult": [request]}, {}])
    code, clients, outcome = exercise(envelope, scripts, seconds=seconds)
    assert code == 0, outcome
    assert expected - 5 < remaining["consult-0"] <= expected
    assert deadlines["consult-0"] <= deadlines["researcher"]
    if seconds < 1800:
        assert deadlines["consult-0"] == deadlines["researcher"]
    if caller == "coach":
        assert deadlines["coach-0"] == deadlines["coach-1"] == deadlines["researcher"]
    adviser = clients[2 if caller == "coach" else 1].session
    assert adviser.options["session_limits"] == {"max_ai_credits": 190.0 if caller == "coach" else 260.0}
    assert "30 minutes including shutdown" in adviser.options["system_message"]["content"]
    assert read_json(Path(envelope.root) / "sessions" / "consult-0" / "response.json")["purpose"] == purpose


def test_local_consultation_deadline_is_bounded_without_resetting_run(project, ledger, monkeypatch):
    monkeypatch.setattr(consultation, "CONSULT_SECONDS", 30.4)
    envelope = arm(project, ledger, consultant=True)
    code, clients, outcome = exercise(envelope, [{"consult": [QUESTION]}, {"wait": True}])
    assert code == 0 and outcome["status"] == "completed"
    assert clients[0].session.tool_results[0].result_type == "failure"
    assert clients[1].session.aborted
    assert len(clients[0].session.prompts) == 1
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 20 * credits.NANO


def test_whole_run_deadline_stops_waiting_caller_and_adviser(project, ledger):
    envelope = arm(project, ledger, consultant=True)
    code, clients, outcome = exercise(envelope, [{"consult": [QUESTION]}, {"wait": True}], seconds=30.5)
    assert code == 124 and outcome["status"] == "timed_out"
    assert all(client.stopped for client in clients)


def test_insufficient_campaign_headroom_starts_no_additional_adviser(project, ledger):
    envelope = arm(project, ledger, consultant=True)
    code, clients, _ = exercise(envelope, [{"consult": [QUESTION, QUESTION]}, {"cost": 250}])
    assert code == 0 and len(clients) == 2
    assert clients[0].session.tool_results[1].result_type == "failure"
    assert "no adviser started" in clients[0].session.tool_results[1].error


def test_original_300_credit_pool_failure_now_uses_shared_campaign_balance(project, ledger):
    root, config, record = project
    (root / "report.md").write_text("A consequential claim needing independent review.")
    envelope = arm((root, replace(config, max_ai_credits=1000), record), ledger,
                   consultant=True, allowance=1000 * credits.NANO)
    request = {**QUESTION, "purpose": "claim_review", "evidence_paths": ["report.md"]}
    code, clients, outcome = exercise(envelope, [
        {"consult": [request, request]}, {"cost_nano": 224926250000}, {"cost_nano": 83767750000}])
    assert code == 0 and outcome["status"] == "completed"
    assert len(clients) == 3 and all(result.result_type == "success" for result in clients[0].session.tool_results)
    assert clients[2].session.options["session_limits"]["max_ai_credits"] == 735.07375
    assert clients[0].session.updates == [40, 775.07375, 40, 691.306]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 318694000000


def test_adviser_may_spend_more_than_old_quota_without_renewing_campaign(project, ledger):
    root, config, record = project
    envelope = arm((root, replace(config, max_ai_credits=1000), record), ledger,
                   consultant=True, allowance=1000 * credits.NANO)
    code, clients, _ = exercise(envelope, [{"consult": [QUESTION]}, {"cost": 500}])
    assert code == 0 and clients[0].session.updates == [40, 500]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 510 * credits.NANO


def test_unlimited_campaign_has_no_hidden_adviser_quota(project, ledger):
    root, config, record = project
    envelope = arm((root, replace(config, max_ai_credits="unlimited"), record), ledger,
                   consultant=True, allowance=None)
    code, clients, _ = exercise(envelope, [{"consult": [QUESTION, QUESTION]}, {"cost": 500}, {"cost": 500}])
    assert code == 0 and len(clients) == 3
    assert all(client.session.options["session_limits"] is None for client in clients)
    assert clients[0].session.updates == []
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 1010 * credits.NANO


@pytest.mark.parametrize("update", [1, 2])
def test_failed_transfer_never_resumes_caller_or_buys_overlapping_inference(project, ledger, update):
    envelope = arm(project, ledger, consultant=True)
    code, clients, outcome = exercise(envelope, [{"consult": [QUESTION], "update_error_at": update}, {}])
    assert code == 1 and outcome["status"] == "failed"
    assert "allowance transfer failed" in outcome["reason"]
    assert len(clients) == update
    assert clients[0].session.tool_results == [] and clients[0].session.aborted
    assert all(client.stopped for client in clients)
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == update * 10 * credits.NANO


def test_transfer_acknowledgment_precedes_adviser_and_restore_precedes_tool_result(project, ledger, monkeypatch):
    from tests.test_simple_provider import FakeSession
    envelope = arm(project, ledger, consultant=True)
    sequence = []
    update, send, finish = FakeSession.update, FakeSession.send, FakeSession.finish

    async def updated(session, request):
        value = await update(session, request)
        sequence.append(("limit", request.session_limits.max_ai_credits))
        return value

    async def sent(session, prompt):
        sequence.append(("send", "adviser" if "available_tools" in session.options else "researcher"))
        return await send(session, prompt)

    async def finished(session):
        if "available_tools" not in session.options:
            sequence.append(("caller_result", len(session.tool_results)))
        return await finish(session)

    monkeypatch.setattr(FakeSession, "update", updated)
    monkeypatch.setattr(FakeSession, "send", sent)
    monkeypatch.setattr(FakeSession, "finish", finished)
    code, _, _ = exercise(envelope, [{"consult": [QUESTION]}, {}])
    assert code == 0
    assert sequence == [("send", "researcher"), ("limit", 40), ("send", "adviser"),
                        ("limit", 290), ("caller_result", 1)]
    records = [read_json(path) for path in sorted((Path(envelope.root) / "sessions" / "researcher").glob("allowance-*.json"))]
    assert [(value["previous_nano"], value["new_nano"]) for value in records] == [
        (300 * credits.NANO, 40 * credits.NANO), (40 * credits.NANO, 290 * credits.NANO)]
    assert all(value["tool_call_id"] == "call-0" for value in records)


@pytest.mark.parametrize("state", ["unowned", "wrong_call", "idle", "aborting", "exhausted"])
def test_tool_boundary_transfer_requires_exact_blocked_unexhausted_caller(project, ledger, state):
    from tests.test_simple_provider import FakeClient
    envelope = arm(project, ledger, consultant=True)
    client = FakeClient({})
    session = provider.Session(envelope, "researcher", envelope.id, 300 * credits.NANO, client, time.monotonic() + 60)

    async def check():
        try:
            await session.start()
            session.waiting = True
            session.consultation_call_id = None if state == "unowned" else "other" if state == "wrong_call" else "owned"
            if state == "idle":
                session.idle.set()
            session.aborting = state == "aborting"
            session.limit_reached = state == "exhausted"
            with pytest.raises(ValueError, match="exact blocked"):
                await session.set_consultation_allowance(30 * credits.NANO, "owned")
            assert client.session.updates == []
        finally:
            await session.close()

    asyncio.run(check())


def test_failed_artifact_publication_cannot_claim_completed_consultation(project, ledger):
    root, _, _ = project
    (root / "artifacts").write_text("An existing ordinary file", encoding="utf-8")
    envelope = arm(project, ledger, consultant=True)
    code, clients, _ = exercise(envelope, [{"consult": [QUESTION]}, {}])
    assert code == 0 and clients[0].session.tool_results[0].result_type == "failure"
    assert (root / "artifacts").read_text() == "An existing ordinary file"
    response = read_json(Path(envelope.root) / "sessions" / "consult-0" / "response.json")
    assert response["status"] == "failed"


def test_incomplete_web_retention_is_explicit_not_a_valid_brief(tmp_path):
    audit = consultation.EvidenceAudit(tmp_path)
    request = {"toolName": "web_fetch", "toolArgs": {"url": "https://example.org"},
               "toolResult": "x" * (128 * 1024)}
    audit.before_web(request, {})
    result = audit.after_web(request, {})
    assert audit.error and result["additionalContext"]
    assert audit.count == 0


def test_consultations_are_unavailable_after_the_coach_decides(project, ledger):
    from labgoblin import provider
    envelope = arm(project, ledger, coach=True, consultant=True)
    caller = SimpleNamespace(id="fixed-caller", waiting=True, aborting=False)
    pool = provider.Consultations(envelope, {}, [], 0, None, None)
    tool = pool.tool(caller, decision={"decision": "accept"})
    result = asyncio.run(tool.handler(SimpleNamespace(
        session_id=caller.id, tool_call_id="fixed-call", arguments=QUESTION)))
    assert result.result_type == "failure"
    assert not pool.advisers()


def test_shutdown_does_not_cancel_in_progress_adviser_settlement_twice(project, ledger):
    from labgoblin import provider
    envelope = arm(project, ledger, consultant=True)
    settled = []

    async def check():
        cleanup_started = asyncio.Event()

        async def interrupted():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await asyncio.sleep(0)
                settled.append(True)

        pool = provider.Consultations(envelope, {}, [], 0, None, None)
        pool.active = asyncio.create_task(interrupted())
        await asyncio.sleep(0)
        pool.active.cancel()
        await cleanup_started.wait()
        await pool.close()

    asyncio.run(check())
    assert settled == [True]


@pytest.mark.parametrize("tamper", ["duplicate", "gap", "session", "prompts", "disabled"])
def test_adviser_usage_requires_exact_authorized_sessions(project, ledger, tamper):
    envelope = arm(project, ledger, consultant=True)
    exercise(envelope, [{"consult": [QUESTION]}, {}])
    path = Path(envelope.root) / "usage.json"
    usage = read_json(path)
    adviser = usage["sessions"][1]
    if tamper == "duplicate":
        usage["sessions"].append(dict(adviser))
        usage["totalNanoAiu"] += credits.usage_nano(adviser["usage"])
    elif tamper == "gap":
        adviser["role"] = "consult-1"
        adviser["session_id"] = str(uuid.uuid5(uuid.UUID(envelope.id), "consult-1"))
    elif tamper == "session":
        adviser["session_id"] = str(uuid.uuid4())
    elif tamper == "prompts":
        adviser["prompts"] = 2
    else:
        envelope = replace(envelope, config={**envelope.config, "consultant": False})
        usage["envelope_digest"] = envelope.digest
    atomic_json(path, usage)
    with pytest.raises(ValueError):
        runtime.usage_record(envelope, complete=True)
