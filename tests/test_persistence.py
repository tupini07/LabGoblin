"""Persistence policy, side-effect-free preflight and bounded final-result repair."""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from labgoblin import agent_policy, agent_worker, briefing, cli, coaching, credits, research_tools, reporting
from labgoblin.campaign import Campaign
from labgoblin.copilot_worker import drive
from labgoblin.protocol import AdmissionClosed, Handoff, LaunchReceipt, canonical, fingerprint
from tests.test_copilot_worker import Client, Session
from tests.test_research_loop import campaign, charter, coach_result, finish, owned, publish
from tests.test_state import receipt


def dump(state):
    with state.db.read() as conn:
        return "\n".join(conn.iterdump())


def proposed(packet, **changes):
    return json.loads(canonical({
        **asdict(Handoff(packet["turn_id"], packet["id"], "Retained findings", "A limited result",
                        "Choose further inquiry", "continue", "Goal remains unresolved")), **changes}))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("basis", ["", "discretionary"])
def test_partial_closure_requires_operator_even_if_coach_accepts(tmp_path, enabled, basis):
    config, state = campaign(tmp_path, coaching=enabled)
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, disposition="finalize", stop_basis=basis,
           stopping_criterion="A branch failed; another pivot has uncertain value")
    if enabled:
        coach, envelope = owned(config, state, "coach")
        coach_result(state, coach, envelope, "accept_stop")
    assert state.campaign()["generation_state"] == "open"
    assert state.credit_budget()["remaining"] > 1900
    with state.db.read() as conn:
        episode = dict(coaching.pending(conn))
    assert episode["state"] == "needs_operator"
    assert not state.reasoning_due()
    coaching.resolve(state, episode["id"], "accept_stop", "Operator explicitly accepts this partial outcome")
    assert state.campaign()["generation_state"] == "sealed"


@pytest.mark.parametrize("enabled", [False, True])
def test_evidence_linked_goal_completion_can_enter_final_analysis(tmp_path, enabled):
    config, state = campaign(tmp_path, coaching=enabled)
    note = publish(state, body="Retained evidence interpretation for a synthetic completed goal")
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, disposition="finalize", stop_basis="goal_satisfied",
           stopping_criterion="Original synthetic criterion met", research_refs=[note["source_id"]])
    if enabled:
        assert state.campaign()["generation_state"] == "open"
        coach, envelope = owned(config, state, "coach")
        coach_result(state, coach, envelope, "accept_stop")
    assert state.campaign()["generation_state"] == "sealed"
    assert state.campaign()["research_outcome"] == "active"


def test_goal_claim_without_references_fails_and_operator_dependency_is_explicit(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = proposed(packet, disposition="finalize", stop_basis="goal_satisfied", stopping_criterion="Claimed")
    before = dump(state)
    with pytest.raises(ValueError, match="exact delivered"):
        research_tools.preflight(state, value, turn_id=packet["turn_id"], invocation_id=envelope.key.work_id)
    assert dump(state) == before
    finish(state, packet, envelope, disposition="blocked", stop_basis="operator_needed",
           reason="All remaining work requires permission to read a missing input")
    with state.db.read() as conn:
        episode = dict(coaching.pending(conn))
    assert episode["state"] == "needs_operator" and "missing input" in episode["reason"]
    assert state.campaign()["invocations"] == 1
    coaching.resolve(state, episode["id"], "continue", "Continue within existing permissions")
    assert state.reasoning_due()


def test_retiring_a_branch_and_rewriting_reflection_does_not_reset_review_bound(tmp_path):
    config, state = campaign(tmp_path)
    note = publish(state)
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, research_updates=[{
        "id": "branch", "request_id": "branch-1", "kind": "hypothesis", "title": "Retired candidate",
        "body": "This candidate failed, not the whole campaign.", "activity": "retired", "outcome": "refuted",
        "links": [{"target": note["source_id"], "relation": "refutes"}]}])
    with state.db.read() as conn:
        assert coaching.pending(conn) is None
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, disposition="blocked")
    coach, envelope = owned(config, state, "coach")
    coach_result(state, coach, envelope)
    reflection, envelope = owned(config, state, "reflection")
    finish(state, reflection, envelope, disposition="blocked", research_updates=[{
        "id": "strategy", "request_id": "strategy-1", "kind": "reflection",
        "title": "Reworded strategy", "body": "A different rationale for the same evidence."}])
    second, envelope = owned(config, state, "coach")
    assert second["content"]["coaching"]["id"] == coach["content"]["coaching"]["id"]
    coach_result(state, second, envelope)
    with state.db.read() as conn:
        assert coaching.pending(conn)["state"] == "needs_operator"


