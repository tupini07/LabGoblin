import asyncio
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from labgoblin import checkins, cli, ideas, payload, provider, runtime, supervisor
from labgoblin.config import Config
from labgoblin.protocol import LaunchReceipt
from labgoblin.processes import own_handle
from labgoblin.storage import digest, hash_file, publish_json, read_json
from tests.conftest import arm
from tests.test_closure_critic import closing
from tests.test_ideas import publish
from tests.test_simple_provider import Event, FakeClient, FakeSession, Value, exercise
from tests.test_simple_runtime import fixture_provider


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize("every,now,expected", [
    (1, "2026-03-07T17:00:00+00:00", "2026-03-07T17:30:00+00:00"),
    (1, "2026-03-07T18:00:00+00:00", "2026-03-08T16:30:00+00:00"),
    (2, "2026-03-07T18:00:00+00:00", "2026-03-09T16:30:00+00:00"),
    (2, "2026-03-21T16:30:00+00:00", "2026-03-23T16:30:00+00:00"),
])
def test_calendar_anchor_and_missed_occurrences(every, now, expected):
    schedule = supervisor.Schedule("09:30", "America/Los_Angeles", "2026-03-07", every)
    assert schedule.next_after(stamp(now)) == stamp(expected)


def test_dst_gap_moves_forward_and_fold_occurs_once():
    gap = supervisor.Schedule("02:30", "America/Los_Angeles", "2026-03-08")
    assert gap.next_after(stamp("2026-03-08T09:00:00+00:00")) == stamp("2026-03-08T10:00:00+00:00")
    fold = supervisor.Schedule("01:30", "America/Los_Angeles", "2026-11-01")
    assert fold.next_after(stamp("2026-11-01T07:00:00+00:00")) == stamp("2026-11-01T08:30:00+00:00")
    assert fold.next_after(stamp("2026-11-01T08:40:00+00:00")) == stamp("2026-11-02T09:30:00+00:00")


def test_skipped_calendar_day_and_date_overflow_are_explicit():
    skipped = supervisor.Schedule("09:00", "Pacific/Apia", "2011-12-30")
    assert skipped.next_after(stamp("2011-12-29T12:00:00+00:00")) == stamp("2011-12-30T10:00:00+00:00")
    last = supervisor.Schedule("09:00", "UTC", "2026-01-01", every_days=10**12)
    with pytest.raises(ValueError, match="supported calendar"):
        last.next_after(stamp("2026-01-01T10:00:00+00:00"))


@pytest.mark.parametrize("changes", [
    {"time": "9am"}, {"time": "24:00"}, {"timezone": "not/a/timezone"},
    {"start_date": "2026-02-30"}, {"every_days": 0}, {"every_days": True}, {"extra": 1},
])
def test_invalid_calendar_rejected(changes):
    with pytest.raises(ValueError):
        Config.parse({"version": 5, "supervisor_review": {
            "time": "09:00", "timezone": "UTC", "start_date": "2026-01-01", **changes}})


def test_schedule_round_trip_and_historical_omission(tmp_path):
    config = Config(supervisor_review=supervisor.Schedule("09:00", "UTC", "2026-01-01", 2))
    path = tmp_path / "labgoblin.toml"
    path.write_text(config.toml(), encoding="utf-8")
    assert Config.load(tmp_path) == config
    assert Config.parse(config.to_dict(), frozen=True) == config
    assert Config.parse({"version": 5}, frozen=True).supervisor_review is None


def meeting_envelope(project, ledger, *, due=None, **options):
    root, config, record = project
    config = replace(config, supervisor_review=supervisor.Schedule("09:00", "UTC", "2026-01-01"))
    return arm((root, config, record), ledger, appointment=due or time.time() - 1, **options)


