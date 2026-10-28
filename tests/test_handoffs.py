import json
from pathlib import Path

import pytest

from labgoblin import handoffs, inspector, runtime
from labgoblin.inspection_evidence import Evidence, EvidenceTools
from labgoblin.storage import digest, read_json
from tests.test_inspector import captured, command, identities, observer
from tests.test_simple_recovery import wait_until


def test_dated_handoffs_are_fresh_read_only_and_keep_every_execution(observer, project, ledger):
    observer.approve(100, model="model-free", timeout=60)
    argv = command(observer)
    observer.ask("A previous chat must not seed the reporter", command=argv)
    first = captured(project, ledger)
    before, original_rows = identities(observer.project), ledger.runs(observer.project_id)
    report = handoffs.create(observer, first.id, command=argv)
    assert report["status"] == "completed", report
    assert first.id in report["report"] and Path(report["report"]).is_file()
    inspection = observer.state / "runs" / report["inspection_id"]
    prompt = read_json(inspection / "prompt.json")
    assert prompt["history"] == [] and "idea_drafts_version" not in prompt
    assert observer.source(report["inspection_id"], "E2")["result"]["scope"] == first.id
    assert identities(observer.project) == before and ledger.runs(observer.project_id) == original_rows
    assert observer.status()["inspection"]["budget"]["remaining"] == 80
    assert handoffs.create(observer, first.id, command=argv) == report
    assert len(ledger.runs(observer.id, inspection=True)) == 2
    original_html = Path(report["report"]).read_bytes()
    (observer.project / "report.md").write_text("Later research must not rewrite the previous handoff.")
    second = captured(project, ledger, allowance=290_000_000_000)
    another = handoffs.create(observer, second.id, command=argv)
    assert another["status"] == "completed" and another["inspection_id"] != report["inspection_id"]
    history = handoffs.history(observer)
    assert [item["research_run"] for item in history["handoffs"]] == [first.id, second.id]
    assert Path(report["report"]).read_bytes() == original_html
    assert not ledger.rows()


def test_terminal_report_tools_cannot_escape_source_or_publish(observer, project, ledger):
    envelope = captured(project, ledger)
    receipt = json.loads(ledger.consumer_run(envelope.id)["receipt"])
    terminal = {"research_run": envelope.id, "receipt_sha256": digest(receipt)}
    evidence = Evidence(observer.project, observer.project_id, terminal=terminal)
    assert evidence.read({"path": "report.md"})["scope"] == envelope.id
    for selected in ("workspace", "0" * 32):
        with pytest.raises(ValueError, match="handoff"):
            evidence.read({"scope": selected, "path": "report.md"})
    with pytest.raises(ValueError, match="only its exact"):
        evidence.execution({"run": "0" * 32})
    names = {tool.name for tool in EvidenceTools(evidence, observer.root / "captures").tools()}
    assert "labgoblin_inspect_session" in names
    assert "labgoblin_inspect_ideas" not in names and "labgoblin_prepare_idea" not in names


@pytest.mark.parametrize("failure", ["approval", "missing_usage", "citation"])
def test_unavailable_handoff_never_changes_research_or_retries(observer, project, ledger, failure):
    observer.approve(100, model="model-free", timeout=60)
    envelope = captured(project, ledger)
    before, original = identities(observer.project), ledger.consumer_run(envelope.id)
    argv = command(observer, mode="missing_usage" if failure == "missing_usage" else "inspection",
                   answer="Unsupported citation [E999]" if failure == "citation" else "Existing evidence [E2].")
    report = handoffs.create(observer, envelope.id, command=argv,
                             approval_sha256="changed" if failure == "approval" else None)
    assert report["status"] != "completed" and "report" not in report
    assert report["reason"]
    assert identities(observer.project) == before and ledger.consumer_run(envelope.id) == original
    count = len(ledger.runs(observer.id, inspection=True))
    assert handoffs.create(observer, envelope.id, command=argv) == report
    assert len(ledger.runs(observer.id, inspection=True)) == count
    if failure == "approval":
        assert count == 0
    if failure == "missing_usage":
        assert observer.status()["inspection"]["budget"]["uncovered_unknown"]
    assert not ledger.rows()


