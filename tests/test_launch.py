from dataclasses import asdict
import json
import os
from pathlib import Path
import time

import pytest

from labgoblin.config import initial_config, parse_config
from labgoblin.protocol import (
    LaunchEnvelope, LaunchKey, LaunchReceipt, PreExecutionError, Resources,
    UncertainExecution, identifier,
)
from labgoblin.scheduler import MachineSample, ResourceLedger
from labgoblin.state import State
from labgoblin import worker


@pytest.fixture
def admission(tmp_path):
    cfg = parse_config(initial_config("fixture"), tmp_path / "labgoblin.toml")
    ledger = ResourceLedger.create(
        tmp_path / "machine.db",
        sampler=lambda gpus: MachineSample((0, 1), 8192, 8192),
        eligibility=lambda row: (True, ""))
    ledger.configure(2, 4096, (), 0)
    state = State.create(cfg, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    work_id = identifier()
    root = state.root / "attempts" / work_id
    root.mkdir(parents=True)
    spec = {"id": work_id, "key": "work", "request": {"key": "work"}, "experiment_id": "work",
            "cpus": 1, "memory_mb": 128, "gpus": [], "seconds": 10, "root": str(root)}
    state.enqueue(spec)
    resources = Resources(1, 128)
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
