import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
import threading
from types import SimpleNamespace

import pytest

from labgoblin import checkins, consultation, credits, guidance, provider, runtime
from labgoblin.config import Config
from labgoblin.inspection_evidence import Evidence
from labgoblin.protocol import UncertainExecution
from labgoblin.storage import atomic_json, read_json
from tests.conftest import arm
from tests.test_simple_provider import Event, FakeClient, FakeSession, exercise
from tests.test_simple_runtime import fixture_provider


@pytest.fixture
def due(monkeypatch):
    original = checkins.Checkins.__init__
    def initialize(self, *args):
        original(self, *args)
        self.next_review = 0
    monkeypatch.setattr(checkins.Checkins, "__init__", initialize)


def test_frozen_configs_and_prompts_do_not_acquire_checkins(project, ledger, due):
    assert Config.parse({"version": 5}, frozen=True).goal_review_interval_seconds == 0
    assert Config.parse({"version": 5}).goal_review_interval_seconds == 10800
    for bad in (-1, True, 0.1, "10800"):
        with pytest.raises(ValueError):
            Config(goal_review_interval_seconds=bad)
    with pytest.raises(ValueError, match="capability"):
        checkins.enabled({"research_checkins_version": True})
    envelope = arm(project, ledger, coach=True)
    code, clients, _ = exercise(envelope, [
        {"pre_tools": ["read_file"]}, {"decision": "accept"}, {"decision": "accept"}])
    assert code == 0 and len(clients) == 3
    assert all(value == {} for value in clients[0].session.hook_results)
    assert not (Path(envelope.root) / "checkins").exists()


@pytest.mark.parametrize("unlimited", [False, True])
def test_mid_turn_review_coalesces_parallel_hooks_and_preserves_stopping(
        project, ledger, due, unlimited):
    root, config, record = project
    config = replace(config, max_ai_credits="unlimited" if unlimited else 300,
                     coach_model="model-free")
    envelope = arm((root, config, record), ledger, coach=True, checkins=True,
                   allowance=None if unlimited else 300 * credits.NANO)
    code, clients, outcome = exercise(envelope, [
        {"pre_tools": ["read_file", "powershell"], "parallel_hooks": True},
        {"answer": "The goal gap remains; retaining the current direction is reasonable.",
         "trajectory_reads": [{}, {"path": "outside"}]},
        {"decision": "accept"}, {"decision": "accept", "progress": "meaningful"}])
    assert code == 0, outcome
    researcher, reviewer = clients[0].session, clients[1].session
    assert len(clients) == 4 and len(researcher.prompts) == 2
    assert len(outcome["reviews"]) == 2
    feedback = [value["additionalContext"] for value in researcher.hook_results if value]
    assert len(feedback) == 1 and "staying the course" in feedback[0].lower()
    assert "goal gap" in feedback[0]
    assert "available_tools" not in researcher.options
    assert not researcher.options.get("tools")
    assert guidance.CONSULT_CALLER not in researcher.options["system_message"]["content"]
    assert reviewer.options["system_message"]["content"] == guidance.GOAL_REVIEW
    assert reviewer.options["model"] == "model-free"
    assert {tool.name for tool in reviewer.options["tools"]} == {
        "labgoblin_read_evidence", "labgoblin_list_evidence", "labgoblin_read_trajectory"}
    assert reviewer.tool_results[0].result_type == "success"
    assert "assistant.turn_start" in reviewer.tool_results[0].text_result_for_llm
    assert reviewer.tool_results[1].result_type == "failure"
    if unlimited:
        assert researcher.updates == [] and reviewer.options["session_limits"] is None
    else:
        assert researcher.updates[:2] == [40, 260]
        assert reviewer.options["session_limits"] == {"max_ai_credits": 230.0}
    directory = Path(envelope.root)
    request = read_json(directory / "sessions" / "goal-review-0" / "request.json")
    assert request["tool_call_id"] is None and len(request["hook_id"]) == 32
    assert read_json(directory / "checkins" / request["hook_id"] / "response.json")["status"] == "success"
    if not unlimited:
        transfer = read_json(directory / "sessions" / "researcher" / "allowance-1-0.json")
        assert transfer["tool_call_id"] is None and transfer["hook_id"] == request["hook_id"]
    usage = runtime.usage_record(envelope, complete=True)
    assert [row["role"] for row in usage["sessions"]] == [
        "researcher", "goal-review-0", "coach-0", "coach-1"]
    assert all(client.stopped for client in clients)
    hook_path = directory / "checkins" / request["hook_id"] / "request.json"
    hook = read_json(hook_path)
    for changed in ({"prompt": 0}, {"prompt": 3}, {"interval_seconds": 1}, {"trigger": "invented"}):
        atomic_json(hook_path, {**hook, **changed})
        with pytest.raises(ValueError):
            runtime.usage_record(envelope, complete=True)
    atomic_json(hook_path, hook)
    assert runtime.usage_record(envelope, complete=True) == usage
    request["hook_id"] = "0" * 32
    atomic_json(directory / "sessions" / "goal-review-0" / "request.json", request)
    with pytest.raises((ValueError, OSError)):
        runtime.usage_record(envelope, complete=True)


