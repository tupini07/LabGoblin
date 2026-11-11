from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest

from labgoblin import guidance, provider, runtime
from labgoblin.cli import main
from labgoblin.config import Config
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchReceipt, UncertainExecution
from labgoblin.scheduler import MachineSample, ResourceLedger, _wire_envelope, _wire_receipt
from labgoblin.storage import digest, hash_file, publish_json, read_json
from tests.conftest import arm


def fixture_provider(root, monkeypatch, mode="normal"):
    publish_json(root / "fixture-provider.json", {"model_free": True, "root": str(root), "mode": mode})
    command = [sys.executable, str(Path(__file__).with_name("fixed_copilot.py"))]
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: {"command": command, "sdk_controls": "model-free"})


def test_local_admission_never_publishes_a_legacy_pending_reservation(project, ledger, monkeypatch):
    _, config, record = project
    token = uuid.uuid4().hex
    owner = {"kind": "research-run", "handle": own_handle(record["id"])}
    grant = ledger.request(token, record["id"], token, "research-run", config, owner, native=True)
    assert grant["state"] == "granted" and grant["granted"] is not None
    assert grant["cpus"] == grant["memory_mb"] == 0
    assert json.loads(grant["gpus"]) == json.loads(grant["native_cpus"]) == []
    ledger.release(token, owner_id=record["id"])
    assert ledger.request(token, record["id"], token, "research-run", config, owner, native=True)["state"] == "released"
    monkeypatch.setattr("labgoblin.processes.process_state", lambda _: "unknown")
    other = uuid.uuid4().hex
    with pytest.raises(ValueError, match="ownership is unknown"):
        ledger.request(other, record["id"], other, "research-run", config, owner, native=True)
    assert ledger.grant(other) is None


def test_local_request_cannot_reinterpret_a_legacy_pending_claim(project, ledger):
    _, config, record = project
    token = uuid.uuid4().hex
    owner = {"kind": "research-run", "handle": own_handle(record["id"])}
    ledger.request(token, record["id"], token, "research-run", config, owner, native=True)
    with ledger.write() as conn:
        conn.execute("UPDATE grants SET state='pending',digest='',request='{}',cpus=1,memory_mb=1024 WHERE token=?",
                     (token,))
    before = ledger.grant(token)
    with pytest.raises(ValueError, match="different request"):
        ledger.request(token, record["id"], token, "research-run", config, owner, native=True)
    assert ledger.grant(token) == before


def test_concurrent_first_admission_creates_one_ledger_without_capacity(tmp_path):
    path = tmp_path / "new" / "executions.db"
    barrier = threading.Barrier(4)
    def create(_):
        barrier.wait(timeout=10)
        return ResourceLedger.ensure(path).id
    with ThreadPoolExecutor(max_workers=4) as executor:
        identities = list(executor.map(create, range(4)))
    assert len(set(identities)) == 1
    ledger = ResourceLedger(path)
    assert ledger.capacity() is None and not ledger.rows()


def test_lost_bound_ledger_cannot_be_recreated_or_replace_accounting(project):
    root, config, record = project
    missing = root.parent / "missing.db"
    state = root / ".labgoblin"
    publish_json(state / "ledger.json", {"path": str(missing), "id": "historical-ledger"})
    with pytest.raises(FileNotFoundError):
        runtime.bound_ledger(state, config=replace(config, ledger=str(missing)), bind=True)
    assert not missing.exists()
    assert read_json(state / "ledger.json")["id"] == "historical-ledger"


def test_local_gpu_admission_ignores_legacy_capacity_and_busy_devices(project, ledger):
    root, config, record = project
    sample = MachineSample((0, 1), 32768, 1, frozenset({"GPU-test"}), frozenset({"GPU-test"}))
    ledger.sampler = lambda *_: sample
    config = replace(config, cpus=2, memory_mb=16384, gpus=("GPU-test",))
    legacy = arm((root, config, record), ledger)
    with ledger.write() as conn:
        conn.execute("INSERT INTO capacity VALUES(1,7,1,1024,'[]',512,'[0]',1)")
        conn.execute("UPDATE grants SET cpus=2,memory_mb=32768,gpus='[\"GPU-test\"]',native_cpus='[0,1]' "
                     "WHERE token=?", (legacy.id,))
    before_grant, before_consumer = ledger.grant(legacy.id), ledger.consumer_run(legacy.id)
    capacity = ledger.capacity()
    other = root.parent / "other"
    other_record = runtime.initialize(other, config, goal="Use only synthetic data.", research_venv=False, git=False)
    envelope = arm((other, config, other_record), ledger)
    grant = ledger.grant(envelope.id)
    assert grant["state"] == "granted" and grant["cpus"] == grant["memory_mb"] == 0
    assert json.loads(grant["gpus"]) == json.loads(grant["native_cpus"]) == []
    assert envelope.cpu_ids == legacy.cpu_ids == (0, 1)
    assert envelope.resources.gpus == ("GPU-test",)
    assert ledger.grant(legacy.id) == before_grant
    assert ledger.consumer_run(legacy.id) == before_consumer
    assert ledger.capacity() == capacity
    with pytest.raises(ValueError, match="visible"):
        ledger.request(uuid.uuid4().hex, record["id"], uuid.uuid4().hex, "research-run",
                       replace(config, gpus=("GPU-missing",)),
                       {"kind": "research-run", "handle": own_handle(record["id"])}, native=True)
    with pytest.raises(ValueError, match="physical capacity"):
        ledger.request(uuid.uuid4().hex, record["id"], uuid.uuid4().hex, "research-run",
                       replace(config, cpus=3),
                       {"kind": "research-run", "handle": own_handle(record["id"])}, native=True)


