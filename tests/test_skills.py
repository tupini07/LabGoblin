import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from labgoblin import runtime, skill_extraction, skills, supervisor
from labgoblin.config import Config
from labgoblin.protocol import LaunchEnvelope
from labgoblin.storage import digest, hash_file, publish_json, read_json
from tests.test_simple_runtime import fixture_provider


def fixture_options(root, **values):
    path = root / "fixture-provider.json"
    path.write_text(json.dumps({**read_json(path), **values}), encoding="utf-8")


def run_source(project, monkeypatch, **options):
    root, config, _ = project
    config = replace(config, model="model-free", advice_model="model-free",
                     timeout_seconds=120, archive_workspace=False, **options)
    (root / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch)
    receipt = runtime.run(root)
    assert receipt["status"] == "completed", receipt
    return receipt


def test_native_extraction_review_delivery_and_rejected_revision(project, ledger, monkeypatch):
    root, _, record = project
    source = run_source(project, monkeypatch)
    source_path = root / ".labgoblin" / "runs" / source["id"] / "receipt.json"
    original = hash_file(source_path)
    request_id = uuid.uuid4().hex
    attempt = skill_extraction.extract(root, source["id"], request_id=request_id)
    assert attempt["status"] == "proposed", attempt
    assert skill_extraction.extract(root, source["id"], request_id=request_id) == attempt
    store = skills.Store(root)
    assert store.listing()["active"] == {}
    show = store.show(request_id)
    assert {change["path"] for change in show["changes"]} == {"SKILL.md", "check.py"}
    assert show["batch"]["entries"][0]["sources"][0]["citation"] == "E1"
    with pytest.raises(ValueError, match="await review"):
        runtime.run(root, resume=True)
    assert store.listing()["active"] == {}
    fixture_options(root, read_skill=True)
    continued = runtime.run(root, resume=True, skills_batch=request_id)
    assert continued["status"] == "completed", continued
    directory = root / ".labgoblin" / "runs" / continued["id"]
    prompt = read_json(directory / "prompt.json")
    assert prompt["campaign_skills"]["source"]["receipt_sha256"] == digest(source)
    active = store.listing()["active"]
    assert list(active) == ["verify-total"]
    assert (directory / "skills" / "verify-total" / "check.py").read_text().startswith("assert")
    calls = [json.loads(line) for path in root.glob("fixture-tools-*.jsonl")
             for line in path.read_text().splitlines()]
    assert sum(call["name"] == "labgoblin_skill" for call in calls) == 3
    assert hash_file(source_path) == original
    fixture_options(root, read_skill=False, skill_body="A revised procedure.")
    revised_id = uuid.uuid4().hex
    revised = skill_extraction.extract(root, continued["id"], request_id=revised_id)
    assert revised["status"] == "proposed", revised
    decision = store.review(revised_id, "verify-total", "reject", expected=None)
    with pytest.raises(ValueError, match="review changed"):
        store.review(revised_id, "verify-total", "eligible", expected=None)
    selected = store.selection(store.rows(), batch_id=revised_id)
    assert selected["active"] == active and selected["decision_revision"] == decision["revision"]
    assert len(ledger.runs(record["id"])) == 2
    assert len(ledger.runs(record["id"], funded=True)) == 4
    status = runtime.status(root)
    assert status["runs"][-1]["id"] == continued["id"]
    assert status["budget"]["used"] == sum(
        json.loads(row["receipt"])["usage"]["totalNanoAiu"] for row in store.rows()) / 1e9
    assert not ledger.rows()
    # Frozen execution reads do not depend on the mutable campaign store.
    envelope = LaunchEnvelope.parse(read_json(directory / "envelope.json"))
    monkeypatch.setattr(skills.Store, "revision", lambda *_: pytest.fail("read mutable store"))
    tool = skills.tools(envelope)[0]
    reply = asyncio.run(tool.handler(SimpleNamespace(arguments={"name": "verify-total"})))
    assert "Check an exact integer total." in reply.text_result_for_llm