def test_due_meeting_keeps_one_prompt_skips_coach_and_retains_idea_response(project, ledger, tmp_path, monkeypatch):
    publish(project[0], tmp_path)
    envelope = meeting_envelope(project, ledger, coach=True, ideas=True)
    monkeypatch.setattr(payload, "tree_members", lambda *_: [{"pid": 1, "created": 2}])
    code, clients, outcome = exercise(envelope, [{"pre_tools": ["read_file"]}])
    assert code == 0 and outcome["status"] == "checkpointed"
    assert outcome["checkpoint_reason"] == "supervisor_review"
    assert len(clients) == 1 and len(clients[0].session.prompts) == 1
    assert supervisor.NOTICE in clients[0].session.prompts[0]
    root = Path(envelope.root)
    assert read_json(root / "supervisor-review" / "checkpoint.json")["prompt"] == 1
    assert read_json(root / "usage.json")["complete"]
    assert list((root / "sessions").glob("*/settled-prompt-1.json"))
    assert ideas.Inbox(project[0]).entries()[0]["response"]["prompt"] == 1
    assert outcome["supervisor_checkpoint_sha256"] == hash_file(root / "supervisor-review" / "checkpoint.json")


def test_runtime_wait_timeout_is_scoped_to_the_owned_meeting_client(project, ledger, monkeypatch):
    import copilot
    monkeypatch.setattr(copilot, "CopilotClient", lambda **options: options)
    monkeypatch.setenv("COPILOT_TASK_WAIT_TIMEOUT_SECONDS", "1")
    envelope = meeting_envelope(project, ledger)
    options = provider.client_for(envelope)
    assert options["env"]["COPILOT_TASK_WAIT_TIMEOUT_SECONDS"] == str(supervisor.TASK_WAIT_SECONDS)
    assert options["env"]["PATH"] == os.environ["PATH"]
    assert provider.client_for(envelope, coach=True)["env"] is None
    assert os.environ["COPILOT_TASK_WAIT_TIMEOUT_SECONDS"] == "1"


def test_late_followup_preserves_settled_idea_response_and_latest_checkpoint(project, ledger, tmp_path, monkeypatch):
    publish(project[0], tmp_path)
    envelope = meeting_envelope(project, ledger, ideas=True)
    monkeypatch.setattr(payload, "tree_members", lambda *_: [])
    original = FakeSession.metrics
    answer_path = Path(envelope.root) / "sessions" / "researcher" / "answer-1.txt"
    emitted = False

    async def metrics(session):
        nonlocal emitted
        value = await original(session)
        if answer_path.exists() and not emitted:
            emitted = True
            session.event("assistant.turn_start")
            session.used += 5_000_000_000
            session.event("assistant.message", content="Later automatic result retained.")
            session.event("session.idle")
        return value

    monkeypatch.setattr(FakeSession, "metrics", metrics)
    code, _, outcome = exercise(envelope, [{}])
    assert code == 0, outcome
    assert answer_path.read_text() == "Research answer with retained evidence"
    response = ideas.Inbox(project[0]).entries()[0]["response"]
    assert response["sha256"] == hash_file(answer_path)
    meeting = Path(envelope.root) / "supervisor-review"
    assert (meeting / "response.txt").read_text() == "Later automatic result retained."
    assert read_json(meeting / "checkpoint.json")["latest_response_sha256"] == hash_file(meeting / "response.txt")
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 15_000_000_000


