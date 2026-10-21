import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from labgoblin import credits, guidance, provider
from labgoblin.storage import read_json
from tests.conftest import arm


class Value:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return self.value


class Event:
    def __init__(self, kind, **data):
        self.type = SimpleNamespace(value=kind)
        self.data = SimpleNamespace(**data)
        self.raw = {"type": kind, "data": data}

    def to_dict(self):
        return self.raw


class FakeSession:
    def __init__(self, options, script):
        self.options, self.script = options, script
        self.used, self.prompts, self.aborted, self.updates = 0, [], False, []
        self.tool_results = []
        self.tool_worker = None
        self.rpc = SimpleNamespace(
            permissions=SimpleNamespace(set_mode=self.permissions),
            usage=SimpleNamespace(get_metrics=self.metrics),
            options=SimpleNamespace(update=self.update),
            tools=SimpleNamespace(initialize_and_validate=self.initialize_tools, get_current_metadata=self.metadata))

    async def initialize_tools(self):
        return Value({"success": True})

    async def metadata(self):
        if self.script.get("metadata_unavailable"):
            return Value({"tools": None})
        names = [tool.name for tool in self.options.get("tools", [])]
        if not self.script.get("missing_web"):
            names.append("web_fetch")
        return Value({"tools": [{"name": name} for name in names]})

    async def permissions(self, request):
        return Value({"success": True, "mode": request.mode.value})

    async def metrics(self):
        if self.prompts and self.script.get("missing_usage"):
            raise RuntimeError("Deliberate missing adviser usage")
        return Value({"totalNanoAiu": self.used, "model_free_fixture": True})

    async def update(self, request):
        self.updates.append(request.session_limits.max_ai_credits)
        return Value({"success": not self.script.get("update_error", False)})

    def event(self, kind, **data):
        self.options["on_event"](Event(kind, **data))

    async def send(self, prompt):
        self.prompts.append(prompt)
        self.event("assistant.turn_start")
        self.used += self.script.get("cost", 10) * credits.NANO
        if self.script.get("wait"):
            if self.script.get("cancel"):
                Path(self.script["cancel"]).write_text("operator stop\n", encoding="utf-8")
            return
        if self.script.get("consult"):
            self.tool_worker = asyncio.create_task(self.consult_and_finish())
            return
        await self.finish()

    async def consult_and_finish(self):
        if self.script.get("concurrent"):
            tool = next(tool for tool in self.options["tools"] if tool.name == "labgoblin_consult")
            self.tool_results = await asyncio.gather(*(tool.handler(SimpleNamespace(
                session_id=self.options["session_id"], tool_call_id=f"call-{index}",
                tool_name=tool.name, arguments=args)) for index, args in enumerate(self.script["consult"])))
            await self.finish()
            return
        for index, args in enumerate(self.script["consult"]):
            tool = next(tool for tool in self.options["tools"] if tool.name == "labgoblin_consult")
            call = SimpleNamespace(session_id=self.options["session_id"], tool_call_id=f"call-{index}",
                                   tool_name=tool.name, arguments=args)
            result = await tool.handler(call)
            self.tool_results.append(result)
            if self.script.get("duplicate"):
                self.tool_results.append(await tool.handler(call))
            if self.script.get("changed_duplicate"):
                call.arguments = {**args, "question": "A different question under the same call ID"}
                self.tool_results.append(await tool.handler(call))
            if self.script.get("changed_purpose"):
                call.arguments = {**args, "purpose": "claim_review"}
                self.tool_results.append(await tool.handler(call))
        await self.finish()

    async def finish(self):
        script = {**self.script, **self.script.get("turns", [{}] * len(self.prompts))[len(self.prompts) - 1]}
        if self.script.get("error"):
            self.event("session.error", message="ordinary failure", error_type="ordinary")
            self.event("session.error", message="quota failure", error_type="session_limits_exhausted")
        if "decision" in script and not script.get("no_decision"):
            tools = self.options["tools"]
            tool = next(tool for tool in tools if tool.name == "labgoblin_review_decision")
            args = {
                "decision": script["decision"], "reason": script.get("reason", "A meaningful stopping assessment"),
                "question": "Does the result hold on the required second family?",
                "coverage": "Read the supplied goal and retained answer; geometry not independently recomputed",
            }
            if self.prompts[-1].startswith("Stopping review after researcher follow-up"):
                args.update(progress=script.get("progress", "none"),
                            progress_reason=script.get("progress_reason", "The fixture supplies no changed scientific evidence."))
            if "critique_response" in script:
                args["critique_response"] = script["critique_response"]
            if script["decision"] == "needs_operator":
                args.update(dependency="permission", question="May I access the required unavailable input?")
            if script["decision"] == "checkpoint":
                args["checkpoint_reason"] = script["checkpoint_reason"]
            result = await tool.handler(SimpleNamespace(tool_name="labgoblin_review_decision", arguments=args))
            assert result.result_type == script.get("decision_result_type", "success")
        self.event("assistant.message", content=script.get("answer", "Research answer with retained evidence"))
        if self.script.get("quota"):
            self.event("session.info", info_type="session_limits",
                       message="Session limit reached. Increase or unset the session limit to continue.")
        self.event("session.idle")

    async def abort(self):
        self.aborted = True
        if self.tool_worker is not None and not self.tool_worker.done():
            self.tool_worker.cancel()
            try:
                await self.tool_worker
            except asyncio.CancelledError:
                pass
        self.event("session.idle")

    async def disconnect(self):
        pass