@pytest.mark.parametrize("kind", ["evidence", "task_result"])
def test_wait_completed_after_packet_bypasses_coach_but_repeated_empty_wait_does_not(tmp_path, kind):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    state.event(kind, {"reason": "Owned work settled after this packet was prepared"})
    finish(state, packet, envelope, disposition="wait")
    assert state.reasoning_due()
    with state.db.read() as conn:
        assert coaching.pending(conn) is None
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, disposition="wait")
    with state.db.read() as conn:
        assert coaching.pending(conn)["state"] == "pending"
    assert not state.reasoning_due()


def test_preflight_uses_acceptance_validation_without_publishing_or_acknowledging(tmp_path, monkeypatch, capsys):
    config, state = campaign(tmp_path)
    note = publish(state)
    packet, envelope = owned(config, state)
    value = proposed(packet, evidence=[{"event_id": packet["content"]["events"][0]["id"],
                                      "disposition": "assessed", "reason": "Read exact retained input"}],
                     research_updates=[{"id": "assessment", "request_id": "assessment-v1", "kind": "knowledge",
                                        "title": "Limited finding", "body": "Evidence is inconclusive",
                                        "outcome": "inconclusive"}])
    before = dump(state)
    with pytest.raises(ValueError, match="requires evidence references"):
        research_tools.preflight(state, value, turn_id=packet["turn_id"], invocation_id=envelope.key.work_id)
    assert dump(state) == before
    value["research_updates"][0]["links"] = [{"target": note["source_id"], "relation": "informs"}]
    assert state.preflight_handoff(Handoff.parse(value), invocation_id=envelope.key.work_id)["valid"]
    assert dump(state) == before
    path = tmp_path / "draft-handoff.json"
    path.write_bytes(canonical(value))
    monkeypatch.setenv("LABGOBLIN_TURN_ID", packet["turn_id"])
    monkeypatch.setenv("LABGOBLIN_INVOCATION_ID", envelope.key.work_id)
    assert cli.main(["--project", str(tmp_path), "research", "preflight", "--file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is False
    assert dump(state) == before
    tool = next(t for t in research_tools.sdk_tools(state, envelope) if t.name == "research_preflight")
    result = asyncio.run(tool.handler(SimpleNamespace(tool_name=tool.name, arguments={"handoff": value})))
    assert result.result_type == "success" and dump(state) == before
    state.control("pause", "pause", state.campaign()["revision"])
    with pytest.raises(AdmissionClosed, match="authority changed"):
        state.preflight_handoff(Handoff.parse(value), invocation_id=envelope.key.work_id)


def test_worker_artifact_preflight_does_not_publish_files(tmp_path):
    from labgoblin import research_tasks
    config, state = campaign(tmp_path)
    task = research_tasks.create(state, charter())
    path = Path(task["root"]) / "work" / "output.txt"
    path.write_text("Local result", encoding="utf-8")
    import hashlib
    packet, envelope = owned(config, state, "worker", task["id"])
    value = proposed(packet, disposition="finalize", stopping_criterion="Assigned scope complete",
                     task_artifacts=[{"path": str(path), "digest": hashlib.sha256(path.read_bytes()).hexdigest()}])
    before = dump(state)
    state.preflight_handoff(Handoff.parse(value), invocation_id=envelope.key.work_id)
    assert dump(state) == before
    assert not (Path(task["root"]) / "returned").exists()


@pytest.mark.parametrize("links", [None, "not an array", [False], [{"target": "unknown", "relation": "informs"}]])
def test_malformed_assessment_links_fail_explicitly_without_mutation(tmp_path, links):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = proposed(packet, research_updates=[{"id": "bad", "request_id": "bad", "kind": "knowledge",
                     "title": "Malformed", "body": "Not published", "outcome": "inconclusive", "links": links}])
    before = dump(state)
    with pytest.raises(ValueError):
        state.preflight_handoff(Handoff.parse(value), invocation_id=envelope.key.work_id)
    assert dump(state) == before


def test_dashboard_exposes_operator_gate_without_changing_state(tmp_path):
    from tests.test_dashboard import get, serve
    config, state = campaign(tmp_path, coaching=False)
    packet, envelope = owned(config, state)
    finish(state, packet, envelope, disposition="finalize", stop_basis="discretionary",
           stopping_criterion="A useful but incomplete result")
    before = dump(state)
    with serve(config.config_path, chat=False) as base:
        status, _, body = get(base, "/tasks")
        assert status == 200
        text = body.decode("utf-8")
        assert "discretionary" in text and "needs operator" in text
        assert "Discretionary partial closure requires an operator decision" in text
    assert dump(state) == before


def test_archive_growth_does_not_queue_paid_compaction_and_old_requests_are_cancelled(tmp_path):
    from tests.test_controller import fixture
    config, state, ledger, _ = fixture(tmp_path)
    Campaign(config, state=state, ledger=ledger).run()
    calls = state.campaign()["invocations"]
    state.source("journal_import", b"Large retained history. " * 10000, origin="operator")
    Campaign(config, state=state, ledger=ledger).run()
    assert state.campaign()["invocations"] == calls
    with state.db.read() as conn:
        assert not conn.execute("SELECT 1 FROM maintenance").fetchone()
    request = state.request_maintenance("compact", origin="automatic")
    assert state.pending_maintenance() is None
    with state.db.read() as conn:
        row = conn.execute("SELECT * FROM maintenance WHERE id=?", (request["id"],)).fetchone()
        assert row["state"] == "cancelled" and "explicitly" in row["reason"]


def final_fixture(tmp_path):
    config, state = campaign(tmp_path)
    note = publish(state)
    state.close_admission("Operator-requested assessment of retained evidence")
    view = reporting.seal_view(state, kind="closure")
    packet, envelope = owned(config, state, "final_analysis", view_id=view["id"])
    directory = Path(envelope.root) / "launches" / envelope.key.nonce
    directory.mkdir(parents=True, exist_ok=True)
    value = proposed(packet, disposition="finalize", stopping_criterion="Sealed inventory assessed",
        assessment={"view_id": view["id"], "inventory_digest": view["metadata"]["inventory_digest"],
                    "assess_all": True, "exclusions": [], "limitations": "Synthetic partial outcome, not goal success"},
        research_updates=[{"id": "final", "request_id": "final-v1", "kind": "knowledge",
                           "title": "Final assessment", "body": "Evidence remains inconclusive", "outcome": "inconclusive"}])
    state.retrieved(packet["turn_id"], note["source_id"])
    return config, state, note, packet, envelope, directory, value


@pytest.mark.parametrize("mode", ["correct", "invalid", "weaken", "cancel", "budget", "valid"])
def test_final_sdk_correction_is_owned_bounded_and_preserves_inventory(tmp_path, monkeypatch, mode):
    config, state, note, packet, envelope, directory, value = final_fixture(tmp_path)
    before = dump(state)
    prompts = []

    class FinalSession(Session):
        async def send(self, prompt):
            prompts.append(prompt)
            if len(prompts) > 1:
                assert "structure-only correction" in prompt
            self.task = asyncio.create_task(self.scenario(self))
            return "message"

    async def scenario(session):
        if len(prompts) == 2 or mode == "valid":
            if mode in ("correct", "valid"):
                value["research_updates"][0]["links"] = [{"target": note["source_id"], "relation": "informs"}]
            elif mode == "weaken":
                value["research_updates"][0]["outcome"] = "unassessed"
        Path(envelope.metadata["result_path"]).write_bytes(canonical(value))
        if mode == "cancel":
            (directory / "cancel").write_text("cancel", encoding="utf-8")
        session.event("assistant.message", content="Final draft written")
        session.event("session.idle")

    amount = envelope.metadata["credit_allowance_nano"] if mode == "budget" else 2 * credits.NANO
    session = FinalSession(scenario, metrics=[0, amount, amount])
    code = asyncio.run(drive(envelope, Client(session), time.monotonic() + 120, poll_seconds=0.002))
    assert len(prompts) == (1 if mode in ("cancel", "budget", "valid") else 2)
    assert dump(state) == before
    assert (directory / "handoff-draft-0.json").is_file()
    if mode != "valid":
        assert "requires evidence references" in json.loads((directory / "handoff-preflight-0.json").read_bytes())["error"]
        assert "links" not in json.loads((directory / "handoff-draft-0.json").read_bytes())["research_updates"][0]
    assert code == (0 if mode in ("correct", "valid") else 130 if mode == "cancel" else 1)
    native = LaunchReceipt(envelope.key, envelope.digest, "completed" if code == 0 else "failed",
                           True, 1, returncode=code, executed=True, reason="" if code == 0 else "Provider exited nonzero")
    monkeypatch.setattr(agent_policy, "execute_spec", lambda *args, **kwargs: native)
    settled = agent_policy.supervise(envelope)
    state.finish_launch(envelope.key.nonce, asdict(settled))
    state.released(envelope.key.grant_id)
    if code == 0:
        agent_worker.accept_result(state, packet["turn_id"])
    else:
        state.finish_turn(packet["turn_id"], settled.reason)
    controller = Campaign(config, state=state, ledger=SimpleNamespace())
    errors = []
    controller._advance_closure(errors, no_agent=False, admit=True)
    assert not errors
    expected = "assessed" if code == 0 else "unassessed"
    assert state.campaign()["research_outcome"] == expected
    assert state.campaign()["generation_state"] == "closed"
    assert state.campaign()["invocations"] == 1
    assert state.credit_budget()["used_nano"] == amount
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE kind='final_analysis'").fetchone()[0] == 1
        frozen = dict(conn.execute("SELECT * FROM generations WHERE id=1").fetchone())
        evidence = list(conn.execute("SELECT id,digest FROM sources"))
    state.control("reopen", "resume-same-campaign", state.campaign()["revision"])
    assert state.campaign()["generation"] == 2
    assert state.credit_budget()["used_nano"] == amount
    with state.db.read() as conn:
        assert dict(conn.execute("SELECT * FROM generations WHERE id=1").fetchone()) == frozen
        assert list(conn.execute("SELECT id,digest FROM sources")) == evidence


def test_legacy_handoff_capture_and_accepted_history_remain_readable(tmp_path):
    config, state = campaign(tmp_path)
    packet, envelope = owned(config, state)
    value = proposed(packet)
    del value["stop_basis"]
    state.finish_launch(envelope.key.nonce, receipt(envelope, usage={"totalNanoAiu": credits.NANO},
        metadata={"result_capture": {"handoff_digest": fingerprint(value)}}))
    state.released(envelope.key.grant_id)
    state.accept_handoff(Handoff.parse(value), invocation_id=envelope.key.work_id)
    with state.db.write() as conn:
        conn.execute("UPDATE turns SET result=? WHERE id=?", (canonical(value).decode(), packet["turn_id"]))
    before = dump(state)
    state.accept_handoff(Handoff.parse(value), invocation_id=envelope.key.work_id)
    assert dump(state) == before
