from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import uuid
import threading
import time

import pytest

from labgoblin import ideas, provider, runtime
from labgoblin.inspection_evidence import Evidence
from labgoblin.inspection_ideas import Drafts, retained
from labgoblin.storage import hash_file, read_json, run_lock
from tests.conftest import arm
from tests.test_closure_critic import closing
from tests.test_inspector import command, identities, observer, request, server
from tests.test_simple_provider import FakeSession, exercise


def publish(project, tmp_path, *, name="operator-lead", body="An attributed suggestion, not an instruction.", base=None):
    path = tmp_path / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return ideas.Inbox(project).publish([path], idea_id=name, heading="Useful lead", base=base)


def after_send(monkeypatch, action):
    original = FakeSession.send

    async def send(session, prompt):
        result = await original(session, prompt)
        action(session)
        return result

    monkeypatch.setattr(FakeSession, "send", send)


def test_publication_captures_revisions_and_never_takes_controller_lock(project, tmp_path):
    root = project[0]
    inbox = ideas.Inbox(root)
    before = identities(root)
    assert inbox.status()["pending"] == 0 and inbox.entries() == []
    assert runtime.status(root)["ideas"]["pending"] == 0
    assert identities(root) == before
    with run_lock(root / ".labgoblin.lock"):
        first = publish(root, tmp_path)
    draft = tmp_path / "operator-lead.md"
    captured = root / inbox.entries()[0]["file"]
    original = captured.read_bytes()
    same = inbox.publish([draft], idea_id=first["id"], heading="Useful lead")
    assert same["unchanged"] and inbox.status()["sequence"] == 1
    draft.write_text("A revised suggestion.", encoding="utf-8")
    with pytest.raises(ValueError, match="--base"):
        inbox.publish([draft], idea_id=first["id"], heading="Useful lead")
    second = inbox.publish([draft], idea_id=first["id"], heading="Useful lead", base=first["revision"])
    assert second["revision"] != first["revision"] and captured.read_bytes() == original
    assert inbox.manifest(second["id"], second)["base"]["revision"] == first["revision"]
    assert inbox.status()["pending"] == 1
    before = identities(root)
    assert inbox.entries()[0]["revision"] == second["revision"]
    assert identities(root) == before


def test_capture_failures_never_admit_partial_publications(project, tmp_path, monkeypatch):
    root = project[0]
    path = tmp_path / "draft.md"
    path.write_text("Too large for this deliberately tiny fixture bound.", encoding="utf-8")
    inbox = ideas.Inbox(root)
    with monkeypatch.context() as patch:
        patch.setattr(ideas, "MAX_BYTES", 2)
        with pytest.raises(ValueError, match="exceed"):
            inbox.publish([path], heading="Bounded")
    with pytest.raises(ValueError, match="SHA256"):
        inbox.publish([path], heading="Bounded", expected_sha256="0" * 64)
    with monkeypatch.context() as patch:
        patch.setattr(ideas, "hash_file", lambda _: "0" * 64)
        with pytest.raises(ValueError, match="changed"):
            inbox.publish([path], heading="Changed during copy")
    assert inbox.status()["pending"] == 0 and not list(inbox.control.glob("capture-*"))
    with monkeypatch.context() as patch:
        def fail(_):
            raise OSError("Deliberate index publication failure")
        patch.setattr(inbox, "save", fail)
        with pytest.raises(OSError, match="Deliberate"):
            inbox.publish([path], heading="Not admitted")
    assert inbox.entries() == [] and not list(inbox.control.glob("capture-*"))