@pytest.mark.parametrize("kind", ["native_child", "tracked_task", "followup_turn", "unknown_tree", "wait_timeout"])
def test_drain_never_treats_idle_alone_as_quiescence(project, ledger, monkeypatch, kind):
    envelope = meeting_envelope(project, ledger)

    async def scenario():
        client = FakeClient({})
        caller = provider.Session(envelope, "researcher", "fixture", envelope.allowance_nano,
                                  client, time.monotonic() + 120)
        caller.supervisor = meeting = supervisor.Meeting(caller, supervisor.binding(envelope))
        members = [{"pid": 1, "created": 2}]
        monkeypatch.setattr(payload, "tree_members", lambda *_: list(members))
        try:
            await caller.start(system="model-free")
            await meeting.start()
            caller.idle.set()
            caller.observed_turn = True
            meeting.request_due()
            if kind == "native_child":
                members.append({"pid": 3, "created": 4})
            elif kind == "tracked_task":
                async def tasks():
                    return Value({"tasks": [{"id": "shell", "status": "running"}]})
                caller.session.rpc.tasks.list = tasks
            elif kind == "followup_turn":
                async def pending(**kwargs):
                    caller.event(Event("assistant.turn_start"))
                    await asyncio.sleep(.01)
                caller.session.rpc.tasks.wait_for_pending = pending
            elif kind == "wait_timeout":
                offset = [0]
                monkeypatch.setattr(supervisor, "time", SimpleNamespace(
                    time=time.time, monotonic=lambda: time.monotonic() + offset[0]))
                async def pending(**kwargs):
                    offset[0] += supervisor.TASK_WAIT_SECONDS
                caller.session.rpc.tasks.wait_for_pending = pending
            else:
                def unknown(*_):
                    raise runtime.UncertainExecution("test membership unavailable")
                monkeypatch.setattr(payload, "tree_members", unknown)
            assert not await meeting.drained()
            await asyncio.wait_for(meeting.task_wait, timeout=5)
            assert not await meeting.drained()
            if kind == "unknown_tree":
                assert "unavailable" in read_json(meeting.directory / "drain.json")["error"]
            if kind == "wait_timeout":
                assert "internal timeout" in read_json(meeting.directory / "drain.json")["error"]
                caller.session.rpc.tasks.wait_for_pending = caller.session.wait_for_tasks
                offset[0] = 0
                assert not await meeting.drained()
                await asyncio.wait_for(meeting.task_wait, timeout=5)
                assert await meeting.drained()
            assert not client.stopped and not caller.session.aborted
        finally:
            await caller.close()
    asyncio.run(scenario())


def test_malformed_tool_event_cannot_fail_open_in_sdk_callback(project, ledger, monkeypatch):
    envelope = meeting_envelope(project, ledger)
    monkeypatch.setattr(payload, "tree_members", lambda *_: [])
    original = FakeSession.send
    async def send(session, prompt):
        await original(session, prompt)
        session.event("tool.execution_start", tool_call_id=None)
        session.event("session.idle")
    monkeypatch.setattr(FakeSession, "send", send)
    code, clients, outcome = exercise(envelope, [{}])
    assert code == 1 and outcome["status"] == "failed"
    assert "Invalid supervisor activity evidence" in outcome["reason"]
    assert clients[0].stopped and not (Path(envelope.root) / "supervisor-review" / "checkpoint.json").exists()


@pytest.mark.parametrize("stage", ["coach-0", "consult-0", "adjudicator-1", "advice", "goal-review-0"])
def test_due_during_support_settles_without_another_research_prompt(project, ledger, monkeypatch, stage):
    root, config, record = project
    config = replace(config, coach=True, consultant=True, coach_model="model-free",
                     closure_critic=stage in ("consult-0", "adjudicator-1"))
    envelope = meeting_envelope((root, config, record), ledger, due=time.time() + 100,
                                coach=stage != "advice", consultant=stage != "coach-0",
                                checkins=stage == "goal-review-0")
    monkeypatch.setattr(payload, "tree_members", lambda *_: [])
    meetings = []
    start = supervisor.Meeting.start
    close = provider.Session.close
    timer = checkins.Checkins.__init__

    async def capture(meeting):
        meetings.append(meeting)
        await start(meeting)

    async def finish_support(session):
        await close(session)
        if session.role == ("consult-0" if stage == "advice" else stage):
            meetings[0].due_at = time.time() - 1

    def due_timer(self, *args):
        timer(self, *args)
        self.next_review = 0

    monkeypatch.setattr(supervisor.Meeting, "start", capture)
    monkeypatch.setattr(provider.Session, "close", finish_support)
    if stage == "goal-review-0":
        monkeypatch.setattr(checkins.Checkins, "__init__", due_timer)
        scripts = [{"pre_tools": ["read_file"]}, {"answer": "Retain the useful control."}]
    elif stage == "advice":
        scripts = [{"consult": [{"question": "Check a useful assumption."}]}, {}]
    else:
        scripts = [{}, {"decision": "accept"}, *closing()]
    code, clients, outcome = exercise(envelope, scripts, seconds=1800)
    assert code == 0 and outcome["checkpoint_reason"] == "supervisor_review", outcome
    assert all(client.stopped for client in clients)
    assert len(clients[0].session.prompts) == (3 if stage == "adjudicator-1" else 2 if stage == "consult-0" else 1)
    assert runtime.usage_record(envelope, complete=True)["complete"]
    if stage == "goal-review-0":
        assert supervisor.NOTICE in clients[0].session.hook_results[0]["additionalContext"]


