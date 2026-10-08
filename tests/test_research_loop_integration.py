import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from labgoblin import briefing, cli, credits, results, workspace
from labgoblin import research_memory as memory, research_tasks as tasks, research_tools
from labgoblin.copilot_worker import drive
from labgoblin.protocol import BudgetExhausted, canonical, fingerprint
from tests.test_copilot_worker import Client, Session
from tests.test_dashboard import dump, get, serve
from tests.test_research_loop import campaign, charter, coach_result, finish, owned, publish


def test_many_hypothesis_pins_survive_revision_and_posthoc_interpretation(tmp_path):
    config, state = campaign(tmp_path)
    first = publish(state, "h1", kind="hypothesis", body="First claim")
    second = publish(state, "h2", kind="hypothesis", body="Other claim")
    request = {"key": "evaluation", "argv": ["python", "-c", "print(1)"], "memory_mb": 128,
               "purpose": "evaluation", "research_refs": [first["source_id"], second["source_id"]]}
    attempt = workspace.submit(state, config, request)
    revised = publish(state, "h1", request_id="h1-v2", base=first["source_id"], kind="hypothesis", body="Reframed claim")
    assert json.loads(state.attempt(attempt["id"])["spec"])["research_refs"] == request["research_refs"]
    for hid in ("h1", "h2"):
        projection = results.hypothesis(state.db, hid)
        assert projection["results"]["total"] == 1
        assert len(projection["results"]["attempts"][0]["research_refs"]) == 2
    evidence = publish(state, "h1", request_id="h1-v3", base=revised["source_id"], kind="hypothesis",
                       body="Reframed claim", outcome="inconclusive",
                       links=[{"target": attempt["id"], "relation": "informs"}])
    assert evidence["content"]["evidence_associations"][0]["association"] == "post_hoc"
    assert memory.history(state, "h1", limit=1)["has_more"]
    for i in range(2):
        second = publish(state, "h2", request_id=f"h2-assessment-{i}", base=second["source_id"], kind="hypothesis",
                         body="Other claim", outcome="inconclusive",
                         links=[{"target": attempt["id"], "relation": "informs"}])
        assert second["content"]["evidence_associations"][0]["association"] == "interpretation_of_pinned_revision"


def test_worker_jobs_have_scoped_keys_and_confined_source_paths(tmp_path):
    config, state = campaign(tmp_path)
    (tmp_path / "code.py").write_text("print(1)\n", encoding="utf-8")
    task = tasks.create(state, charter(source_files=["code.py"]))
    root, root_launch = owned(config, state)
    child, child_launch = owned(config, state, "worker", task["id"])
    base = {"key": "same-local-name", "argv": ["python", "code.py"], "source_files": ["code.py"],
            "memory_mb": 128, "purpose": "support"}
    first = workspace.submit(state, config, base, turn_id=root["turn_id"])
    with pytest.raises(ValueError, match="escapes"):
        workspace.submit(state, config, base, turn_id=child["turn_id"])
    directory = Path(task["root"]) / "work"
    second = workspace.submit(state, config, {**base, "cwd": str(directory),
                                             "source_files": [str(directory / "code.py")]}, turn_id=child["turn_id"])
    assert first["id"] != second["id"] and second["scope_id"] == task["id"]
    tasks.cancel(state, task["id"], "Test cancellation")
    finish(state, child, child_launch, disposition="wait")
    state.cancel_attempt(first["id"])
    finish(state, root, root_launch, disposition="wait")