def test_published_tampering_is_not_a_silent_revision(project, tmp_path):
    first = publish(project[0], tmp_path)
    inbox = ideas.Inbox(project[0])
    (project[0] / inbox.entries()[0]["file"]).write_text("Changed behind the manifest", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        inbox.manifest(first["id"], first, verify_files=True)
    for bad in ("../outside", "C:\\outside", "a:b", "MixedCase", ""):
        with pytest.raises(ValueError):
            inbox.publish([tmp_path / "operator-lead.md"], heading="Bad identity", idea_id=bad or "../empty")


def test_large_publication_is_a_pointer_not_prompt_injection(project, ledger, tmp_path):
    secret = "DOCUMENT_BODY_MUST_NOT_BE_INJECTED"
    first = publish(project[0], tmp_path, body=(secret + "\n") * 100000)
    envelope = arm(project, ledger, ideas=True)
    result, clients, outcome = exercise(envelope, [{}])
    assert result == 0, outcome
    assert len(clients) == 1 and len(clients[0].session.prompts) == 1
    prompt = clients[0].session.prompts[0]
    assert secret not in prompt and len(prompt) < 2000
    assert "ideas_inbox" in prompt and clients[0].session.options.get("tools") is None
    inbox = ideas.Inbox(project[0])
    entry = inbox.entries()[0]
    assert entry["revision"] == first["revision"] and entry["status"] == "responded"
    assert entry["delivery"]["phase"] == "responded" and inbox.status()["pending"] == 0
    assert (project[0] / entry["response"]["path"]).read_text() == "Research answer with retained evidence"
    assert runtime.usage_record(envelope, complete=True)["sessions"][0]["prompts"] == 1


def test_large_idea_pages_stream_and_reject_changed_page_identity(project, tmp_path):
    body = "Large captured operator document.\n" * 300000
    publish(project[0], tmp_path, body=body)
    entry = ideas.Inbox(project[0]).entries()[0]
    evidence = Evidence(project[0], project[2]["id"])
    page = evidence.read({"scope": "workspace", "path": entry["file"]})
    assert page["bytes"] > 8 * 1024 * 1024 and len(page["text"].encode()) <= 16384
    tail = evidence.read({"scope": "workspace", "path": entry["file"],
                          "offset": page["bytes"] - 40, "sha256": page["sha256"]})
    assert tail["text"] == (tmp_path / "operator-lead.md").read_bytes()[-40:].decode("utf-8")
    assert tail["next_offset"] is None
    (project[0] / entry["file"]).write_text("Altered", encoding="utf-8")
    with pytest.raises(ValueError, match="changed between pages"):
        evidence.read({"scope": "workspace", "path": entry["file"], "sha256": page["sha256"]})


def test_concurrent_publishers_and_closure_use_one_short_cutoff(project, ledger, tmp_path, monkeypatch):
    inbox = ideas.Inbox(project[0])
    barrier = threading.Barrier(2)

    def concurrent(index):
        source = tmp_path / f"concurrent-{index}.md"
        source.write_text(f"Suggestion {index}", encoding="utf-8")
        barrier.wait(timeout=5)
        return inbox.publish([source], idea_id=f"concurrent-{index}", heading=f"Lead {index}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(concurrent, range(2)))
    assert len(results) == 2 and inbox.status()["sequence"] == 2 and inbox.status()["pending"] == 2
    envelope = arm(project, ledger, ideas=True)
    assert exercise(envelope, [{}])[0] == 0
    later = ideas.RunInbox(envelope)
    entered, release = threading.Event(), threading.Event()
    original = inbox.save

    def delayed(value):
        entered.set()
        assert release.wait(timeout=5)
        original(value)

    monkeypatch.setattr(inbox, "save", delayed)
    source = tmp_path / "arriving.md"
    source.write_text("A genuinely later suggestion", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as executor:
        publishing = executor.submit(inbox.publish, [source], idea_id="arriving", heading="During close")
        assert entered.wait(timeout=5)
        closing = executor.submit(later.close_if_empty)
        time.sleep(0.05)
        assert not closing.done()
        release.set()
        publishing.result(timeout=5)
        assert closing.result(timeout=5) is False


def test_low_remaining_allowance_leaves_new_ideas_pending(project, ledger, tmp_path, monkeypatch):
    root, config, record = project
    envelope = arm((root, replace(config, max_ai_credits=30), record), ledger, allowance=30 * 10**9, ideas=True)
    after_send(monkeypatch, lambda _: publish(project[0], tmp_path))
    result, clients, outcome = exercise(envelope, [{"cost": 1}])
    assert result == 1 and outcome["status"] == "budget_exhausted"
    assert len(clients[0].session.prompts) == 1 and ideas.Inbox(project[0]).status()["pending"] == 1
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 10**9


def test_new_revision_during_reply_is_not_acknowledged_by_old_response(project, ledger, tmp_path, monkeypatch):
    first = publish(project[0], tmp_path)
    envelope = arm(project, ledger, ideas=True)
    after_send(monkeypatch, lambda session: publish(project[0], tmp_path, body="A later revision.",
                                                   base=first["revision"]) if len(session.prompts) == 1 else None)
    result, clients, outcome = exercise(envelope, [{}])
    assert result == 0, outcome
    assert len(clients[0].session.prompts) == 2
    directory = Path(envelope.root) / "ideas"
    assert read_json(directory / "prompt-1.json")["items"][0]["revision"] == first["revision"]
    assert read_json(directory / "prompt-2.json")["items"][0]["revision"] != first["revision"]
    assert ideas.Inbox(project[0]).entries()[0]["response"]["prompt"] == 2


@pytest.mark.parametrize("coach", [False, True])
def test_ideas_arriving_during_research_use_same_session_and_owned_extra_prompt(project, ledger, tmp_path, monkeypatch, coach):
    envelope = arm(project, ledger, ideas=True, coach=coach)
    researcher_id = str(uuid.UUID(envelope.id))

    def arrived(session):
        if session.options["session_id"] == researcher_id and len(session.prompts) == 1:
            publish(project[0], tmp_path)

    after_send(monkeypatch, arrived)
    scripts = [{}] + ([{"decision": "accept"}, {"decision": "accept"}] if coach else [])
    result, clients, outcome = exercise(envelope, scripts)
    assert result == 0, outcome
    assert len(clients[0].session.prompts) == (3 if coach else 2)
    assert ideas.extra_prompts(envelope, len(clients[0].session.prompts)) == 1
    assert ideas.Inbox(project[0]).status()["pending"] == 0
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == (50 if coach else 20) * 10**9
    if coach:
        assert "operator-lead" in clients[1].session.prompts[0]
        assert "reply.md" in clients[1].session.prompts[0]
    path = Path(envelope.root) / "ideas" / "prompt-2.json"
    value = read_json(path)
    value["run_id"] = "0" * 32
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="exact operator-idea"):
        runtime.usage_record(envelope, complete=True)


def test_unseen_ideas_invalidate_closing_review_without_repeating_neutral_challenge(project, ledger, tmp_path, monkeypatch):
    envelope = arm(project, ledger, ideas=True, coach=True)
    final_coach = str(uuid.uuid5(uuid.UUID(envelope.id), "coach-1"))
    after_send(monkeypatch, lambda session: publish(project[0], tmp_path)
               if session.options["session_id"] == final_coach else None)
    result, clients, outcome = exercise(envelope, [{}, *[{"decision": "accept"}] * 3])
    assert result == 0, outcome
    assert len(clients[0].session.prompts) == 3
    assert [review["stalled_reviews"] for review in outcome["reviews"]] == [0, 1, 2]
    assert "New operator-published" in clients[0].session.prompts[2]
    assert "neutral evidence-grounded stopping challenge" not in clients[0].session.prompts[2]
    assert runtime.usage_record(envelope, complete=True)["sessions"][0]["prompts"] == 3
    assert ideas.Inbox(project[0]).status()["last_closure"]["run_id"] == envelope.id
    publish(project[0], tmp_path, name="after-closure")
    assert ideas.Inbox(project[0]).status()["pending"] == 1
    assert len(clients[0].session.prompts) == 3


@pytest.mark.parametrize("boundary", ["critic", "response", "adjudicator"])
def test_arrivals_at_closing_boundaries_reach_researcher_before_exit(project, ledger, tmp_path, monkeypatch, boundary):
    root, config, record = project
    config = replace(config, coach=True, consultant=True, closure_critic=True)
    envelope = arm((root, config, record), ledger, coach=True, consultant=True, ideas=True)
    target = (str(uuid.UUID(envelope.id)) if boundary == "response" else
              str(uuid.uuid5(uuid.UUID(envelope.id), "consult-0" if boundary == "critic" else "adjudicator-1")))
    after_send(monkeypatch, lambda session: publish(root, tmp_path)
               if session.options["session_id"] == target and
               (boundary != "response" or len(session.prompts) == 3) else None)
    result, clients, outcome = exercise(envelope, [
        {}, {"decision": "accept"}, *closing(), *closing()], seconds=3600)
    assert result == 0, outcome
    assert len(clients[0].session.prompts) == (3 if boundary == "critic" else 5)
    assert len(outcome["reviews"]) == (2 if boundary == "critic" else 3)
    assert "operator-lead" in clients[-1].session.prompts[0]
    assert "SECRET PRELIMINARY COACH APPROVAL" not in clients[-1].session.prompts[0]
    assert ideas.Inbox(root).status()["pending"] == 0
    assert ideas.extra_prompts(envelope, len(clients[0].session.prompts)) == 0
    assert runtime.usage_record(envelope, complete=True)


@pytest.mark.parametrize("failure", ["cancel", "quota", "missing_usage"])
def test_interrupted_or_unsettled_delivery_is_not_a_researcher_response(project, ledger, tmp_path, monkeypatch, failure):
    publish(project[0], tmp_path)
    envelope = arm(project, ledger, ideas=True)
    script = {failure: True} if failure != "cancel" else {}
    if failure == "cancel":
        after_send(monkeypatch, lambda _: (Path(envelope.root) / "cancel").write_text("Stop", encoding="utf-8"))
    result, _, outcome = exercise(envelope, [script])
    assert result != 0, outcome
    assert ideas.Inbox(project[0]).status()["pending"] == 1
    assert ideas.Inbox(project[0]).entries()[0]["status"] == "submitted"
    assert not list((Path(envelope.root) / "ideas").glob("response-*.json"))


def test_publication_cannot_renew_budget_or_stalled_review_counter(project, ledger, tmp_path, monkeypatch):
    root, config, record = project
    config = replace(config, max_stalled_reviews=1)
    envelope = arm((root, config, record), ledger, coach=True, ideas=True)
    final_coach = str(uuid.uuid5(uuid.UUID(envelope.id), "coach-1"))
    after_send(monkeypatch, lambda session: publish(root, tmp_path)
               if session.options["session_id"] == final_coach else None)
    result, clients, outcome = exercise(envelope, [{}, {"decision": "accept"}, {"decision": "accept"}])
    assert result == 1 and outcome["status"] == "checkpointed"
    assert len(clients[0].session.prompts) == 2 and ideas.Inbox(root).status()["pending"] == 1
    assert not (Path(envelope.root) / "ideas" / "closure.json").exists()


def test_legacy_frozen_prompt_does_not_gain_idea_continuations(project, ledger, tmp_path, monkeypatch):
    envelope = arm(project, ledger)
    after_send(monkeypatch, lambda _: publish(project[0], tmp_path))
    result, clients, outcome = exercise(envelope, [{}])
    assert result == 0, outcome
    assert len(clients[0].session.prompts) == 1 and ideas.Inbox(project[0]).status()["pending"] == 1
    assert runtime.usage_record(envelope, complete=True)


def test_chat_drafts_capture_sources_outside_research_and_reject_private_paths(observer, tmp_path):
    before = identities(observer.project)
    drafts = Drafts(Evidence(observer.project, observer.project_id), tmp_path / "chat-run", "a" * 32)
    for path in (".labgoblin\\project.json", "..\\outside.md", "inputs\\secret.txt"):
        with pytest.raises((OSError, ValueError)):
            drafts.prepare({"title": "Not allowed", "message": "Draft", "sources": [{"scope": "workspace", "path": path}]})
    record = drafts.prepare({"title": "Compare the control", "message": "A suggestion, not evidence.",
                             "sources": [{"scope": "workspace", "path": "report.md"}]})
    assert identities(observer.project) == before
    (observer.project / "report.md").write_text("Later evidence.", encoding="utf-8")
    directory, value = retained(drafts.directory, observer.project_id, "a" * 32, record)
    assert "failed positive control" in (directory / value["files"][1]["path"]).read_text()
    (directory / "idea.md").write_text("Changed after review", encoding="utf-8")
    with pytest.raises(ValueError, match="bytes changed"):
        retained(drafts.directory, observer.project_id, "a" * 32, record)


def test_owned_chat_draft_needs_exact_human_publication_and_is_idempotent(server, observer):
    observer.approve(100, model="model-free", timeout=60)
    before = identities(observer.project)
    result = observer.ask("Prepare this idea for publication", command=command(
        observer, idea={"title": "Try a different control", "message": "Operator suggestion: compare the retained control.",
                        "sources": [{"scope": "workspace", "path": "report.md"}]}))
    assert result["status"] == "completed", result
    assert identities(observer.project) == before
    history = observer.history()["messages"][0]
    draft = history["idea_drafts"][0]
    args = {"run": result["id"], "draft": draft["id"], "sha256": draft["sha256"]}
    assert request(server, "POST", "/api/publish-idea", args)[0] == 400
    server.chat = True
    assert request(server, "POST", "/api/publish-idea", args, token=False)[0] == 403
    assert request(server, "POST", "/api/publish-idea", args, Origin="https://other.example")[0] == 400
    assert request(server, "POST", "/api/publish-idea", {**args, "sha256": "0" * 64})[0] == 400
    status, _, body = request(server, "POST", "/api/idea-draft-file", {**{key: args[key] for key in ("run", "draft")}, "index": 1})
    assert status == 200 and "failed positive control" in json.loads(body)["text"]
    status, _, body = request(server, "POST", "/api/publish-idea", args)
    assert status == 200, body
    assert ideas.Inbox(observer.project).status()["pending"] == 1
    status, _, body = request(server, "POST", "/api/publish-idea", args)
    assert status == 200 and json.loads(body)["unchanged"]
    assert observer.status()["inspection"]["budget"]["used"] == 10
    assert observer.status()["research"]["budget"]["used"] == 0