@pytest.mark.parametrize("script, expected", [
    ({"update_error_at": 1}, "failed"),
    ({"update_error_at": 2}, "failed"),
    ({}, "failed"),
    ({}, "budget_exhausted"),
])
def test_unsafe_review_never_releases_the_hook(project, ledger, due, script, expected):
    envelope = arm(project, ledger, coach=True, checkins=True)
    adviser = ({"missing_usage": True} if not script and expected == "failed"
               else {"cost": 250} if expected == "budget_exhausted" else {})
    code, clients, outcome = exercise(envelope, [{"pre_tools": ["read_file"], **script}, adviser])
    assert code == 1 and outcome["status"] == expected, outcome
    assert not clients[0].session.hook_results
    assert clients[0].session.aborted and all(client.stopped for client in clients)
    assert not (Path(envelope.root) / "checkins" / "deadline-notice.json").exists()
    if adviser.get("missing_usage"):
        assert not (Path(envelope.root) / "usage.json").exists()


@pytest.mark.parametrize("event", ["session.info", "session.error", "tool.execution_complete"])
@pytest.mark.parametrize("idle", [False, True])
def test_exhausted_caller_checkin_stops_without_another_review(
        project, ledger, due, monkeypatch, event, idle):
    envelope = arm(project, ledger, coach=True, checkins=True)
    hooks = FakeSession.hooks_and_finish

    async def exhaust_before_hook(session):
        if event == "session.info":
            session.event(event, info_type="session_limits",
                          message="Session limit reached. Increase or unset the session limit to continue.")
        elif event == "session.error":
            session.event(event, message="Session credit quota exhausted", error_type="session_limits_exhausted")
        else:
            session.event(event, error={"code": "session_limits_exhausted"})
        if idle:
            session.event("session.idle")
        await hooks(session)

    monkeypatch.setattr(FakeSession, "hooks_and_finish", exhaust_before_hook)
    code, clients, outcome = exercise(envelope, [{"pre_tools": ["read_file"]}])
    assert code == 1 and outcome["status"] == "budget_exhausted", outcome
    assert outcome["primary_outcome"]["status"] == "budget_exhausted"
    assert len(clients) == 1 and len(clients[0].session.prompts) == 1
    assert not clients[0].session.hook_results and not clients[0].session.updates
    assert not (Path(envelope.root) / "checkins").exists()
    assert clients[0].stopped and not outcome["finalization_errors"]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 10 * credits.NANO


@pytest.mark.parametrize("invalid", ["request_identity", "context_identity", "ordinary_failure",
                                   "inactive_without_limit"])