def test_native_scheduled_extraction_keeps_exact_hold(project, ledger, monkeypatch):
    root, config, record = project
    config = replace(config, model="model-free", advice_model="model-free", timeout_seconds=120,
                     archive_workspace=False, extract_skills=True,
                     supervisor_review=supervisor.Schedule("09:00", "UTC", "2026-01-01"))
    (root / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "supervisor_review")
    fixture_options(root, supervisor_mode="foreground", empty_skills=True)
    monkeypatch.setattr(supervisor.Schedule, "next_after", lambda *_: 1.0)
    receipt = runtime.run(root)
    assert receipt["status"] == "checkpointed", receipt
    store = skills.Store(root)
    attempts = store.listing()["attempts"]
    assert len(attempts) == 1 and attempts[0]["status"] == "no-useful-skill", attempts
    skill_extraction.automatic(root, receipt["id"])
    assert len(ledger.runs(record["id"], funded=True)) == 2
    assert runtime.status(root)["supervisor_review"]["run_id"] == receipt["id"]
    with pytest.raises(ValueError):
        runtime.run(root, resume=True, defer_skills=True)
    with pytest.raises(ValueError, match="await review"):
        runtime.run(root, resume=True, reviewed=receipt["id"])
    (root / "labgoblin.toml").write_text(
        replace(config, supervisor_review=None, extract_skills=False).toml(), encoding="utf-8")
    fixture_options(root, mode="normal")
    continued = runtime.run(root, resume=True, reviewed=receipt["id"], skills_batch=attempts[0]["id"])
    assert continued["status"] == "completed", continued
    assert runtime.status(root)["supervisor_review"] is None


@pytest.mark.parametrize("cutpoint", ["dispatch", "intent"])
def test_scheduled_intent_survives_controller_interruption(project, ledger, monkeypatch, cutpoint):
    root, config, record = project
    config = replace(config, model="model-free", timeout_seconds=120, extract_skills=True,
                     archive_workspace=False,
                     supervisor_review=supervisor.Schedule("09:00", "UTC", "2026-01-01"))
    (root / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "supervisor_review")
    fixture_options(root, supervisor_mode="foreground", empty_skills=True)
    monkeypatch.setattr(supervisor.Schedule, "next_after", lambda *_: 1.0)
    original = getattr(skill_extraction, "automatic" if cutpoint == "dispatch" else "prepare_automatic")

    def interrupt(*args):
        raise KeyboardInterrupt("Injected controller exit")

    target = "automatic" if cutpoint == "dispatch" else "prepare_automatic"
    monkeypatch.setattr(skill_extraction, target, interrupt)
    with pytest.raises(KeyboardInterrupt):
        runtime.run(root)
    monkeypatch.setattr(skill_extraction, target, original)
    store = skills.Store(root)
    assert len(ledger.runs(record["id"], funded=True)) == 1
    assert len(store.requests()) == (1 if cutpoint == "dispatch" else 0)
    runtime.status(root)
    assert len(store.requests()) == (1 if cutpoint == "dispatch" else 0)
    runtime.reconcile(root)
    request, = store.requests()
    before = read_json(store.root / "requests" / f"{request['id']}.json")
    runtime.reconcile(root)
    assert store.requests() == [before]
    assert len(ledger.runs(record["id"], funded=True)) == 1
    attempt = skill_extraction.extract(root, request_id=request["id"])
    assert attempt["status"] == "no-useful-skill", attempt
    assert runtime.status(root)["supervisor_review"]["run_id"] == request["source"]["run_id"]


@pytest.mark.parametrize("relative", ["../escape", "C:\\escape", "file:stream", "trailing."])
def test_skill_paths_reject_escape(tmp_path, relative):
    with pytest.raises(ValueError, match="Unsafe"):
        skills.safe(tmp_path, relative)


def test_extraction_config_cannot_be_requested_through_toml():
    frozen = skill_extraction.extraction_config(Config())
    assert Config.parse(frozen.to_dict(), frozen=True) == frozen
    with pytest.raises(ValueError, match="host-owned"):
        Config.parse(frozen.to_dict())
    assert Config.parse({"version": 5}, frozen=True).extract_skills is False


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_skill_frontmatter_accepts_native_line_endings(newline):
    skills.validate_entrypoint(newline.join([
        "---", "name: verify-total", "description: Check totals", "---", "Procedure",
    ]), "verify-total")


@pytest.mark.parametrize("body", [
    "---\nname: verify-total\ndescription: Check totals\n",
    "---\nname: other\ndescription: Check totals\n---\nname: verify-total\n",
    "---\nname: verify-total\n---\ndescription: Only in body\n",
    "---\nname: other\nname: verify-total\ndescription: Check totals\n---\n",
])
def test_skill_frontmatter_rejects_missing_or_misplaced_fields(body):
    with pytest.raises(ValueError, match="SKILL.md requires"):
        skills.validate_entrypoint(body, "verify-total")