class FakeClient:
    def __init__(self, script):
        self.script = script
        self.stopped = False
        self.session = None

    async def start(self):
        if self.script.get("startup_error"):
            raise RuntimeError("Runtime unavailable")

    async def create_session(self, **options):
        self.session = FakeSession(options, self.script)
        return self.session

    async def stop(self):
        self.stopped = True

    async def force_stop(self):
        self.stopped = True


def exercise(envelope, scripts, *, seconds=120):
    clients = []
    def factory(envelope, *, coach=False):
        value = FakeClient(scripts[len(clients)])
        clients.append(value)
        return value
    result = asyncio.run(provider.drive(envelope, time.monotonic() + seconds, client_factory=factory))
    return result, clients, read_json(Path(envelope.root) / "result.json")


def test_plain_research_is_one_normal_session_without_handoffs(project, ledger):
    envelope = arm(project, ledger)
    result, clients, outcome = exercise(envelope, [{}])
    assert result == 0
    assert outcome["status"] == "completed"
    assert len(clients) == 1
    options = clients[0].session.options
    assert "tools" not in options and "available_tools" not in options
    assert options["system_message"]["mode"] == "append"
    assert options["session_limits"] == {"max_ai_credits": 300.0}
    assert clients[0].stopped
    usage = read_json(Path(envelope.root) / "usage.json")
    assert usage["complete"] and usage["totalNanoAiu"] == 10 * credits.NANO
    assert not list(Path(envelope.project).rglob("handoff.json"))


def test_coach_pushback_continues_same_researcher_and_uses_one_pool(project, ledger):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [{}, {"decision": "continue"}, {"decision": "accept"}])
    assert result == 0 and len(clients) == 3
    assert len(clients[0].session.prompts) == 2
    assert clients[0].session.updates == [260.0]
    assert clients[0].session.options["session_limits"] == {"max_ai_credits": 270.0}
    assert clients[1].session.options["session_limits"] == {"max_ai_credits": 230.0}
    assert clients[2].session.options["session_limits"] == {"max_ai_credits": 210.0}
    assert all(client.stopped for client in clients)
    assert [item["decision"] for item in outcome["reviews"]] == ["continue", "accept"]
    usage = read_json(Path(envelope.root) / "usage.json")
    assert usage["totalNanoAiu"] == 40 * credits.NANO
    assert len({session["session_id"] for session in usage["sessions"]}) == 3
    assert clients[1].session.options["system_message"]["mode"] == "replace"
    assert clients[1].session.options["system_message"]["content"] == guidance.COACH
    assert clients[2].session.options["system_message"]["content"] == guidance.COACH
    follow_up = clients[0].session.prompts[1]
    assert "Does the result hold on the required second family?" in follow_up
    assert "Continue useful experiments toward the original goal" in follow_up
    assert "Keep the original goal and operational boundaries unchanged." in follow_up
    assert guidance.CLOSING_CHALLENGE not in follow_up
    assert {tool.name for tool in clients[1].session.options["tools"]} == {
        "labgoblin_read_evidence", "labgoblin_list_evidence", "labgoblin_review_decision"}