def test_ready_hook_denies_tools_but_keeps_failure_precedence(project, ledger):
    envelope = meeting_envelope(project, ledger)
    async def scenario():
        caller = provider.Session(envelope, "researcher", "exact", envelope.allowance_nano,
                                  FakeClient({}), time.monotonic() + 120)
        caller.supervisor = supervisor.Meeting(caller, supervisor.binding(envelope))
        caller.supervisor.ready = True
        caller.idle.set()
        timer = checkins.Checkins(caller, None, caller.deadline)
        request, context = {"sessionId": caller.id, "toolName": "read_file"}, {"session_id": caller.id}
        try:
            assert (await timer.before_tool(request, context))["permissionDecision"] == "deny"
            caller.failure = "Retained real failure"
            with pytest.raises(asyncio.CancelledError, match="Retained real failure"):
                await timer.before_tool(request, context)
        finally:
            caller.events.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["cancelled", "budget_exhausted", "failed", "timed_out"])
def test_operational_boundaries_win_over_meeting(project, ledger, monkeypatch, stop):
    envelope = meeting_envelope(project, ledger)
    monkeypatch.setattr(payload, "tree_members", lambda *_: [])
    script = {"wait": True}
    if stop == "cancelled":
        script["cancel"] = str(Path(envelope.root) / "cancel")
    elif stop == "budget_exhausted":
        script = {"info_limit": True}
    elif stop == "failed":
        script = {"error": "ordinary failure"}
    if stop in ("budget_exhausted", "failed"):
        # Emit exact provider failures after the request, without buying another prompt.
        original = provider.Session.event
        def inject(self, event):
            original(self, event)
            if event.type.value == "assistant.turn_start":
                self.limit_reached = stop == "budget_exhausted"
                if stop == "failed":
                    self.failure = "ordinary failure"
        monkeypatch.setattr(provider.Session, "event", inject)
    code, _, result = exercise(envelope, [script], seconds=30.3 if stop == "timed_out" else 120)
    assert code != 0 and result["status"] == stop
    assert result["checkpoint_reason"] != "supervisor_review"
    assert not (Path(envelope.root) / "supervisor-review" / "checkpoint.json").exists()


def finish(envelope, ledger, *, status="interrupted", executed=True, metadata=None, usage=None):
    owner = own_handle(envelope.id)
    assert ledger.claim_consumer(envelope, owner)
    receipt = LaunchReceipt(envelope.id, envelope.digest, status, 1, executed,
                            returncode=1, reason="fixture ended", usage=usage or {"totalNanoAiu": 0},
                            metadata={"supervisor": owner, **(metadata or {})})
    ledger.finish_consumer(receipt)
    return receipt


