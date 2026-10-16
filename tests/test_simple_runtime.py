from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest

from labgoblin import guidance, provider, runtime
from labgoblin.config import Config
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchReceipt, UncertainExecution
from labgoblin.scheduler import ResourceLedger, _wire_envelope, _wire_receipt
from labgoblin.storage import digest, hash_file, publish_json, read_json
from tests.conftest import arm


def fixture_provider(root, monkeypatch, mode="normal"):
    publish_json(root / "fixture-provider.json", {"model_free": True, "root": str(root), "mode": mode})
    command = [sys.executable, str(Path(__file__).with_name("fixed_copilot.py"))]
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: {"command": command, "sdk_controls": "model-free"})


def test_real_sdk_stdio_owned_execution_and_cumulative_resume(project, ledger, monkeypatch):
    root, config, record = project
    fixture_provider(root, monkeypatch)
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 10_000_000_000
    directory = root / ".labgoblin" / "runs" / result["id"]
    metrics = read_json(directory / "snapshot" / "artifacts" / "metrics.json")
    assert metrics["means"] == [4, 4, 5]
    assert Path(metrics["python_executable"]).resolve() == Path(sys.executable).resolve()
    assert ledger.grant(result["id"])["state"] == "released"
    assert runtime.status(root)["budget"]["remaining"] == 290
    prompt_path = directory / "prompt.json"
    first_prompt = prompt_path.read_bytes()
    assert read_json(prompt_path)["instructions"] == guidance.RESEARCH
    frozen = Path(read_json(directory / "envelope.json")["runtime"]["root"])
    assert (frozen / "labgoblin" / "guidance.py").read_bytes() == Path(guidance.__file__).read_bytes()
    with pytest.raises(ValueError, match="resume explicitly"):
        runtime.run(root)
    first = (directory / "receipt.json").read_bytes()
    project_guidance = "Retain the human-reviewed comparison criteria."
    (root / ".github" / "copilot-instructions.md").write_text(project_guidance, encoding="utf-8")
    second = runtime.run(root, resume=True)
    assert second["status"] == "completed", second
    assert second["id"] != result["id"]
    assert runtime.status(root)["budget"]["remaining"] == 280
    assert (directory / "receipt.json").read_bytes() == first
    assert prompt_path.read_bytes() == first_prompt
    second_prompt = read_json(root / ".labgoblin" / "runs" / second["id"] / "prompt.json")
    assert second_prompt["instructions"] == (
        guidance.RESEARCH + "\n\nProject-specific research guidance:\n" + project_guidance)
    assert not ledger.rows()


def test_real_sdk_missing_usage_retains_artifacts_but_blocks_restart(project, ledger, monkeypatch):
    root, config, record = project
    fixture_provider(root, monkeypatch, "missing_usage")
    result = runtime.run(root)
    assert result["status"] == "failed"
    assert "Deliberate final usage failure" in result["reason"]
    assert result["metadata"]["provider"]["status"] == "failed"
    assert result["usage"] is None
    assert (root / "report.md").exists()
    assert ledger.grant(result["id"])["state"] == "released"
    with pytest.raises(UncertainExecution, match="Final credit usage"):
        runtime.run(root, resume=True)
    assert len(ledger.runs(record["id"])) == 1


def test_real_sdk_provisional_acceptance_runs_owned_follow_up_before_final_review(project, ledger, monkeypatch):
    root, config, record = project
    (root / "labgoblin.toml").write_text(replace(config, coach=True).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "closing_challenge")
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 40_000_000_000
    directory = root / ".labgoblin" / "runs" / result["id"]
    snapshot = directory / "snapshot"
    assert read_json(snapshot / "artifacts" / "closing-control.json") == {"baseline": 4, "shifted": 5}
    sessions = result["usage"]["sessions"]
    assert [s["role"] for s in sessions] == ["researcher", "coach-0", "coach-1"]
    assert [s["prompts"] for s in sessions] == [2, 1, 1]
    assert [read_json(directory / f"review-{index}.json")["decision"] for index in range(2)] == ["accept", "accept"]
    follow_up = (snapshot / f"fixture-prompt-{sessions[0]['session_id']}-2.txt").read_text()
    assert guidance.CLOSING_CHALLENGE in follow_up
    assert "Fixed stopping assessment" not in follow_up
    calls = [json.loads(line) for line in
             (snapshot / f"fixture-tools-{sessions[2]['session_id']}.jsonl").read_text().splitlines()]
    assert [call["name"] for call in calls] == ["labgoblin_read_evidence", "labgoblin_review_decision"]
    assert calls[0]["arguments"]["path"] == "artifacts/closing-control.json"
    assert runtime.status(root)["budget"]["remaining"] == 260
    assert ledger.grant(result["id"])["state"] == "released"
    assert not ledger.rows()