@pytest.mark.parametrize("allowance", [300 * credits.NANO, None])
def test_initial_acceptance_gets_one_neutral_challenge_and_final_review(project, ledger, allowance):
    root, config, record = project
    if allowance is None:
        config = replace(config, max_ai_credits="unlimited")
    envelope = arm((root, config, record), ledger, coach=True, allowance=allowance)
    result, clients, outcome = exercise(envelope, [{}, {"decision": "accept"}, {"decision": "accept"}])
    assert result == 0 and outcome["status"] == "completed"
    assert len(clients) == 3
    researcher = clients[0].session
    assert len(researcher.prompts) == 2
    assert researcher.updates == ([] if allowance is None else [260.0])
    follow_up = researcher.prompts[1]
    assert follow_up == "Original goal:\nAnswer the original question\n\n" + guidance.CLOSING_CHALLENGE
    assert "A meaningful stopping assessment" not in follow_up
    assert '"decision": "accept"' not in follow_up
    assert clients[1].session.prompts[0].startswith("Initial stopping review (provisional).")
    assert clients[2].session.prompts[0].startswith("Stopping review after researcher follow-up 1.")
    assert [item["decision"] for item in outcome["reviews"]] == ["accept", "accept"]
    usage = read_json(Path(envelope.root) / "usage.json")
    assert usage["totalNanoAiu"] == 40 * credits.NANO
    assert [s["prompts"] for s in usage["sessions"]] == [2, 1, 1]
    assert all(client.stopped for client in clients)


def test_final_review_receives_updated_answer_and_can_refuse_stopping(project, ledger, monkeypatch):
    envelope = arm(project, ledger, coach=True)
    send = FakeSession.send

    async def investigate(self, prompt):
        if self.prompts and self.options["system_message"]["mode"] == "append":
            self.script["answer"] = "The additional control failed; the original question remains unresolved."
            (Path(envelope.project) / "research-notes.md").write_text(self.script["answer"], encoding="utf-8")
        await send(self, prompt)

    monkeypatch.setattr(FakeSession, "send", investigate)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "accept"}, {"decision": "continue"}, {"decision": "accept"}])
    assert result == 0 and outcome["status"] == "completed"
    assert len(clients) == 4 and len(clients[0].session.prompts) == 3
    assert "additional control failed" in clients[2].session.prompts[0]
    assert "additional control failed" in (Path(envelope.root) / "sessions" / "researcher" / "answer-2.txt").read_text()
    assert [item["decision"] for item in outcome["reviews"]] == ["accept", "continue", "accept"]
    assert sum(guidance.CLOSING_CHALLENGE in prompt for prompt in clients[0].session.prompts) == 1


@pytest.mark.parametrize("final", [{}, {"startup_error": True}])
def test_provisional_acceptance_cannot_mask_a_failed_final_review(project, ledger, final):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [{}, {"decision": "accept"}, final])
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert [item["decision"] for item in outcome["reviews"]] == ["accept"]
    assert len(clients[0].session.prompts) == 2
    assert all(client.stopped for client in clients)


def test_provisional_acceptance_does_not_buy_an_unfunded_challenge(project, ledger):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [{}, {"decision": "accept", "cost": 240}])
    assert result == 1 and outcome["status"] == "budget_exhausted"
    assert "cannot fund continuation" in outcome["reason"]
    assert len(clients) == 2 and len(clients[0].session.prompts) == 1
    assert clients[0].session.updates == []
    assert [item["decision"] for item in outcome["reviews"]] == ["accept"]
    assert read_json(Path(envelope.root) / "usage.json")["totalNanoAiu"] == 250 * credits.NANO


