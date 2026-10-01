from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import psutil
import pytest

from labgoblin import agent, agent_policy, agent_worker, backends, worker
from labgoblin.config import initial_config, parse_config
from labgoblin.evidence import publish_bytes, read_json, tail
from labgoblin.payload import command_units, process_environment
from labgoblin.protocol import LaunchReceipt, Resources, canonical, identifier
from labgoblin.scheduler import MachineSample, ResourceLedger
from labgoblin.state import State


HELP = """--prompt <text>
--model <model>
--reasoning-effort <level>
  [possible values: low, high, xhigh]
--effort <level>
  [possible values: low, high, max]
--no-auto-update --no-remote --no-remote-export --no-ask-user
--stream --log-level --usage-output-file
"""

DOUBLE = r'''
import json,pathlib,re,sys,time
if "--help" in sys.argv:
    print(HELP_TEXT)
    raise SystemExit(0)
prompt=sys.argv[sys.argv.index("-p")+1]
paths=[json.loads(item) for item in re.findall(r'"(?:\\.|[^"\\])*"',prompt)]
packet=json.loads(pathlib.Path(paths[0]).read_text(encoding="utf-8"))
if packet.get("sleep"):
    time.sleep(packet["sleep"])
result=dict(turn_id=packet["turn_id"],packet_id=packet["packet_id"],summary="Owned provider double",
            rationale="Deterministic provider protocol fixture",next_step="Wait for evidence",
            disposition="wait",reason="No uncontrolled inference",evidence=[])
pathlib.Path(paths[1]).write_text(json.dumps(result),encoding="utf-8")
if "--usage-output-file" in sys.argv:
    usage=pathlib.Path(sys.argv[sys.argv.index("--usage-output-file")+1])
    usage.write_text(json.dumps(dict(provider_double=True,tokens=7)),encoding="utf-8")
print(json.dumps(sys.argv[1:]))
'''