@pytest.mark.parametrize("failure", ["disconnect", "missing_final_usage", "incomplete_receipt"])
def test_failed_settlement_never_displays_a_successful_review_checkpoint(project, ledger, monkeypatch, failure):
    envelope = meeting_envelope(project, ledger)
    checkpoint = Path(envelope.root) / "supervisor-review" / "checkpoint.json"
    monkeypatch.setattr(payload, "tree_members", lambda *_: [])
    original = FakeSession.metrics
    async def metrics(session):
        if failure == "missing_final_usage" and checkpoint.exists():
            raise RuntimeError("Deliberate missing final usage")
        return await original(session)
    async def disconnect(session):
        if failure == "disconnect":
            raise RuntimeError("Deliberate disconnect failure")
    monkeypatch.setattr(FakeSession, "metrics", metrics)
    monkeypatch.setattr(FakeSession, "disconnect", disconnect)
    code, _, outcome = exercise(envelope, [{}])
    assert checkpoint.exists()
    assert outcome["status"] == ("checkpointed" if failure == "incomplete_receipt" else "failed")
    assert code == (0 if failure == "incomplete_receipt" else 1)
    finish(envelope, ledger, status=outcome["status"], metadata={"provider": outcome},
           usage={"totalNanoAiu": 0, "complete": failure == "disconnect"})
    rows = ledger.runs(project[2]["id"])
    assert supervisor.observation(rows)["state"] == "interrupted"
    with checkpoint.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="settled receipt"):
        supervisor.observation(rows)


def test_due_crash_holds_without_request_and_cannot_be_bypassed_by_config(project, ledger, monkeypatch):
    root, config, record = project
    envelope = meeting_envelope(project, ledger)
    finish(envelope, ledger)
    rows = ledger.runs(record["id"])
    before = json.dumps(rows, sort_keys=True)
    held = supervisor.observation(rows)
    assert held["state"] == "interrupted" and not held["notice_delivered"]
    for wrong in (None, "not-the-run"):
        with pytest.raises(ValueError, match="Supervisor review"):
            supervisor.continuation(rows, wrong, resume=True)
    acknowledgment = supervisor.continuation(rows, envelope.id, resume=True)
    assert acknowledgment == {"run_id": envelope.id, "receipt_sha256": digest(json.loads(rows[0]["receipt"]))}
    assert json.dumps(ledger.runs(record["id"]), sort_keys=True) == before


def test_armed_continuation_consumes_exact_acknowledgment_even_if_proven_not_started(project, ledger):
    envelope = meeting_envelope(project, ledger)
    finish(envelope, ledger)
    rows = ledger.runs(project[2]["id"])
    with pytest.raises(ValueError, match="Supervisor review"):
        supervisor.continuation(rows, envelope.id, resume=False)
    acknowledgment = supervisor.continuation(rows, envelope.id, resume=True)
    continued = arm(project, ledger, reviewed=acknowledgment)
    assert supervisor.observation(ledger.runs(project[2]["id"])) is None
    finish(continued, ledger, status="not_started", executed=False)
    current = ledger.runs(project[2]["id"])
    assert current[0] == rows[0]
    assert supervisor.observation(current) is None
    with pytest.raises(ValueError, match="exact pending"):
        supervisor.continuation(current, envelope.id, resume=True)


def test_active_attach_cannot_acknowledge_a_review(project, ledger):
    envelope = meeting_envelope(project, ledger)
    runtime.bound_ledger(project[0] / ".labgoblin", config=project[1], bind=True)
    with pytest.raises(ValueError, match="not while attaching"):
        runtime.run(project[0], resume=True, reviewed=envelope.id)
    finish(envelope, ledger, status="not_started", executed=False)


@pytest.mark.parametrize("reason,code", [("supervisor_review", 0), ("stalled_reviews", 1)])
def test_cli_routes_exact_reviewed_continuation_and_only_planned_hold_exits_zero(monkeypatch, capsys, reason, code):
    received = {}
    def run(project, **options):
        received.update(project=project, **options)
        return {"status": "checkpointed", "metadata": {"provider": {"checkpoint_reason": reason}}}
    monkeypatch.setattr(runtime, "run", run)
    assert cli.main(["run", "fixture-project", "--resume", "--reviewed", "exact-run"]) == code
    assert received == {"project": "fixture-project", "resume": True, "handoff": False, "reviewed": "exact-run"}
    assert json.loads(capsys.readouterr().out)["status"] == "checkpointed"