@pytest.mark.parametrize("boundary", ["cancelled", "timed_out"])
def test_provisional_acceptance_respects_cancellation_and_deadline(project, ledger, monkeypatch, boundary):
    envelope = arm(project, ledger, coach=True)
    close = provider.Session.close

    async def finish_at_boundary(self):
        await close(self)
        if self.role == "coach-0":
            if boundary == "cancelled":
                (Path(envelope.root) / "cancel").write_text("Operator stop", encoding="utf-8")
            else:
                await asyncio.sleep(max(0, self.deadline - provider.GRACE_SECONDS - time.monotonic()) + 0.02)

    monkeypatch.setattr(provider.Session, "close", finish_at_boundary)
    result, clients, outcome = exercise(envelope, [{}, {"decision": "accept"}], seconds=32)
    assert result == (130 if boundary == "cancelled" else 124)
    assert outcome["status"] == boundary
    assert len(clients) == 2 and len(clients[0].session.prompts) == 1
    assert [item["decision"] for item in outcome["reviews"]] == ["accept"]
    assert read_json(Path(envelope.root) / "usage.json")["complete"]


def test_quota_during_challenge_does_not_start_another_review(project, ledger, monkeypatch):
    envelope = arm(project, ledger, coach=True)
    send = FakeSession.send

    async def exhaust_on_follow_up(self, prompt):
        if self.prompts:
            self.script["quota"] = True
        await send(self, prompt)

    monkeypatch.setattr(FakeSession, "send", exhaust_on_follow_up)
    result, clients, outcome = exercise(envelope, [{}, {"decision": "accept"}])
    assert result == 1 and outcome["status"] == "budget_exhausted"
    assert len(clients) == 2 and len(clients[0].session.prompts) == 2
    assert [item["decision"] for item in outcome["reviews"]] == ["accept"]
    assert read_json(Path(envelope.root) / "usage.json")["totalNanoAiu"] == 30 * credits.NANO


@pytest.mark.parametrize("decision", ["continue", "needs_operator"])
def test_bounded_disagreement_does_not_create_infinite_continuation(project, ledger, decision):
    envelope = arm(project, ledger, coach=True)
    scripts = [{}, {"decision": decision}] + [{"decision": "continue"}] * 10
    result, clients, outcome = exercise(envelope, scripts)
    assert result == 1
    assert outcome["status"] == ("checkpointed" if decision == "continue" else "needs_operator")
    assert len(clients) == (12 if decision == "continue" else 2)
    assert len(clients[0].session.prompts) == (11 if decision == "continue" else 1)
    if decision == "continue":
        assert outcome["reviews"][-1]["stalled_reviews"] == 10
        assert outcome["checkpoint_reason"] == "stalled_reviews"
        assert "scientifically exhausted" in outcome["reason"]
    else:
        assert "May I access" in outcome["reason"]
        assert outcome["checkpoint_reason"] is None


def test_failed_coach_is_not_silent_acceptance(project, ledger):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [{}, {}])
    assert result == 1 and outcome["status"] == "review_unavailable"
    assert "without recording" in outcome["reason"]
    assert len(clients[0].session.prompts) == 1
    assert read_json(Path(envelope.root) / "usage.json")["totalNanoAiu"] == 20 * credits.NANO


def test_actual_soft_cap_overshoot_prevents_another_review(project, ledger):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [{"cost": 290}])
    assert result == 1 and outcome["status"] == "budget_exhausted"
    assert len(clients) == 1
    assert read_json(Path(envelope.root) / "usage.json")["totalNanoAiu"] == 290 * credits.NANO


def test_ordinary_error_has_precedence_over_later_limit_error(project, ledger):
    envelope = arm(project, ledger)
    result, clients, outcome = exercise(envelope, [{"error": True}])
    assert outcome["status"] == "failed"
    assert outcome["reason"] == "ordinary failure"
    assert len(clients) == 1


def test_quota_stop_never_buys_a_shrinking_retry(project, ledger):
    envelope = arm(project, ledger, coach=True)
    result, clients, outcome = exercise(envelope, [{"quota": True}])
    assert outcome["status"] == "budget_exhausted"
    assert len(clients) == 1
    assert len(clients[0].session.prompts) == 1
    assert (Path(envelope.root) / "sessions" / "researcher" / "answer-1.txt").exists()


