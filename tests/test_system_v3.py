"""Prepared-backend acceptance; never invokes a real provider or pulls images."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
import tomli_w

from tests.test_controller import fixture
from xgenius import backends, worker, workspace
from xgenius.campaign import Campaign
from xgenius.config import parse_config
from xgenius.evidence import read_json
from xgenius.processes import background_options, process_state
from xgenius.protocol import LaunchEnvelope


pytestmark = pytest.mark.skipif(
    os.environ.get("XGENIUS_SYSTEM_E2E") != "1",
    reason="Explicit opt-in to prepared native/WSL/Docker system execution")

TASK = """import json,os,sys,time
from pathlib import Path
out=Path(os.environ['XGENIUS_OUTPUT_DIR'])
assert 'XGENIUS_TEST_CREDENTIAL' not in os.environ
(out/'ready.json').write_text(json.dumps(dict(pid=os.getpid(),platform=sys.platform)))
if 'hold' in sys.argv:
    deadline=time.monotonic()+30
    while not (out/'release').exists():
        assert time.monotonic()<deadline, 'fixture barrier not released'
        time.sleep(.05)
(out/'metrics.json').write_text('{"score":42}')
print('synthetic payload complete',flush=True)
"""


def prepare(tmp_path):
    config, state, ledger, raw = fixture(tmp_path)
    (config.root / "experiment.py").write_text(TASK, encoding="utf-8")
    raw["runners"].update(
        ubuntu={"kind": "wsl", "distro": os.environ.get("XGENIUS_WSL_DISTRO", "Ubuntu"), "python": "python3"},
        container={"kind": "docker", "context": os.environ.get("XGENIUS_DOCKER_CONTEXT", "default"),
                   "image": "python:3.11-slim", "python": "python", "network": False})
    config = parse_config(raw, config.config_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    return config, state, ledger


def cli(config, *arguments, expected=0):
    env = {**os.environ, "PYTHONUTF8": "1", "XGENIUS_TEST_CREDENTIAL": "synthetic-not-a-real-credential"}
    result = subprocess.run(
        [sys.executable, "-m", "xgenius.cli", "--project", str(config.root), *arguments, "--json"],
        cwd=config.root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
        **background_options())
    assert result.returncode == expected, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def submit(state, config, *, kind="native", key="fixture", hold=False, **changes):
    return workspace.submit(state, config, {
        "key": key, "runner": kind, "argv": ["python", "experiment.py", *(["hold"] if hold else [])],
        "source_files": ["experiment.py"], "cpus": 1, "memory_mb": 256, "seconds": 45,
        "artifacts": ["metrics.json"], **changes})


def await_condition(condition, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("Owned synthetic system condition did not converge")


def finish(state, ledger):
    controller = Campaign(state=state, ledger=ledger)
    await_condition(lambda: (controller.reconcile(),
        not state.active_launches() and not ledger.rows())[1], timeout=60)


def cleanup_docker(state):
    with state.db.read() as conn:
        values = [row[0] for row in conn.execute("SELECT envelope FROM launches")]
    for value in values:
        envelope = LaunchEnvelope.parse(json.loads(value))
        if envelope.metadata.get("runner", {}).get("kind") != "docker":
            continue
        container = backends.docker_state(envelope)
        assert not container["state"]["Running"], container
        backends.command([*backends.docker_prefix(envelope.metadata["runner"]), "rm", container["id"]])


@pytest.mark.parametrize("kind", ["native", "ubuntu", "container"])
def test_prepared_runner_cli_snapshot_receipt_and_collection(tmp_path, kind):
    config, state, ledger = prepare(tmp_path)
    backends.validate_runner(asdict(config.runners[kind]))
    attempt = submit(state, config, kind=kind)
    (config.root / "experiment.py").write_text("raise RuntimeError('mutable source must not run')", encoding="utf-8")
    try:
        cli(config, "run", "--no-agent")
        assert state.attempt(attempt["id"])["status"] == "completed"
        assert state.collection(attempt["id"])["validation"] == "valid"
        spec = json.loads(attempt["spec"])
        assert read_json(Path(spec["output"]) / "metrics.json") == {"score": 42}
        with state.db.read() as conn:
            row = conn.execute("SELECT envelope,receipt,supervisor FROM launches").fetchone()
        receipt = json.loads(row["receipt"])
        assert receipt["quiescent"]
        assert receipt["metadata"]["payload_spec_digest"]
        supervisor = json.loads(row["supervisor"])
        await_condition(lambda: process_state(supervisor) == "dead")
        assert not ledger.rows() and state.campaign()["invocations"] == 0
    finally:
        cleanup_docker(state)


@pytest.mark.parametrize("parent_job", [False, True])
def test_controller_exit_and_parent_job_closure_preserve_owned_payload(tmp_path, parent_job):
    if parent_job and os.name != "nt":
        pytest.skip("Windows parent Job Object")
    config, state, ledger = prepare(tmp_path)
    attempt = submit(state, config, hold=True)
    output = Path(json.loads(attempt["spec"])["output"])
    prefix = ""
    if parent_job:
        prefix = (
            "import win32job,win32api;"
            "j=win32job.CreateJobObject(None,'');"
            "v=win32job.QueryInformationJobObject(j,win32job.JobObjectExtendedLimitInformation);"
            "v['BasicLimitInformation']['LimitFlags']=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|"
            "win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK;"
            "win32job.SetInformationJobObject(j,win32job.JobObjectExtendedLimitInformation,v);"
            "win32job.AssignProcessToJobObject(j,win32api.GetCurrentProcess());")
    code = prefix + "from xgenius.cli import main;main(['run','--no-agent','--json'])"
    with (config.root / "controller.out").open("wb") as out, (config.root / "controller.err").open("wb") as err:
        process = subprocess.Popen([sys.executable, "-c", code], cwd=config.root, stdout=out, stderr=err,
                                   **background_options())
    try:
        await_condition(lambda: (output / "ready.json").exists())
        assert process.poll() is None
        process.kill()
        process.wait(timeout=10)
        (output / "release").touch()
        Path(config.config_path).write_text("deliberately broken [ TOML", encoding="utf-8")
        finish(state, ledger)
        assert state.attempt(attempt["id"])["status"] == "completed"
        assert read_json(output / "metrics.json") == {"score": 42}
    finally:
        (output / "release").touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        finish(state, ledger)


@pytest.mark.parametrize("kind", ["native", "ubuntu", "container"])
def test_real_cancel_during_output_validator_preserves_owned_shutdown(tmp_path, kind):
    config, state, ledger = prepare(tmp_path)
    backends.validate_runner(asdict(config.runners[kind]))
    script = ("import os,time;from pathlib import Path;"
              "(Path(os.environ['XGENIUS_OUTPUT_DIR'])/'validator-ready').touch();time.sleep(30)")
    attempt = submit(state, config, kind=kind, validators=[["python", "-c", script]])
    output = Path(json.loads(attempt["spec"])["output"])
    try:
        cli(config, "run", "--once", "--no-agent")
        await_condition(lambda: (output / "validator-ready").exists())
        cli(config, "cancel", "--id", attempt["id"])
        finish(state, ledger)
        assert state.attempt(attempt["id"])["status"] == "cancelled"
    finally:
        state.cancel_attempt(attempt["id"])
        finish(state, ledger)
        cleanup_docker(state)


def test_shipped_example_full_cli_and_deterministic_report(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples" / "local-synthetic"
    project = tmp_path / "example"
    shutil.copytree(source, project)
    from types import SimpleNamespace
    config = SimpleNamespace(root=project)
    cli(config, "init", "--agent", "copilot", "--ledger", str(tmp_path / "example-ledger.db"))
    cli(config, "machine", "configure", "--cpus", "2", "--memory-mb", "4096", "--headroom-mb", "0")
    submitted = cli(config, "batch-submit", "--file", str(project / "batch.json"))
    assert len(submitted["items"]) == 3 and submitted["failed"] == 0
    cli(config, "run", "--no-agent")
    from xgenius.state import State
    state = State.open(project / ".xgenius")
    with state.db.read() as conn:
        means = sorted(json.loads(row[0])["metrics"]["mean"] for row in conn.execute(
            "SELECT metadata FROM observations WHERE kind='metrics'"))
    assert means == [4, 4, 5]
    report = cli(config, "report", "--no-agent")
    assert report
    assert state.campaign()["invocations"] == 0
    cli(config, "stop")
    archived = cli(config, "reset", "--confirm", state.id)
    assert (Path(archived["archive"]) / "xgenius.db").is_file()
    assert not (project / ".xgenius").exists()


def test_lost_wsl_supervisor_retains_live_guest_ownership(tmp_path):
    config, state, ledger = prepare(tmp_path)
    runner = asdict(config.runners["ubuntu"])
    backends.validate_runner(runner)
    attempt = submit(state, config, kind="ubuntu", hold=True)
    output = Path(json.loads(attempt["spec"])["output"])
    try:
        cli(config, "run", "--once", "--no-agent")
        await_condition(lambda: (output / "ready.json").exists())
        with state.db.read() as conn:
            envelope = LaunchEnvelope.parse(json.loads(conn.execute("SELECT envelope FROM launches").fetchone()[0]))
        handle = read_json(worker.launch_directory(envelope) / "main" / "payload-handle.json")
        kill = (
            "import os,signal,sys;from pathlib import Path;"
            "fd=os.pidfd_open(int(sys.argv[1]));"
            "assert Path('/proc/'+sys.argv[1]+'/stat').read_text().rsplit(')',1)[1].split()[19]==sys.argv[2];"
            "assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==sys.argv[3];"
            "signal.pidfd_send_signal(fd,signal.SIGKILL);os.close(fd)")
        backends.command(["wsl", "-d", runner["distro"], "--exec", runner["python"], "-c", kill,
                          str(handle["pid"]), handle["start_ticks"], handle["boot_id"]])
        Campaign(state=state, ledger=ledger).reconcile()
        child = read_json(worker.launch_directory(envelope) / "main" / "process-handle.json")
        probe = (
            "import sys;from pathlib import Path;"
            "v=Path('/proc/'+sys.argv[1]+'/stat').read_text().rsplit(')',1)[1].split();"
            "assert v[19]==sys.argv[2] and v[0]!='Z'")
        backends.command(["wsl", "-d", runner["distro"], "--exec", runner["python"], "-c", probe,
                          str(child["pid"]), child["start_ticks"]])
        assert backends.inspect_payload(envelope) != "dead"
        assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    finally:
        (output / "release").touch()
        finish(state, ledger)
    assert state.attempt(attempt["id"])["status"] == "interrupted"


def test_external_owned_container_loss_is_not_success(tmp_path):
    config, state, ledger = prepare(tmp_path)
    attempt = submit(state, config, kind="container", hold=True)
    output = Path(json.loads(attempt["spec"])["output"])
    try:
        cli(config, "run", "--once", "--no-agent")
        await_condition(lambda: (output / "ready.json").exists())
        with state.db.read() as conn:
            envelope = LaunchEnvelope.parse(json.loads(conn.execute("SELECT envelope FROM launches").fetchone()[0]))
        container = backends.docker_state(envelope)
        backends.command([*backends.docker_prefix(envelope.metadata["runner"]), "kill", container["id"]])
        finish(state, ledger)
        assert state.attempt(attempt["id"])["status"] == "interrupted"
    finally:
        (output / "release").touch()
        finish(state, ledger)
        cleanup_docker(state)


def test_two_real_campaigns_share_capacity_and_distinct_native_cpu_sets(tmp_path):
    from xgenius.config import initial_config
    from xgenius.state import State
    config, state, ledger = prepare(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    raw = initial_config("other")
    other_config = parse_config(raw, other_root / "xgenius.toml")
    other_root.joinpath("xgenius.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")
    other_root.joinpath("experiment.py").write_text(TASK, encoding="utf-8")
    other_root.joinpath("research_goal.md").write_text("Synthetic shared-capacity check.", encoding="utf-8")
    other = State.create(other_config, ledger.path)
    other.bind_ledger(ledger.path, ledger.id)
    other.source("goal", other_root.joinpath("research_goal.md").read_bytes(), origin="operator", head="goal")
    a, b = submit(state, config, hold=True), submit(other, other_config, hold=True)
    outputs = [Path(json.loads(value["spec"])["output"]) for value in (a, b)]
    try:
        cli(config, "run", "--once", "--no-agent")
        cli(other_config, "run", "--once", "--no-agent")
        await_condition(lambda: all((path / "ready.json").exists() for path in outputs))
        active = [row for row in ledger.rows() if row["state"] == "granted"]
        assert len(active) == 2 and sum(row["cpus"] for row in active) == 2
        assert not set(json.loads(active[0]["native_cpus"])) & set(json.loads(active[1]["native_cpus"]))
        state.cancel_attempt(a["id"])
        await_condition(lambda: (Campaign(state=state, ledger=ledger).reconcile(),
            state.attempt(a["id"])["status"] == "cancelled")[1])
        assert other.attempt(b["id"])["status"] in ("running", "starting")
        assert len([row for row in ledger.rows() if row["state"] == "granted"]) == 1
    finally:
        for output in outputs:
            (output / "release").touch()
        for value in (state, other):
            await_condition(lambda: (Campaign(state=value, ledger=ledger).reconcile(), not value.active_launches())[1])
    assert other.attempt(b["id"])["status"] == "completed" and not ledger.rows()
