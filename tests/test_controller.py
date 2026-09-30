from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

import psutil
import pytest

from tests.test_providers import DOUBLE, HELP, fixture as provider_fixture
from tests.test_workspace import PROGRAM, request
from xgenius import agent, workspace
from xgenius.campaign import Campaign
from xgenius.config import initial_config, parse_config
from xgenius.protocol import AdmissionWait, LaunchEnvelope, LaunchKey, LaunchReceipt, UncertainExecution, identifier
from xgenius.scheduler import MachineSample, ResourceLedger
from xgenius.state import State


def fixture(tmp_path, *, max_invocations=10, retries=0, program=DOUBLE):
    project = tmp_path / "project"
    project.mkdir()
    (project / "experiment.py").write_text(PROGRAM, encoding="utf-8")
    goal = b"Verify the deterministic score, then report the limitations."
    (project / "research_goal.md").write_bytes(goal)
    provider = project / "provider-double.py"
    provider.write_text(f"HELP_TEXT={HELP!r}\n" + program, encoding="utf-8")
    raw = initial_config("controller-fixture", "copilot")
    raw["execution"]["source_files"] = ["experiment.py"]
    raw["campaign"]["max_invocations"] = max_invocations
    raw["agent"].update(command=[sys.executable, str(provider)], retries=retries,
                        timeout_seconds=10, resources={"cpus": 1, "memory_mb": 256})
    config = parse_config(raw, project / "xgenius.toml")
    cpus = tuple(psutil.Process().cpu_affinity()[:2])
    ledger = ResourceLedger.create(tmp_path / "machine.db",
                                   sampler=lambda _: MachineSample(cpus, 8192, 8192),
                                   eligibility=lambda _: (True, ""))
    ledger.configure(2, 4096, (), 0)
    state = State.create(config, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    state.source("goal", goal, origin="operator", head="goal")
    return config, state, ledger, raw


def drain(controller, predicate, *, no_agent=True):
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        last = controller.step(no_agent=no_agent)
        if predicate(last):
            return last
        time.sleep(0.04)
    pytest.fail(f"Controller did not converge: {last}")


def forbid_provider(*args, **kwargs):
    raise AssertionError("No-agent operation attempted provider inspection")


def test_no_agent_runs_owned_native_work_and_collects_without_inference(tmp_path, monkeypatch):
    config, state, ledger, _ = fixture(tmp_path)
    monkeypatch.setattr(agent, "inspect_provider", forbid_provider)
    attempt = workspace.submit(state, config, request())
    result = Campaign(config, state=state, ledger=ledger).run(no_agent=True)
    assert not result["errors"]
    assert state.attempt(attempt["id"])["status"] == "completed"
    assert state.collection(attempt["id"])["validation"] == "valid"
    assert state.campaign()["invocations"] == 0
    assert state.campaign()["controller"] is None
    assert not ledger.rows()


def test_controller_identity_cannot_be_replaced_by_another_live_owner(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    first, second = Campaign(config), Campaign(config)
    with first:
        with pytest.raises(UncertainExecution, match="alive"):
            second.acquire()
        assert state.campaign()["controller"] is not None
    with second:
        assert state.campaign()["controller"] is not None
    assert state.campaign()["controller"] is None


def test_pause_gates_queued_work_and_stop_drains_already_armed_work(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    first = workspace.submit(state, config, request())
    second = workspace.submit(state, config, request(key="unperformed"))
    state.control("pause", "pause", 0)
    with Campaign(config, state=state, ledger=ledger) as controller:
        assert not controller.step(no_agent=True)["started"]
        assert state.attempt(first["id"])["status"] == "queued"
        state.control("resume", "resume", 1)
        assert controller.step(no_agent=True)["started"] == [first["id"]]
        state.control("stop", "stop", 2)
        drain(controller, lambda _: state.campaign()["operator_mode"] == "stopped")
    assert state.attempt(first["id"])["status"] == "completed"
    assert state.attempt(second["id"])["status"] == "cancelled"
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM closure_members").fetchone()[0] == 2
        assert conn.execute("SELECT SUM(admitted) FROM closure_members").fetchone()[0] == 1
    assert not ledger.rows()


def test_no_agent_retires_reserved_turn_and_refunds_its_unused_grant(tmp_path, monkeypatch):
    config, state, ledger, turn_id, ids, grant = provider_fixture(tmp_path)
    monkeypatch.setattr(agent, "inspect_provider", forbid_provider)
    with Campaign(config, state=state, ledger=ledger) as controller:
        controller.step(no_agent=True)
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM turns WHERE id=?", (turn_id,)).fetchone()[0] == "cancelled"
        assert conn.execute("SELECT state FROM invocations WHERE id=?", (ids[0],)).fetchone()[0] == "cancelled"
    assert state.campaign()["invocations"] == 0
    assert ledger.grant(grant["token"])["state"] == "released"


def test_pause_between_canary_and_main_releases_bundle_without_main_call(tmp_path):
    config, state, ledger, turn_id, ids, grant = provider_fixture(tmp_path, sandbox=True)
    envelope = LaunchEnvelope(
        LaunchKey(state.id, 1, ids[0], grant["token"], identifier()), "canary", ("fake-canary",),
        str(config.root), str(state.root / "turns" / turn_id), str(state.path), str(ledger.path),
        ledger.id, 10, config.agent.resources, config.revision, metadata={"last_in_bundle": False})
    state.arm(envelope)
    state.finish_launch(envelope.key.nonce, asdict(LaunchReceipt(
        envelope.key, envelope.digest, "completed", True, 0.1, returncode=0)))
    state.control("pause", "pause", 0)
    with Campaign(config, state=state, ledger=ledger) as controller:
        controller.step()
    assert state.campaign()["invocations"] == 1
    assert ledger.grant(grant["token"])["state"] == "released"
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM invocations WHERE id=?", (ids[1],)).fetchone()[0] == "cancelled"


def test_broken_current_config_does_not_prevent_owned_recovery_and_collection(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    attempt = workspace.submit(state, config, request())
    with Campaign(config, state=state, ledger=ledger) as first:
        assert first.step(no_agent=True)["started"]
    deadline = time.monotonic() + 15
    while state.attempt(attempt["id"])["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.05)
    Path(config.config_path).write_text("not [valid TOML", encoding="utf-8")
    recovered = Campaign(state=state, ledger=ledger).run(no_agent=True)
    assert state.collection(attempt["id"])["collection"] == "complete"
    assert any(error["category"] == "configuration" for error in recovered["errors"])
    assert not ledger.rows()


def test_provider_failure_retries_are_separately_counted_and_bounded(tmp_path):
    program = """import sys\nif '--help' in sys.argv: print(HELP_TEXT)\nelse: print('No owned result was produced')\n"""
    config, state, ledger, _ = fixture(tmp_path, retries=1, program=program)
    Campaign(config, state=state, ledger=ledger).run()
    assert state.campaign()["progress"] == "blocked"
    assert state.campaign()["invocations"] == 2
    assert state.campaign()["failures"] == 2
    assert not ledger.rows()


def test_closure_reserve_does_not_allow_an_unbudgeted_initial_turn(tmp_path, monkeypatch):
    config, state, ledger, raw = fixture(tmp_path, max_invocations=1)
    monkeypatch.setattr(agent, "inspect_provider", forbid_provider)
    result = Campaign(config, state=state, ledger=ledger).run()
    assert state.campaign()["invocations"] == 0
    assert any(item["category"] == "budget" for item in result["errors"])
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 1
    assert not ledger.rows()


def test_waiting_handoff_does_not_pay_again_for_old_unacknowledged_events(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    result = Campaign(config, state=state, ledger=ledger).run()
    assert not result["errors"]
    assert state.campaign()["progress"] == "wait"
    assert state.campaign()["invocations"] == 1
    assert state.pending_events()
    Campaign(config, state=state, ledger=ledger).run(once=True)
    assert state.campaign()["invocations"] == 1
    assert not ledger.rows()


def test_admission_order_is_not_changed_by_wall_clock_timestamps(tmp_path, monkeypatch):
    config, state, ledger, _ = fixture(tmp_path)
    attempt = workspace.submit(state, config, request())
    with state.db.write() as conn:
        conn.execute("UPDATE attempts SET created=999999999999 WHERE id=?", (attempt["id"],))
    observed = []
    with Campaign(config, state=state, ledger=ledger) as controller:
        def wait_for_oldest(work):
            observed.append(work["id"])
            raise AdmissionWait("Held at deterministic admission boundary")
        monkeypatch.setattr(controller, "_launch_attempt", wait_for_oldest)
        monkeypatch.setattr(controller, "_launch_turn", forbid_provider)
        controller.step()
    assert observed == [attempt["id"]]


def test_cancel_marks_request_before_owned_tree_is_quiescent(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    (config.root / "experiment.py").write_text("import time; time.sleep(20)", encoding="utf-8")
    attempt = workspace.submit(state, config, request(seconds=5, artifacts=[]))
    with Campaign(config, state=state, ledger=ledger) as controller:
        controller.step(no_agent=True)
        cancellation = state.cancel_attempt(attempt["id"])
        assert cancellation["status"] in ("starting", "running")
        assert cancellation["cancel_requested"] is not None
        drain(controller, lambda _: state.attempt(attempt["id"])["status"] == "cancelled")
    assert not ledger.rows()
