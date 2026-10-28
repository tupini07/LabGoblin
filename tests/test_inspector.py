import asyncio
import base64
from dataclasses import asdict
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
import psutil

from labgoblin import inspector, payload, runtime
from labgoblin.inspection_evidence import Evidence, EvidenceTools, IMAGE_BYTES, image_type
from labgoblin.inspection_server import InspectionServer
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchReceipt, UncertainExecution
from labgoblin.storage import hash_file, publish_json, read_json, snapshot
from tests.conftest import arm
from tests.test_simple_provider import FakeClient
from tests.test_simple_recovery import wait_until


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=")


@pytest.fixture
def observer(project, tmp_path):
    root, _, _ = project
    (root / "report.md").write_text("A failed positive control is not an impossibility proof.\n", encoding="utf-8")
    (root / "comparison.png").write_bytes(PNG)
    return inspector.Inspector(root, directory=tmp_path / "inspector")


def command(observer, *, mode="inspection", **settings):
    publish_json(observer.root / "fixture-provider.json",
                 {"model_free": True, "root": str(observer.root), "mode": mode, **settings})
    return [sys.executable, str(Path(__file__).with_name("fixed_copilot.py"))]


def identities(root):
    return {str(path.relative_to(root)): hash_file(path) for path in root.rglob("*")
            if path.is_file() and path.name != ".labgoblin.lock"}


def captured(project, ledger, *, provider=None, allowance=300_000_000_000):
    root, config, record = project
    runtime.bound_ledger(root / ".labgoblin", config=config, bind=True)
    envelope = arm(project, ledger, allowance=allowance)
    handle = own_handle(envelope.id)
    assert ledger.claim_consumer(envelope, handle)
    directory = Path(envelope.root)
    manifest = snapshot(root, directory / "snapshot", config.max_snapshot_bytes)
    receipt = LaunchReceipt(envelope.id, envelope.digest, "completed", 1, True, returncode=0,
                            usage={"totalNanoAiu": 10_000_000_000},
                            metadata={"supervisor": handle, "snapshot": manifest, "provider": provider or {}})
    publish_json(directory / "receipt.json", asdict(receipt))
    ledger.finish_consumer(receipt)
    return envelope


def test_snapshot_selection_identity_paging_and_live_refresh(observer, project, ledger):
    root, _, record = project
    envelope = captured(project, ledger)
    evidence = Evidence(root, record["id"])
    first = evidence.read({"path": "report.md", "limit": 10})
    assert first["scope"] == envelope.id and not first["live"]
    assert first["truncated"] and first["next_offset"] == 10
    (root / "report.md").write_text("New mutable result")
    assert evidence.read({"path": "report.md"})["text"].startswith("A failed")
    assert evidence.read({"path": "report.md", "scope": "workspace"})["text"] == "New mutable result"
    (Path(envelope.root) / "snapshot" / "report.md").write_text("Tampered")
    with pytest.raises(ValueError, match="captured snapshot"):
        evidence.read({"path": "report.md"})


@pytest.mark.parametrize("path", ["..", "..\\report.md", ".labgoblin\\project.json", ".env",
                                  ".Env.local", "inputs\\private.txt", ".ssh\\id_rsa",
                                  "report.md:alternate", "C:\\private.txt"])
def test_evidence_denies_private_and_escaping_paths(observer, path):
    evidence = Evidence(observer.project, observer.project_id)
    with pytest.raises((ValueError, FileNotFoundError)):
        evidence.read({"scope": "workspace", "path": path})


def test_evidence_does_not_follow_directory_aliases(observer, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("Must not be inspected")
    alias = observer.project / "alias"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(alias))
    else:
        alias.symlink_to(outside, target_is_directory=True)
    try:
        evidence = Evidence(observer.project, observer.project_id)
        with pytest.raises(ValueError, match="links or directory aliases"):
            evidence.read({"scope": "workspace", "path": str(Path("alias") / "private.txt")})
        assert "alias" not in [item["path"] for item in evidence.listing({})["entries"]]
        assert not evidence.search({"query": "Must not be inspected"})["hits"]
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()
    assert (outside / "private.txt").read_text() == "Must not be inspected"


