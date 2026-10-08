from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
from threading import Event
import time

import pytest

from labgoblin.config import initial_config, parse_config
from labgoblin.protocol import (
    BudgetExhausted, LaunchEnvelope, LaunchKey, LaunchReceipt, PreExecutionError, Resources,
    UncertainExecution, identifier,
)
from labgoblin.scheduler import MachineSample, ResourceLedger
from labgoblin.state import State
from labgoblin import worker


@pytest.fixture
def admission(tmp_path, request):
    raw = initial_config("fixture")
    raw["campaign"].update(getattr(request, "param", {}))
    cfg = parse_config(raw, tmp_path / "labgoblin.toml")
    gpus = cfg.campaign.resources.gpus
    ledger = ResourceLedger.create(
        tmp_path / "machine.db",
        sampler=lambda requested: MachineSample((0, 1), 8192, 8192, frozenset(gpus)),
        eligibility=lambda row: (True, ""))
    ledger.configure(2, 4096, gpus, 0)
    state = State.create(cfg, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    work_id = identifier()
    root = state.root / "attempts" / work_id
    root.mkdir(parents=True)
    spec = {"id": work_id, "key": "work", "request": {"key": "work"}, "experiment_id": "work",
            "cpus": 1, "memory_mb": 128, "gpus": list(gpus), "seconds": 10, "root": str(root)}
    state.enqueue(spec)
    resources = Resources(1, 128, gpus)
    allocation = state.allocation(work_id, "attempt", {"resources": asdict(resources)}, 0)
    ledger.request(allocation["token"], state.id, work_id, "attempt", resources,
                   {"kind": "campaign", "state_dir": str(state.root), "generation": 1, "revision": 0},
                   native=True)
    assert ledger.reserve(allocation["token"])["state"] == "granted"
    assert state.granted(allocation["token"])
    envelope = LaunchEnvelope(
        LaunchKey(state.id, 1, work_id, allocation["token"], identifier()), "attempt",
        ("program",), str(tmp_path), str(root), str(state.path), str(ledger.path), ledger.id,
        10, resources, cfg.revision)
    return state, ledger, envelope


def prepared(admission):
    state, ledger, envelope = admission
    envelope = worker.prepare_runtime(state, envelope)
    worker.launch_directory(envelope).mkdir(parents=True)
    state.arm(envelope)
    return state, ledger, envelope


def completed(envelope):
    return LaunchReceipt(envelope.key, envelope.digest, "completed", True, 1, returncode=0)


@pytest.mark.parametrize("admission,used,allowed", [
    ({"gpus": ["GPU-test"], "max_gpu_hours": "unlimited"}, 1000000, True),
    ({"gpus": ["GPU-test"], "max_gpu_hours": 0}, 0, False),
    ({"gpus": ["GPU-test"], "max_gpu_hours": 10 / 3600}, 0, True),
    ({"gpus": ["GPU-test"], "max_gpu_hours": 10 / 3600}, 0.001, False),
], indirect=["admission"])
def test_gpu_hour_admission_preserves_finite_bounds_and_unlimited_accounting(admission, used, allowed):
    state, ledger, envelope = admission
    with state.db.write() as conn:
        conn.execute("UPDATE campaign SET gpu_hours=?", (used,))
    if not allowed:
        with pytest.raises(BudgetExhausted, match="GPU-hour"):
            prepared(admission)
        assert state.attempt(envelope.key.work_id)["status"] == "queued"
        return
    state, ledger, envelope = prepared(admission)
    budget = state.budget()["gpu_hours"]
    assert budget["used"] == used and budget["reserved"] == 10 / 3600
    assert envelope.timeout_seconds == 10 and envelope.resources.gpus == ("GPU-test",)
    assert worker.execute(envelope, completed) == 0
    budget = state.budget()["gpu_hours"]
    assert budget["used"] == pytest.approx(used + 1 / 3600)
    assert budget["reserved"] == 0
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"


def test_proven_os_creation_failure_is_terminal_and_released(admission, monkeypatch):
    state, ledger, envelope = admission

    def failed(*args, **kwargs):
        raise OSError("injected CreateProcess failure before child creation")

    monkeypatch.setattr(worker.subprocess, "Popen", failed)
    with pytest.raises(PreExecutionError, match="CreateProcess"):
        worker.start(state, envelope)
    assert state.attempt(envelope.key.work_id)["status"] == "not_started"
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert not state.active_launches()
    assert not state.campaign()["blockers"]


def test_armed_without_os_call_remains_uncertain_even_if_fixture_knows_no_child(admission):
    state, ledger, envelope = prepared(admission)
    for _ in range(3):
        result = worker.reconcile(state)
        assert len(result["unresolved"]) == 1
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    assert len(state.active_launches()) == 1
    assert state.campaign()["state"] == "recovery_required"


def test_failure_after_creation_does_not_claim_no_child(admission, monkeypatch):
    import psutil
    state, ledger, envelope = admission
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: type("Child", (), {"pid": 123})())

    def missing(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", missing)
    with pytest.raises(UncertainExecution, match="created"):
        worker.start(state, envelope)
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    assert state.active_launches()[0]["phase"] == "armed"
    assert state.campaign()["blockers"]


def dead_launcher(state, envelope):
    import psutil
    launcher = {"pid": os.getpid(), "created": psutil.Process().create_time() - 60,
                "token": envelope.key.nonce}
    state.attach_launcher(envelope, launcher)
    assert worker.process_state(launcher) == "dead"
    return launcher


def test_exited_unclaimed_launcher_is_retired_without_execution_or_replay(admission):
    state, ledger, envelope = prepared(admission)
    launcher = dead_launcher(state, envelope)
    state.launch_problem(envelope.key.nonce, "Worker could not claim its launch")
    assert worker.reconcile(state) == {"recovered": [envelope.key.work_id], "unresolved": []}
    receipt = json.loads(state.launch(envelope.key.nonce)["receipt"])
    assert receipt["status"] == "not_started" and receipt["executed"] is False
    assert receipt["metadata"]["launcher"] == launcher
    assert receipt["metadata"]["runtime_id"] == envelope.metadata["runtime"]["id"]
    assert json.loads((worker.launch_directory(envelope) / "receipt.json").read_bytes()) == receipt
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert not state.campaign()["blockers"]
    assert worker.execute(envelope, lambda _: pytest.fail("Retired launch was replayed")) == 0
    assert worker.reconcile(state) == {"recovered": [], "unresolved": []}


def test_unclaimed_provider_recovery_preserves_settled_spend_and_releases_only_unused_allowance(tmp_path):
    from labgoblin import agent
    from labgoblin.campaign import Campaign
    from tests.test_providers import HELP, fixture

    config, state, ledger, turn, ids, grant = fixture(tmp_path, sandbox=True, credit_limit=200)
    home = config.root / config.agent.copilot_home
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "sandbox": {"enabled": True, "allowBypass": False, "userPolicy": {
            "filesystem": {"deniedPaths": [str(home / "preflight" / "denied")]}}}
    }), encoding="utf-8")
    adapter = agent.inspect_provider(config.agent, probe=lambda _: HELP + "\n--sandbox")
    canary = worker.prepare_runtime(state, agent.prepare(state, config, turn, ids[0], grant, adapter=adapter))
    worker.launch_directory(canary).mkdir(parents=True, exist_ok=True)
    state.arm(canary)
    assert worker.execute(canary, lambda value: LaunchReceipt(
        value.key, value.digest, "completed", True, 1, returncode=0,
        usage={"totalNanoAiu": 14_244_520_000})) == 0
    canary_receipt = state.launch(canary.key.nonce)["receipt"]
    envelope = worker.prepare_runtime(state, agent.prepare(state, config, turn, ids[1], grant, adapter=adapter))
    worker.launch_directory(envelope).mkdir(parents=True, exist_ok=True)
    state.arm(envelope)
    dead_launcher(state, envelope)
    assert state.credit_budget()["reserved"] == 110
    result = Campaign(config, state=state, ledger=ledger).reconcile()
    assert result["recovered"] == [ids[1]] and not result["unresolved"]
    assert result["errors"][0]["category"] == "provider_result"
    budget = state.credit_budget()
    assert budget["used_nano"] == 14_244_520_000
    assert budget["reserved"] == budget["unknown"] == budget["estimated"] == 0
    assert state.campaign()["invocations"] == 1
    assert state.launch(canary.key.nonce)["receipt"] == canary_receipt
    assert json.loads(state.launch(envelope.key.nonce)["receipt"])["usage"] is None
    assert ledger.grant(grant["token"])["state"] == "released"
    assert Campaign(config, state=state, ledger=ledger).reconcile()["errors"] == []