@pytest.mark.parametrize("executed", [True, False])
def test_nonstarts_and_runs_finished_before_appointment_do_not_create_late_holds(project, ledger, executed):
    envelope = meeting_envelope(project, ledger, due=time.time() + 100)
    finish(envelope, ledger, status="failed" if executed else "not_started", executed=executed)
    result = supervisor.observation(ledger.runs(project[2]["id"]), now=time.time() + 1000)
    assert not result["requires_review"]


@pytest.mark.parametrize("mode", ["native_child", "tracked_task", "followup_turn", "foreground"])
def test_native_scheduled_checkpoint_waits_for_experiments_and_requires_exact_resume(project, ledger, monkeypatch, mode):
    root, config, record = project
    config = replace(config, timeout_seconds=180,
                     supervisor_review=supervisor.Schedule("09:00", "UTC", "2026-01-01"))
    (root / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "supervisor_review")
    options = read_json(root / "fixture-provider.json")
    options["supervisor_mode"] = mode
    (root / "fixture-provider.json").write_text(json.dumps(options), encoding="utf-8")
    monkeypatch.setattr(supervisor.Schedule, "next_after", lambda *_: time.time() - .1)
    receipt = runtime.run(root)
    assert receipt["status"] == "checkpointed", receipt
    assert receipt["metadata"]["provider"]["checkpoint_reason"] == "supervisor_review"
    assert (root / "fixture-experiment-complete").read_text() == "survived"
    status = runtime.status(root)
    assert payload.tree_state(Path(status["runs"][-1]["directory"]) / "process", receipt["id"]) == "dead"
    assert status["supervisor_review"]["state"] == "awaiting_review"
    directory = Path(status["runs"][-1]["directory"])
    checkpoint = read_json(directory / "supervisor-review" / "checkpoint.json")
    assert receipt["usage"]["complete"]
    assert receipt["metadata"]["provider"]["supervisor_checkpoint_sha256"] == hash_file(
        directory / "supervisor-review" / "checkpoint.json")
    assert checkpoint["latest_response_sha256"] == hash_file(directory / "supervisor-review" / "response.txt")
    if mode == "followup_turn":
        assert (directory / "supervisor-review" / "response.txt").read_text() == "Background result retained; ready for review."
    old_id = status["runs"][-1]["id"]
    old_budget = status["budget"]
    (root / "labgoblin.toml").write_text(replace(config, supervisor_review=None).toml(), encoding="utf-8")
    with pytest.raises(ValueError, match="Supervisor review"):
        runtime.run(root, resume=True)
    if mode == "native_child":
        def fail_preflight():
            raise RuntimeError("Deliberate pre-admission failure")
        with monkeypatch.context() as patch:
            patch.setattr(provider, "capabilities", fail_preflight)
            with pytest.raises(RuntimeError, match="pre-admission"):
                runtime.run(root, resume=True, reviewed=old_id)
        assert runtime.status(root)["supervisor_review"]["run_id"] == old_id
    options["mode"] = "normal"
    (root / "fixture-provider.json").write_text(json.dumps(options), encoding="utf-8")
    continued = runtime.run(root, resume=True, reviewed=old_id)
    assert continued["status"] == "completed", continued
    status = runtime.status(root)
    assert status["supervisor_review"] is None
    latest = Path(status["runs"][-1]["directory"])
    prompt = read_json(latest / "prompt.json")
    assert prompt["supervisor_reviewed"] == {"run_id": old_id, "receipt_sha256": digest(receipt)}
    assert status["runs"][-1]["config"]["timeout_seconds"] == 180
    assert status["budget"]["used_nano"] > old_budget["used_nano"]
    assert continued["metadata"]["provider"]["sessions"][0]["session_id"] != receipt["metadata"]["provider"]["sessions"][0]["session_id"]