def test_scoped_search_listing_images_and_retained_citations(observer, tmp_path):
    evidence = Evidence(observer.project, observer.project_id)
    found = evidence.search({"query": "POSITIVE", "scope": "workspace"})
    assert found["hits"][0]["path"] == "report.md" and found["hits"][0]["line"] == 1
    assert ".labgoblin" not in [item["path"] for item in evidence.listing({})["entries"]]
    tools = EvidenceTools(evidence, tmp_path / "evidence")
    image = tools.capture("read", {"path": "comparison.png", "image": True})
    assert base64.b64decode(image["image"]) == PNG and image["mime"] == "image/png"
    assert read_json(tmp_path / "evidence" / "E1.json")["result"] == image
    assert "image" not in read_json(tmp_path / "evidence" / "E1.meta.json")
    (observer.project / "bad.svg").write_text("<svg onload='alert(1)'/>")
    with pytest.raises(ValueError, match="PNG and JPEG"):
        evidence.read({"path": "bad.svg", "image": True})
    with pytest.raises(ValueError, match="dimensions"):
        image_type(PNG[:16] + (100000).to_bytes(4, "big") * 2 + PNG[24:])
    (observer.project / "oversize.png").write_bytes(PNG + b"x" * IMAGE_BYTES)
    with pytest.raises(ValueError, match="at most"):
        evidence.read({"path": "oversize.png", "image": True})
    tools.count = 128
    with pytest.raises(ValueError, match="128"):
        tools.capture("status", {})


def test_evidence_rejects_another_projects_run(observer, project, ledger, tmp_path):
    other = tmp_path / "other"
    config = project[1]
    record = runtime.initialize(other, config, goal="Another project")
    other_run = captured((other, config, record), ledger)
    runtime.bound_ledger(observer.project / ".labgoblin", config=config, bind=True)
    with pytest.raises(ValueError, match="exact project"):
        Evidence(observer.project, observer.project_id).run(other_run.id)


def test_inspection_requires_external_history_and_explicit_finite_allowance(observer):
    with pytest.raises(ValueError, match="outside"):
        inspector.Inspector(observer.project, directory=observer.project / "chat")
    with pytest.raises(ValueError, match="Chat is disabled"):
        observer.ask("What changed?")
    with pytest.raises(ValueError, match="finite"):
        observer.approve("unlimited", model="model-free")
    assert observer.status()["inspection"]["budget"] is None
    with pytest.raises(ValueError, match="explicit --model"):
        observer.approve(100, model="auto")


def test_owned_sdk_chat_is_read_only_separately_billed_and_remembers_context(observer, project, ledger):
    observer.approve(100, model="model-free", timeout=60)
    argv = command(observer, image="comparison.png")
    before, research_rows, capacity = identities(observer.project), ledger.runs(project[2]["id"]), ledger.capacity()
    first = observer.ask("Does that failed control prove impossibility?", command=argv)
    assert first["status"] == "completed", first
    assert first["usage"]["totalNanoAiu"] == 10_000_000_000
    assert identities(observer.project) == before
    assert ledger.runs(project[2]["id"]) == research_rows and ledger.capacity() == capacity
    assert not ledger.rows()
    assert payload.tree_state(Path(observer.state / "runs" / first["id"] / "process"), first["id"]) == "dead"
    history = observer.history()["messages"]
    assert history[0]["answer"] and len(history[0]["sources"]) == 3
    assert observer.source(first["id"], "E3")["result"]["image"] == base64.b64encode(PNG).decode()
    assert "impossibility" in observer.source(first["id"], "E2")["result"]["text"]
    other_project = observer.root.parent / "other-project"
    runtime.initialize(other_project, project[1], goal="Another project's evidence")
    other = inspector.Inspector(other_project, directory=observer.root.parent / "other-chat")
    other.approve(100, model="model-free")
    with pytest.raises(ValueError, match="does not belong"):
        other.source(first["id"], "E2")
    session = first["usage"]["sessions"][0]
    options = read_json(observer.root / f"fixture-session-{session['session_id']}.json")
    assert options["enableConfigDiscovery"] is False
    assert options["enableOnDemandInstructionDiscovery"] is False
    assert options["enableSkills"] is False and options["requestExtensions"] is False
    assert {name.removeprefix("custom:") for name in options["availableTools"]} == {
        "labgoblin_inspect_status", "labgoblin_inspect_read", "labgoblin_inspect_listing",
        "labgoblin_inspect_search", "labgoblin_inspect_session", "labgoblin_inspect_execution",
        "labgoblin_inspect_ideas", "labgoblin_prepare_idea"}
    assert not options.get("mcpServers")
    (observer.project / "report.md").write_text("A new control succeeded; global impossibility remains unproved.")
    second = observer.ask("What about the new control?", command=argv)
    assert second["status"] == "completed", second
    assert "new control succeeded" in observer.source(second["id"], "E2")["result"]["text"]
    prompt = read_json(observer.state / "runs" / second["id"] / "prompt.json")
    assert prompt["history"][0]["question"] == history[0]["question"]
    assert first["usage"]["sessions"][0]["session_id"] != second["usage"]["sessions"][0]["session_id"]
    observer.approve(100, model="model-free")
    reopened = inspector.Inspector(observer.project, directory=observer.root)
    assert reopened.status()["inspection"]["budget"]["remaining"] == 80
    assert len(reopened.history()["messages"]) == 2
    assert not ledger.rows()
    source = observer.state / "runs" / first["id"] / "evidence" / "E2.json"
    source.write_text('{"result":{"text":"A forged claim"}}')
    with pytest.raises(ValueError, match="owned receipt"):
        observer.source(first["id"], "E2")