def fixture(tmp_path, provider="copilot", *, sandbox=False):
    program = tmp_path / "provider double.py"
    program.write_text(f"HELP_TEXT={HELP!r}\n" + DOUBLE, encoding="utf-8")
    raw = initial_config("provider-fixture", provider)
    raw["agent"].update(command=[sys.executable, str(program)], resources={"cpus": 1, "memory_mb": 256})
    if sandbox:
        raw["agent"].update(sandbox=True, copilot_home=str(Path(".labgoblin") / "sandbox"))
    config = parse_config(raw, tmp_path / "labgoblin.toml")
    cpus = tuple(psutil.Process().cpu_affinity()[:2])
    ledger = ResourceLedger.create(tmp_path / "machine.db",
                                   sampler=lambda g: MachineSample(cpus, 8192, 8192),
                                   eligibility=lambda row: (True, ""))
    ledger.configure(2, 4096, (), 0)
    state = State.create(config, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    turn_id, packet_id = identifier(), identifier()
    content = canonical({"turn_id": turn_id, "packet_id": packet_id, "context": "x" * 40000})
    packet_path = state.root / "packets" / f"{packet_id}.json"
    digest = publish_bytes(packet_path, content)
    with state.db.write() as conn:
        conn.execute("""INSERT INTO packets(id,turn_id,generation,watermark,content,digest,path,ready,created)
            VALUES(?,?,1,0,?,?,?,1,?)""", (packet_id, turn_id, content.decode(), digest, str(packet_path), time.time()))
        conn.execute("""INSERT INTO turns(id,generation,kind,packet_id,state,created,revision)
            VALUES(?,1,'research',?,'prepared',?,0)""", (turn_id, packet_id, time.time()))
    kinds = ("canary", "research") if sandbox else ("research",)
    invocations = state.reserve_invocations(turn_id, "research", kinds)
    resources = config.agent.resources
    allocation = state.allocation(turn_id, "research", {"resources": asdict(resources)}, 0)
    ledger.request(allocation["token"], state.id, turn_id, "research", resources,
                   {"kind": "campaign", "state_dir": str(state.root), "generation": 1, "revision": 0},
                   native=True)
    grant = ledger.reserve(allocation["token"])
    assert state.granted(grant["token"])
    return config, state, ledger, turn_id, invocations, grant


def wait(state, invocation_id):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        with state.db.read() as conn:
            row = conn.execute("SELECT state,nonce FROM invocations WHERE id=?", (invocation_id,)).fetchone()
        if row["state"] in ("completed", "failed", "not_started"):
            worker.reconcile(state, backends.inspect_payload)
            return row["state"]
        time.sleep(0.05)
    pytest.fail(f"Provider double has no durable outcome: {state.campaign()}")


@pytest.mark.parametrize("provider", ["claude", "copilot"])
def test_owned_provider_double_packet_argv_result_and_accounting(tmp_path, provider):
    config, state, ledger, turn, ids, grant = fixture(tmp_path, provider)
    envelope = agent.prepare(state, config, turn, ids[0], grant)
    assert command_units(envelope.argv) < 4000
    assert "x" * 100 not in envelope.argv[-1]
    frozen = worker.start(state, envelope)
    assert wait(state, ids[0]) == "completed"
    result = agent_worker.read_result(state, turn)
    assert result["content"]["turn_id"] == turn
    assert result["content"]["summary"] == "Owned provider double"
    assert ledger.grant(grant["token"])["state"] == "released"
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocations WHERE state='completed'").fetchone()[0] == 1
    assert state.campaign()["invocations"] == 1
    receipt = result["provider_receipt"]
    assert receipt["metadata"]["provider"]["effective_model"] is None
    assert receipt["metadata"]["provider"]["configured_model"] is None
    assert receipt["metadata"]["provider"]["configured_effort"] is None
    assert not {"--model", "--reasoning-effort", "--effort"}.intersection(envelope.argv)
    if provider == "copilot":
        assert receipt["usage"] == {"provider_double": True, "tokens": 7}
        assert "--no-auto-update" in envelope.argv
    else:
        assert receipt["usage"] is None
    assert read_json(worker.launch_directory(frozen) / "backend-receipt.json") == receipt
    agent_worker.accept_result(state, turn)
    assert state.campaign()["progress"] == "wait"


def test_explicit_model_and_effort_use_supported_provider_flags(tmp_path):
    config, *_ = fixture(tmp_path)
    settings = replace(config.agent, model="explicit-model", reasoning_effort="xhigh")
    adapter = agent.inspect_provider(settings, probe=lambda args: HELP)
    command = agent.invocation_command(adapter, "a prompt", tmp_path)
    assert command[command.index("--model") + 1] == "explicit-model"
    assert command[command.index("--reasoning-effort") + 1] == "xhigh"
    assert adapter["effective_model"] is None


@pytest.mark.parametrize("effort,help_text", [("max", HELP), ("high", "--prompt --model")])
def test_unsupported_explicit_effort_fails_before_inference(tmp_path, effort, help_text):
    config, *_ = fixture(tmp_path)
    with pytest.raises(ValueError):
        agent.inspect_provider(replace(config.agent, reasoning_effort=effort),
                               probe=lambda args: help_text)


def test_managed_provider_command_cannot_resume_old_sessions(tmp_path):
    config, *_ = fixture(tmp_path)
    for flag in ("--resume=old", "--model=other", "--share-gist", "--fleet", "-pold"):
        with pytest.raises(ValueError, match="override"):
            agent.inspect_provider(replace(config.agent, command=(*config.agent.command, flag)),
                                   probe=lambda args: HELP)


def test_unsupported_sandbox_never_launches_a_canary(tmp_path):
    config, state, ledger, turn, ids, grant = fixture(tmp_path, sandbox=True)
    with pytest.raises(ValueError, match="unsandboxed fallback"):
        agent.prepare(state, config, turn, ids[0], grant)
    assert not state.active_launches()
    with state.db.read() as conn:
        assert all(row[0] == "reserved" for row in conn.execute("SELECT state FROM invocations"))


def test_mutated_packet_is_not_a_consumed_provider_invocation(tmp_path):
    config, state, ledger, turn, ids, grant = fixture(tmp_path)
    envelope = agent.prepare(state, config, turn, ids[0], grant)
    Path(envelope.metadata["packet_path"]).write_text("changed", encoding="utf-8")
    worker.start(state, envelope)
    assert wait(state, ids[0]) == "not_started"
    assert state.campaign()["invocations"] == 0
    assert ledger.grant(grant["token"])["state"] == "released"
    with pytest.raises(ValueError, match="successfully completed"):
        agent_worker.read_result(state, turn)


def test_human_file_without_completed_owned_invocation_is_not_a_result(tmp_path):
    config, state, _, turn, _, _ = fixture(tmp_path)
    path = state.root / "turns" / turn / "handoff.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"summary":"human note"}', encoding="utf-8")
    with pytest.raises(ValueError, match="successfully completed"):
        agent_worker.read_result(state, turn)


def test_changed_result_after_owned_provider_completion_is_rejected(tmp_path):
    config, state, _, turn, ids, grant = fixture(tmp_path)
    envelope = worker.start(state, agent.prepare(state, config, turn, ids[0], grant))
    assert wait(state, ids[0]) == "completed"
    path = Path(envelope.metadata["result_path"])
    value = json.loads(path.read_text(encoding="utf-8"))
    value["summary"] = "This was not the owned provider's conclusion."
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after"):
        agent_worker.accept_result(state, turn)
    assert state.campaign()["progress"] == "research"


def test_claude_subscription_environment_does_not_modify_parent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder-not-a-credential")
    env = process_environment({"cpus": 1, "provider": "claude"})
    assert "ANTHROPIC_API_KEY" not in env
    assert os.environ["ANTHROPIC_API_KEY"] == "test-placeholder-not-a-credential"


def test_provider_preliminary_receipt_is_not_published_before_policy_validation(tmp_path, monkeypatch):
    config, state, _, turn, ids, grant = fixture(tmp_path)
    envelope = agent.prepare(state, config, turn, ids[0], grant)
    observed = []

    def execute(spec, *, publish_receipt):
        observed.append(publish_receipt)
        return LaunchReceipt(envelope.key, envelope.digest, "completed", True, 1, returncode=0)

    monkeypatch.setattr(agent_policy, "execute_spec", execute)
    result = agent_policy.supervise(envelope)
    assert observed == [False]
    assert read_json(worker.launch_directory(envelope) / "backend-receipt.json") == asdict(result)


def test_unicode_and_spaces_survive_real_provider_double_argv(tmp_path):
    directory = tmp_path / "research \u03bb with spaces"
    directory.mkdir()
    config, state, _, turn, ids, grant = fixture(directory)
    envelope = worker.start(state, agent.prepare(state, config, turn, ids[0], grant))
    assert wait(state, ids[0]) == "completed"
    captured = json.loads(tail(worker.launch_directory(envelope) / "main" / "stdout.log")["text"])
    assert captured[captured.index("-p") + 1] == envelope.argv[-1]
    assert agent_worker.read_result(state, turn)["content"]["summary"] == "Owned provider double"


def sandbox_fixture(tmp_path, monkeypatch):
    config, state, ledger, turn, ids, grant = fixture(tmp_path, sandbox=True)
    home = config.state_dir / "sandbox"
    home.mkdir()
    settings = {"sandbox": {"enabled": True, "allowBypass": False, "userPolicy": {
        "filesystem": {"deniedPaths": [str(home / "preflight" / "denied")]}}}}
    (home / "settings.json").write_bytes(canonical(settings))
    adapter = agent.inspect_provider(config.agent, probe=lambda args: HELP + "\n--sandbox --experimental")
    envelope = agent.prepare(state, config, turn, ids[0], grant, adapter=adapter)

    def execute(spec, *, publish_receipt):
        current = worker.LaunchEnvelope.parse(spec["envelope"])
        return LaunchReceipt(current.key, current.digest, "completed", True, 1, returncode=0)

    monkeypatch.setattr(agent_policy, "execute_spec", execute)
    return config, state, ledger, turn, ids, grant, adapter, envelope


def test_budgeted_canary_and_main_share_one_grant_and_recheck_policy(tmp_path, monkeypatch):
    config, state, ledger, turn, ids, grant, adapter, canary = sandbox_fixture(tmp_path, monkeypatch)
    frozen = worker.prepare_runtime(state, canary)
    state.arm(frozen)
    result = {"challenge": canary.key.nonce, "allowed_write": True, "denied_write_blocked": True}
    publish_bytes(Path(canary.metadata["canary"]["result"]), canonical(result))
    assert worker.execute(frozen) == 0
    assert ledger.grant(grant["token"])["state"] == "granted"
    main = agent.prepare(state, config, turn, ids[1], grant, adapter=adapter)
    assert main.key.grant_id == canary.key.grant_id
    frozen_main = worker.prepare_runtime(state, main)
    state.arm(frozen_main)
    assert worker.execute(frozen_main) == 0
    assert ledger.grant(grant["token"])["state"] == "released"
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocations WHERE state='completed'").fetchone()[0] == 2


def test_failed_canary_cancels_uninvoked_main_and_releases_exact_grant(tmp_path, monkeypatch):
    _, state, ledger, _, ids, grant, _, canary = sandbox_fixture(tmp_path, monkeypatch)
    frozen = worker.prepare_runtime(state, canary)
    state.arm(frozen)
    result = {"challenge": canary.key.nonce, "allowed_write": True, "denied_write_blocked": False}
    publish_bytes(Path(canary.metadata["canary"]["result"]), canonical(result))
    assert worker.execute(frozen) == 1
    assert ledger.grant(grant["token"])["state"] == "released"
    with state.db.read() as conn:
        outcomes = {row["id"]: row["state"] for row in conn.execute("SELECT id,state FROM invocations")}
    assert outcomes == {ids[0]: "failed", ids[1]: "cancelled"}


def test_canary_cannot_authorize_main_after_operator_pause(tmp_path, monkeypatch):
    config, state, ledger, turn, ids, grant, adapter, canary = sandbox_fixture(tmp_path, monkeypatch)
    frozen = worker.prepare_runtime(state, canary)
    state.arm(frozen)
    publish_bytes(Path(canary.metadata["canary"]["result"]),
                  canonical({"challenge": canary.key.nonce, "allowed_write": True, "denied_write_blocked": True}))
    assert worker.execute(frozen) == 0
    state.control("pause", "pause-before-main", 0)
    main = agent.prepare(state, config, turn, ids[1], grant, adapter=adapter)
    with pytest.raises(ValueError, match="control revision"):
        state.arm(worker.prepare_runtime(state, main))
    assert ledger.grant(grant["token"])["state"] == "granted"