@pytest.mark.parametrize("seconds", [7200, 30000, "unlimited"])
def test_six_hour_job_admission_uses_optional_task_deadline(tmp_path, seconds):
    from dataclasses import asdict, replace
    from labgoblin.protocol import Resources
    from tests.test_state import receipt
    config, state = campaign(tmp_path, task_max_seconds=seconds)
    task = tasks.create(state, charter())
    packet, provider = owned(config, state, "worker", task["id"])
    request = {"key": "long-compile", "argv": ["python", "-c", "print('compiled')"],
               "cwd": str(Path(task["root"]) / "work"), "seconds": 21600, "memory_mb": 128}
    if seconds == 7200:
        with pytest.raises(ValueError, match="remaining time allowance"):
            workspace.submit(state, config, request, turn_id=packet["turn_id"])
        assert not state.attempts()
        finish(state, packet, provider, disposition="finalize", stopping_criterion="Admission checked")
        return
    for invalid in (0, "unlimited", None):
        with pytest.raises(ValueError, match="seconds"):
            workspace.submit(state, config, {**request, "seconds": invalid}, turn_id=packet["turn_id"])
    attempt = workspace.submit(state, config, request, turn_id=packet["turn_id"])
    finish(state, packet, provider, disposition="wait")
    assert tasks.get(state, task["id"])["state"] == "waiting"
    assert task["id"] not in tasks.ready(state)
    resources = Resources(1, 128)
    allocation = state.allocation(attempt["id"], "attempt", {"resources": asdict(resources)},
                                  state.campaign()["revision"])
    assert state.granted(allocation["token"])
    grant = {"state": "granted", "owner_id": state.id, "work_id": attempt["id"],
             "token": allocation["token"], "native_cpus": "[0]"}
    envelope = workspace.prepare_envelope(state, config, attempt["id"], grant)
    assert envelope.timeout_seconds == 21600
    assert envelope.metadata["task_deadline"] == tasks.deadline(task)
    forged = None if seconds != "unlimited" else time.time() + 3600
    with pytest.raises(ValueError, match="task's absolute deadline"):
        state.arm(replace(envelope, metadata={**envelope.metadata, "task_deadline": forged}))
    state.arm(envelope)
    state.finish_launch(envelope.key.nonce, receipt(envelope))
    state.released(envelope.key.grant_id)
    workspace.collect_artifacts(state, attempt["id"])
    assert task["id"] in tasks.ready(state)
    resumed, provider = owned(config, state, "worker", task["id"])
    finish(state, resumed, provider, disposition="finalize", stopping_criterion="Long-job result handled")
    assert tasks.get(state, task["id"])["budget"]["invocations"] == 2


def test_worker_artifact_requires_exact_validation_and_conflict_checked_integration(tmp_path, monkeypatch):
    config, state = campaign(tmp_path)
    original = tmp_path / "code.py"
    original.write_bytes(b"print(1)\n")
    task = tasks.create(state, charter(source_files=["code.py"]))
    child, invocation = owned(config, state, "worker", task["id"])
    directory = Path(task["root"]) / "work"
    output = directory / "code.py"
    output.write_bytes(b"print(2)\n")
    from labgoblin.evidence import hash_file
    digest = hash_file(output, 1024)
    validation = workspace.submit(state, config, {"key": "validate", "argv": ["python", "code.py"],
        "cwd": str(directory), "source_files": [str(output)], "memory_mb": 128, "purpose": "support"},
        turn_id=child["turn_id"])
    with state.db.write() as conn:
        conn.execute("UPDATE attempts SET status='completed',exit_code=0,collection='complete' WHERE id=?", (validation["id"],))
    finish(state, child, invocation, disposition="finalize", stopping_criterion="Bounded task complete",
           task_artifacts=[{"path": "code.py", "digest": digest}])
    decision = {"reason": "Adopt the tested local change", "artifacts": ["code.py"], "validation_attempt": validation["id"]}
    original.write_bytes(b"# A concurrent root edit\n")
    with pytest.raises(ValueError, match="conflict"):
        tasks.integrate(state, task["id"], decision)
    assert original.read_bytes() == b"# A concurrent root edit\n"
    original.write_bytes(b"print(1)\n")
    from labgoblin import evidence
    atomic = evidence.atomic_bytes

    def interrupted(path, body):
        atomic(path, body)
        raise OSError("Synthetic interruption after filesystem publication")

    with monkeypatch.context() as patch:
        patch.setattr(evidence, "atomic_bytes", interrupted)
        with pytest.raises(OSError, match="Synthetic interruption"):
            tasks.integrate(state, task["id"], decision)
    assert json.loads(tasks.get(state, task["id"])["integrated"])["state"] == "applying"
    assert tasks.integrate(state, task["id"], decision)["state"] == "complete"
    assert original.read_bytes() == b"print(2)\n"
    assert tasks.integrate(state, task["id"], decision)["replayed"]