@pytest.mark.parametrize("pre", [False, True])
def test_cancellation_aborts_and_collects_settled_usage(project, ledger, pre):
    envelope = arm(project, ledger)
    path = Path(envelope.root) / "cancel"
    if pre:
        path.write_text("operator stop\n", encoding="utf-8")
    result, clients, outcome = exercise(envelope, [{"wait": True, "cancel": str(path)}])
    assert result == 130
    assert outcome["status"] == "cancelled"
    assert clients[0].session.aborted is not pre
    usage = read_json(Path(envelope.root) / "usage.json")
    assert usage["complete"] is True
    assert usage["totalNanoAiu"] == (0 if pre else 10 * credits.NANO)


def test_startup_failure_records_no_prompt_and_zero_spend(project, ledger):
    envelope = arm(project, ledger)
    result, clients, outcome = exercise(envelope, [{"startup_error": True}])
    assert result == 1 and "Runtime unavailable" in outcome["reason"]
    assert clients[0].session is None and clients[0].stopped
    assert read_json(Path(envelope.root) / "usage.json")["totalNanoAiu"] == 0


def test_coach_cannot_read_state_or_escape_workspace(project):
    root, config, record = project
    tools = {tool.name: tool for tool in provider.coach_tools(root, {})}
    assert all(tool.skip_permission and tool.defer == "never" for tool in tools.values())
    for path in ("..", ".labgoblin\\project.json"):
        response = asyncio.run(tools["labgoblin_read_evidence"].handler(
            SimpleNamespace(tool_name="labgoblin_read_evidence", arguments={"path": path})))
        assert response.result_type == "failure"


@pytest.mark.parametrize("name", ["task", "functions.run_subagent", "run-dynamic-workflow", "fleet", "delegate", "manage_schedule"])
def test_nested_model_calls_are_denied(name):
    assert provider.research_guard({"toolName": name}, {})["permissionDecision"] == "deny"


def test_research_guard_does_not_override_normal_permission_policy():
    assert provider.research_guard({"toolName": "powershell"}, {}) == {}


@pytest.mark.parametrize("path", [".ENV", "nested/.env.production", ".SSH/identity", ".AWS/credentials", ".Copilot/auth.json"])
def test_coach_rejects_common_credential_names_independently_of_case(project, path):
    root, _, _ = project
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic private fixture", encoding="utf-8")
    tool = next(tool for tool in provider.coach_tools(root, {}) if tool.name == "labgoblin_read_evidence")
    response = asyncio.run(tool.handler(SimpleNamespace(tool_name=tool.name, arguments={"path": path})))
    assert response.result_type == "failure"
    assert "not coach evidence" in response.error


def test_coach_tools_advertise_and_enforce_exact_page_bounds(project):
    root, _, _ = project
    (root / "inputs").mkdir()
    (root / "inputs" / "evidence.txt").write_text("x" * 20000, encoding="utf-8")
    tools = {tool.name: tool for tool in provider.coach_tools(root, {})}
    for name, maximum, path in (("labgoblin_read_evidence", 16384, "inputs/evidence.txt"),
                                ("labgoblin_list_evidence", 256, "inputs")):
        tool = tools[name]
        assert tool.parameters["properties"]["limit"]["maximum"] == maximum
        response = asyncio.run(tool.handler(SimpleNamespace(tool_name=name, arguments={"path": path})))
        assert response.result_type == "success"
        accepted = asyncio.run(tool.handler(SimpleNamespace(
            tool_name=name, arguments={"path": path, "limit": maximum})))
        assert accepted.result_type == "success"
        rejected = asyncio.run(tool.handler(SimpleNamespace(
            tool_name=name, arguments={"path": path, "limit": maximum + 1})))
        assert rejected.result_type == "failure"
        if name == "labgoblin_read_evidence":
            assert len(json.loads(accepted.text_result_for_llm)["text"]) == maximum
            assert json.loads(accepted.text_result_for_llm)["next_offset"] == maximum