@pytest.mark.parametrize("mode,answer", [("missing_usage", None), ("inspection", "Unretrieved claim [E999]")])
def test_missing_usage_and_unretrieved_citations_are_explicit_failures(observer, ledger, mode, answer):
    observer.approve(100, model="model-free", timeout=60)
    argv = command(observer, mode=mode, **({"answer": answer} if answer else {}))
    result = observer.ask("Why stop?", command=argv)
    assert result["status"] == "failed", result
    assert observer.history()["messages"][0]["answer"] is None
    assert not ledger.rows()
    if mode == "missing_usage":
        assert result["usage"] is None
        observer.approve(1000, model="model-free")
        with pytest.raises(UncertainExecution, match="usage is unknown"):
            observer.ask("Try again", command=argv)
        assert len(ledger.runs(observer.id, inspection=True)) == 1
    else:
        assert "did not retrieve" in result["reason"]
        assert result["usage"]["totalNanoAiu"] == 10_000_000_000


def test_cancel_owns_only_inspection_and_settles_usage(observer, ledger):
    observer.approve(100, model="model-free", timeout=60)
    competing = inspector.Inspector(observer.project, directory=observer.root.parent / "other-inspector")
    competing.approve(100, model="model-free", timeout=60)
    argv = command(observer, mode="inspection_wait")
    errors = []

    def cancel():
        until = time.monotonic() + 20
        while time.monotonic() < until:
            path = observer.root / "fixture-rpc.jsonl"
            if path.exists() and '"session.send"' in path.read_text():
                try:
                    with pytest.raises(UncertainExecution, match="not proven dead"):
                        competing.ask("Do not overlap", command=argv)
                    assert competing.cancel()["inspection_cancellation_requested"] == []
                finally:
                    observer.cancel()
                return
            time.sleep(.05)
        errors.append("No inspection prompt observed")

    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        result = observer.ask("Wait", command=argv)
    finally:
        thread.join(timeout=25)
    assert not errors and not thread.is_alive()
    assert result["status"] == "cancelled", result
    assert result["usage"]["totalNanoAiu"] == 10_000_000_000
    assert not ledger.rows()
    assert not list((observer.project / ".labgoblin" / "runs").glob("*/cancel"))
    assert len(ledger.runs(observer.id, inspection=True)) == 1


def test_cancellation_before_launch_never_starts_the_sdk(observer, ledger):
    observer.approve(100, model="model-free", timeout=60)
    observer.cancel()
    result = observer.ask("Cancelled before starting", command=command(observer))
    assert result["status"] == "cancelled" and result["executed"] is False
    assert result["usage"] is None and not (observer.root / "fixture-rpc.jsonl").exists()
    assert observer.status()["inspection"]["budget"]["remaining"] == 100
    assert not ledger.rows()


def test_soft_overshoot_is_retained_and_blocks_more_questions(observer, ledger):
    observer.approve(30, model="model-free", timeout=60)
    argv = command(observer, cost=31_000_000_000)
    first = observer.ask("Read the report", command=argv)
    assert first["usage"]["totalNanoAiu"] == 31_000_000_000
    observer.approve(30, model="model-free")
    with pytest.raises(ValueError, match="Remaining allowance"):
        observer.ask("A second question", command=argv)
    assert len(ledger.runs(observer.id, inspection=True)) == 1


