"""Repair verdict errors in the owned coach session, without accepting research."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from labgoblin import coaching, research_tools
from labgoblin.protocol import fingerprint, identifier
from tests.test_context_selection import completed
from tests.test_dashboard import dump
from tests.test_research_loop import campaign, finish, owned, publish
from tests.test_state import receipt


def review(tmp_path):
    config, state = campaign(tmp_path)
    attempt_id, _ = completed(state, "comparison")
    refs = []
    for name in ("metrics.json", "rows.json"):
        path = tmp_path / name
        path.write_bytes(b"{}")
        refs.append({"id": identifier(), "path": name, "kind": "artifact", "size": 2,
                     "digest": hashlib.sha256(b"{}").hexdigest(),
                     "metadata": {"capture_path": str(path)}, "assurance": "captured"})
    collection = state.record_collection(attempt_id, refs, "complete", "valid", "Synthetic retained evidence",
                                         recollect=True)
    root, invocation = owned(config, state)
    for ref in refs:
        research_tools.read(state, {"operation": "observation", "id": ref["id"]}, turn_id=root["turn_id"])
    finish(state, root, invocation, disposition="finalize", stop_basis="goal_satisfied",
           stopping_criterion="Bounded comparison assessed", evidence=[{
               "event_id": collection["event_id"], "disposition": "assessed",
               "reason": "Synthetic captured comparison", "references": [ref["id"] for ref in refs]}])
    packet, envelope = owned(config, state, "coach")
    result = {"turn_id": packet["turn_id"], "packet_id": packet["id"],
              "episode_id": packet["content"]["coaching"]["id"], "decision": "accept_stop",
              "reason": "The bounded goal has been assessed", "questions": [],
              "references": [ref["id"] for ref in refs], "limitations": "Process review, not independent science"}
    return state, packet, envelope, result


def test_missing_observations_repair_within_same_sdk_session_before_recording(tmp_path):
    state, packet, envelope, result = review(tmp_path)
    assert not set(result["references"]) & set(packet["content"]["references"])
    assert packet["content"]["coaching"]["proposal"]["evidence"][0]["references"] == result["references"]
    assert "not independently delivered" in packet["content"]["coaching"]["reference_coverage"]["mentioned"]
    tools = {tool.name: tool for tool in research_tools.sdk_tools(state, envelope)}
    args = {key: value for key, value in result.items() if key not in ("turn_id", "packet_id", "episode_id")}
    output = Path(envelope.metadata["result_path"])
    before = dump(state)

    async def scenario():
        failed = await tools["coach_decide"].handler(SimpleNamespace(tool_name="coach_decide", arguments=args))
        assert failed.result_type == "failure"
        assert all(ref in failed.error for ref in result["references"])
        assert '"operation":"observation"' in failed.error
        assert not output.exists() and dump(state) == before
        for ref in result["references"]:
            read = await tools["research_read"].handler(SimpleNamespace(
                tool_name="research_read", arguments={"operation": "observation", "id": ref}))
            assert read.result_type == "success"
        retrieved = dump(state)
        repaired = await tools["coach_decide"].handler(SimpleNamespace(tool_name="coach_decide", arguments=args))
        assert repaired.result_type == "success" and json.loads(repaired.text_result_for_llm)["recorded"]
        assert dump(state) == retrieved

    asyncio.run(scenario())
    assert json.loads(output.read_bytes()) == result
    assert state.campaign()["generation_state"] == "open"
    state.finish_launch(envelope.key.nonce, receipt(envelope, metadata={
        "result_capture": {"result_digest": fingerprint(result)}}))
    state.released(envelope.key.grant_id)
    coaching.accept(state, result, invocation_id=envelope.key.work_id)
    assert state.campaign()["generation_state"] == "sealed"
    after = dump(state)
    coaching.accept(state, result, invocation_id=envelope.key.work_id)
    assert dump(state) == after
    with pytest.raises(ValueError, match="active owned invocation"):
        coaching.preflight(state, result, invocation_id=envelope.key.work_id)


@pytest.mark.parametrize("change,match", [
    ({"questions": ["why"] * 5}, "at most|one to four"),
    ({"decision": "request_reflection", "questions": []}, "one to four"),
    ({"limitations": ""}, "coach limitations"),
    ({"references": ["not-an-evidence-id"]}, "undelivered reference"),
    ({"decision": "approve"}, "Invalid coach decision"),
    ({"extra": True}, "Unknown coach result"),
    ({"episode_id": "another-episode"}, "episode"),
    ({"packet_id": "another-packet"}, "owned turn"),
    ({"turn_id": "another-turn"}, "owned turn"),
])
def test_preflight_rejects_invalid_verdict_without_writing_state(tmp_path, change, match):
    state, _, envelope, result = review(tmp_path)
    result["references"] = []
    before = dump(state)
    with pytest.raises(ValueError, match=match):
        coaching.preflight(state, {**result, **change}, invocation_id=envelope.key.work_id)
    assert dump(state) == before


@pytest.mark.parametrize("change", ["invocation", "pause", "authority", "snapshot", "packet"])
def test_preflight_cannot_correct_changed_ownership_or_review_context(tmp_path, change):
    state, packet, envelope, result = review(tmp_path)
    result["references"] = []
    invocation_id = envelope.key.work_id
    if change == "invocation":
        invocation_id = "another-invocation"
    elif change == "pause":
        state.control("pause", "pause-coach", state.campaign()["revision"])
    elif change == "authority":
        state.directive("A new governing constraint")
    elif change == "snapshot":
        publish(state, "later-evidence")
    else:
        with state.db.write() as conn:
            conn.execute("UPDATE packets SET digest=? WHERE id=?", ("0" * 64, packet["id"]))
    before = dump(state)
    with pytest.raises(ValueError, match="owned turn|snapshot|authority|integrity|episode"):
        coaching.preflight(state, result, invocation_id=invocation_id)
    assert dump(state) == before
    assert not Path(envelope.metadata["result_path"]).exists()


def test_final_acceptance_still_checks_receipt_references_and_later_authority(tmp_path):
    state, packet, envelope, result = review(tmp_path)
    for ref in result["references"]:
        research_tools.read(state, {"operation": "observation", "id": ref}, turn_id=packet["turn_id"])
    assert coaching.preflight(state, result, invocation_id=envelope.key.work_id)["accepted"] is False
    with pytest.raises(ValueError, match="completed quiescent"):
        coaching.accept(state, result, invocation_id=envelope.key.work_id)
    state.finish_launch(envelope.key.nonce, receipt(envelope, metadata={
        "result_capture": {"result_digest": fingerprint(result)}}))
    state.released(envelope.key.grant_id)
    with pytest.raises(ValueError, match="capture"):
        coaching.accept(state, {**result, "reason": "Altered verdict"}, invocation_id=envelope.key.work_id)
    with state.db.write() as conn:
        conn.execute("DELETE FROM turn_references WHERE turn_id=?", (packet["turn_id"],))
    with pytest.raises(ValueError, match="undelivered reference"):
        coaching.accept(state, result, invocation_id=envelope.key.work_id)
    with state.db.write() as conn:
        conn.executemany("INSERT INTO turn_references VALUES(?,?)",
                         [(packet["turn_id"], ref) for ref in result["references"]])
    state.directive("Constraint changed after preflight")
    coaching.accept(state, result, invocation_id=envelope.key.work_id)
    assert state.campaign()["generation_state"] == "open"
    with state.db.read() as conn:
        assert coaching.pending(conn)["state"] == "needs_operator"


def test_reported_handoff_claim_does_not_force_independent_observation_reads(tmp_path):
    state, packet, envelope, result = review(tmp_path)
    rationale = packet["content"]["sources"]["rationale"]["id"]
    result["references"] = [rationale]
    result["limitations"] = "Relies on the delivered researcher handoff; raw observations not independently reviewed."
    before = dump(state)
    assert coaching.preflight(state, result, invocation_id=envelope.key.work_id)["valid"]
    assert dump(state) == before


def test_preflight_rejects_accept_stop_for_a_continuing_coaching_request(tmp_path):
    config, state = campaign(tmp_path)
    root, invocation = owned(config, state)
    finish(state, root, invocation, disposition="continue", coaching=True)
    packet, envelope = owned(config, state, "coach")
    result = {"turn_id": packet["turn_id"], "packet_id": packet["id"],
              "episode_id": packet["content"]["coaching"]["id"], "decision": "accept_stop",
              "reason": "No stop was proposed", "questions": [], "references": [],
              "limitations": "Process review only"}
    before = dump(state)
    with pytest.raises(ValueError, match="without a stopping proposal"):
        coaching.preflight(state, result, invocation_id=envelope.key.work_id)
    assert dump(state) == before


def test_rejected_sdk_call_preserves_an_already_recorded_valid_verdict(tmp_path):
    state, _, envelope, result = review(tmp_path)
    tool = next(tool for tool in research_tools.sdk_tools(state, envelope) if tool.name == "coach_decide")
    args = {key: value for key, value in result.items() if key not in ("turn_id", "packet_id", "episode_id")}
    args["references"] = []
    output = Path(envelope.metadata["result_path"])

    async def scenario():
        recorded = await tool.handler(SimpleNamespace(tool_name="coach_decide", arguments=args))
        assert recorded.result_type == "success"
        before = output.read_bytes(), dump(state)
        failed = await tool.handler(SimpleNamespace(tool_name="coach_decide", arguments={
            **args, "references": result["references"]}))
        assert failed.result_type == "failure"
        assert (output.read_bytes(), dump(state)) == before

    asyncio.run(scenario())