@pytest.fixture
def proposal(project, monkeypatch):
    root = project[0]
    store = skills.Store(root)
    run_id = uuid.uuid4().hex
    directory = root / ".labgoblin" / "runs" / run_id
    request = {"version": 1, "id": run_id, "project_id": store.project_id,
               "active": {}, "source": {"run_id": uuid.uuid4().hex, "receipt_sha256": "source"},
               "observed_at": 1}
    publish_json(directory / "prompt.json", {"skill_extraction": request})
    publish_json(directory / "evidence" / "E1.json", {
        "action": "read", "arguments": {"path": "research-notes.md"},
        "result": {"sha256": "source", "scope": "workspace", "truncated": False}})
    publish_json(store.root / "requests" / f"{run_id}.json", request)
    output = store.root / "proposals" / run_id
    folder = output / "verify-total"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: verify-total\ndescription: Check totals\n---\nCheck the result.", encoding="utf-8")
    (folder / "check.py").write_text("raise RuntimeError('Never execute during review')", encoding="utf-8")
    publish_json(output / "proposal.json", {
        "entries": [{"name": "verify-total", "action": "upsert", "base": None,
                     "description": "Check totals", "rationale": "Costly successful procedure",
                     "sources": ["E1"], "validation": "Not executed in this fixture"}],
        "coverage": "Synthetic captured note", "reason": "Retain procedure"})
    envelope = SimpleNamespace(id=run_id, root=str(directory))
    rows = []
    monkeypatch.setattr(store, "rows", lambda: rows)
    return store, envelope, output, rows


def publish_proposal(proposal):
    store, envelope, _, rows = proposal
    binding = store.publish(envelope)
    rows.append({"token": envelope.id, "purpose": "skill-extraction", "phase": "quiescent",
                 "receipt": json.dumps({"status": "completed", "usage": {"totalNanoAiu": 0},
                                        "reason": "", "metadata": {"skills_batch": binding}})})
    return store, envelope, rows


def test_extraction_prompt_example_publishes_inactive_complete_folder(proposal):
    store, envelope, output, _ = proposal
    example = skill_extraction.INSTRUCTIONS.split("```json\n", 1)[1].split("\n```", 1)[0]
    value = json.loads(example)
    (output / "proposal.json").write_text(json.dumps(value), encoding="utf-8")
    binding = store.publish(envelope)
    batch = read_json(store.root / "batches" / f"{envelope.id}.json")
    assert binding["sha256"] == digest(batch)
    assert batch["coverage"] == value["coverage"]
    assert batch["entries"][0]["validation"] == value["entries"][0]["validation"]
    _, folder = store.revision(batch["entries"][0]["revision"])
    assert {path.name for path in folder.iterdir()} == {"SKILL.md", "check.py"}
    assert store.listing()["active"] == {}


@pytest.mark.parametrize("field", ["coverage", "validation"])
def test_object_shaped_extraction_metadata_cannot_publish(proposal, field):
    store, envelope, output, _ = proposal
    value = read_json(output / "proposal.json")
    target = value if field == "coverage" else value["entries"][0]
    target[field] = {"detail": target[field]}
    (output / "proposal.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a nonempty string"):
        store.publish(envelope)
    assert not (store.root / "batches" / f"{envelope.id}.json").exists()
    assert store.listing()["active"] == {}


@pytest.mark.parametrize("decision", ["reject", "challenge", "defer"])
def test_review_changes_invalidate_frozen_selection_without_activation(proposal, tmp_path, decision):
    store, envelope, rows = publish_proposal(proposal)
    selected = store.selection(rows, batch_id=envelope.id)
    target = tmp_path / "prepared"
    store.freeze(target, selected)
    publish_json(target / "prompt.json", {"campaign_skills": selected})
    assert store.listing()["active"] == {}
    store.review(envelope.id, "verify-total", decision, expected=None, feedback="Needs a better check")
    with pytest.raises(ValueError, match="changed before arming"):
        store.verify_selection(SimpleNamespace(root=str(target)), rows)
    assert store.selection(rows, batch_id=envelope.id)["active"] == {}
    assert len(store.decisions(envelope.id)["history"]) == 1
    assert store.listing()["active"] == {}


def test_proposal_bytes_are_frozen_and_read_tampering_is_rejected(proposal, tmp_path):
    store, envelope, rows = publish_proposal(proposal)
    (proposal[2] / "verify-total" / "check.py").write_text("changed staging bytes")
    assert "Never execute during review" in store.show(envelope.id)["changes"][1]["diff"]
    selected = store.selection(rows, batch_id=envelope.id)
    target = tmp_path / "prepared"
    store.freeze(target, selected)
    publish_json(target / "prompt.json", {"campaign_skills": selected})
    (target / "skills" / "verify-total" / "check.py").write_text("tampered")
    with pytest.raises(ValueError, match="Frozen skill bytes"):
        store.verify_selection(SimpleNamespace(root=str(target)), rows)
    tool = skills.tools(SimpleNamespace(root=str(target)))[0]
    response = asyncio.run(tool.handler(SimpleNamespace(arguments={"name": "verify-total"})))
    assert response.result_type == "failure"
    assert "Frozen skill bytes" in response.error