def test_limit_checkin_does_not_hide_unrelated_caller_failures(project, ledger, invalid):
    envelope = arm(project, ledger, coach=True, checkins=True)

    async def scenario():
        caller = provider.Session(envelope, "researcher", "exact", envelope.allowance_nano,
                                  FakeClient({}), time.monotonic() + 120)
        pool = provider.Consultations(envelope, {}, [caller], caller.deadline, None, lambda: None)
        timer = caller.checkins = checkins.Checkins(caller, pool, caller.deadline)
        caller.waiting = True
        caller.limit_reached = True
        request, context = {"sessionId": caller.id, "toolName": "read_file"}, {"session_id": caller.id}
        if invalid == "request_identity":
            request["sessionId"] = "another-session"
        elif invalid == "context_identity":
            context["session_id"] = "another-session"
        elif invalid == "ordinary_failure":
            caller.failure = "Retained ordinary provider failure"
        else:
            caller.limit_reached = False
            caller.idle.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await timer.before_tool(request, context)
            assert caller.failure and not caller.failure_is_limit
            if invalid == "ordinary_failure":
                assert caller.failure == "Retained ordinary provider failure"
            assert pool.active is None and not timer.directory.exists()
        finally:
            caller.events.close()

    asyncio.run(scenario())


def test_settled_review_failure_returns_explicit_feedback_without_stopping(project, ledger, due):
    envelope = arm(project, ledger, coach=True, checkins=True)
    code, clients, outcome = exercise(envelope, [
        {"pre_tools": ["read_file"]}, {"answer": ""},
        {"decision": "accept"}, {"decision": "accept"}])
    assert code == 0, outcome
    assert "nonempty" in clients[0].session.hook_results[0]["additionalContext"]
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 50 * credits.NANO


@pytest.mark.parametrize("configured,seconds,expected", [
    (None, 86400, 1800), (2400, 86400, 2400), (None, 1200, 1050),
])
def test_goal_review_uses_its_own_timeout_without_consuming_checkpoint_time(
        project, ledger, due, monkeypatch, configured, seconds, expected):
    root, config, record = project
    config = replace(config, timeout_seconds=seconds, consultation_timeout_seconds=60)
    if configured is not None:
        config = replace(config, goal_review_timeout_seconds=configured)
    envelope = arm((root, config, record), ledger, coach=True, checkins=True)
    remaining = {}
    ask = provider.Session.ask

    async def observe(session, prompt, checkpoint, **kwargs):
        remaining[session.role] = session.deadline - time.monotonic()
        return await ask(session, prompt, checkpoint, **kwargs)

    monkeypatch.setattr(provider.Session, "ask", observe)
    code, clients, outcome = exercise(envelope, [
        {"pre_tools": ["read_file"]}, {"answer": "Staying the course is reasonable."},
        {"decision": "accept"}, {"decision": "accept"}], seconds=seconds)
    assert code == 0, outcome
    assert expected - 5 < remaining["goal-review-0"] <= expected
    assert len(clients[0].session.prompts) == 2
    assert all(client.stopped for client in clients)
    assert runtime.usage_record(envelope, complete=True)["complete"]


@pytest.mark.parametrize("timed", [False, True])
def test_cancellation_during_adviser_settlement_preserves_final_usage(
        project, ledger, due, monkeypatch, timed):
    envelope = arm(project, ledger, coach=timed, consultant=not timed, checkins=timed)
    ask, abort = provider.Session.ask, FakeSession.abort

    async def expire_adviser(session, prompt, checkpoint, **kwargs):
        if session.role == "researcher":
            return await ask(session, prompt, checkpoint, **kwargs)
        def expire():
            checkpoint()
            session.deadline = time.monotonic() + provider.GRACE_SECONDS - 1
        return await ask(session, prompt, expire, **kwargs)

    async def cancel_during_abort(session):
        await abort(session)
        if session.options["on_event"].__self__.role != "researcher":
            Path(envelope.root, "cancel").write_text("operator stop\n", encoding="utf-8")
            await asyncio.sleep(0.5)

    monkeypatch.setattr(provider.Session, "ask", expire_adviser)
    monkeypatch.setattr(FakeSession, "abort", cancel_during_abort)
    caller = {"pre_tools": ["read_file"]} if timed else {"consult": [{"question": "Check the evidence."}]}
    code, clients, outcome = exercise(envelope, [caller, {"wait": True}])
    assert code == 130 and outcome["status"] == "cancelled", outcome
    assert not clients[0].session.hook_results and not clients[0].session.tool_results
    assert all(client.stopped for client in clients)
    assert all(session["complete"] for session in outcome["sessions"])
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 20 * credits.NANO