def test_opted_in_native_execution_automatically_reports_after_settlement(observer, project, ledger, monkeypatch):
    from labgoblin.storage import atomic_json, publish_json
    root, _, _ = project
    observer.approve(100, model="model-free", timeout=60)
    argv = command(observer)
    publish_json(root / "fixture-provider.json", {"model_free": True, "root": str(root)})
    monkeypatch.setattr(inspector, "default_directory", lambda _: observer.root)
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: {"command": argv})
    result = runtime.run(root, handoff=True)
    assert result["status"] == "completed", result
    outcome = observer.state / "handoffs" / result["id"] / "outcome.json"
    wait_until(outcome.is_file, seconds=30)
    report = handoffs.get(observer, result["id"])
    assert report["status"] == "completed", report
    assert json.loads(ledger.consumer_run(result["id"])["receipt"]) == result
    assert result["usage"]["sessions"][0]["prompts"] == 1
    session = Evidence(root, observer.project_id).session({"run": result["id"]})
    assert session["receipt_verified"] and session["manifest_closed"]
    assert observer.status()["inspection"]["budget"]["used"] == 10
    assert runtime.status(root)["budget"]["used"] == 10
    assert not ledger.rows()
    identity_path = root / ".labgoblin" / "runs" / result["id"] / "sessions" / "researcher" / "identity.json"
    identity = read_json(identity_path)
    identity.pop("event_log_version")
    atomic_json(identity_path, identity)
    with pytest.raises(ValueError, match="capability differs"):
        Evidence(root, observer.project_id).session({"run": result["id"]})


@pytest.mark.parametrize("failure", ["research_cancelled", "report_citation"])
def test_native_handoff_preserves_research_across_failures(observer, project, ledger, monkeypatch, failure):
    from concurrent.futures import ThreadPoolExecutor
    from labgoblin import payload
    from labgoblin.processes import process_state
    from labgoblin.storage import hash_file, publish_json
    from tests.test_simple_recovery import started
    root, _, record = project
    observer.approve(100, model="model-free", timeout=60)
    argv = command(observer, answer="Unsupported citation [E999]" if failure == "report_citation"
                   else "The retained report is a partial checkpoint [E2].")
    publish_json(root / "fixture-provider.json", {
        "model_free": True, "root": str(root),
        "mode": "wait" if failure == "research_cancelled" else "normal"})
    monkeypatch.setattr(inspector, "default_directory", lambda _: observer.root)
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: {"command": argv})
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run, root, handoff=True)
        try:
            if failure == "research_cancelled":
                wait_until(lambda: started(root))
                runtime.stop(root)
            result = future.result(timeout=90)
        finally:
            if any(row["phase"] != "quiescent" for row in ledger.runs(record["id"])):
                runtime.stop(root)
    expected = "cancelled" if failure == "research_cancelled" else "completed"
    assert result["status"] == expected, result
    original_rows = ledger.runs(record["id"])
    directory = root / ".labgoblin" / "runs" / result["id"]
    session_files = identities(directory / "sessions")
    outcome = observer.state / "handoffs" / result["id"] / "outcome.json"
    wait_until(outcome.is_file, seconds=90)
    report = handoffs.get(observer, result["id"])
    assert report["status"] == ("failed" if failure == "report_citation" else "completed"), report
    assert handoffs.create(observer, result["id"], command=argv) == report
    assert ledger.runs(record["id"]) == original_rows
    assert read_json(directory / "receipt.json") == result
    assert identities(directory / "sessions") == session_files
    for item in read_json(directory / "snapshot.json")["files"]:
        assert hash_file(root / item["path"]) == item["sha256"]
    assert result["usage"]["complete"] and result["usage"]["sessions"][0]["prompts"] == 1
    inspection_rows = ledger.runs(observer.id, inspection=True)
    assert len(original_rows) == len(inspection_rows) == 1
    for row in original_rows + inspection_rows:
        envelope = json.loads(row["envelope"])
        wait_until(lambda: process_state(json.loads(row["supervisor"])) == "dead")
        assert payload.tree_state(Path(envelope["root"]) / "process", envelope["id"]) == "dead"
    assert not ledger.rows()
    assert not observer.status()["inspection"]["budget"]["uncovered_unknown"]