@pytest.mark.parametrize("role", ["researcher", "adjudicator-1"])
def test_session_events_are_qualified_bounded_and_show_retention_limits(observer, project, ledger, role):
    envelope = captured(project, ledger)
    directory = Path(envelope.root) / "sessions" / role
    import uuid
    session_id = str(uuid.UUID(envelope.id) if role == "researcher" else uuid.uuid5(uuid.UUID(envelope.id), role))
    identity = {"run_id": envelope.id, "envelope_digest": envelope.digest, "session_id": session_id, "role": role}
    publish_json(directory / "identity.json", identity)
    event = {"type": "assistant.message", "timestamp": "now", "data": {"content": "x" * 9000}}
    (directory / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    evidence = Evidence(observer.project, observer.project_id)
    request = {"run": envelope.id, **({"role": role} if role != "researcher" else {})}
    result = evidence.session(request)
    assert len(result["events"][0]["data"]) == 8192 and result["events"][0]["truncated"]
    assert result["retention_truncated"] is None
    assert result["next_offset"] is None
    with pytest.raises(ValueError, match="inside an event"):
        evidence.session({**request, "offset": 1})
    with pytest.raises(ValueError, match="role"):
        evidence.session({"run": envelope.id, "role": "..\\other"})
    with pytest.raises(ValueError, match="role"):
        evidence.session({"run": envelope.id, "role": "adjudicator-0"})
    rows = [json.dumps({**event, "data": {"content": "x" * 800000}}).encode() + b"\n"] * 4
    (directory / "events.jsonl").write_bytes(b"".join(rows))
    offsets, offset = [], 0
    for _ in rows:
        page = evidence.session({**request, "offset": offset})
        assert page["scanned_bytes"] <= 2 * 1024 * 1024
        offsets.extend(item["offset"] for item in page["events"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert offsets == [sum(map(len, rows[:number])) for number in range(len(rows))]
    assert page["next_offset"] is None
    (directory / "identity.json").write_text(json.dumps({**identity, "session_id": str(uuid.uuid4())}))
    with pytest.raises(ValueError, match="not bound"):
        evidence.session(request)


@pytest.mark.parametrize("indexed", [False, True])
def test_session_tool_pages_stay_below_cli_spill_limit(observer, project, ledger, tmp_path, indexed):
    import hashlib
    from types import SimpleNamespace
    import uuid

    from labgoblin.session_log import EventLog, VERSION

    envelope = captured(project, ledger)
    directory = Path(envelope.root) / "sessions" / "researcher"
    identity = {"run_id": envelope.id, "envelope_digest": envelope.digest,
                "session_id": str(uuid.UUID(envelope.id)), "role": "researcher"}
    if indexed:
        identity["event_log_version"] = VERSION
    publish_json(directory / "identity.json", identity)
    originals = [
        {"type": "assistant.message", "timestamp": "now",
         "data": {"content": f"{number}:" + '\\\"\u0394\U0001f600' * 3000}}
        for number in range(4)]
    if indexed:
        log = EventLog(directory, event_bytes=1024 * 1024, total_bytes=4 * 1024 * 1024)
        for value in originals:
            log.append(value, prompt=1)
        log.close()
        positions = list(range(len(originals)))
    else:
        rows = [json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n" for value in originals]
        (directory / "events.jsonl").write_bytes(b"".join(rows))
        positions = [sum(map(len, rows[:number])) for number in range(len(rows))]
    evidence = Evidence(observer.project, observer.project_id)
    captured_tools = EvidenceTools(evidence, tmp_path / "session-captures")
    tool = next(value for value in captured_tools.tools() if value.name == "labgoblin_inspect_session")

    def request(**args):
        response = asyncio.run(tool.handler(SimpleNamespace(
            tool_name=tool.name, arguments={"run": envelope.id, **args})))
        assert response.result_type != "failure", response.error
        assert len(response.text_result_for_llm.encode("utf-8")) < 20480
        value = json.loads(response.text_result_for_llm)
        assert read_json(captured_tools.directory / f"{value['citation']}.json")["result"] == value
        return value

    offset, found = 0, []
    for _ in range(20):
        page = request(offset=offset)
        found.extend(item["sequence" if indexed else "offset"] for item in page["events"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    else:
        pytest.fail("Session summary pagination did not finish")
    assert found == positions
    if indexed:
        fragments, offset = [], 0
        for _ in range(64):
            page = request(event=1, offset=offset)
            fragments.append(page["text"])
            if page["next_offset"] is None:
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        else:
            pytest.fail("Full-event pagination did not finish")
        body = "".join(fragments).encode("utf-8")
        assert len(body) == page["bytes"]
        assert hashlib.sha256(body).hexdigest() == page["sha256"]
        assert json.loads(body) == originals[1]


def test_restricted_driver_verifies_tools_and_denies_unregistered_actions(observer, project, ledger):
    envelope = arm(project, ledger)
    publish = Path(envelope.root) / "prompt.json"
    value = read_json(publish)
    value.update(target=str(observer.project), project_id=observer.project_id, history=[], history_omitted=0,
                 question="What is the observed state?")
    publish.write_text(json.dumps(value), encoding="utf-8")
    client = FakeClient({"missing_web": True, "answer": "Only observed status [E1]."})
    assert asyncio.run(inspector.drive(envelope, time.monotonic() + 90,
                                      client_factory=lambda *a, **k: client)) == 0
    options = client.session.options
    guard = options["hooks"]["on_pre_tool_use"]
    for name in ("powershell", "web_fetch", "labgoblin_review_decision", "task", "edit_file"):
        assert guard({"toolName": name}, None)["permissionDecision"] == "deny"
    assert options["system_message"]["mode"] == "replace"
    assert client.stopped


@pytest.fixture
def server(observer):
    value = InspectionServer(observer)
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    try:
        yield value
    finally:
        value.shutdown()
        value.close_questions()
        value.server_close()
        thread.join(timeout=5)


def request(server, method, path, body=None, *, token=True, **headers):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    if token:
        headers["Authorization"] = "Bearer " + server.token
    if body is not None:
        body = json.dumps(body)
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


def test_http_auth_origin_read_only_routes_and_no_model_on_browse(server, observer):
    before = identities(observer.project)
    status, headers, body = request(server, "GET", "/", token=False)
    assert status == 200 and b"Research inspector" in body
    assert "frame-ancestors 'none'" in dict(headers)["Content-Security-Policy"]
    assert request(server, "GET", "/api/status", token=False)[0] == 403
    assert request(server, "GET", "/api/status", Origin="https://evil.example")[0] == 400
    assert request(server, "GET", "/api/status", Host="evil.example")[0] == 400
    status, _, body = request(server, "GET", "/api/status")
    assert status == 200 and json.loads(body)["chat_enabled"] is False
    assert request(server, "POST", "/api/question", {"question": "Explain"})[0] == 400
    status, _, body = request(server, "POST", "/api/read", {"path": "report.md"})
    assert status == 200 and "positive control" in json.loads(body)["text"]
    assert request(server, "POST", "/api/read", {"path": ".labgoblin\\project.json"})[0] == 400
    assert request(server, "POST", "/api/stop", {})[0] == 404
    assert request(server, "GET", "/api/read?path=report.md")[0] == 404
    assert identities(observer.project) == before and observer.history()["messages"] == []


def test_http_question_runs_owned_sdk_and_returns_cited_image(server, observer, monkeypatch):
    observer.approve(100, model="model-free", timeout=60)
    argv = command(observer, image="comparison.png")
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: {"command": argv})
    server.chat = True
    assert request(server, "POST", "/api/question", {"question": "Show the control"})[0] == 200
    server.job.join(timeout=30)
    assert not server.job.is_alive() and not server.error, server.error
    status, _, body = request(server, "GET", "/api/history")
    item = json.loads(body)["messages"][0]
    assert status == 200 and item["status"] == "completed"
    status, _, body = request(server, "GET", f"/api/source?run={item['id']}&citation=E3")
    assert status == 200 and json.loads(body)["result"]["mime"] == "image/png"


def test_http_shutdown_does_not_wait_forever_for_unresolved_ownership(server, observer, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def unsettled(question):
        started.set()
        release.wait()
        return {"status": "cancelled", "reason": "Fixture released"}

    monkeypatch.setattr(observer, "ask", unsettled)
    server.chat = True
    server.submit("A stalled ownership check")
    join = server.job.join
    try:
        assert started.wait(timeout=5)
        assert server.job.daemon
        waits = []

        def bounded_join(timeout):
            waits.append(timeout)
            join(timeout=0.01)

        monkeypatch.setattr(server.job, "join", bounded_join)
        with pytest.raises(UncertainExecution, match="still settling"):
            server.close_questions()
        assert waits == [45] and observer.cancel_requested.is_set()
    finally:
        release.set()
        join(timeout=5)
    assert not server.job.is_alive()


def test_running_citations_are_not_read_before_publication_finishes(observer, monkeypatch):
    observer.approve(100, model="model-free")
    result = observer.ask("Retained evidence", command=command(observer))
    row = {**observer.ledger().consumer_run(result["id"]), "phase": "executing", "receipt": None}
    (observer.state / "runs" / result["id"] / "evidence" / "E1.meta.json").write_text('{"citation":')
    monkeypatch.setattr(inspector.ResourceLedger, "runs", lambda *a, **k: [row])
    monkeypatch.setattr(inspector.ResourceLedger, "consumer_run", lambda *a, **k: row)
    message = observer.history()["messages"][0]
    assert message["status"] == "running" and message["sources"] == []
    with pytest.raises(ValueError, match="still being written"):
        observer.source(result["id"], "E1")


def test_frozen_goal_reviews_and_changed_page_identity(observer, project, ledger):
    reviews = [{"decision": "continue", "reason": str(i)} for i in range(12)]
    envelope = captured(project, ledger, provider={"reviews": reviews, "checkpoint_reason": "stalled_reviews"})
    evidence = Evidence(observer.project, observer.project_id)
    result = evidence.execution({"run": envelope.id})
    assert result["goal"] == "Answer the original question"
    assert result["reviews"] == reviews[:10] and result["next_offset"] == 10
    assert evidence.execution({"run": envelope.id, "offset": 10})["reviews"] == reviews[10:]
    first = evidence.read({"scope": "workspace", "path": "report.md", "limit": 5})
    (observer.project / "report.md").write_text("Changed after the first page")
    with pytest.raises(ValueError, match="changed between pages"):
        evidence.read({"scope": "workspace", "path": "report.md", "offset": 5, "sha256": first["sha256"]})
    (Path(envelope.root) / "prompt.json").write_text('{"goal":"Changed goal"}')
    with pytest.raises(ValueError, match="prompt identity"):
        evidence.execution({"run": envelope.id})


def test_history_defaults_to_recent_questions_and_pages_older(observer, monkeypatch):
    observer.approve(100, model="model-free")
    result = observer.ask("A retained question", command=command(observer))
    row = observer.ledger().consumer_run(result["id"])
    rows = [{**row, "created": i} for i in range(45)]
    monkeypatch.setattr(inspector.ResourceLedger, "runs", lambda *a, **k: rows)
    latest = observer.history()
    assert latest["offset"] == 5 and [m["created"] for m in latest["messages"]] == list(range(5, 45))
    earlier = observer.history(offset=0, limit=5)
    assert [m["created"] for m in earlier["messages"]] == list(range(5))


def test_inspection_accounting_recovery_preserves_research_and_receipts(observer, project, ledger):
    observer.approve(100, model="model-free")
    result = observer.ask("Missing final bill", command=command(observer, mode="missing_usage"))
    row = ledger.consumer_run(result["id"])
    before = identities(observer.project)
    with pytest.raises(ValueError, match="confirmation"):
        observer.record_usage(result["id"], 20_000_000_000, "Fixture", confirmation="wrong", estimated=True)
    observer.record_usage(result["id"], 20_000_000_000, "Explicit synthetic estimate",
                          confirmation=observer.project_id, estimated=True)
    budget = observer.status()["inspection"]["budget"]
    assert budget["remaining"] == 80 and budget["remaining_is_estimated"]
    assert not budget["uncovered_unknown"]
    observer.record_usage(result["id"], 18_000_000_000, "Synthetic final metrics",
                          confirmation=observer.project_id, estimated=False)
    assert observer.status()["inspection"]["budget"]["remaining"] == 82
    assert ledger.consumer_run(result["id"])["receipt"] == row["receipt"]
    assert identities(observer.project) == before
    assert ledger.runs(project[2]["id"]) == []
    path = observer.state / "runs" / result["id"] / "receipt.json"
    expected = path.read_bytes()
    path.unlink()
    observer.reconcile()
    assert path.read_bytes() == expected
    from labgoblin.storage import atomic_json
    atomic_json(observer.root / "fixture-provider.json",
                {"model_free": True, "root": str(observer.root), "mode": "inspection"})
    following = observer.ask("After explicit reconciliation",
                             command=[sys.executable, str(Path(__file__).with_name("fixed_copilot.py"))])
    assert following["status"] == "completed"
    assert observer.status()["inspection"]["budget"]["remaining"] == 72


def test_inspection_deadline_retires_owned_tree(observer, ledger):
    observer.approve(100, model="model-free", timeout=60)
    result = observer.ask("Wait until the deadline", command=command(observer, mode="inspection_wait"))
    assert result["status"] == "timed_out", result
    assert 25 <= result["elapsed"] <= 65
    assert result["usage"]["totalNanoAiu"] == 10_000_000_000
    assert payload.tree_state(observer.state / "runs" / result["id"] / "process", result["id"]) == "dead"
    assert not ledger.rows()


def test_inspection_preclaim_death_is_fenced_but_unknown_ownership_is_not(observer, monkeypatch):
    observer.approve(100, model="model-free")
    argv = command(observer)
    launcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    handle = {"pid": launcher.pid, "created": psutil.Process(launcher.pid).create_time(), "token": observer.id}
    monkeypatch.setattr(inspector, "own_handle", lambda _: handle)

    def failed_launch(*args, **kwargs):
        launcher.terminate()
        launcher.wait(timeout=5)
        raise OSError("Injected supervisor start failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(inspector.subprocess, "Popen", failed_launch)
            with pytest.raises(OSError, match="Injected"):
                observer.ask("A failed launch", command=argv)
        ledger = observer.ledger()
        row = ledger.runs(observer.id, inspection=True)[0]
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "process_state", lambda _: "unknown")
            with pytest.raises(UncertainExecution, match="not proven dead"):
                observer.reconcile()
        assert ledger.grant(row["token"])["state"] == "granted"
        observer.reconcile()
        assert observer.history()["messages"][0]["status"] == "not_started"
        assert observer.status()["inspection"]["budget"]["remaining"] == 100
        assert not ledger.rows()
    finally:
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object retirement after supervisor death")
def test_inspection_supervisor_death_retains_unknown_billing(observer):
    observer.approve(100, model="model-free")
    argv = command(observer, mode="inspection_wait")
    errors = []

    def interrupt():
        try:
            wait_until(lambda: observer.active)
            directory = observer.state / "runs" / observer.active
            wait_until(lambda: (directory / "usage-progress.json").exists())
            row = observer.ledger().consumer_run(observer.active)
            handle = json.loads(row["supervisor"])
            process = psutil.Process(handle["pid"])
            assert process.create_time() == handle["created"]
            process.kill()
            process.wait(timeout=5)
        except (OSError, ValueError, RuntimeError, AssertionError, psutil.Error) as error:
            errors.append(str(error))
            observer.cancel()

    thread = threading.Thread(target=interrupt)
    thread.start()
    try:
        result = observer.ask("Interrupted inspection", command=argv)
    finally:
        thread.join(timeout=25)
    assert not thread.is_alive() and not errors
    assert result["status"] == "interrupted" and result["usage"] is None
    assert observer.status()["inspection"]["budget"]["uncovered_unknown"] == [result["id"]]
    assert not observer.ledger().rows()


def test_cli_inspector_view_only_does_not_reuse_old_spending_permission(observer, monkeypatch):
    from labgoblin import cli, inspection_server
    observer.approve(100, model="model-free")
    monkeypatch.setattr(inspector, "default_directory", lambda _: observer.root)
    calls = []
    monkeypatch.setattr(inspection_server, "serve", lambda *a, **k: calls.append(k))
    assert cli.main(["inspect", str(observer.project)]) == 0
    assert calls[-1]["chat"] is False
    assert cli.main(["inspect", str(observer.project), "--credits", "100"]) == 1
    assert len(calls) == 1
    assert cli.main(["inspect", str(observer.project), "--credits", "100", "--model", "model-free"]) == 0
    assert calls[-1]["chat"] is True