def test_insufficient_shared_funds_skips_review_without_losing_stopping_reserve(project, ledger, due):
    root, config, record = project
    envelope = arm((root, replace(config, max_ai_credits=80), record), ledger,
                   coach=True, checkins=True, allowance=80 * credits.NANO)
    code, clients, outcome = exercise(envelope, [
        {"pre_tools": ["read_file"]}, {"decision": "accept"}, {"decision": "accept"}])
    assert code == 0, outcome
    assert len(clients) == 3
    assert "no adviser started" in clients[0].session.hook_results[0]["additionalContext"]
    assert not (Path(envelope.root) / "sessions" / "goal-review-0").exists()


def test_deadline_notice_once_without_prompt_or_deadline_extension(project, ledger, due, monkeypatch):
    monkeypatch.setattr(checkins.Checkins, "checkpoint_at", lambda self: time.monotonic() - 1)
    envelope = arm(project, ledger, checkins=True)
    code, clients, outcome = exercise(envelope, [{"pre_tools": ["powershell", "read_file"]}])
    assert code == 0 and len(clients) == 1, outcome
    assert len(clients[0].session.prompts) == 1
    assert clients[0].session.hook_results == [{"additionalContext": guidance.DEADLINE_CHECKPOINT}, {}]
    notice = read_json(Path(envelope.root) / "checkins" / "deadline-notice.json")
    assert notice["delivery"] == "pre_tool_hook_response" and notice["prompt"] == 1
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 10 * credits.NANO


@pytest.mark.parametrize("coach, interval, tools", [(False, 1, ["read_file"]), (True, 0, ["read_file"]),
                                                 (True, 1, [])])
def test_disabled_or_missing_boundary_starts_no_goal_review(project, ledger, due, coach, interval, tools):
    root, config, record = project
    envelope = arm((root, replace(config, goal_review_interval_seconds=interval), record), ledger,
                   coach=coach, checkins=True)
    scripts = [{"pre_tools": tools}] + ([{"decision": "accept"}, {"decision": "accept"}] if coach else [])
    code, clients, outcome = exercise(envelope, scripts)
    assert code == 0, outcome
    assert len(clients) == len(scripts)
    assert not (Path(envelope.root) / "checkins").exists()


def test_due_review_defers_near_checkpoint_window(project, ledger, due, monkeypatch):
    monkeypatch.setattr(checkins.Checkins, "checkpoint_at", lambda self: time.monotonic() + 40)
    envelope = arm(project, ledger, coach=True, checkins=True)
    code, clients, outcome = exercise(envelope, [
        {"pre_tools": ["read_file"]}, {"decision": "accept"}, {"decision": "accept"}])
    assert code == 0 and len(clients) == 3, outcome
    assert all(value == {} for value in clients[0].session.hook_results)
    assert not (Path(envelope.root) / "checkins").exists()


def test_unexpected_resumption_and_forged_hook_transfer_fail_closed(project, ledger):
    envelope = arm(project, ledger, checkins=True)
    async def scenario():
        caller = provider.Session(envelope, "researcher", "exact", envelope.allowance_nano,
                                  FakeClient({}), time.monotonic() + 120)
        pool = provider.Consultations(envelope, {}, [caller], caller.deadline, None, lambda: None)
        timer = caller.checkins = checkins.Checkins(caller, pool, caller.deadline)
        caller.waiting = True
        caller.consultation_call_id = "unowned"
        try:
            async with timer.lock:
                timer.active_hook_id = "owned"
                with pytest.raises(ValueError, match="exact held"):
                    await caller.set_consultation_allowance(40 * credits.NANO, "unowned", hook=True)
                for kind in ("assistant.turn_start", "session.idle"):
                    caller.event(Event(kind))
                    assert "before its owned check-in returned" in caller.failure
        finally:
            caller.events.close()
    asyncio.run(scenario())