@pytest.mark.parametrize("condition", ["alive", "unknown", "claimed", "payload", "changed_runtime"])
def test_unclaimed_recovery_requires_complete_ownership_proof(admission, monkeypatch, condition):
    state, ledger, envelope = prepared(admission)
    launcher = dead_launcher(state, envelope)
    if condition in ("alive", "unknown"):
        monkeypatch.setattr(worker, "process_state", lambda _: condition)
    elif condition == "claimed":
        assert state.claim_launch(envelope, launcher)
    elif condition == "payload":
        with state.db.write() as conn:
            conn.execute("UPDATE launches SET payload=? WHERE nonce=?",
                         (json.dumps(launcher), envelope.key.nonce))
    else:
        helper = Path(envelope.metadata["runtime"]["root"]) / "labgoblin" / "worker.py"
        helper.write_bytes(helper.read_bytes() + b"\n# changed\n")
    result = worker.reconcile(state)
    assert not result["recovered"]
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    assert not (worker.launch_directory(envelope) / "receipt.json").exists()
    assert state.launch(envelope.key.nonce)["receipt"] is None


def test_unclaimed_recovery_cannot_win_after_a_concurrent_execution_claim(admission, monkeypatch):
    state, ledger, envelope = prepared(admission)
    dead_launcher(state, envelope)
    finish = state.finish_launch

    def claim_first(nonce, receipt, **kwargs):
        assert state.claim_launch(envelope, worker.own_handle(envelope.key.nonce))
        return finish(nonce, receipt, **kwargs)

    monkeypatch.setattr(state, "finish_launch", claim_first)
    result = worker.reconcile(state)
    assert "no longer proves" in result["unresolved"][0]["error"]
    assert state.launch(envelope.key.nonce)["phase"] == "executing"
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    assert not (worker.launch_directory(envelope) / "receipt.json").exists()


