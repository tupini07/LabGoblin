"""Fresh-turn context selection must not become another source of research authority."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from labgoblin import briefing, coaching, reporting, research_memory as memory, research_tasks as tasks, research_tools
from labgoblin.campaign import Campaign
from labgoblin.protocol import PACKET_BYTES, canonical
from labgoblin.state import _source_revision
from tests.test_research_loop import campaign, charter, finish, owned, publish
from tests.test_state import allocate, envelope as attempt_envelope, receipt, spec


def focus(config, state, references=(), **changes):
    packet, envelope = owned(config, state)
    for ref in references:
        research_tools.read(state, {"operation": "record", "id": ref}, turn_id=packet["turn_id"])
    finish(state, packet, envelope, research_refs=list(references), **changes)
    return packet


def checkpoint(packet):
    return packet["content"]["research_workspace"]["focus"]["rationale"]


def completed(state, key):
    attempt = spec(state, key)
    state.enqueue(attempt)
    launch = attempt_envelope(state, attempt, allocate(state, attempt))
    state.arm(launch)
    state.finish_launch(launch.key.nonce, receipt(launch))
    state.released(launch.key.grant_id)
    collected = state.record_collection(attempt["id"], [], "complete", "valid", "Synthetic collection")
    return attempt["id"], collected["event_id"]


@pytest.mark.parametrize("activity", ["active", "parked", "retired"])
def test_selected_old_revision_precedes_recent_discovery_and_never_follows_its_head(tmp_path, activity):
    config, state = campaign(tmp_path)
    original = publish(state, "important", activity=activity, body="Revisit only if the control changes.")
    focus(config, state, [original["source_id"]], rationale="Preserve the reason for the earlier negative result.")
    changed = publish(state, "important", base=original["source_id"], request_id="important-v2",
                      body="A newer, not selected interpretation.")
    for i in range(15):
        publish(state, f"new-{i}")
    packet = briefing.prepare(state)
    workspace = packet["content"]["research_workspace"]
    selected = workspace["selected"]
    assert [item["source_id"] for item in selected] == [original["source_id"]]
    assert selected[0]["body"]["text"] == original["content"]["body"]
    assert selected[0]["activity"] == activity and selected[0]["version"] == 1
    assert selected[0]["is_current_head"] is False
    assert selected[0]["selected_by"] == checkpoint(packet)["source_id"]
    assert changed["source_id"] not in packet["content"]["references"]
    assert workspace["recent_total"] == 15 and workspace["recent_has_more"]
    assert len(workspace["records"]) == 12
    assert all(item["id"] != "important" for item in workspace["records"])
    saved = Path(packet["path"]).read_bytes()
    publish(state, "later")
    assert briefing.prepare(state, turn_id=packet["turn_id"])["content"] == packet["content"]
    assert Path(packet["path"]).read_bytes() == saved


def test_publications_surviving_a_rejected_turn_do_not_become_accepted_focus(tmp_path):
    config, state = campaign(tmp_path)
    original = publish(state, "direction", kind="reflection")
    focus(config, state, [original["source_id"]], rationale="Accepted direction.")
    packet, envelope = owned(config, state)
    changed = memory.publish(state, {"id": "direction", "base": original["source_id"], "request_id": "unaccepted",
        "kind": "reflection", "title": "Not accepted", "body": "Unaccepted replacement direction."},
        turn_id=packet["turn_id"])
    with pytest.raises(ValueError, match="evidence references"):
        finish(state, packet, envelope, rationale="Rejected direction.", research_updates=[{
            "id": "bad-assessment", "request_id": "bad-assessment", "kind": "hypothesis",
            "title": "Invalid", "body": "No evidence links", "outcome": "supported"}])
    state.finish_turn(packet["turn_id"], "Rejected handoff")
    next_packet = briefing.prepare(state)
    assert memory.read(state, "direction")["source_id"] == changed["source_id"]
    assert checkpoint(next_packet)["applicability"] == "current"
    assert json.loads(next_packet["content"]["sources"]["rationale"]["text"])["rationale"] == "Accepted direction."
    assert [item["source_id"] for item in next_packet["content"]["research_workspace"]["selected"]] == [original["source_id"]]
    assert all(item["id"] != "direction" for item in next_packet["content"]["research_workspace"]["records"])
    assert changed["source_id"] in {item["reference"] for item in next_packet["content"]["supervisor_memo"]["requests"]}


def test_accepted_updates_carry_forward_beside_prioritized_pinned_revisions(tmp_path):
    config, state = campaign(tmp_path)
    original = publish(state, "claim", kind="hypothesis", body="Original hypothesis.")
    comparison = publish(state, "comparison", activity="parked")
    for i in range(15):
        publish(state, f"recent-{i}")
    accepted = focus(config, state, [comparison["source_id"], original["source_id"]], research_updates=[{
        "id": "claim", "request_id": "assessed", "base": original["source_id"], "kind": "hypothesis",
        "title": "Assessed claim", "body": "Scoped interpretation with remaining uncertainty.", "outcome": "inconclusive",
        "links": [{"target": comparison["source_id"], "relation": "context"}]}])
    assessed = memory.read(state, "claim")
    publish(state, "claim", base=assessed["source_id"], request_id="later-unselected", kind="hypothesis",
            body="Later publication, not the adopted assessment.")
    packet = briefing.prepare(state)
    selected = packet["content"]["research_workspace"]["selected"]
    assert checkpoint(packet)["turn_id"] == accepted["turn_id"]
    assert [item["source_id"] for item in selected] == [comparison["source_id"], original["source_id"], assessed["source_id"]]
    assert [item["selection"] for item in selected] == ["research_refs", "research_refs", "research_updates"]
    assert selected[-1]["outcome"] == "inconclusive" and selected[-1]["version"] == 2
    assert selected[-1]["is_current_head"] is False
    assert not packet["content"]["research_workspace"]["selected_has_more"]


def test_late_stale_checkpoint_and_changed_authority_do_not_supply_current_selection(tmp_path):
    config, state = campaign(tmp_path)
    record = publish(state)
    accepted = focus(config, state, [record["source_id"]], rationale="Earlier accepted direction.")
    late, envelope = owned(config, state)
    state.source("goal", b"New governing question.", origin="operator", head="goal", notify=True)
    finish(state, late, envelope, rationale="Late stale direction.")
    packet = briefing.prepare(state)
    header = checkpoint(packet)
    assert header["turn_id"] == accepted["turn_id"]
    assert header["applicability"] == "historical"
    assert header["qualifications"] == ["Different governing authority"]
    assert packet["content"]["research_workspace"]["selected"] == []
    assert json.loads(packet["content"]["sources"]["rationale"]["text"])["rationale"] == "Earlier accepted direction."
    with state.db.read() as conn:
        source = conn.execute("SELECT source_id FROM source_heads WHERE name='checkpoint:1:root'").fetchone()[0]
        assert json.loads(conn.execute("SELECT metadata FROM sources WHERE id=?", (source,)).fetchone()[0])["stale"]


def test_reopened_generation_labels_old_focus_historical_without_selecting_it(tmp_path):
    config, state = campaign(tmp_path)
    record = publish(state)
    focus(config, state, [record["source_id"]])
    state.close_admission("Sealed fixture")
    errors = []
    Campaign(config, state=state, ledger=SimpleNamespace())._advance_closure(errors, no_agent=True, admit=True)
    assert not errors
    state.control("reopen", "resume", state.campaign()["revision"])
    packet = briefing.prepare(state)
    assert checkpoint(packet)["applicability"] == "historical"
    assert "Earlier generation" in checkpoint(packet)["qualifications"]
    assert packet["content"]["research_workspace"]["selected_total"] == 0


def test_sealed_focus_uses_sealed_authority_and_revisions_not_later_publications(tmp_path):
    config, state = campaign(tmp_path)
    record = publish(state, "sealed", activity="parked", body="Original sealed interpretation.")
    focus(config, state, [record["source_id"]])
    with state.db.read() as conn:
        original_goal = conn.execute("SELECT source_id FROM source_heads WHERE name='goal'").fetchone()[0]
    state.close_admission("Sealed fixture")
    state.source("goal", b"A different subsequent goal.", origin="operator", head="goal", notify=True)
    changed = publish(state, "sealed", base=record["source_id"], request_id="post-seal",
                      body="POST_SEAL_INTERPRETATION")
    late = publish(state, "late-record", body="POST_SEAL_DISCOVERY")
    view = reporting.seal_view(state, kind="closure")
    packet = briefing.prepare(state, "final_analysis", view_id=view["id"])
    assert checkpoint(packet)["applicability"] == "sealed"
    assert packet["content"]["sources"]["goal"]["id"] == original_goal
    workspace = packet["content"]["research_workspace"]
    assert workspace["selected"][0]["source_id"] == record["source_id"]
    assert workspace["selected"][0]["body"]["text"] == record["content"]["body"]
    assert workspace["selected"][0]["is_current_head"] is None
    assert workspace["records"] == [] and workspace["recent_total"] == 0
    assert changed["source_id"] not in packet["content"]["references"]
    assert late["source_id"] not in packet["content"]["references"]
    assert b"POST_SEAL" not in canonical(packet["content"])
    assert packet["content"]["inventory"]["metadata"]["authority_revision"] != packet["content"]["authority_revision"]


def test_workers_receive_own_checkpoint_and_charter_not_another_workers_strategy(tmp_path):
    config, state = campaign(tmp_path)
    root_record = publish(state, "root-direction")
    charter_record = publish(state, "charter-note", activity="parked")
    root = focus(config, state, [root_record["source_id"]], rationale="Root interpretation.")
    task = tasks.create(state, charter(research_refs=[charter_record["source_id"]]))
    other = tasks.create(state, charter(request_id="other"))
    child, envelope = owned(config, state, "worker", task["id"])
    local = memory.publish(state, {"id": "local", "request_id": "local", "title": "Local", "kind": "knowledge",
                                   "body": "Local revisit condition."}, turn_id=child["turn_id"])
    finish(state, child, envelope, rationale="Worker interpretation.", research_refs=[local["source_id"]])
    peer, envelope = owned(config, state, "worker", other["id"])
    peer_note = memory.publish(state, {"id": "peer", "request_id": "peer", "title": "Peer", "kind": "knowledge",
                                      "body": "Unintegrated worker claim."}, turn_id=peer["turn_id"])
    finish(state, peer, envelope, disposition="finalize", research_refs=[peer_note["source_id"]],
           stopping_criterion="Task deliverables returned")
    packet = briefing.prepare(state, "worker", scope_id=task["id"])
    workspace = packet["content"]["research_workspace"]
    assert checkpoint(packet)["turn_id"] == child["turn_id"]
    assert workspace["focus"]["root_rationale"]["turn_id"] == root["turn_id"]
    assert [item["source_id"] for item in workspace["selected"]] == [local["source_id"], charter_record["source_id"]]
    assert all(item["scope_id"] == task["id"] for item in workspace["records"])
    assert peer_note["source_id"] not in packet["content"]["references"]
    assert [item["id"] for item in packet["content"]["work"]["tasks"]["records"]] == [task["id"]]
    root_packet = briefing.prepare(state)
    assert checkpoint(root_packet)["turn_id"] == root["turn_id"]
    assert local["source_id"] not in root_packet["content"]["references"]
    assert peer_note["source_id"] not in root_packet["content"]["references"]
    peer_status = next(item for item in root_packet["content"]["work"]["tasks"]["records"] if item["id"] == other["id"])
    assert peer_status["state"] == "completed" and peer_status["integration_state"] is None


def test_amended_worker_charter_makes_previous_checkpoint_historical(tmp_path):
    config, state = campaign(tmp_path)
    task = tasks.create(state, charter())
    child, envelope = owned(config, state, "worker", task["id"])
    local = memory.publish(state, {"id": "local", "request_id": "local", "title": "Local", "kind": "knowledge",
                                   "body": "Previous focus"},
                           turn_id=child["turn_id"])
    finish(state, child, envelope, research_refs=[local["source_id"]])
    task = tasks.get(state, task["id"])
    tasks.amend(state, task["id"], {"base": task["charter_revision"], "objective": "Changed task", "reason": "New scope"})
    packet = briefing.prepare(state, "worker", scope_id=task["id"])
    assert checkpoint(packet)["applicability"] == "historical"
    assert checkpoint(packet)["qualifications"] == ["Different task charter"]
    assert packet["content"]["research_workspace"]["selected"] == []


def test_unicode_previews_measure_utf8_and_preserve_exact_sources(tmp_path):
    config, state = campaign(tmp_path)
    narrative = "\u6bd4\u8f83\\\"\n" * 600
    body = "\u8bc1\u636e\\\"\n" * 1200
    record = publish(state, body=body)
    focus(config, state, [record["source_id"]], rationale=narrative)
    packet = briefing.prepare(state)
    value = packet["content"]["sources"]["rationale"]
    text = json.loads(value["text"])["rationale"]
    coverage = value["field_coverage"]["rationale"]
    assert narrative.startswith(text)
    assert coverage == {"bytes": len(narrative.encode()), "returned_bytes": len(text.encode()), "truncated": True}
    assert len(text.encode()) <= 2560
    selected = packet["content"]["research_workspace"]["selected"][0]
    assert body.startswith(selected["body"]["text"]) and selected["body"]["truncated"]
    assert selected["body"]["returned_bytes"] <= 512
    assert memory.read(state, selected["source_id"])["content"]["body"] == body
    assert len(canonical(packet["content"])) <= PACKET_BYTES


@pytest.mark.parametrize("sealed", [False, True])
def test_optional_context_yields_to_exact_authority_evidence_and_inventory(tmp_path, monkeypatch, sealed):
    config, state = campaign(tmp_path)
    goal = b"G" * 39000
    state.source("goal", goal, origin="operator", head="goal")
    records = [publish(state, f"selected-{i}", body="Detailed selected context " * 600) for i in range(24)]
    focus(config, state, [item["source_id"] for item in records], rationale="Priority rationale " * 500)
    if sealed:
        for i in range(3):
            completed(state, f"sealed-{i}")
    evidence = state.event("evidence", {"body": "Captured evidence " * 1000})
    for i in range(80):
        state.event("directive", {"body": str(i) + "q" * 2000})
    if sealed:
        state.close_admission("Sealed fixture")
    view_id = reporting.seal_view(state, kind="closure")["id"] if sealed else None
    before = {}
    original = briefing._working_context

    def observe(conn, packet, *args):
        before.update(json.loads(canonical(packet)))
        original(conn, packet, *args)

    monkeypatch.setattr(briefing, "_working_context", observe)
    packet = briefing.prepare(state, "final_analysis" if sealed else "research", view_id=view_id)["content"]
    assert packet["sources"]["goal"]["text"].encode() == goal
    assert packet["events"] == before["events"]
    assert evidence in {item["id"] for item in packet["events"]}
    assert packet["coverage"] == before["coverage"] and packet["coverage"]["has_more"]
    if sealed:
        assert packet["inventory"] == before["inventory"]
    assert len(canonical(packet)) <= PACKET_BYTES
    assert len(canonical(packet)) - len(canonical(before)) <= briefing.WORKING_CONTEXT_BYTES
    workspace = packet["research_workspace"]
    assert workspace["selected_total"] == 24 and workspace["selected_has_more"]
    assert len(workspace["selected"]) <= briefing.CONTEXT_RECORDS
    assert workspace["focus"]["rationale"]["preview_included"] == ("rationale" in packet["sources"])
    assert workspace["focus"]["rationale"]["source_id"]


def test_work_pages_budget_facts_and_packet_selection_are_not_research_progress(tmp_path, monkeypatch):
    config, state = campaign(tmp_path)
    record = publish(state)
    focus(config, state, [record["source_id"]])
    for i in range(9):
        state.enqueue(spec(state, f"queued-{i}"))
    with state.db.read() as conn:
        original = (_source_revision(conn), coaching.basis(conn), coaching.snapshot(conn))
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("sources", "invocations", "maintenance", "research_revisions", "stop_episodes")}
    packet = briefing.prepare(state)["content"]
    expected = state.budget(now=packet["recorded_at"])
    for key in ("ai_credits", "gpu_hours", "active_experiments", "maximum_experiments",
                "configuration", "elapsed_admission_seconds", "managed_invocations"):
        assert packet["budgets"][key] == expected[key]
    assert packet["budgets"]["resources"] == expected["resources"]["configured"]
    assert packet["work"]["recorded_at"] == packet["recorded_at"]
    assert packet["work"]["attempts"]["counts"] == {"queued": 9}
    assert packet["work"]["attempts"]["has_more"]
    first = research_tools.read(state, {"operation": "work", "limit": 5})
    second = research_tools.read(state, {"operation": "work", "limit": 5, "offset": first["attempts"]["next_offset"]})
    assert len(first["attempts"]["records"]) == 5 and len(second["attempts"]["records"]) == 4
    assert not second["attempts"]["has_more"]
    assert len({row["id"] for row in first["attempts"]["records"] + second["attempts"]["records"]}) == 9
    with state.db.read() as conn:
        assert (_source_revision(conn), coaching.basis(conn), coaching.snapshot(conn)) == original
        assert {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in counts} == counts
    from labgoblin.cli import execute, parser
    monkeypatch.delenv("LABGOBLIN_TURN_ID", raising=False)
    result = execute(parser().parse_args(["research", "work", "--project", str(config.root), "--limit", "3", "--offset", "3"]))
    assert result["attempts"]["offset"] == 3 and len(result["attempts"]["records"]) == 3


def test_completed_collected_deferred_and_assessed_work_remain_distinct(tmp_path):
    config, state = campaign(tmp_path)
    attempt, event = completed(state, "operational-success")
    packet, launch = owned(config, state)
    status = next(row for row in packet["content"]["work"]["attempts"]["records"] if row["id"] == attempt)
    assert status["status"] == "completed" and status["exit_code"] == 0
    assert status["collection"] == "complete" and status["validation"] == "valid"
    assert status["evidence_disposition"] == "pending" and status["acknowledged_by"] is None
    finish(state, packet, launch, evidence=[{"event_id": event, "disposition": "deferred",
                                           "reason": "Needs interpretation", "wake_condition": "Next comparison"}])
    assert research_tools.read(state, {"operation": "work"})["attempts"]["records"][0]["evidence_disposition"] == "pending"
    packet, launch = owned(config, state)
    finish(state, packet, launch, evidence=[{"event_id": event, "disposition": "assessed",
                                           "reason": "Reviewed operational completion, not scientific support",
                                           "references": [event]}])
    value = research_tools.read(state, {"operation": "work"})["attempts"]["records"][0]
    assert value["evidence_disposition"] == "assessed" and value["acknowledged_by"] == packet["turn_id"]
    assert state.campaign()["research_outcome"] == "active"
    assert memory.index(state)["total"] == 0


def test_sdk_and_cli_work_reads_are_scoped_and_register_only_delivered_work(tmp_path, monkeypatch):
    config, state = campaign(tmp_path)
    root_attempt, _ = completed(state, "root-only")
    task = tasks.create(state, charter())
    tasks.create(state, charter(request_id="other-task"))
    packet, envelope = owned(config, state, "worker", task["id"])
    tool = next(item for item in research_tools.sdk_tools(state, envelope) if item.name == "research_read")
    assert "work" in tool.parameters["properties"]["operation"]["enum"]
    result = asyncio.run(tool.handler(SimpleNamespace(tool_name="research_read", arguments={"operation": "work"})))
    assert result.result_type == "success"
    value = json.loads(result.text_result_for_llm)
    assert value["scope_id"] == task["id"] and value["attempts"]["records"] == []
    assert [item["id"] for item in value["tasks"]["records"]] == [task["id"]]
    with state.db.read() as conn:
        refs = {row[0] for row in conn.execute("SELECT reference_id FROM turn_references WHERE turn_id=?", (packet["turn_id"],))}
    assert task["id"] in refs and root_attempt not in refs
    from labgoblin.cli import execute, parser
    monkeypatch.setenv("LABGOBLIN_TURN_ID", packet["turn_id"])
    cli_value = execute(parser().parse_args(["research", "work", "--project", str(config.root), "--json"]))
    assert cli_value["scope_id"] == task["id"] and cli_value["attempts"]["records"] == []
    assert cli_value["tasks"]["records"] == value["tasks"]["records"]


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 21}, {"offset": -1}, {"cutoff": 1}, {"id": "not-a-filter"}])
def test_work_rejects_invalid_or_misleading_paging(tmp_path, options):
    _, state = campaign(tmp_path)
    with pytest.raises(ValueError):
        research_tools.read(state, {"operation": "work", **options})