def test_trajectory_reader_bounds_original_events_and_rejects_other_paths(project, ledger):
    envelope = arm(project, ledger, checkins=True)
    caller = provider.Session(envelope, "researcher", "exact", envelope.allowance_nano,
                              FakeClient({}), time.monotonic() + 120)
    audit = consultation.EvidenceAudit(Path(envelope.root) / "audit")
    tool = checkins.trajectory_tool(caller, audit)
    try:
        event = Event("tool.execution_complete", toolCallId="scientific-work",
                      result={"text": "\u00e9\n" * 30000}).to_dict()
        caller.events.append(event, prompt=1)
        offset, fragments = 0, []
        while offset is not None:
            response = tool.handler(SimpleNamespace(arguments={"event": 0, "offset": offset}))
            assert response.result_type == "success"
            assert len(response.text_result_for_llm.encode()) <= 20000
            page = json.loads(response.text_result_for_llm)
            assert page["text"] and page["role"] == "researcher"
            fragments.append(page["text"])
            assert page["next_offset"] is None or page["next_offset"] > offset
            offset = page["next_offset"]
        assert json.loads("".join(fragments)) == event
        assert audit.count == len(fragments) > 1
        for args in ({"path": "..\\secret"}, {"role": "coach-0"}, {"offset": -1}):
            assert tool.handler(SimpleNamespace(arguments=args)).result_type == "failure"
    finally:
        caller.events.close()


def test_active_owned_work_defers_and_cancelled_hook_does_not_resume(project, ledger):
    envelope = arm(project, ledger, coach=True, checkins=True)
    async def scenario():
        caller = provider.Session(envelope, "researcher", "exact", 270 * credits.NANO,
                                  FakeClient({}), time.monotonic() + 120)
        pool = provider.Consultations(envelope, {}, [caller], caller.deadline, None, lambda: None)
        timer = caller.checkins = checkins.Checkins(caller, pool, caller.deadline)
        caller.waiting = True
        timer.next_review = 0
        request, context = {"sessionId": caller.id, "toolName": "powershell"}, {"session_id": caller.id}
        try:
            pool.active = object()
            assert await timer.before_tool(request, context) == {}
            pool.active = None
            pool.evaluation = SimpleNamespace(active=object())
            assert await timer.before_tool(request, context) == {}
            assert timer.next_review == 0
            pool.evaluation.active = None
            timer.reviews_allowed = False
            assert await timer.before_tool(request, context) == {}
            assert not timer.directory.exists()
            Path(envelope.root, "cancel").write_text("stop", encoding="utf-8")
            with pytest.raises(asyncio.CancelledError):
                await timer.before_tool(request, context)
            assert "Operator cancellation" in caller.failure
        finally:
            caller.events.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("shutdown_delays", [
    {}, {"session.detach": 4}, {"runtime.shutdown": 6},
], ids=["normal", "slow-detach", "slow-runtime-stop"])
def test_native_sdk_checkin_before_first_answer_and_owned_experiment_survives(
        project, ledger, monkeypatch, shutdown_delays):
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(
        config, model="model-free", coach_model="fixture-coach", coach=True,
        goal_review_interval_seconds=1, timeout_seconds=180).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, mode="goal_checkins")
    atomic_json(root / "fixture-provider.json", {
        **read_json(root / "fixture-provider.json"), "models": ["model-free", "fixture-coach"],
        "shutdown_delays": {"goal-review": shutdown_delays}})
    result = runtime.run(root)
    assert result["status"] == "completed", result
    directory = root / ".labgoblin" / "runs" / result["id"]
    sessions = result["usage"]["sessions"]
    assert [item["role"] for item in sessions] == ["researcher", "goal-review-0", "coach-0", "coach-1"]
    assert sessions[0]["prompts"] == 2
    assert all(item["prompts"] == 1 for item in sessions[1:])
    assert [item["model"] for item in sessions] == ["model-free"] + ["fixture-coach"] * 3
    request = read_json(directory / "sessions" / "goal-review-0" / "request.json")
    assert read_json(directory / "checkins" / request["hook_id"] / "request.json")["prompt"] == 1
    observed = read_json(root / "fixture-checkin-observed.json")
    assert observed["experiment_returncode"] == 0 and observed["experiment_output"] == "experiment survived"
    assert "original intended use" in observed["feedback"]
    shutdown = read_json(directory / "sessions" / "goal-review-0" / "shutdown.json")
    assert shutdown["usage_complete"] and not shutdown["errors"]
    assert result["quiescent"] and runtime.payload.tree_state(directory / "process", result["id"]) == "dead"
    view = Evidence(root, runtime.project_record(root)[2]["id"]).session({
        "run": result["id"], "role": "goal-review-0"})
    assert view["events"] and not view["live"] and view["receipt_verified"]
    assert not ledger.rows()