def test_unclaimed_recovery_is_durable_if_receipt_file_publication_fails(admission, monkeypatch):
    state, ledger, envelope = prepared(admission)
    dead_launcher(state, envelope)

    def fail(*args, **kwargs):
        raise OSError("injected recovery receipt publication failure")

    monkeypatch.setattr(worker, "publish_bytes", fail)
    result = worker.reconcile(state)
    assert "publication failure" in result["unresolved"][0]["error"]
    receipt = json.loads(state.launch(envelope.key.nonce)["receipt"])
    assert receipt["executed"] is False and receipt["status"] == "not_started"
    assert state.launch(envelope.key.nonce)["phase"] == "quiescent"
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert worker.execute(envelope, lambda _: pytest.fail("Retired launch was replayed")) == 0
    assert worker.reconcile(state) == {"recovered": [], "unresolved": []}


def test_status_inside_healthy_launch_does_not_latch_or_discard_late_identity(admission, monkeypatch):
    state, ledger, envelope = admission
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: type("Child", (), {"pid": os.getpid()})())
    envelope = worker.start(state, envelope)
    assert worker.reconcile(state) == {"recovered": [], "unresolved": []}
    assert not state.campaign()["blockers"]
    assert worker.execute(envelope, completed) == 0
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert state.attempt(envelope.key.work_id)["status"] == "completed"


def test_exact_authorization_claim_executes_only_once(admission):
    state, ledger, envelope = prepared(admission)
    calls = []

    def run(value):
        calls.append(value.key.nonce)
        return completed(value)

    assert worker.execute(envelope, run) == 0
    assert worker.execute(envelope, run) == 0
    assert calls == [envelope.key.nonce]
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"