@pytest.mark.parametrize("invalid", ["undeclared", "missing-entrypoint", "bad-citation", "hardlink"])
def test_partial_or_invalid_proposal_never_publishes_batch(proposal, invalid, tmp_path):
    store, envelope, output, _ = proposal
    if invalid == "undeclared":
        (output / "undeclared.txt").write_text("not inventoried")
    elif invalid == "missing-entrypoint":
        (output / "verify-total" / "SKILL.md").unlink()
    elif invalid == "bad-citation":
        value = read_json(output / "proposal.json")
        value["entries"][0]["sources"] = ["../outside"]
        (output / "proposal.json").write_text(json.dumps(value))
    else:
        target = tmp_path / "outside.txt"
        target.write_text("outside bytes")
        (output / "verify-total" / "linked.txt").hardlink_to(target)
    with pytest.raises(ValueError):
        store.publish(envelope)
    assert not (store.root / "batches" / f"{envelope.id}.json").exists()
    assert store.listing()["active"] == {}


def test_pending_request_can_only_be_explicitly_deferred(proposal):
    store, envelope, _, rows = proposal
    with pytest.raises(ValueError, match="await review"):
        store.selection(rows)
    with pytest.raises(ValueError, match="No complete"):
        store.selection(rows, batch_id=envelope.id)
    selection = store.selection(rows, defer=True)
    assert selection["active"] == {}
    assert selection["handled"] == [envelope.id]
    assert store.listing()["attempts"][0]["reviewed_for_continuation"] is False


def test_large_skill_index_and_escaped_file_remain_pageable(proposal, tmp_path):
    content = ("\x01\u263a\U0001f600" * 1500) + "\u263a"
    (proposal[2] / "verify-total" / "check.py").write_text(content, encoding="utf-8")
    store, envelope, rows = publish_proposal(proposal)
    selected = store.selection(rows, batch_id=envelope.id)
    entry = selected["active"]["verify-total"]
    selected["active"].update({f"skill-{i}": {**entry, "description": "\u263a" * 5000}
                              for i in range(20)})
    selected["active_sha256"] = digest(selected["active"])
    target = tmp_path / "prepared"
    store.freeze(target, selected)
    publish_json(target / "prompt.json", {"campaign_skills": selected})
    tool = skills.tools(SimpleNamespace(root=str(target)))[0]
    found, offset = {}, 0
    while offset is not None:
        reply = asyncio.run(tool.handler(SimpleNamespace(arguments={"offset": offset})))
        assert reply.result_type != "failure", reply
        assert len(reply.text_result_for_llm.encode()) <= 16000
        page = json.loads(reply.text_result_for_llm)
        found.update(page["entries"])
        offset = page["next_offset"]
    assert set(found) == set(selected["active"])
    assert found["skill-0"]["description_truncated"]
    result, offset = "", 0
    while offset is not None:
        reply = asyncio.run(tool.handler(SimpleNamespace(arguments={
            "name": "verify-total", "path": "check.py", "offset": offset})))
        assert reply.result_type != "failure", reply
        page = json.loads(reply.text_result_for_llm)
        result += page["text"]
        offset = page["next_offset"]
    assert result == content


def test_native_extraction_budget_and_unknown_usage_do_not_replace_research(project, ledger, monkeypatch):
    root, config, record = project
    source = run_source(project, monkeypatch)
    # The source costs ten; less than the provider minimum remains.
    (root / "labgoblin.toml").write_text(replace(config, max_ai_credits=30).toml(), encoding="utf-8")
    skipped_id = uuid.uuid4().hex
    skipped = skill_extraction.extract(root, source["id"], request_id=skipped_id)
    assert skipped["status"] == "skipped-for-budget"
    assert skill_extraction.extract(root, source["id"], request_id=skipped_id) == skipped
    assert len(ledger.runs(record["id"], funded=True)) == 1
    (root / "labgoblin.toml").write_text(replace(config, max_ai_credits="unlimited").toml(), encoding="utf-8")
    fixture_options(root, empty_skills=True)
    attempt = skill_extraction.extract(root, source["id"], request_id=uuid.uuid4().hex)
    assert attempt["status"] == "no-useful-skill", attempt
    view = runtime.status(root)
    assert view["runs"][-1]["id"] == source["id"]
    assert view["budget"]["by_purpose"]["skill-extraction"]["used"] == 10
    assert not ledger.rows()
