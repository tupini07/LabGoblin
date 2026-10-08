"""Model-free research workspace, coaching and scoped-accounting contracts."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import time
import tomllib

import pytest
import tomli_w

from labgoblin import agent, briefing, coaching, credits, research_memory as memory, research_tasks as tasks, worker
from labgoblin.config import initial_config, parse_config
from labgoblin.protocol import BudgetExhausted, Handoff, canonical, fingerprint
from labgoblin.state import State
from tests.test_providers import HELP
from tests.test_state import receipt


def campaign(tmp_path, **settings):
    raw = initial_config("loop", "copilot")
    raw["research"].update({"coaching": True, "max_tasks": 4, "max_parallel_agents": 3, **settings})
    raw["campaign"].update(max_seconds=0, max_invocations=0, max_ai_credits=2000, cpus=4, memory_mb=4096)
    raw["agent"].update(resources={"cpus": 1, "memory_mb": 128}, command=[sys.executable])
    path = tmp_path / "labgoblin.toml"
    path.write_text(tomli_w.dumps(raw), encoding="utf-8")
    config = parse_config(raw, path)
    state = State.create(config, tmp_path / "machine.db")
    state.bind_ledger(tmp_path / "machine.db", "machine")
    state.source("goal", b"Understand synthetic evidence without inventing observations.", origin="operator", head="goal")
    return config, state


def owned(config, state, kind="research", scope="root", *, view_id=None):
    packet = briefing.prepare(state, kind, scope_id=scope, view_id=view_id)
    turn = packet["turn_id"]
    invocation = state.reserve_invocations(turn, kind, (kind,))[0]
    allocation = state.allocation(turn, kind, {"resources": asdict(config.agent.resources)}, state.campaign()["revision"])
    assert state.granted(allocation["token"])
    grant = {"state": "granted", "owner_id": state.id, "work_id": turn, "token": allocation["token"],
             "native_cpus": "[0]", "cpus": 1, "memory_mb": 128}
    adapter = agent.inspect_provider(config.agent, probe=lambda argv: HELP, max_ai_credits=2000)
    envelope = worker.prepare_runtime(state, agent.prepare(state, config, turn, invocation, grant, adapter=adapter))
    state.arm(envelope)
    return packet, envelope


def finish(state, packet, envelope, *, spent=1, **changes):
    value = Handoff(packet["turn_id"], packet["id"], "Findings", "Researcher interpretation", "Next action",
                    "continue", "Further inquiry")
    value = Handoff.parse(json.loads(canonical({**asdict(value), **changes})))
    state.finish_launch(envelope.key.nonce, receipt(envelope, usage={"totalNanoAiu": spent * credits.NANO},
        metadata={"result_capture": {"handoff_digest": fingerprint(asdict(value))}}))
    state.released(envelope.key.grant_id)
    state.accept_handoff(value, invocation_id=envelope.key.work_id)
    return value


def publish(state, name="note", **changes):
    return memory.publish(state, {"id": name, "request_id": "publish-" + name, "kind": "knowledge",
                                  "title": "Useful context", "body": "A generally useful fact.", **changes})


def charter(**changes):
    return {"request_id": "task-one", "objective": "Investigate a separable synthetic question",
            "deliverables": "Return exact references and limitations", **changes}


def test_publication_is_explicit_idempotent_and_version_qualified(tmp_path):
    _, state = campaign(tmp_path)
    draft = tmp_path / "research" / "draft.json"
    draft.parent.mkdir()
    request = {"id": "question", "request_id": "draft-v1", "kind": "question", "title": "Question", "body": "An idea."}
    draft.write_bytes(canonical(request))
    assert memory.index(state)["total"] == 0
    one = memory.publish_file(state, draft)
    assert memory.publish_file(state, draft) == one
    assert one["content"]["provenance"]["draft_digest"] == fingerprint(request)
    with pytest.raises(ValueError, match="conflict"):
        publish(state, "question", request_id="different", kind="hypothesis")
    two = publish(state, "question", request_id="v2", base=one["source_id"], kind="hypothesis", body="Testable claim")
    assert memory.read(state, one["source_id"]) == one
    assert memory.read(state, "question") == two
    assert memory.index(state, query="Testable")["total"] == 1
    with state.db.read() as conn:
        assert memory.validate_refs(conn, [two["source_id"]], "evaluation")
        with pytest.raises(ValueError, match="hypothesis"):
            memory.validate_refs(conn, [one["source_id"]], "evaluation")
    retired = publish(state, "question", request_id="retired", base=two["source_id"], kind="hypothesis", activity="retired")
    assert memory.index(state, include_retired=False)["total"] == 0
    assert memory.index(state)["records"][0]["source_id"] == retired["source_id"]
    assert memory.index(state, cutoff=1)["total"] == 0


def test_assessments_commit_only_with_owned_handoff_and_known_evidence(tmp_path):
    config, state = campaign(tmp_path)
    note = publish(state)
    packet, envelope = owned(config, state)
    request = {"id": "h", "request_id": "h", "kind": "hypothesis", "title": "Claim", "body": "Synthetic claim",
               "outcome": "supported", "links": [{"target": note["source_id"], "relation": "supports"}]}
    with pytest.raises(ValueError, match="accepted owned handoff"):
        memory.publish(state, request, turn_id=packet["turn_id"])
    finish(state, packet, envelope, research_updates=[request], disposition="wait")
    assert memory.read(state, "h")["content"]["outcome"] == "supported"


def test_scoped_turns_cannot_acknowledge_or_publish_for_each_other(tmp_path):
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    root, root_launch = owned(config, state)
    child, child_launch = owned(config, state, "worker", task["id"])
    root_events = {event["id"] for event in root["content"]["events"]}
    child_events = {event["id"] for event in child["content"]["events"]}
    assert child_events and not root_events & child_events
    created = memory.publish(state, {"id": "worker-note", "request_id": "wn", "kind": "knowledge",
                                    "title": "Local", "body": "Worker observation"}, turn_id=child["turn_id"])
    assert created["content"]["scope_id"] == task["id"]
    with pytest.raises(ValueError, match="recursive"):
        tasks.create(state, charter(request_id="nested"), turn_id=child["turn_id"])
    finish(state, child, child_launch, disposition="finalize", stopping_criterion="Task deliverables returned")
    assert tasks.get(state, task["id"])["state"] == "completed"
    assert state.campaign()["generation_state"] == "open"
    finish(state, root, root_launch, disposition="wait")
    assert memory.read(state, "worker-note")["source_id"] == created["source_id"]


def test_atomic_credit_reservations_do_not_give_each_worker_remaining_pool(tmp_path):
    config, state = campaign(tmp_path)
    first = tasks.create(state, charter())
    second = tasks.create(state, charter(request_id="second"))
    one, first_launch = owned(config, state, "worker", first["id"])
    two, second_launch = owned(config, state, "worker", second["id"])
    first_allowance = first_launch.metadata["credit_allowance_nano"]
    second_allowance = second_launch.metadata["credit_allowance_nano"]
    assert first_allowance == (2000 - 120) * credits.NANO // 3
    assert abs(first_allowance - second_allowance) <= 1
    assert state.credit_budget()["reserved_nano"] == first_allowance + second_allowance
    assert tasks.get(state, first["id"])["budget"]["reserved_nano"] == first_allowance
    assert tasks.get(state, first["id"])["budget"]["credit_scope"] == "campaign"
    finish(state, one, first_launch, disposition="finalize", stopping_criterion="Task complete")
    assert state.credit_budget()["reserved_nano"] == second_allowance
    assert state.credit_budget()["used"] == 1
    finish(state, two, second_launch, disposition="finalize", stopping_criterion="Task complete")
    assert state.credit_budget()["used"] == 2


def test_new_tasks_have_no_credit_subcap_and_unlimited_campaigns_are_not_capped(tmp_path):
    from labgoblin.campaign import Campaign
    config, state = campaign(tmp_path)
    raw = tomllib.loads(Path(config.config_path).read_text(encoding="utf-8"))
    raw["campaign"]["max_ai_credits"] = "unlimited"
    config = parse_config(raw, config.config_path)
    with Campaign(config, state=state) as controller:
        state.configure(config, controller=controller.handle)
    task = tasks.create(state, charter())
    assert task["max_credits_nano"] == 0
    assert "max_credits" not in task["charter"]
    packet, envelope = owned(config, state, "worker", task["id"])
    assert envelope.metadata["credit_allowance_nano"] is None
    root, root_envelope = owned(config, state)
    assert root_envelope.metadata["credit_allowance_nano"] is None
    assert "--max-ai-credits" not in envelope.argv
    with pytest.raises(ValueError, match="shared campaign"):
        tasks.create(state, charter(request_id="not-accepted", max_credits=200))


def test_task_can_use_shared_pool_beyond_former_per_task_ceiling(tmp_path):
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    for _ in range(2):
        packet, envelope = owned(config, state, "worker", task["id"])
        assert envelope.metadata["credit_allowance_nano"] >= 400 * credits.NANO
        finish(state, packet, envelope, spent=400)
    assert tasks.get(state, task["id"])["budget"]["used"] == 800
    assert state.credit_budget()["used"] == 800
    assert state.credit_budget()["remaining"] == 1200


def test_waiting_for_reserved_credits_is_not_budget_exhaustion(tmp_path):
    from labgoblin.protocol import AdmissionWait
    config, state = campaign(tmp_path, max_parallel_agents=1)
    task = tasks.create(state, charter())
    packet, envelope = owned(config, state)
    child = briefing.prepare(state, "worker", scope_id=task["id"])
    with pytest.raises(AdmissionWait, match="reserved"):
        state.reserve_invocations(child["turn_id"], "worker", ("worker",))
    assert state.campaign()["generation_state"] == "open"
    finish(state, packet, envelope)
    ids = state.reserve_invocations(child["turn_id"], "worker", ("worker",))
    assert state.credit_allowance(ids[0]) == (2000 - 1 - 120) * credits.NANO


def test_legacy_task_credit_ceiling_is_preserved(tmp_path):
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    with state.db.write() as conn:
        conn.execute("UPDATE research_tasks SET max_credits_nano=? WHERE id=?", (150 * credits.NANO, task["id"]))
    _, envelope = owned(config, state, "worker", task["id"])
    assert envelope.metadata["credit_allowance_nano"] == 150 * credits.NANO
    assert tasks.get(state, task["id"])["budget"]["credit_scope"] == "legacy_task"


def test_legacy_config_snapshot_remains_readable_without_old_user_credit_fields(tmp_path):
    from labgoblin.config import restore_config
    config, _ = campaign(tmp_path)
    snapshot = json.loads(canonical(asdict(config)))
    snapshot["research"].update(coach_credits=100, task_max_credits=300, invocation_credits=300)
    before = canonical(snapshot)
    assert restore_config(snapshot, revision=config.revision, path=config.config_path) == config
    assert canonical(snapshot) == before


@pytest.mark.parametrize("seconds", [7200, 0.5, "unlimited"])
def test_task_lifetime_config_roundtrips_without_rewriting_snapshot(tmp_path, seconds):
    from labgoblin.config import restore_config
    config, state = campaign(tmp_path, task_max_seconds=seconds)
    expected = None if seconds == "unlimited" else seconds
    assert config.research.task_max_seconds == expected
    snapshot = json.loads(canonical(asdict(config)))
    before = canonical(snapshot)
    assert restore_config(snapshot, revision=config.revision, path=config.config_path) == config
    assert canonical(snapshot) == before
    assert tasks.create(state, charter())["charter"]["max_seconds"] == expected


@pytest.mark.parametrize("ceiling,overrides,expected", [
    (7200, {}, 7200), (7200, {"max_seconds": 3600}, 3600),
    ("unlimited", {}, None), ("unlimited", {"max_seconds": "unlimited"}, None),
    ("unlimited", {"max_seconds": 21600}, 21600),
])
def test_task_lifetime_inherits_or_accepts_allowed_request(tmp_path, ceiling, overrides, expected):
    _, state = campaign(tmp_path, task_max_seconds=ceiling)
    task = tasks.create(state, charter(**overrides))
    assert task["charter"]["max_seconds"] == expected
    assert tasks.deadline(task) == (None if expected is None else task["created"] + expected)
    assert tasks.create(state, charter(**overrides)) == task


@pytest.mark.parametrize("seconds", ["unlimited", 7201])
def test_finite_task_ceiling_cannot_be_bypassed(tmp_path, seconds):
    _, state = campaign(tmp_path)
    with pytest.raises(ValueError, match="time allowance exceeds"):
        tasks.create(state, charter(max_seconds=seconds))
    assert tasks.listing(state)["total"] == 0


@pytest.mark.parametrize("value", [0, -1, True, None, "", "0", "Unlimited", float("inf"), float("nan")])
def test_task_lifetime_rejects_invalid_config_and_requests(tmp_path, value):
    raw = initial_config("invalid", "copilot")
    raw["research"]["task_max_seconds"] = value
    with pytest.raises(ValueError, match="research.task_max_seconds|Out of range float"):
        parse_config(raw, tmp_path / "invalid.toml")
    _, state = campaign(tmp_path, task_max_seconds="unlimited")
    with pytest.raises(ValueError):
        tasks.create(state, charter(max_seconds=value))
    assert tasks.listing(state)["total"] == 0


@pytest.mark.parametrize("seconds", [7200, "unlimited"])
@pytest.mark.parametrize("status", ["queued", "waiting"])
def test_task_lifetime_expiry_includes_waits_only_when_finite(tmp_path, seconds, status):
    _, state = campaign(tmp_path, task_max_seconds=seconds)
    task = tasks.create(state, charter())
    with state.db.write() as conn:
        conn.execute("UPDATE research_tasks SET created=?,state=? WHERE id=?",
                     (time.time() - 10800, status, task["id"]))
    with state.db.read() as conn:
        if seconds == "unlimited":
            assert tasks.assert_owned(conn, task["id"], admission=True)
        else:
            with pytest.raises(BudgetExhausted, match="elapsed-time"):
                tasks.assert_owned(conn, task["id"], admission=True)
    tasks.reconcile(state)
    current = tasks.get(state, task["id"])
    assert current["state"] == (status if seconds == "unlimited" else "cancelled")
    if seconds != "unlimited":
        assert current["reason"] == "Task elapsed-time allowance exhausted"
    else:
        tasks.cancel(state, task["id"], "Explicit cancellation still applies")
        assert tasks.get(state, task["id"])["state"] == "cancelled"


@pytest.mark.parametrize("seconds", [7200, "unlimited"])
def test_provider_launch_pins_exact_task_deadline_and_finite_operation(tmp_path, monkeypatch, seconds):
    config, state = campaign(tmp_path, task_max_seconds=seconds)
    task = tasks.create(state, charter())
    arm = state.arm

    def checked(envelope):
        assert envelope.timeout_seconds == config.agent.timeout_seconds
        assert envelope.metadata["task_deadline"] == tasks.deadline(task)
        forged = None if seconds != "unlimited" else time.time() + 3600
        with pytest.raises(ValueError, match="task's absolute deadline"):
            arm(replace(envelope, metadata={**envelope.metadata, "task_deadline": forged}))
        arm(envelope)

    monkeypatch.setattr(state, "arm", checked)
    packet, envelope = owned(config, state, "worker", task["id"])
    finish(state, packet, envelope, disposition="finalize", stopping_criterion="Synthetic task settled")


@pytest.mark.parametrize("field", ["coach_credits", "task_max_credits", "invocation_credits"])
def test_removed_credit_controls_fail_explicitly(tmp_path, field):
    raw = initial_config("single-pool", "copilot")
    assert not any(name in raw["research"] for name in ("coach_credits", "task_max_credits", "invocation_credits"))
    raw["research"][field] = 100
    with pytest.raises(ValueError, match="campaign.max_ai_credits"):
        parse_config(raw, tmp_path / "labgoblin.toml")


def coach_result(state, packet, envelope, decision="request_reflection"):
    result = {"turn_id": packet["turn_id"], "packet_id": packet["id"],
              "episode_id": packet["content"]["coaching"]["id"], "decision": decision,
              "reason": "Check whether a branch limitation applies to the objective.",
              "questions": ["Which constraints are established and which are assumptions?"],
              "references": [], "limitations": "Review of the retained process record, not scientific certification"}
    state.finish_launch(envelope.key.nonce, receipt(envelope, usage={"totalNanoAiu": credits.NANO},
        metadata={"result_capture": {"result_digest": fingerprint(result)}}))
    state.released(envelope.key.grant_id)
    coaching.accept(state, result, invocation_id=envelope.key.work_id)
    return result


def test_stop_review_pushes_back_without_prescribing_and_cannot_loop(tmp_path):
    config, state = campaign(tmp_path)
    initial, invocation = owned(config, state)
    finish(state, initial, invocation, disposition="blocked", reason="One optional branch is unavailable")
    assert not state.reasoning_due()
    coach, invocation = owned(config, state, "coach")
    coach_result(state, coach, invocation)
    assert state.reasoning_due()
    assert "Do not prescribe experiments" in coach["content"]["instructions"]
    reflection, invocation = owned(config, state, "reflection")
    finish(state, reflection, invocation, disposition="blocked", reason="Still proposing to stop")
    second, invocation = owned(config, state, "coach")
    assert second["content"]["coaching"]["rounds"] == 1
    coach_result(state, second, invocation)
    with state.db.read() as conn:
        assert coaching.pending(conn)["state"] == "needs_operator"
    assert not state.reasoning_due()


def test_stale_or_forged_coaching_never_approves_stop(tmp_path):
    config, state = campaign(tmp_path)
    root, invocation = owned(config, state)
    finish(state, root, invocation, disposition="finalize", stopping_criterion="Synthetic objective")
    assert state.campaign()["generation_state"] == "open"
    coach, invocation = owned(config, state, "coach")
    state.directive("Additional operator constraint")
    result = coach_result(state, coach, invocation, "accept_stop")
    assert state.campaign()["generation_state"] == "open"
    with state.db.read() as conn:
        assert coaching.pending(conn)["state"] == "needs_operator"
    with pytest.raises(ValueError, match="capture"):
        coaching.accept(state, {**result, "reason": "Different result"}, invocation_id=invocation.key.work_id)


def test_failed_dependency_converges_and_cancel_does_not_release_owned_launch(tmp_path):
    config, state = campaign(tmp_path)
    first = tasks.create(state, charter())
    second = tasks.create(state, charter(request_id="dependent", dependencies=[first["id"]]))
    packet, envelope = owned(config, state, "worker", first["id"])
    tasks.cancel(state, first["id"], "Operator no longer wants this work")
    assert state.launch(envelope.key.nonce)["phase"] == "armed"
    tasks.reconcile(state)
    assert tasks.get(state, second["id"])["state"] == "cancelled"
    assert Path(envelope.root, "launches", envelope.key.nonce, "cancel").exists()
    finish(state, packet, envelope, disposition="finalize", stopping_criterion="Late result")
    assert tasks.get(state, first["id"])["state"] == "cancelled"


@pytest.mark.parametrize("field,value", [("coach_credits", "unlimited"), ("task_max_credits", None),
                                         ("max_parallel_agents", 0), ("task_max_seconds", 0)])
def test_invalid_enhanced_allowances_are_rejected(tmp_path, field, value):
    raw = initial_config("invalid", "copilot")
    raw["research"][field] = value
    with pytest.raises(ValueError):
        parse_config(raw, tmp_path / "labgoblin.toml")