def test_reporter_reads_late_original_events_without_changing_research_context(observer, project, ledger):
    import uuid
    from labgoblin.session_log import EventLog
    from labgoblin.storage import publish_json
    envelope = captured(project, ledger)
    directory = Path(envelope.root) / "sessions" / "researcher"
    publish_json(directory / "identity.json", {
        "run_id": envelope.id, "envelope_digest": envelope.digest, "session_id": str(uuid.UUID(envelope.id)),
        "role": "researcher", "event_log_version": 1})
    log = EventLog(directory, event_bytes=1000, total_bytes=10000)
    log.append({"type": "tool.execution_complete", "data": {"content": "Late negative result, absent from report."}}, prompt=2)
    log.close()
    before = identities(observer.project)
    observer.approve(100, model="model-free", timeout=60)
    report = handoffs.create(observer, envelope.id, command=command(
        observer, inspect_session={"run": envelope.id, "prompt": 2},
        required_event_text="Late negative result", answer="The written report [E2] predates a late negative result [E3]."))
    assert report["status"] == "completed", report
    source = observer.source(report["inspection_id"], "E3")["result"]
    assert source["events"][0]["prompt"] == 2 and "Late negative" in source["events"][0]["data"]
    assert not source["receipt_verified"]
    assert identities(observer.project) == before
    path = Path(report["report"])
    path.write_text("changed")
    with pytest.raises(ValueError, match="changed after creation"):
        handoffs.get(observer, envelope.id)


def test_interrupted_publication_is_explicit_and_never_rebuys_inference(observer, project, ledger, monkeypatch):
    envelope = captured(project, ledger)
    observer.approve(100, model="model-free", timeout=60)
    before = identities(observer.project)
    argv = command(observer)
    original = handoffs.publish_json
    def fail_outcome(path, value):
        if Path(path).name == "outcome.json":
            raise OSError("Injected publication failure")
        return original(path, value)
    monkeypatch.setattr(handoffs, "publish_json", fail_outcome)
    with pytest.raises(OSError, match="publication failure"):
        handoffs.create(observer, envelope.id, command=argv)
    assert handoffs.get(observer, envelope.id)["status"] == "incomplete"
    assert handoffs.create(observer, envelope.id, command=argv)["status"] == "incomplete"
    assert observer.status()["inspection"]["budget"]["used"] == 10
    assert len(ledger.runs(observer.id, inspection=True)) == 1
    assert identities(observer.project) == before


def test_missing_snapshot_and_modified_source_facts_never_fall_back(observer, project, ledger):
    from labgoblin.storage import atomic_json
    envelope = captured(project, ledger)
    receipt = json.loads(ledger.consumer_run(envelope.id)["receipt"])
    evidence = Evidence(observer.project, observer.project_id, terminal={
        "research_run": envelope.id, "receipt_sha256": digest(receipt)})
    (Path(envelope.root) / "snapshot.json").unlink()
    with pytest.raises(FileNotFoundError):
        evidence.read({"path": "report.md"})
    assert handoffs.history(observer)["handoffs"][0]["status"] == "not_produced"
    with pytest.raises(ValueError, match="separately approved"):
        handoffs.create(observer, envelope.id)
    assert not (observer.state / "handoffs" / envelope.id / "request.json").exists()
    observer.approve(100, model="model-free", timeout=60)
    handoffs.create(observer, envelope.id, approval_sha256="changed")
    request_path = observer.state / "handoffs" / envelope.id / "request.json"
    request = read_json(request_path)
    request["facts"]["status"] = "invented success"
    atomic_json(request_path, request)
    with pytest.raises(ValueError, match="source identity"):
        handoffs.get(observer, envelope.id)