def test_coach_sdk_is_empty_role_with_only_evidence_and_verdict_tools(tmp_path):
    config, state = campaign(tmp_path)
    root, invocation = owned(config, state)
    finish(state, root, invocation, disposition="blocked")
    packet, envelope = owned(config, state, "coach")
    directory = Path(envelope.root) / "launches" / envelope.key.nonce
    directory.mkdir(parents=True)

    class CoachSession(Session):
        async def send(self, prompt):
            assert "Review this exact stopping snapshot" in prompt
            self.task = asyncio.create_task(self.scenario(self))
            return "message"

    async def scenario(session):
        tools = {tool.name: tool for tool in client.arguments["tools"]}
        assert set(tools) == {"research_read", "coach_decide"}
        guard = client.arguments["hooks"]["on_pre_tool_use"]
        assert guard({"toolName": "powershell"}, None)["permissionDecision"] == "deny"
        assert guard({"toolName": "research_task"}, None)["permissionDecision"] == "deny"
        denied = await tools["research_read"].handler(SimpleNamespace(tool_name="research_publish", arguments={}))
        assert denied.result_type == "failure"
        result = await tools["coach_decide"].handler(SimpleNamespace(tool_name="coach_decide", arguments={
            "decision": "request_reflection", "reason": "Check assumptions",
            "questions": ["Which limitations apply to the primary objective?"], "references": [],
            "limitations": "Process review only"}))
        assert result.result_type == "success"
        session.event("session.idle")

    session = CoachSession(scenario)
    client = Client(session)
    assert asyncio.run(drive(envelope, client, time.monotonic() + 60, poll_seconds=0.002)) == 0
    assert client.arguments["enable_config_discovery"] is False
    assert client.arguments["skip_custom_instructions"] is True
    assert session.permission_mode == "manual"
    value = json.loads(Path(envelope.metadata["result_path"]).read_bytes())
    assert value["episode_id"] == packet["content"]["coaching"]["id"]


@pytest.mark.parametrize("seconds", [7200, "unlimited"])
def test_task_invocation_ceiling_and_operator_pause_preserve_bounded_scope(tmp_path, seconds):
    config, state = campaign(tmp_path, task_max_seconds=seconds)
    task = tasks.create(state, charter(max_invocations=1))
    packet, invocation = owned(config, state, "worker", task["id"])
    finish(state, packet, invocation)
    later = briefing.prepare(state, "worker", scope_id=task["id"])
    with pytest.raises(BudgetExhausted, match="allowance"):
        state.reserve_invocations(later["turn_id"], "worker", ("worker",))
    state.control("pause", "operator-pause", state.campaign()["revision"])
    state.finish_turn(later["turn_id"], "Pending inference fenced by pause", cancelled=True)
    assert tasks.get(state, task["id"])["state"] == "waiting"


@pytest.mark.parametrize("seconds", [7200, "unlimited"])
def test_relative_publication_cli_and_observer_routes_do_not_mutate(tmp_path, capsys, seconds):
    config, state = campaign(tmp_path, task_max_seconds=seconds)
    directory = tmp_path / "research"
    directory.mkdir()
    (directory / "draft.json").write_bytes(canonical({"id": "unlinked", "request_id": "unlinked-v1",
        "kind": "knowledge", "title": "A useful fact", "body": "Useful independently of current hypotheses"}))
    assert cli.main(["--project", str(tmp_path), "research", "publish", "--file", "research\\draft.json", "--json"]) == 0
    publication = json.loads(capsys.readouterr().out)
    task = tasks.create(state, charter())
    before = dump(state)
    from labgoblin.dashboard_data import EvidenceReader
    assert EvidenceReader(config.config_path).read("research_workspace", {})["data"]["total"] == 1
    with serve(config.config_path, chat=False) as base:
        for path in ("/research", "/research?id=" + publication["source_id"], "/tasks",
                     "/tasks?id=" + task["id"], "/journal?entry=" + task["charter"]["source_id"]):
            assert get(base, path)[0] == 200
        detail = get(base, "/tasks?id=" + task["id"])[2].decode("utf-8")
        assert ("Unlimited; individual operations remain bounded" in detail) == (seconds == "unlimited")
    assert dump(state) == before
    assert cli.main(["--project", str(tmp_path), "research", "read", "--json"]) == 1
    assert "exact ID" in capsys.readouterr().out


def test_relative_declared_inputs_cannot_be_task_sources_or_integration_targets(tmp_path):
    import tomli_w
    import tomllib
    from labgoblin.config import parse_config
    config, state = campaign(tmp_path)
    source = tmp_path / "dataset" / "protected.py"
    source.parent.mkdir()
    source.write_bytes(b"Do not copy or modify this input")
    raw = tomllib.loads(Path(config.config_path).read_text(encoding="utf-8"))
    raw["inputs"] = {"corpus": {"path": "dataset"}}
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    config = parse_config(raw, config.config_path)
    with pytest.raises(ValueError, match="declared datasets"):
        tasks.create(state, charter(source_files=["dataset\\protected.py"]))
    with pytest.raises(ValueError, match="protected"):
        tasks._target(config, "dataset\\protected.py")
    assert source.read_bytes() == b"Do not copy or modify this input"