def test_two_uncapped_owned_runs_overlap_without_machine_provisioning(tmp_path, monkeypatch):
    ledger_path = tmp_path / "machine.db"
    roots = [tmp_path / name for name in ("first", "second")]
    for root in roots:
        runtime.initialize(root, Config(model="model-free", coach=False, consultant=False,
                                        ledger=str(ledger_path), timeout_seconds=180, max_ai_credits=300),
                           goal="Determine whether adding one increases the mean by one.",
                           research_venv=False, git=False)
        fixture_provider(root, monkeypatch, "wait_for_release")
    assert not ledger_path.exists()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(runtime.run, root) for root in roots]
        try:
            until = time.monotonic() + 40
            while not all((root / "fixture-rpc.jsonl").exists() and
                          '"session.send"' in (root / "fixture-rpc.jsonl").read_text() for root in roots):
                for future in futures:
                    if future.done():
                        pytest.fail(f"Run ended before overlap: {future.result()}")
                assert time.monotonic() < until, "Both native researchers must be active"
                time.sleep(.05)
            ledger = ResourceLedger(ledger_path)
            grants = ledger.rows()
            assert len(grants) == 2 and all(row["state"] == "granted" for row in grants)
            envelopes = [json.loads(ledger.consumer_run(row["token"])["envelope"]) for row in grants]
            assert envelopes[0]["cpu_ids"] == envelopes[1]["cpu_ids"]
            for envelope in envelopes:
                from labgoblin.protocol import LaunchEnvelope
                assert runtime.payload.resource_spec(LaunchEnvelope.parse(envelope)) == {
                    "cpus": None, "memory_mb": None, "cpu_ids": None, "gpus": []}
                assert runtime.payload.tree_state(Path(envelope["root"]) / "process", envelope["id"]) == "alive"
        finally:
            for root in roots:
                (root / "finish-fixture").touch()
        results = [future.result(timeout=40) for future in futures]
    assert all(result["status"] == "completed" for result in results), results
    assert not ledger.rows() and ledger.capacity() is None
    assert all(runtime.status(root)["budget"]["remaining"] == 290 for root in roots)