def test_worker_waits_for_slow_writer_before_claiming_launch(admission, monkeypatch):
    state, ledger, envelope = prepared(admission)
    entering, finished = Event(), Event()
    calls = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(
            lambda statement: entering.set() if statement == "BEGIN IMMEDIATE" else None)
        return connection

    def execute():
        try:
            return worker.execute(envelope, complete)
        finally:
            finished.set()

    def complete(value):
        calls.append(value.key.nonce)
        return completed(value)

    with closing(connect(state.path, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE campaign SET reason=reason")
        monkeypatch.setattr(sqlite3, "connect", traced_connect)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute)
            try:
                assert entering.wait(10), "Worker did not reach its launch claim"
                assert not finished.wait(12), f"Worker abandoned its owned launch: {future.exception()}"
            finally:
                blocker.rollback()
            assert future.result(timeout=15) == 0
    assert calls == [envelope.key.nonce]
    assert state.attempt(envelope.key.work_id)["status"] == "completed"
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert not state.active_launches()
    assert not state.campaign()["blockers"]


def test_worker_startup_lock_failure_retains_traceback_and_reservations(admission, monkeypatch, capsys):
    from labgoblin import db
    from labgoblin.protocol import canonical

    state, ledger, envelope = prepared(admission)
    manifest = worker.launch_directory(envelope) / "envelope.json"
    manifest.write_bytes(canonical(asdict(envelope)))
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.05)
    with closing(sqlite3.connect(state.path, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        assert worker.main(str(manifest)) == 1
        blocker.rollback()
    error = capsys.readouterr().err
    assert "database is locked" in error
    assert str(state.path) in error
    assert "stage=BEGIN IMMEDIATE" in error
    assert "SQLITE_BUSY" in error
    assert "Traceback" in error and "claim_launch" in error
    assert state.launch(envelope.key.nonce)["phase"] == "armed"
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    assert not (manifest.parent / "receipt.json").exists()


def test_unarmed_helper_cannot_execute(admission):
    state, ledger, envelope = admission
    envelope = worker.prepare_runtime(state, envelope)
    calls = []
    with pytest.raises(ValueError, match="authorization"):
        worker.execute(envelope, lambda value: calls.append(value))
    assert calls == []
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"


def test_changed_frozen_helper_fails_before_execution(admission):
    state, ledger, envelope = prepared(admission)
    helper = Path(envelope.metadata["runtime"]["root"]) / "labgoblin" / "payload.py"
    helper.write_bytes(helper.read_bytes() + b"\n# changed\n")
    calls = []
    assert worker.execute(envelope, lambda value: calls.append(value)) == 1
    assert calls == []
    assert state.attempt(envelope.key.work_id)["status"] == "not_started"
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"


def test_execution_exception_does_not_fabricate_quiescence(admission):
    state, ledger, envelope = prepared(admission)

    def failed(value):
        raise RuntimeError("Execution ownership cannot be checked")

    assert worker.execute(envelope, failed) == 1
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    assert state.active_launches()[0]["phase"] == "executing"
    assert not (worker.launch_directory(envelope) / "receipt.json").exists()


def test_receipt_published_before_database_crash_is_recoverable_without_config(admission, monkeypatch):
    state, ledger, envelope = prepared(admission)
    original = State.finish_launch
    calls = []

    def fail_once(self, *args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("injected DB commit interruption")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(State, "finish_launch", fail_once)
    assert worker.execute(envelope, completed) == 1
    assert (worker.launch_directory(envelope) / "receipt.json").exists()
    (state.root.parent / "labgoblin.toml").write_text("invalid [config", encoding="utf-8")
    result = worker.reconcile(State.open(state.root))
    assert result["recovered"] == [envelope.key.work_id]
    assert not result["unresolved"]
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert not state.campaign()["blockers"]


def test_failed_ledger_release_does_not_turn_terminal_execution_into_unknown(admission, monkeypatch):
    state, ledger, envelope = prepared(admission)
    original = ResourceLedger.release

    def failed(*args, **kwargs):
        raise OSError("injected unavailable ledger release")

    monkeypatch.setattr(ResourceLedger, "release", failed)
    with pytest.raises(OSError, match="unavailable ledger"):
        worker.execute(envelope, completed)
    assert not state.active_launches()
    assert not state.campaign()["blockers"]
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    monkeypatch.setattr(ResourceLedger, "release", original)
    worker.reconcile(state)
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"


def test_provisional_grant_repairs_and_control_revocation_converge(admission):
    state, ledger, envelope = admission
    with state.db.write() as conn:
        conn.execute("UPDATE allocations SET state='requested'")
    worker.recover_allocations(state)
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM allocations").fetchone()[0] == "granted"
    state.control("pause", "pause", 0)
    worker.recover_allocations(state)
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert state.campaign()["operator_mode"] == "paused"


def test_late_completion_cannot_clear_operator_pause(admission):
    state, ledger, envelope = prepared(admission)
    state.control("pause", "pause", 0)
    assert worker.execute(envelope, completed) == 0
    assert state.campaign()["operator_mode"] == "paused"
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"


def test_recycled_pid_is_not_a_live_owner():
    from labgoblin.processes import own_handle, process_state
    handle = own_handle("owned")
    assert process_state(handle) == "alive"
    handle["created"] += 1
    assert process_state(handle) == "dead"


def test_late_launcher_attachment_failure_cannot_block_completed_worker(admission, monkeypatch):
    import psutil
    state, ledger, envelope = admission
    process_class = psutil.Process

    def fast_child(*args, **kwargs):
        value = worker.LaunchEnvelope.parse(json.loads(state.active_launches()[0]["envelope"]))
        worker.execute(value, completed)
        return type("Child", (), {"pid": 123})()

    def missing_launcher(pid=None):
        if pid == 123:
            raise psutil.NoSuchProcess(pid)
        return process_class(pid)

    monkeypatch.setattr(worker.subprocess, "Popen", fast_child)
    monkeypatch.setattr(psutil, "Process", missing_launcher)
    frozen = worker.start(state, envelope)
    worker._uncertain(state, frozen, RuntimeError("late obsolete observation"))
    assert state.attempt(envelope.key.work_id)["status"] == "completed"
    assert not state.campaign()["blockers"]
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