@pytest.mark.parametrize("caller", ["researcher", "coach", "both"])
def test_real_sdk_consultation_tool_in_owned_run(project, ledger, monkeypatch, caller):
    root, config, record = project
    config = replace(config, consultant=True, coach=caller != "researcher", max_ai_credits=1000)
    (root / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "consult")
    fixture = {**read_json(root / "fixture-provider.json"), "caller": caller, "continue": caller != "researcher"}
    (root / "fixture-provider.json").write_text(json.dumps(fixture), encoding="utf-8")
    result = runtime.run(root)
    assert result["status"] == "completed", result
    consultations = 2 if caller == "both" else 1
    expected_prompts = 2 if caller == "researcher" else 4 + consultations
    assert result["usage"]["totalNanoAiu"] == expected_prompts * 10_000_000_000
    directory = root / ".labgoblin" / "runs" / result["id"]
    envelope = runtime.LaunchEnvelope.parse(read_json(directory / "envelope.json"))
    usage = runtime.usage_record(envelope, complete=True)
    assert len([s for s in usage["sessions"] if s["role"].startswith("consult-")]) == consultations
    assert len(list((directory / "snapshot" / "artifacts" / "consultations").glob("*.md"))) == consultations
    researcher = next(s for s in usage["sessions"] if s["role"] == "researcher")
    options = read_json(root / f"fixture-session-{researcher['session_id']}.json")
    assert "availableTools" not in options
    assert researcher["prompts"] == (1 if caller == "researcher" else 2)
    assert not ledger.rows()


@pytest.mark.parametrize("mode", ["consult_missing_usage", "consult_wait"])
def test_real_sdk_consultation_interruption_is_accounted_and_owned(project, ledger, monkeypatch, mode):
    root, config, record = project
    (root / "labgoblin.toml").write_text(replace(config, consultant=True).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, mode)
    errors = []
    def cancel():
        until = time.monotonic() + 20
        while time.monotonic() < until:
            if (root / "fixture-adviser-ready").exists():
                runtime.stop(root)
                return
            time.sleep(0.05)
        errors.append("Adviser did not reach its fixed prompt")
    thread = threading.Thread(target=cancel) if mode == "consult_wait" else None
    if thread:
        thread.start()
    try:
        result = runtime.run(root)
    finally:
        if thread:
            thread.join(timeout=25)
    assert not errors
    assert result["status"] == ("cancelled" if thread else "failed"), result
    assert not ledger.rows()
    if thread:
        assert not thread.is_alive()
        assert result["usage"]["totalNanoAiu"] == 20_000_000_000
    else:
        assert result["usage"] is None
        researcher_id = str(uuid.UUID(result["id"]))
        assert not (root / f"fixture-tools-{researcher_id}.jsonl").exists()
        with pytest.raises(UncertainExecution, match="Final credit usage"):
            runtime.run(root, resume=True)


def test_real_sdk_claim_review_returns_counterexample_to_same_researcher(project, ledger, monkeypatch):
    root, config, record = project
    (root / "labgoblin.toml").write_text(
        replace(config, coach=True, consultant=True, max_ai_credits=1000).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "claim_review")
    fixture = {**read_json(root / "fixture-provider.json"), "caller": "coach", "continue": True}
    (root / "fixture-provider.json").write_text(json.dumps(fixture), encoding="utf-8")
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 50_000_000_000
    sessions = result["usage"]["sessions"]
    assert [s["role"] for s in sessions] == ["researcher", "coach-0", "consult-0", "coach-1"]
    assert [s["prompts"] for s in sessions] == [2, 1, 1, 1]
    directory = root / ".labgoblin" / "runs" / result["id"]
    snapshot = directory / "snapshot"
    assert read_json(snapshot / "artifacts" / "claim-counterexample.json") == {"x": "1/2", "value": "-1/4"}
    assert "claim is false" in (snapshot / "report.md").read_text()
    request = read_json(directory / "sessions" / "consult-0" / "request.json")
    assert request["purpose"] == "claim_review" and guidance.CLAIM_REVIEW in request["operational_boundaries"]
    reviews = result["metadata"]["provider"]["reviews"]
    assert [review["decision"] for review in reviews] == ["continue", "accept"]
    assert reviews[1]["progress"] == "meaningful" and reviews[1]["stalled_reviews"] == 0
    brief = reviews[0]["consultations"][0]
    assert brief["purpose"] == "claim_review"
    assert hash_file(snapshot / brief["artifact"]) == brief["artifact_sha256"]
    assert reviews[1]["consultations"] == []
    follow_up = (snapshot / f"fixture-prompt-{sessions[0]['session_id']}-2.txt").read_text()
    assert "consult-0.md" in follow_up and "Test, repair or narrow" in follow_up
    assert guidance.CLOSING_CHALLENGE not in follow_up
    calls = [json.loads(line) for line in
             (snapshot / f"fixture-tools-{sessions[-1]['session_id']}.jsonl").read_text().splitlines()]
    assert calls[0]["arguments"]["path"] == "artifacts/claim-counterexample.json"
    assert runtime.status(root)["budget"]["remaining"] == 950
    assert ledger.grant(result["id"])["state"] == "released"
    assert not ledger.rows()