def test_previous_database_schema_is_refused_without_mutation(tmp_path):
    import sqlite3
    from labgoblin.state import State
    _, state = campaign(tmp_path)
    with sqlite3.connect(state.path) as conn:
        conn.execute("PRAGMA user_version=3")
    before = state.path.read_bytes()
    with pytest.raises(ValueError, match="version|schema|fresh"):
        State.open(state.root)
    assert state.path.read_bytes() == before


def test_record_churn_does_not_reset_unchanged_evidence_stop_episode(tmp_path):
    config, state = campaign(tmp_path)
    root, invocation = owned(config, state)
    finish(state, root, invocation, disposition="blocked")
    coach, invocation = owned(config, state, "coach")
    coach_result(state, coach, invocation)
    publish(state, "new-note")
    reflection, invocation = owned(config, state, "reflection")
    finish(state, reflection, invocation, disposition="blocked")
    second, invocation = owned(config, state, "coach")
    assert second["content"]["coaching"]["id"] == coach["content"]["coaching"]["id"]
    coach_result(state, second, invocation)


def test_clarification_yields_worker_and_resumes_on_reply(tmp_path):
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    packet, invocation = owned(config, state, "worker", task["id"])
    assert packet["content"]["project"] == str(config.root)
    assert packet["content"]["working_directory"] == str(Path(task["root"]) / "work")
    tasks.message(state, task["id"], "Which declared output is required?", turn_id=packet["turn_id"])
    finish(state, packet, invocation, disposition="wait")
    assert tasks.get(state, task["id"])["state"] == "waiting"
    assert task["id"] not in tasks.ready(state)
    tasks.message(state, task["id"], "Use the original charter's output contract.")
    assert task["id"] in tasks.ready(state)


def test_no_agent_does_not_wait_for_unscheduled_provider_tasks(tmp_path):
    from labgoblin.campaign import Campaign
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    result = Campaign(config, state=state).run(no_agent=True)
    assert not result["errors"]
    assert tasks.get(state, task["id"])["state"] == "queued"
    assert state.campaign()["invocations"] == 0


def test_large_bank_paging_and_optional_context_keep_exact_authority(tmp_path):
    import base64
    from labgoblin.evidence import SizeLimitError
    from labgoblin.protocol import PACKET_BYTES
    config, state = campaign(tmp_path)
    for i in range(80):
        publish(state, f"note-{i}", body=f"Unique fact {i} " + "x" * 2000)
    first = memory.index(state, limit=7)
    publish(state, "newer")
    all_ids = []
    page = first
    while True:
        all_ids += [row["record_id"] for row in page["records"]]
        if not page["has_more"]:
            break
        page = memory.index(state, limit=7, cutoff=first["cutoff"], offset=page["next_offset"])
    assert len(set(all_ids)) == 80 and "newer" not in all_ids
    body = b"Large retained interpretation " * 4000
    source = state.source("summary", body, origin="test", head="summary")
    packet = briefing.prepare(state)
    assert len(packet["content"]["research_workspace"]["records"]) == 12
    assert len(canonical(packet["content"])) < PACKET_BYTES
    assert packet["content"]["sources"]["summary"]["preview"]
    offset, restored = 0, b""
    while True:
        chunk = research_tools.read(state, {"operation": "source", "id": source, "offset": offset})
        restored += base64.b64decode(chunk["base64"])
        offset += chunk["returned_bytes"]
        if not chunk["has_more"]:
            break
    assert restored == body
    state.finish_turn(packet["turn_id"], "Test packet settled", cancelled=True)
    state.source("goal", b"x" * (PACKET_BYTES + 1), origin="operator", head="goal")
    with pytest.raises(SizeLimitError, match="Mandatory"):
        briefing.prepare(state)