@pytest.mark.parametrize("archive", [False, True])
@pytest.mark.parametrize("completed", [False, True])
def test_archive_failure_never_overwrites_research_outcome_or_unknown_usage(
        project, ledger, monkeypatch, archive, completed):
    from labgoblin.inspection_evidence import Evidence
    root, config, record = project
    envelope = arm((root, replace(config, archive_workspace=archive), record), ledger)
    directory = Path(envelope.root)
    publish_json(root / ".labgoblin" / "ledger.json", {"path": str(ledger.path), "id": ledger.id})
    (root / "report.md").write_bytes(b"Current working evidence\n")
    expected_status = "completed" if completed else "timed_out"
    expected_reason = "Research loop completed" if completed else "Researcher deadline reached"
    expected_usage = {"totalNanoAiu": 10_000_000_000} if completed else None
    publish_json(directory / "result.json", {
        "run_id": envelope.id, "envelope_digest": envelope.digest,
        "status": expected_status, "reason": expected_reason})
    monkeypatch.setattr(runtime, "bound_diagnostics", lambda: None)
    monkeypatch.setattr(runtime.payload, "run_process", lambda _: {
        "status": expected_status, "reason": "Native outcome", "returncode": 0 if completed else 124,
        "elapsed": 60, "executed": True})
    monkeypatch.setattr(runtime, "usage_record", lambda *args, **kwargs: expected_usage)
    def fail_snapshot(*args, **kwargs):
        assert archive, "Archival disabled: no workspace traversal should occur"
        raise ValueError("Research snapshot exceeds 100000 filesystem entries")
    monkeypatch.setattr(runtime, "snapshot", fail_snapshot)
    assert runtime.worker_main(directory / "envelope.json") == (0 if completed else 1)
    receipt = read_json(directory / "receipt.json")
    assert receipt["status"] == expected_status and receipt["usage"] == expected_usage
    assert expected_reason in receipt["reason"]
    assert ("spending is unknown" in receipt["reason"]) is not completed
    assert receipt["metadata"]["primary_outcome"] == {"status": expected_status, "reason": expected_reason}
    assert "100000" not in receipt["reason"]
    assert receipt["metadata"]["archive"]["status"] == ("failed" if archive else "disabled")
    if archive:
        assert "100000" in receipt["metadata"]["archive"]["message"]
    assert json.loads(ledger.consumer_run(envelope.id)["receipt"]) == receipt
    assert not ledger.rows()
    budget = runtime.status(root)["budget"]
    assert budget["uncovered_unknown"] == ([] if completed else [envelope.id])
    if completed:
        assert budget["remaining"] == 290
    before = (directory / "receipt.json").read_bytes()
    evidence = Evidence(root, record["id"])
    with pytest.raises(ValueError, match="workspace snapshot"):
        evidence.read({"scope": envelope.id, "path": "report.md"})
    with pytest.raises(ValueError, match="workspace snapshot"):
        evidence.read({"scope": "latest", "path": "report.md"})
    assert evidence.read({"scope": "workspace", "path": "report.md"})["live"]
    terminal = Evidence(root, record["id"], terminal={"research_run": envelope.id,
                                                    "receipt_sha256": digest(receipt)})
    with pytest.raises(ValueError, match="Only retained session and evaluation evidence"):
        terminal.read({"path": "report.md"})
    with pytest.raises(ValueError, match="handoff"):
        terminal.read({"scope": "workspace", "path": "report.md"})
    assert (directory / "receipt.json").read_bytes() == before


def test_resume_without_archival_keeps_working_files_and_history(project, ledger, monkeypatch):
    root, config, record = project
    (root / "labgoblin.toml").write_text(replace(config, archive_workspace=False).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch)
    first = runtime.run(root)
    assert first["status"] == "completed", first
    directory = root / ".labgoblin" / "runs" / first["id"]
    before = (directory / "receipt.json").read_bytes()
    (root / "my-retained-result.txt").write_bytes(b"Evidence outside the standard artifact folder\n")
    second = runtime.run(root, resume=True)
    assert second["status"] == "completed", second
    assert not (directory / "snapshot").exists()
    assert (directory / "receipt.json").read_bytes() == before
    assert (root / "my-retained-result.txt").read_bytes() == b"Evidence outside the standard artifact folder\n"
    assert runtime.status(root)["budget"]["remaining"] == 280
    assert not ledger.rows()


