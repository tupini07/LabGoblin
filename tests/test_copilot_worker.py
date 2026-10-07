import asyncio
from dataclasses import asdict, replace
import inspect
import json
from pathlib import Path
import time
from types import SimpleNamespace

from copilot import CopilotClient
import pytest

from labgoblin import agent, agent_policy, credits, worker
from labgoblin import cli
from labgoblin.config import initial_config, parse_config, restore_config
from labgoblin.copilot_worker import Activity, drive, usage_record
from labgoblin.evidence import atomic_json, read_json
from labgoblin.protocol import LaunchReceipt, canonical
from tests.test_providers import fixture


def sdk_envelope(tmp_path):
    config, state, ledger, turn, ids, grant = fixture(
        tmp_path, credit_limit=100, agent_settings={
            "transport": "sdk", "idle_timeout_seconds": 0.01, "shutdown_grace_seconds": 5})
    envelope = worker.prepare_runtime(state, agent.prepare(state, config, turn, ids[0], grant))
    worker.launch_directory(envelope).mkdir(parents=True, exist_ok=True)
    return state, envelope


class Session:
    def __init__(self, scenario, *, abort_error=False, metrics=None):
        self.scenario, self.abort_error = scenario, abort_error
        self.task = None
        self.handler = None
        self.aborted = False
        self.disconnected = False
        self.calls = 0
        self.values = metrics or [0, 7 * credits.NANO, 12 * credits.NANO]
        self.permission_mode = None
        self.rpc = SimpleNamespace(usage=SimpleNamespace(get_metrics=self.get_metrics),
                                   permissions=SimpleNamespace(set_mode=self.set_mode))

    async def set_mode(self, request):
        self.permission_mode = request.mode.value
        return SimpleNamespace(to_dict=lambda: {"success": True, "mode": self.permission_mode})

    def event(self, kind, **data):
        self.handler(SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data)))

    async def get_metrics(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return SimpleNamespace(to_dict=lambda: {"totalNanoAiu": value})

    async def send(self, prompt):
        assert "LabGoblin research operation" in prompt
        self.task = asyncio.create_task(self.scenario(self))
        return "message"

    async def abort(self):
        self.aborted = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.abort_error:
            raise RuntimeError("Provider did not acknowledge cancellation")
        self.event("session.idle")

    async def disconnect(self):
        self.disconnected = True


class Client:
    def __init__(self, session):
        self.session = session
        self.stopped = False
        self.arguments = None

    async def start(self):
        pass

    async def create_session(self, **kwargs):
        inspect.signature(CopilotClient.create_session).bind(self, **kwargs)
        self.arguments = kwargs
        self.session.handler = kwargs["on_event"]
        return self.session

    async def stop(self):
        self.stopped = True

    async def force_stop(self):
        self.stopped = True


def test_defaults_and_old_snapshot_preserve_explicit_semantics(tmp_path):
    raw = initial_config("new", "copilot")
    config = parse_config(raw, tmp_path / "labgoblin.toml")
    assert config.agent.timeout_seconds == 1800
    assert config.agent.transport == "sdk"
    assert initial_config("new", "claude")["agent"]["transport"] == "cli"
    old = asdict(config)
    old["agent"]["timeout_seconds"] = 600
    for field in ("transport", "idle_timeout_seconds", "shutdown_grace_seconds"):
        del old["agent"][field]
    restored = restore_config(old, revision=config.revision, path=config.config_path)
    assert restored.agent.timeout_seconds == 600
    assert restored.agent.transport == "cli"


@pytest.mark.parametrize("changes", [
    {"transport": "other"}, {"provider": "claude", "transport": "sdk"},
    {"idle_timeout_seconds": 0}, {"shutdown_grace_seconds": 0},
    {"shutdown_grace_seconds": 4}, {"timeout_seconds": 30, "shutdown_grace_seconds": 30},
])
def test_invalid_supervision_settings_are_rejected(tmp_path, changes):
    raw = initial_config("new", "copilot")
    raw["agent"].update(changes)
    with pytest.raises(ValueError):
        parse_config(raw, tmp_path / "labgoblin.toml")


def test_idle_detection_does_not_mistake_pending_work_for_a_hang():
    now = [0]
    activity = Activity(clock=lambda: now[0], wall=lambda: now[0])
    activity.observe("assistant.turn_start", SimpleNamespace())
    now[0] = 100
    assert not activity.stop_reason(200, 10, 5)
    activity.observe("assistant.message", SimpleNamespace())
    assert activity.response_in_progress
    activity.observe("assistant.usage", SimpleNamespace())
    activity.observe("tool.execution_start", SimpleNamespace(tool_call_id="tool"))
    now[0] = 150
    assert not activity.stop_reason(200, 10, 5)
    activity.observe("tool.execution_complete", SimpleNamespace(tool_call_id="tool"))
    now[0] = 156
    assert "idle allowance" in activity.stop_reason(200, 10, 5)
    activity.observe("session.info", SimpleNamespace())
    assert "idle allowance" in activity.stop_reason(200, 10, 5)
    activity.observe("assistant.turn_start", SimpleNamespace())
    now[0] = 191
    assert "deadline" in activity.stop_reason(200, 10, 5)


def test_sdk_command_is_frozen_and_checked_before_arming(tmp_path):
    state, envelope = sdk_envelope(tmp_path)
    assert "--research-sdk" in envelope.argv
    assert envelope.argv[-2:] == ("--max-ai-credits", "70")
    assert "-p" not in envelope.argv
    assert "-p" in envelope.metadata["provider_argv"]
    for key, value in (("idle_timeout_seconds", 100), ("shutdown_grace_seconds", 9)):
        forged = replace(envelope, metadata={**envelope.metadata, key: value})
        with pytest.raises(ValueError, match="supervision"):
            state.arm(forged)
    state.arm(envelope)
    assert state.credit_budget()["reserved"] == 70


@pytest.mark.parametrize("phase", ["response", "tool"])
def test_quiet_real_work_survives_idle_threshold_and_captures_final_usage(tmp_path, phase):
    _, envelope = sdk_envelope(tmp_path)

    async def scenario(session):
        if phase == "response":
            session.event("assistant.turn_start")
        else:
            session.event("tool.execution_start", tool_call_id="long-tool")
        await asyncio.sleep(0.05)
        if phase == "tool":
            session.event("tool.execution_complete", tool_call_id="long-tool")
        session.event("assistant.message", content="Finished the owned handoff")
        session.event("session.idle")

    session = Session(scenario)
    client = Client(session)
    result = asyncio.run(drive(envelope, client, time.monotonic() + 10, poll_seconds=0.002))
    assert result == 0 and not session.aborted
    assert session.disconnected and client.stopped
    assert client.arguments["session_limits"] == {"max_ai_credits": 70}
    assert session.permission_mode == "manual"
    directory = worker.launch_directory(envelope)
    assert read_json(directory / "usage-progress.json")["_labgoblin"]["complete"] is False
    assert read_json(directory / "usage.json")["totalNanoAiu"] == 12 * credits.NANO
    assert read_json(directory / "usage.json")["_labgoblin"]["complete"] is True
    assert read_json(directory / "activity.json")["phase"] == "completed"


def test_inactive_session_aborts_gracefully_and_retains_final_usage(tmp_path):
    _, envelope = sdk_envelope(tmp_path)

    async def scenario(session):
        while True:
            session.event("session.info")
            await asyncio.sleep(0.002)

    session = Session(scenario)
    result = asyncio.run(drive(envelope, Client(session), time.monotonic() + 10, poll_seconds=0.002))
    assert result == 124 and session.aborted
    directory = worker.launch_directory(envelope)
    assert "idle allowance" in read_json(directory / "sdk-result.json")["reason"]
    assert read_json(directory / "usage.json")["_labgoblin"]["complete"] is True


def test_continuous_progress_never_extends_the_hard_deadline(tmp_path):
    _, envelope = sdk_envelope(tmp_path)

    async def scenario(session):
        session.event("assistant.turn_start")
        while True:
            session.event("assistant.message_delta")
            await asyncio.sleep(0.002)

    session = Session(scenario)
    result = asyncio.run(drive(envelope, Client(session), time.monotonic() + 5.05, poll_seconds=0.002))
    assert result == 124 and session.aborted
    assert "deadline" in read_json(worker.launch_directory(envelope) / "sdk-result.json")["reason"]


def test_failed_abort_retains_partial_usage_without_claiming_a_final_total(tmp_path):
    _, envelope = sdk_envelope(tmp_path)

    async def scenario(session):
        session.event("assistant.turn_start")
        while session.calls < 2:
            await asyncio.sleep(0.002)
        session.event("assistant.turn_end")
        await asyncio.sleep(100)

    session = Session(scenario, abort_error=True)
    result = asyncio.run(drive(envelope, Client(session), time.monotonic() + 10, poll_seconds=0.002))
    directory = worker.launch_directory(envelope)
    assert result == 124
    assert not (directory / "usage.json").exists()
    assert read_json(directory / "usage-progress.json")["totalNanoAiu"] == 7 * credits.NANO
    assert "did not acknowledge" in read_json(directory / "sdk-result.json")["usage_error"]


def test_decreasing_provider_metrics_fail_closed(tmp_path):
    _, envelope = sdk_envelope(tmp_path)

    async def scenario(session):
        session.event("assistant.turn_start")
        await asyncio.sleep(0.02)
        session.event("assistant.message", content="done")
        session.event("session.idle")

    session = Session(scenario, metrics=[0, 7 * credits.NANO, 3 * credits.NANO])
    result = asyncio.run(drive(envelope, Client(session), time.monotonic() + 10, poll_seconds=0.002))
    directory = worker.launch_directory(envelope)
    assert result == 1 and not (directory / "usage.json").exists()
    assert "decreased" in read_json(directory / "sdk-result.json")["reason"]


def test_supervisor_preserves_graceful_timeout_and_usage(tmp_path, monkeypatch):
    _, envelope = sdk_envelope(tmp_path)
    directory = worker.launch_directory(envelope)
    receipt = LaunchReceipt(envelope.key, envelope.digest, "failed", True, 1, returncode=124,
                            reason="Process exited with code 124")
    monkeypatch.setattr(agent_policy, "execute_spec", lambda *a, **k: receipt)
    atomic_json(directory / "sdk-result.json", {
        "key": asdict(envelope.key), "envelope_digest": envelope.digest,
        "status": "timed_out", "reason": "Research turn deadline approaching"})
    atomic_json(directory / "usage.json", usage_record(
        envelope, "session", {"totalNanoAiu": 12 * credits.NANO}, complete=True))
    outcome = agent_policy.supervise(envelope)
    assert outcome.status == "timed_out"
    assert outcome.usage["totalNanoAiu"] == 12 * credits.NANO


def test_forced_timeout_is_unknown_even_with_a_partial_checkpoint(tmp_path, monkeypatch):
    _, envelope = sdk_envelope(tmp_path)
    receipt = LaunchReceipt(envelope.key, envelope.digest, "timed_out", True, 1,
                            reason="Operation deadline exceeded")
    monkeypatch.setattr(agent_policy, "execute_spec", lambda *a, **k: receipt)
    atomic_json(worker.launch_directory(envelope) / "usage-progress.json", usage_record(
        envelope, "session", {"totalNanoAiu": 7 * credits.NANO}, complete=False))
    outcome = agent_policy.supervise(envelope)
    assert outcome.usage is None
    budget = credits.summarize([{"envelope": canonical(asdict(envelope)),
                                "receipt": canonical(asdict(outcome)), "phase": "quiescent"}], 100)
    assert budget["used"] == budget["partial_used"] == 7
    assert budget["unknown"] == 1 and budget["remaining"] is None


def test_reconciliation_is_owned_append_only_idempotent_and_not_scientific_acceptance(tmp_path):
    state, envelope = sdk_envelope(tmp_path)
    state.arm(envelope)
    receipt = LaunchReceipt(envelope.key, envelope.digest, "timed_out", True, 1,
                            reason="Operation deadline exceeded",
                            metadata={"partial_usage": {"totalNanoAiu": 7 * credits.NANO}})
    state.finish_launch(envelope.key.nonce, asdict(receipt))
    original = state.launch(envelope.key.nonce)["receipt"]
    revision = state.campaign()["authority_revision"]
    record = usage_record(envelope, "session", {"totalNanoAiu": 12 * credits.NANO}, complete=True)
    invoke = lambda body: state.reconcile_credit(
        envelope.key.work_id, canonical(body), confirm=state.id, reason="Recovered final provider metrics")
    partial = {**record, "_labgoblin": {**record["_labgoblin"], "complete": False}}
    wrong = {**record, "_labgoblin": {**record["_labgoblin"], "nonce": "another"}}
    for bad in (partial, wrong, {**record, "totalNanoAiu": credits.NANO}):
        with pytest.raises(ValueError):
            invoke(bad)
    result = invoke(record)
    assert not result["replayed"]
    assert invoke(record) == {**result, "replayed": True}
    with pytest.raises(ValueError, match="Conflicting"):
        invoke({**record, "totalNanoAiu": 13 * credits.NANO})
    assert state.launch(envelope.key.nonce)["receipt"] == original
    assert state.campaign()["authority_revision"] == revision
    budget = state.credit_budget()
    assert budget["used"] == 12 and budget["unknown"] == 0 and budget["reconciled"] == 1
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind='credit_reconciliation'").fetchone()[0] == 1


def test_reconciliation_command_refuses_research_turn_and_wrong_campaign(tmp_path, monkeypatch):
    state, envelope = sdk_envelope(tmp_path)
    state.arm(envelope)
    state.finish_launch(envelope.key.nonce, asdict(LaunchReceipt(
        envelope.key, envelope.digest, "timed_out", True, 1, reason="Operation deadline exceeded")))
    path = tmp_path / "recovered.json"
    record = usage_record(envelope, "session", {"totalNanoAiu": 12 * credits.NANO}, complete=True)
    path.write_bytes(canonical(record))
    args = cli.parser().parse_args([
        "--project", str(tmp_path), "reconcile-credits", "--id", envelope.key.work_id,
        "--file", str(path), "--confirm", "wrong", "--reason", "Recovered final provider metrics",
    ])
    with pytest.raises(ValueError, match="exact campaign ID"):
        cli.execute(args)
    args.confirm = state.id
    monkeypatch.setenv("LABGOBLIN_TURN_ID", "owned-research")
    with pytest.raises(ValueError, match="operator operation"):
        cli.execute(args)
    monkeypatch.delenv("LABGOBLIN_TURN_ID")
    result = cli.execute(args)
    assert result["ai_credits"]["used"] == 12 and result["ai_credits"]["unknown"] == 0


def test_cancellation_waits_for_provider_idle_before_querying_final_metrics(tmp_path):
    _, envelope = sdk_envelope(tmp_path)
    directory = worker.launch_directory(envelope)

    async def scenario(session):
        session.event("assistant.turn_start")
        (directory / "cancel").touch()
        await asyncio.sleep(100)

    class DelayedIdle(Session):
        async def abort(self):
            self.aborted = True
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

            async def settle():
                await asyncio.sleep(0.02)
                self.values = [19 * credits.NANO] * len(self.values)
                self.event("session.idle")

            asyncio.create_task(settle())

    session = DelayedIdle(scenario)
    result = asyncio.run(drive(envelope, Client(session), time.monotonic() + 10, poll_seconds=0.002))
    assert result == 130 and session.aborted
    assert read_json(directory / "usage.json")["totalNanoAiu"] == 19 * credits.NANO


def test_sdk_permission_mode_is_explicit_and_narrower_cli_rules_are_not_dropped(tmp_path):
    from tests.test_providers import HELP
    config, *_ = fixture(tmp_path)
    settings = replace(config.agent, transport="sdk", command=(*config.agent.command, "--allow-all"))
    adapter = agent.inspect_provider(settings, probe=lambda _: HELP)
    assert adapter["permission_mode"] == "allow-all"
    for flag in ("--deny-tool", "--allow-tool=shell", "--allow-all=false"):
        with pytest.raises(ValueError, match="cannot reinterpret permission flag"):
            agent.inspect_provider(replace(settings, command=(*settings.command, flag)), probe=lambda _: HELP)