def test_actual_sdk_cancel_settles_usage_and_retires_owned_tree(project, ledger, monkeypatch):
    root, config, record = project
    fixture_provider(root, monkeypatch, "wait")
    errors = []
    def cancel():
        until = time.monotonic() + 20
        while time.monotonic() < until:
            path = root / "fixture-rpc.jsonl"
            if path.exists() and '"session.send"' in path.read_text():
                runtime.stop(root)
                return
            time.sleep(0.05)
        errors.append("Provider never received the fixed prompt")
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        result = runtime.run(root)
    finally:
        thread.join(timeout=25)
    assert not thread.is_alive() and not errors
    assert result["status"] == "cancelled", result
    assert result["usage"]["totalNanoAiu"] == 10_000_000_000
    assert not ledger.rows()


def test_frozen_runtime_and_prompt_identity_are_checked(project, ledger):
    envelope = arm(project, ledger)
    runtime.verify_runtime(envelope)
    prompt = Path(envelope.root) / "prompt.json"
    prompt.write_bytes(b"{}")
    with pytest.raises(ValueError, match="snapshot changed"):
        runtime.verify_runtime(envelope)


def test_provider_entry_refuses_an_unclaimed_execution(project, ledger):
    envelope = arm(project, ledger)
    with pytest.raises(ValueError, match="committed supervisor"):
        provider.main(Path(envelope.root) / "envelope.json")


def test_usage_must_match_owned_sessions_and_not_only_a_top_level_total(project, ledger):
    envelope = arm(project, ledger)
    root = Path(envelope.root)
    publish_json(root / "usage.json", {
        "run_id": envelope.id, "envelope_digest": envelope.digest, "complete": True,
        "totalNanoAiu": 0, "sessions": []})
    with pytest.raises(ValueError, match="owned provider sessions"):
        runtime.usage_record(envelope, complete=True)


def test_status_uses_loaded_budget_when_candidate_changes_or_breaks(project, ledger):
    root, config, record = project
    publish_json(root / ".labgoblin" / "ledger.json", {"path": str(ledger.path), "id": ledger.id})
    envelope = arm(project, ledger)
    (root / "labgoblin.toml").write_text(replace(config, max_ai_credits=500).toml(), encoding="utf-8")
    view = runtime.status(root)
    assert view["candidate_config"]["max_ai_credits"] == 500
    assert view["budget"]["configured"] == 300
    (root / "labgoblin.toml").write_text("not valid TOML", encoding="utf-8")
    view = runtime.status(root)
    assert view["candidate_error"] and view["budget"]["configured"] == 300


def test_ledger_exchange_remains_v3_while_inner_execution_is_new(project, ledger):
    envelope = arm(project, ledger)
    with ledger.read() as connection:
        row = connection.execute("SELECT * FROM consumer_runs WHERE token=?", (envelope.id,)).fetchone()
        value = json.loads(row["envelope"])
        assert value["protocol"] == 3
        assert value["metadata"]["simple_envelope"]["protocol"] == 4
        assert value["kind"] == "research"
        assert row["digest"] == digest(value)
        assert set(value) == {"protocol", "key", "kind", "argv", "cwd", "root", "state_path", "ledger_path",
                              "ledger_id", "timeout_seconds", "resources", "config_revision", "environment", "metadata"}
    supervisor = own_handle(envelope.id)
    assert ledger.claim_consumer(envelope, supervisor)
    receipt = LaunchReceipt(envelope.id, envelope.digest, "needs_operator", 1, True, 1,
                            "Need an input", usage={"totalNanoAiu": 10}, metadata={"supervisor": supervisor})
    ledger.finish_consumer(receipt)
    with ledger.read() as connection:
        wire = json.loads(connection.execute("SELECT receipt FROM consumer_runs WHERE token=?", (envelope.id,)).fetchone()[0])
        assert wire["protocol"] == 3 and wire["status"] == "failed"
        assert wire["envelope_digest"] == digest(_wire_envelope(envelope))
    assert json.loads(ledger.consumer_run(envelope.id)["receipt"])["status"] == "needs_operator"