@pytest.mark.parametrize("command", [
    ["init", "--non-interactive"], ["instructions", "--target", "claude"],
    ["machine", "configure", "--cpus", "1", "--memory-mb", "128"], ["dashboard"],
])
def test_role_guards_precede_early_mutation_routes(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv("LABGOBLIN_TURN_ID", "owned-turn")
    assert cli.main(["--project", str(tmp_path), *command, "--json"]) == 1
    assert "Research roles cannot" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("seconds", [7200, "unlimited"])
def test_controller_schedules_real_ledger_scopes_and_native_job_without_parent_grant(tmp_path, monkeypatch, seconds):
    from dataclasses import asdict
    import hashlib
    import tomli_w
    from labgoblin import agent, coaching, worker
    from labgoblin.campaign import Campaign
    from labgoblin.config import parse_config
    from labgoblin.evidence import publish_bytes
    from labgoblin.protocol import Handoff
    from labgoblin.scheduler import request_eligible
    from tests.test_controller import fixture
    from tests.test_providers import HELP
    from tests.test_state import receipt
    from tests.test_workspace import request

    config, state, ledger, raw = fixture(tmp_path)
    raw["research"].update(coaching=True, max_tasks=2, max_parallel_agents=1, task_max_seconds=seconds)
    raw["campaign"].update(max_invocations=20, max_ai_credits=2000)
    raw["agent"].update(transport="sdk", timeout_seconds=60, shutdown_grace_seconds=5)
    config = parse_config(raw, config.config_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    ledger.eligibility = request_eligible
    inspect_provider = agent.inspect_provider
    monkeypatch.setattr(agent, "inspect_provider", lambda settings, **kwargs: inspect_provider(
        settings, probe=lambda argv: HELP, **kwargs))
    start = worker.start
    calls, scopes = [], {}

    def provider_double(current, envelope):
        if envelope.kind == "attempt":
            assert not any(row["kind"] != "attempt" for row in ledger.rows())
            assert envelope.metadata["task_deadline"] == tasks.deadline(scopes["task"])
            if seconds == "unlimited":
                assert envelope.timeout_seconds == 21600
            return start(current, envelope)
        envelope = worker.prepare_runtime(current, envelope)
        current.arm(envelope)
        assert len(ledger.rows()) == 1
        packet = json.loads(Path(envelope.metadata["packet_path"]).read_bytes())
        calls.append(envelope.kind)
        turn_id = envelope.metadata["turn_id"]
        result = asdict(Handoff(turn_id, envelope.metadata["packet_id"], "Bounded synthetic result",
                               "Local evidence interpretation", "Return or continue", "blocked", "Test conclusion"))
        if envelope.kind == "research" and "task" not in scopes:
            scopes["task"] = tasks.create(current, charter(source_files=["experiment.py"]), turn_id=turn_id)
            result["disposition"] = "wait"
        elif envelope.kind == "worker":
            if "attempt" not in scopes:
                directory = Path(scopes["task"]["root"]) / "work"
                scopes["attempt"] = workspace.submit(current, config, {
                    **request(), "seconds": 21600 if seconds == "unlimited" else 10,
                    "cwd": str(directory), "source_files": [str(directory / "experiment.py")]},
                    turn_id=turn_id)
                result["disposition"] = "wait"
            else:
                assert current.attempt(scopes["attempt"]["id"])["status"] == "completed"
                result.update(disposition="finalize", stopping_criterion="Task completed")
        elif envelope.kind == "coach":
            result = {"turn_id": turn_id, "packet_id": envelope.metadata["packet_id"],
                      "episode_id": packet["coaching"]["id"], "reason": "Process review only", "references": [],
                      "limitations": "Synthetic double", "questions": ["Reconsider assumptions"],
                      "decision": "request_reflection" if calls.count("coach") == 1 else "accept_stop"}
        result = json.loads(canonical(result))
        body = canonical(result)
        publish_bytes(Path(envelope.metadata["result_path"]), body)
        capture = {"bytes": len(body), "digest": hashlib.sha256(body).hexdigest(),
                   "result_digest" if envelope.kind == "coach" else "handoff_digest": fingerprint(result)}
        current.finish_launch(envelope.key.nonce, receipt(envelope, usage={"totalNanoAiu": credits.NANO},
                              metadata={"result_capture": capture}))
        return envelope

    monkeypatch.setattr(worker, "start", provider_double)
    result = Campaign(config, state=state, ledger=ledger).run(poll_seconds=0.02)
    assert not result["errors"], result["errors"]
    assert calls == ["research", "worker", "worker", "research", "coach", "reflection", "coach"]
    assert tasks.get(state, scopes["task"]["id"])["state"] == "completed"
    assert state.campaign()["progress"] == "blocked"
    assert state.credit_budget()["used"] == len(calls)
    assert not ledger.rows()
    with state.db.read() as conn:
        assert coaching.pending(conn)["state"] == "needs_operator"
        assert not conn.execute("SELECT 1 FROM turns WHERE state IN ('prepared','running')").fetchone()