def test_cli_scaffold_can_be_replaced_with_a_freeform_brief(tmp_path, ledger, monkeypatch, capsys):
    root = tmp_path / "research"
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Unexpected provider call"))
    assert main(["init", str(root), "--ledger", str(ledger.path), "--no-research-venv",
                 "--cpus", "1", "--memory-mb", "1024", "--timeout", "180",
                 "--model", "model-free", "--no-coach", "--no-consultant"]) == 0
    capsys.readouterr()
    goal = b"Determine whether adding one increases the mean by one.\r\n"
    (root / "research_goal.md").write_bytes(goal)
    fixture_provider(root, monkeypatch)
    assert main(["run", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    directory = root / ".labgoblin" / "runs" / result["id"]
    assert read_json(directory / "prompt.json")["goal"] == goal.decode("utf-8")
    assert read_json(root / "artifacts" / "metrics.json")["means"] == [4, 4, 5]
    assert result["metadata"]["archive"]["status"] == "disabled"
    assert not (directory / "snapshot").exists()
    assert (root / "research_goal.md").read_bytes() == goal
    assert ledger.grant(result["id"])["state"] == "released"
    assert not ledger.rows()


def test_default_init_runs_sol_research_and_astra_closure_with_one_pool(tmp_path, ledger, monkeypatch, capsys):
    root = tmp_path / "default-research"
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Init must not call Copilot"))
    assert main(["init", str(root), "--ledger", str(ledger.path), "--no-git", "--no-research-venv"]) == 0
    capsys.readouterr()
    (root / "research_goal.md").write_text("Determine whether x*x-x is nonnegative for all real x.")
    fixture_provider(root, monkeypatch, "closure_critic")
    (root / "fixture-provider.json").write_text(json.dumps({
        "model_free": True, "root": str(root), "mode": "closure_critic",
        "models": ["gpt-6.1-sol", "gpt-6-astra"],
    }), encoding="utf-8")
    assert main(["run", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed", result
    sessions = result["usage"]["sessions"]
    assert [item["model"] for item in sessions] == ["gpt-6.1-sol"] + ["gpt-6-astra"] * 4
    assert [item["role"] for item in sessions] == [
        "researcher", "coach-0", "coach-1", "consult-0", "adjudicator-1"]
    assert [item["prompts"] for item in sessions] == [3, 1, 1, 1, 1]
    directory = root / ".labgoblin" / "runs" / result["id"]
    frozen = Config.parse(read_json(directory / "envelope.json")["config"], frozen=True)
    assert frozen.coach and frozen.consultant and frozen.closure_critic
    assert frozen.reasoning_effort == "high" and frozen.goal_review_interval_seconds == 10800
    assert frozen.consultation_timeout_seconds == 3600 and frozen.goal_review_timeout_seconds == 1800
    assert read_json(directory / "prompt.json")["research_checkins_version"] == 1
    assert read_json(directory / "prompt.json")["closure_response_version"] == 1
    assert read_json(root / "artifacts" / "closure-counterexample.json") == {"x": "1/2", "value": "-1/4"}
    assert runtime.status(root)["budget"]["remaining"] == 930
    assert result["metadata"]["archive"]["status"] == "disabled"
    assert not ledger.rows()


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
    assert "research_environment" not in read_json(prompt_path)
    assert not (root / ".venv").exists()
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


def test_recorded_environment_authority_is_frozen_without_overriding_restrictions(
        tmp_path, ledger, monkeypatch):
    from labgoblin import research_environment
    root = tmp_path / "authorized"
    goal = "Use only local synthetic inputs. Do not download pretrained models."
    record = runtime.initialize(root, Config(
        model="model-free", coach=False, consultant=False,
        cpus=1, memory_mb=1024, timeout_seconds=180, ledger=str(ledger.path)), goal=goal)
    custom = "Do not install third-party packages; use the standard library."
    (root / ".github" / "copilot-instructions.md").write_text(custom, encoding="utf-8")
    original_authorization = record["research_environment"]["authorization"]
    monkeypatch.setattr(research_environment, "AUTHORIZATION", "Changed future-project defaults.")
    fixture_provider(root, monkeypatch)
    result = runtime.run(root)
    assert result["status"] == "completed", result
    directory = root / ".labgoblin" / "runs" / result["id"]
    prompt = read_json(directory / "prompt.json")
    assert prompt["goal"] == goal
    assert custom in prompt["instructions"]
    assert original_authorization in prompt["instructions"]
    assert "Explicit operator restrictions" in prompt["instructions"]
    assert "Changed future-project defaults" not in prompt["instructions"]
    python = record["research_environment"]["python"]
    assert python in prompt["goal_prompt"] and python in prompt["instructions"]
    assert read_json(root / "artifacts" / "metrics.json")["python_executable"] == python
    before = (directory / "prompt.json").read_bytes()
    (root / "labgoblin.toml").write_text(Config(model="operator-next-execution").toml(), encoding="utf-8")
    record["research_environment"]["authorization"] += "\nNo future package changes."
    (root / ".labgoblin" / "project.json").write_text(json.dumps(record), encoding="utf-8")
    assert (directory / "prompt.json").read_bytes() == before
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


def test_real_sdk_shared_allowance_transfer_is_ordered_and_has_no_300_credit_quota(project, ledger, monkeypatch):
    root, config, _ = project
    (root / "labgoblin.toml").write_text(replace(config, consultant=True, max_ai_credits=1000).toml(),
                                      encoding="utf-8")
    fixture_provider(root, monkeypatch, "consult")
    fixture = {**read_json(root / "fixture-provider.json"), "caller": "researcher",
               "role_costs": {"adviser": 500_000_000_000}}
    (root / "fixture-provider.json").write_text(json.dumps(fixture), encoding="utf-8")
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 510_000_000_000
    researcher, adviser = result["usage"]["sessions"]
    assert researcher["allowance_nano"] == 500_000_000_000
    assert adviser["allowance_nano"] == 960_000_000_000
    calls = [json.loads(line) for line in (root / "fixture-rpc.jsonl").read_text().splitlines()]
    transfers = [(index, call["session_limits"]["maxAiCredits"]) for index, call in enumerate(calls)
                 if call["method"] == "session.options.update" and call["session_id"] == researcher["session_id"]
                 and call["session_limits"] is not None]
    assert [limit for _, limit in transfers] == [40, 500]
    adviser_send = next(index for index, call in enumerate(calls)
                        if call["method"] == "session.send" and call["session_id"] == adviser["session_id"])
    caller_release = next(index for index, call in enumerate(calls)
                          if call["method"] == "session.tools.handlePendingToolCall"
                          and call["session_id"] == researcher["session_id"])
    assert transfers[0][0] < adviser_send < transfers[1][0] < caller_release
    assert runtime.status(root)["budget"]["remaining"] == 490
    assert not ledger.rows()


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