@pytest.mark.parametrize("late_hook", [False, True])
def test_native_sdk_limit_stops_without_new_calls_and_settles_usage(
        project, ledger, monkeypatch, late_hook):
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(
        config, coach=True, goal_review_interval_seconds=1, timeout_seconds=180).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, mode="checkin_quota")
    atomic_json(root / "fixture-provider.json", {
        **read_json(root / "fixture-provider.json"), "late_hook": late_hook})
    result = runtime.run(root)
    assert result["status"] == "budget_exhausted" and result["quiescent"], result
    assert result["metadata"]["provider"]["primary_outcome"]["status"] == "budget_exhausted"
    assert not result["metadata"]["provider"]["finalization_errors"]
    assert result["usage"]["complete"] and result["usage"]["totalNanoAiu"] == 10 * credits.NANO
    assert len(result["usage"]["sessions"]) == 1
    assert result["usage"]["sessions"][0]["prompts"] == 1
    assert not (root / "fixture-exhausted-hook-released.json").exists()
    calls = [json.loads(line)["method"] for line in (root / "fixture-rpc.jsonl").read_text().splitlines()]
    assert calls.count("session.create") == calls.count("session.send") == 1
    assert "session.options.update" not in calls[calls.index("session.send") + 1:]
    directory = root / ".labgoblin" / "runs" / result["id"]
    assert read_json(directory / "receipt.json") == result
    assert runtime.status(root)["budget"]["active"] == []
    assert not ledger.rows()


@pytest.mark.parametrize("failure", ["wait", "missing_usage"])
def test_native_sdk_checkin_cancellation_or_unknown_usage_never_returns_advice(
        project, ledger, monkeypatch, failure):
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(
        config, coach=True, goal_review_interval_seconds=1, timeout_seconds=180).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, mode="goal_checkins")
    atomic_json(root / "fixture-provider.json", {
        **read_json(root / "fixture-provider.json"), "checkin_failure": failure})
    errors = []
    def cancel():
        until = time.monotonic() + 40
        while time.monotonic() < until:
            if (root / "fixture-goal-review-ready").exists():
                runtime.stop(root)
                return
            time.sleep(0.05)
        errors.append("Goal review never started")
    thread = threading.Thread(target=cancel) if failure == "wait" else None
    if thread:
        thread.start()
    try:
        result = runtime.run(root)
    finally:
        if thread:
            thread.join(timeout=45)
    assert not errors and (thread is None or not thread.is_alive())
    assert result["status"] == ("cancelled" if thread else "failed"), result
    assert not (root / "fixture-checkin-observed.json").exists()
    assert not ledger.rows()
    if thread:
        assert result["usage"]["totalNanoAiu"] == 20 * credits.NANO
    else:
        assert result["usage"] is None
        with pytest.raises(UncertainExecution, match="Final credit usage"):
            runtime.run(root, resume=True)
