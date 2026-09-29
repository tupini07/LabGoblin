"""Local research contracts and real owned-process lifecycle tests."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import tomli_w

from xgenius.campaign import Campaign
from xgenius.config import load_config
from xgenius.local_cli import initialize
from xgenius.local_config import Runner
from xgenius.scheduler import ResourceLedger
from xgenius.state import TERMINAL
from xgenius.db import _connect
from xgenius.backends import alive
from xgenius.processes import background_options


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("XGENIUS_RESOURCE_DB", str(tmp_path / "machine.db"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    initialize(argparse.Namespace(force=False, agent="copilot"))
    ResourceLedger().configure(4, 4096, [], 0)
    return Campaign(load_config(str(project / "xgenius.toml")))


def request(**overrides):
    return {"key": "example", "argv": ["python", "-c", "print('hello')"],
            "cpus": 1, "memory_mb": 128, "seconds": 10, **overrides}


def wait(campaign, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        campaign.reconcile()
        value = campaign.state.attempt(job_id)
        if value["status"] in TERMINAL or value["status"] == "recovery_required":
            return value
        time.sleep(0.1)
    raise AssertionError(f"Job did not finish: {campaign.state.attempt(job_id)}")


def test_local_init_and_legacy_config(campaign, tmp_path):
    assert campaign.local.default_runner == "native"
    assert campaign.local.command == ["copilot", "--allow-all"]
    legacy = tmp_path / "legacy.toml"
    legacy.write_text("", encoding="utf-8")
    assert load_config(str(legacy)).local is None


def test_submit_replay(campaign):
    first = campaign.submit(request())
    assert campaign.submit(request()) == first
    with pytest.raises(ValueError, match="Idempotency"):
        campaign.submit(request(argv=["python", "-c", "print('different')"]))
    assert len(campaign.state.attempts()) == 1


@pytest.mark.parametrize("code,expected", [("print('hello')", "completed"),
                                          ("import sys; sys.exit(7)", "failed")])
def test_real_native_process(campaign, code, expected):
    job_id = campaign.submit(request(argv=["python", "-c", code]))
    campaign.dispatch()
    result = wait(campaign, job_id)
    assert result["status"] == expected, result["reason"]
    assert not campaign.ledger.rows()
    if expected == "completed":
        root = Path(json.loads(result["spec"])["root"])
        assert (root / "stdout.log").read_text().strip() == "hello"
        assert result["exit_code"] == 0
    else:
        assert result["exit_code"] == 7


def test_timeout(campaign):
    job_id = campaign.submit(request(argv=["python", "-c", "import time; time.sleep(30)"], seconds=1))
    campaign.dispatch()
    assert wait(campaign, job_id)["status"] == "timed_out"


def test_cancel_queued(campaign):
    job_id = campaign.submit(request())
    campaign.cancel(job_id)
    assert campaign.state.attempt(job_id)["status"] == "cancelled"
    assert not campaign.ledger.rows()


def test_snapshot_and_artifact(campaign):
    source = campaign.project / "experiment.py"
    source.write_text(
        "from pathlib import Path\nimport os\n"
        "Path(os.environ['XGENIUS_OUTPUT_DIR'], 'metrics.json').write_text('{\"score\": 42}')\n",
        encoding="utf-8")
    job_id = campaign.submit(request(
        argv=["python", "experiment.py"], source_files=["experiment.py"], artifacts=["metrics.json"]))
    source.write_text("raise RuntimeError('Changed after submission')", encoding="utf-8")
    campaign.dispatch()
    result = wait(campaign, job_id)
    assert result["status"] == "completed", result
    metadata = Path(json.loads(result["spec"])["root"]) / "artifacts.json"
    assert "42" in metadata.read_text()


def test_missing_artifact_is_not_scientific_success(campaign):
    job_id = campaign.submit(request(artifacts=["missing.json"]))
    campaign.dispatch()
    result = wait(campaign, job_id)
    assert result["status"] == "completed", result
    assert "Artifact validation failed" in result["reason"]
    assert any(e["kind"] == "validation_failed" for e in campaign.state.pending_events())


def test_two_campaign_reservations(campaign, tmp_path):
    first = campaign.submit(request(memory_mb=2048))
    root = tmp_path / "other"
    root.mkdir()
    path = root / "xgenius.toml"
    path.write_bytes(Path(campaign.config.config_path).read_bytes())
    other = Campaign(load_config(str(path)))
    second = other.submit(request())
    campaign.ledger.register(campaign.state, json.loads(campaign.state.attempt(first)["spec"]))
    other.ledger.register(other.state, json.loads(other.state.attempt(second)["spec"]))
    assert campaign.ledger.reserve(first)
    assert campaign.ledger.reserve(second)
    campaign.cancel(first)
    assert other.state.attempt(second)["status"] == "queued"
    other.cancel(second)
    assert not campaign.ledger.rows()


@pytest.mark.parametrize("value", [0, -1, float("nan"), True])
def test_invalid_resources_rejected(campaign, value):
    with pytest.raises(ValueError):
        campaign.submit(request(cpus=value))


def test_output_escape_rejected(campaign):
    with pytest.raises(ValueError, match="escapes"):
        campaign.submit(request(artifacts=["../../outside.txt"]))


def test_cli_native_loop(campaign):
    job_id = campaign.submit(request())
    result = subprocess.run([sys.executable, "-m", "xgenius.cli", "run", "--no-agent", "--json"],
                            cwd=campaign.project, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert campaign.state.attempt(job_id)["status"] == "completed"
    json.loads(result.stdout)


@pytest.mark.skipif(os.environ.get("XGENIUS_INTEGRATION") != "1",
                    reason="Opt-in WSL/Docker execution requires prepared local tools/images")
@pytest.mark.parametrize("runner", [
    Runner(kind="wsl", python="python3", distro="Ubuntu"),
    Runner(kind="docker", python="python", image="python:3.11-slim", context="desktop-linux"),
])
def test_real_linux_runners(campaign, runner):
    campaign.local.runners["linux"] = runner
    source = campaign.project / "experiment.py"
    source.write_text(
        "from pathlib import Path\nimport os\n"
        "Path(os.environ['XGENIUS_OUTPUT_DIR'], 'metrics.json').write_text('{\"score\": 42}')\n",
        encoding="utf-8")
    job_id = campaign.submit(request(
        runner="linux", argv=["python", "experiment.py"],
        source_files=["experiment.py"], artifacts=["metrics.json"]))
    campaign.dispatch()
    result = wait(campaign, job_id)
    assert result["status"] == "completed", result["reason"]
    assert result["exit_code"] == 0
    root = Path(json.loads(result["spec"])["root"])
    assert "42" in (root / "artifacts.json").read_text()
    if runner.kind == "docker":
        info = json.loads((root / "backend.json").read_text())
        subprocess.run(["docker", "--context", runner.context, "rm", info["container_id"]], check=True,
                       capture_output=True)


def test_controller_process_exit_keeps_job_alive(campaign):
    job_id = campaign.submit(request(argv=["python", "-c", "import time; time.sleep(2); print('survived')"]))
    child = subprocess.run(
        [sys.executable, "-m", "xgenius.cli", "run", "--once", "--no-agent", "--json"],
        cwd=campaign.project, capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stdout + child.stderr
    result = wait(campaign, job_id)
    assert result["status"] == "completed", result["reason"]
    root = Path(json.loads(result["spec"])["root"])
    assert "survived" in (root / "stdout.log").read_text()


def test_validator_failure(campaign):
    job_id = campaign.submit(request(validators=[[sys.executable, "-c", "raise SystemExit(3)"]]))
    campaign.dispatch()
    result = wait(campaign, job_id)
    assert result["status"] == "completed", result["reason"]
    assert "Validator 0" in result["reason"]


def configure_agent(campaign, source, **options):
    import tomllib
    script = campaign.project / "fake_agent.py"
    script.write_text(source, encoding="utf-8")
    path = Path(campaign.config.config_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    data["agent"].update(command=[sys.executable, str(script)], **options)
    path.write_bytes(tomli_w.dumps(data).encode("utf-8"))
    return Campaign(load_config(str(path)))


def agent_source(disposition="complete"):
    return (
        "import json, pathlib, sqlite3\n"
        "root=pathlib.Path('.xgenius')\n"
        "with sqlite3.connect(root/'xgenius.db') as c:\n"
        "    tid,events=c.execute(\"SELECT id,events FROM turns ORDER BY started DESC LIMIT 1\").fetchone()\n"
        "(root/'journal.md').write_text('Observed evidence '+tid,encoding='utf-8')\n"
        f"result=dict(turn_id=tid,acknowledged_events=json.loads(events),disposition={disposition!r},"
        "reason='Synthetic decision',journal='.xgenius/journal.md')\n"
        "(root/'turns'/tid/'result.json').write_text(json.dumps(result),encoding='utf-8')\n"
    )


def test_real_agent_supervisor_protocol(campaign):
    campaign = configure_agent(campaign, agent_source())
    campaign.run()
    assert campaign.state.campaign()["state"] == "completed"
    assert not campaign.state.pending_events()
    with _connect(campaign.state.path) as c:
        assert c.execute("SELECT COUNT(*) FROM turns WHERE state='completed'").fetchone()[0] == 1


def test_agent_timeout_is_bounded(campaign):
    campaign = configure_agent(campaign, "import time; time.sleep(30)", timeout_seconds=0.5, retries=0)
    campaign.run()
    assert campaign.state.campaign()["state"] == "blocked"
    assert campaign.state.pending_events()
    receipt = next((campaign.state.root / "turns").glob("*/agent-completion.json"))
    assert "walltime" in json.loads(receipt.read_text())["error"]


def test_wait_without_work_blocks(campaign):
    campaign = configure_agent(campaign, agent_source("wait"))
    campaign.run()
    assert campaign.state.campaign()["state"] == "blocked"


def test_wait_handles_completions_arriving_during_turn(campaign):
    campaign = configure_agent(campaign, agent_source("wait"))
    turn_id, process, directory = campaign._begin_turn()
    campaign.state.event("completion", {"attempt_id": "completed-during-turn"}, "late-completion")
    deadline = time.monotonic() + 15
    while process.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    campaign._end_turn(turn_id, process, directory)
    assert campaign.state.campaign()["state"] == "running"
    assert [e["id"] for e in campaign.state.pending_events()] == ["late-completion"]


@pytest.mark.parametrize("case,expected", [
    ("supervisor-alive", "alive"), ("child-alive", "alive"), ("descendant-alive", "alive"),
    ("validator-alive", "alive"), ("input-validator-alive", "alive"), ("validator-gone", "dead"),
    ("gone", "dead"), ("reboot", "dead"), ("missing-handle", "unknown"), ("recycled-pid", "unknown"),
])
def test_linux_session_liveness_after_supervisor_loss(tmp_path, case, expected):
    from xgenius.payload import inspect_linux
    root = tmp_path / "attempt"
    root.mkdir()
    proc = tmp_path / "proc"
    boot = proc / "sys" / "kernel" / "random" / "boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("new-boot" if case == "reboot" else "original-boot")
    for filename, pid in (("payload-handle.json", 10), ("process-handle.json", 20)):
        if case == "missing-handle" and pid == 20:
            continue
        (root / filename).write_text(json.dumps({
            "pid": pid, "start_ticks": "100", "boot_id": "original-boot", "token": "owned",
        }))
    def process(pid, group, ticks="100"):
        directory = proc / str(pid)
        directory.mkdir()
        fields = ["S", "1", str(group), str(group)] + ["0"] * 15 + [ticks]
        (directory / "stat").write_text(f"{pid} (fixture) " + " ".join(fields))
    if case == "supervisor-alive":
        process(10, 10)
    elif case == "child-alive":
        process(20, 20)
    elif case == "descendant-alive":
        process(30, 20)
    elif case == "recycled-pid":
        process(20, 20, "200")
    elif case in ("validator-alive", "input-validator-alive", "validator-gone"):
        directory = root / ("input-validator-0" if case == "input-validator-alive" else "validator-0")
        directory.mkdir()
        (directory / "payload-handle.json").write_bytes((root / "payload-handle.json").read_bytes())
        (directory / "process-handle.json").write_text(json.dumps({
            "pid": 40, "start_ticks": "100", "boot_id": "original-boot", "token": "owned",
        }))
        if case != "validator-gone":
            process(40, 40)
    assert inspect_linux(root, "owned", proc) == expected


def test_agent_must_update_journal(campaign):
    campaign = configure_agent(campaign, agent_source().replace(
        "(root/'journal.md').write_text('Observed evidence '+tid,encoding='utf-8')", "pass"), retries=0)
    campaign.run()
    assert campaign.state.campaign()["state"] == "blocked"
    assert campaign.state.pending_events()


def test_event_batch_does_not_ack_late_completions(campaign):
    source = agent_source().replace(
        "result=dict(",
        "from xgenius.state import LocalState\nfrom xgenius.config import load_config\n"
        "LocalState(load_config()).event('late', {})\nresult=dict(")
    campaign = configure_agent(campaign, source)
    campaign.run()
    assert [e["kind"] for e in campaign.state.pending_events()] == ["late"]


def test_pause_then_stop_preserves_other_campaign(campaign):
    job_id = campaign.submit(request())
    campaign.control("pause")
    campaign.dispatch()
    assert campaign.state.attempt(job_id)["status"] == "queued"
    campaign.control("stop")
    assert campaign.state.attempt(job_id)["status"] == "cancelled"


def test_unknown_launch_keeps_reservation(campaign):
    job_id = campaign.submit(request())
    spec = json.loads(campaign.state.attempt(job_id)["spec"])
    campaign.ledger.register(campaign.state, spec)
    assert campaign.ledger.reserve(job_id)
    assert campaign.state.claim(job_id)
    campaign.reconcile()
    assert campaign.state.attempt(job_id)["status"] == "recovery_required"
    assert campaign.ledger.rows()[0]["state"] == "reserved"
    assert not campaign.state.claim(job_id)


def test_pid_reuse_is_not_alive():
    import psutil
    assert not alive({"pid": os.getpid(), "created": psutil.Process().create_time() - 1})


def test_killed_controller_does_not_kill_payload(campaign):
    job_id = campaign.submit(request(argv=["python", "-c", "import time; time.sleep(3); print('survived')"]))
    controller = subprocess.Popen(
        [sys.executable, "-m", "xgenius.cli", "run", "--no-agent", "--json"],
        cwd=campaign.project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while campaign.state.attempt(job_id)["status"] != "running":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        controller.kill()
        controller.wait(timeout=5)
        assert wait(campaign, job_id)["status"] == "completed"
        assert len(campaign.state.attempts()) == 1
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows console/parent Job Object ownership")
def test_parent_job_closure_keeps_worker_alive(campaign):
    job_id = campaign.submit(request(argv=["python", "-c", "import time;time.sleep(3);print('survived job closure')"]))
    code = (
        "import win32api,win32job,time;from xgenius.config import load_config;"
        "from xgenius.campaign import Campaign;"
        "job=win32job.CreateJobObject(None,'');"
        "info=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation);"
        "info['BasicLimitInformation']['LimitFlags']=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|"
        "win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK;"
        "win32job.SetInformationJobObject(job,win32job.JobObjectExtendedLimitInformation,info);"
        "win32job.AssignProcessToJobObject(job,win32api.GetCurrentProcess());"
        "Campaign(load_config()).dispatch();time.sleep(30)"
    )
    controller = subprocess.Popen([sys.executable, "-c", code], cwd=campaign.project,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  **background_options())
    try:
        deadline = time.monotonic() + 15
        while campaign.state.attempt(job_id)["status"] != "running":
            assert controller.poll() is None, controller.communicate()[1]
            assert time.monotonic() < deadline, campaign.state.attempt(job_id)
            time.sleep(0.1)
        controller.kill()
        controller.wait(timeout=5)
        result = wait(campaign, job_id)
        assert result["status"] == "completed", result["reason"]
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)


def test_cancel_validator_propagates(campaign):
    job_id = campaign.submit(request(validators=[["python", "-c", "import time; time.sleep(20)"]]))
    campaign.dispatch()
    root = Path(json.loads(campaign.state.attempt(job_id)["spec"])["root"])
    deadline = time.monotonic() + 10
    while not (root / "validator-0" / "payload-handle.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.1)
    campaign.cancel(job_id)
    assert wait(campaign, job_id)["status"] == "cancelled"


def test_argument_fidelity(campaign):
    args = ["", 'a "quoted" word', r"C:\path with spaces\file", "\u03bb"]
    job_id = campaign.submit(request(argv=["python", "-c", "import sys,json;print(json.dumps(sys.argv[1:]))", *args]))
    campaign.dispatch()
    result = wait(campaign, job_id)
    root = Path(json.loads(result["spec"])["root"])
    assert json.loads((root / "stdout.log").read_text()) == args


def test_legacy_history_survives_local_schema(campaign):
    from xgenius.state import LocalState
    db = campaign.state.db
    db.record_job("legacy", "cluster", "old", "", "python old.py")
    db.mark_results_pulled("legacy")
    state = LocalState(campaign.config)
    assert state.db.get_job("legacy")["results_pulled"] == 1
    assert len(list(state.root.glob("xgenius.db.before-local-v2-*"))) == 1


def test_reset_archives_evidence(campaign):
    from xgenius.cli import cmd_reset
    old_id = campaign.state.id
    (campaign.state.root / "journal.md").write_text("keep me", encoding="utf-8")
    cmd_reset(argparse.Namespace(config=campaign.config.config_path, json=True))
    assert Campaign(campaign.config).state.id != old_id
    assert next((campaign.project / ".xgenius-archives").glob("*/journal.md")).read_text() == "keep me"


def test_maintenance_counts_and_has_durable_exit(campaign):
    from xgenius.agent import run_agent
    campaign = configure_agent(campaign, "print('maintenance')")
    result = run_agent(campaign.config, "synthetic", capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "maintenance"
    with _connect(campaign.state.path) as c:
        assert c.execute("SELECT kind,state FROM turns").fetchone()[:] == ("maintenance", "completed")


def test_input_validator_refuses_payload(campaign):
    job_id = campaign.submit(request(
        argv=["python", "-c", "raise RuntimeError('payload must not run')"],
        input_validators=[["python", "-c", "raise SystemExit(9)"]]))
    campaign.dispatch()
    result = wait(campaign, job_id)
    assert result["status"] == "failed"
    assert "Input validator 0 refused" in result["reason"]
    assert result["exit_code"] is None


def test_shared_gpu_reservation_is_atomic(campaign, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    import xgenius.campaign as controller
    monkeypatch.setattr(controller, "validate_runner", lambda *args: None)
    campaign.ledger.configure(2, 4096, ["GPU-test"], 0)
    campaign.local.gpus = ["GPU-test"]
    campaign.local.max_gpu_hours = 1
    root = tmp_path / "second-project"
    root.mkdir()
    path = root / "xgenius.toml"
    path.write_bytes(Path(campaign.config.config_path).read_bytes())
    other = Campaign(load_config(str(path)))
    other.local.gpus = ["GPU-test"]
    other.local.max_gpu_hours = 1
    ids = [c.submit(request(gpus=["GPU-test"])) for c in (campaign, other)]
    for c, job_id in zip((campaign, other), ids):
        c.ledger.register(c.state, json.loads(c.state.attempt(job_id)["spec"]))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(campaign.ledger.reserve, ids))
    assert sum(results) == 1
    assert sum(r["state"] == "reserved" for r in campaign.ledger.rows()) == 1
    winner = results.index(True)
    (campaign, other)[winner].cancel(ids[winner])
    assert campaign.ledger.reserve(ids[1 - winner])
    (campaign, other)[1 - winner].cancel(ids[1 - winner])


def test_native_timeout_kills_descendants(campaign):
    import psutil
    code = (
        "import subprocess,sys,time,os;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "Path(os.environ['XGENIUS_OUTPUT_DIR'],'child').write_text(str(p.pid));time.sleep(30)"
    )
    job_id = campaign.submit(request(argv=["python", "-c", code], seconds=1))
    campaign.dispatch()
    result = wait(campaign, job_id)
    assert result["status"] == "timed_out"
    output = Path(json.loads(result["spec"])["output"])
    pid = int((output / "child").read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_killed_controller_reconnects_same_agent(campaign):
    campaign = configure_agent(campaign, "import time;time.sleep(3)\n" + agent_source())
    controller = subprocess.Popen(
        [sys.executable, "-m", "xgenius.cli", "run", "--json"],
        cwd=campaign.project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while True:
            with _connect(campaign.state.path) as c:
                row = c.execute("SELECT handle FROM turns").fetchone()
            if row and row["handle"]:
                break
            assert time.monotonic() < deadline
            time.sleep(0.1)
        controller.kill()
        controller.wait(timeout=5)
        campaign.run()
        assert campaign.state.campaign()["state"] == "completed"
        with _connect(campaign.state.path) as c:
            assert c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)


@pytest.mark.skipif(os.environ.get("XGENIUS_INTEGRATION") != "1",
                    reason="Requires prepared local Ubuntu and Docker image")
@pytest.mark.parametrize("kind", ["wsl", "docker"])
@pytest.mark.parametrize("mode", ["nonzero", "timeout", "cancel", "readonly"])
def test_linux_failure_modes(campaign, kind, mode):
    runner = Runner(kind="wsl", distro="Ubuntu", python="python3") if kind == "wsl" else Runner(
        kind="docker", image="python:3.11-slim", python="python", context="desktop-linux")
    if kind == "wsl" and mode == "readonly":
        pytest.skip("Native/WSL trusted mode is not a filesystem sandbox")
    campaign.local.runners["linux"] = runner
    seconds = 1 if mode == "timeout" else 15
    expected = {"nonzero": "failed", "timeout": "timed_out", "cancel": "cancelled", "readonly": "completed"}[mode]
    code = ("raise SystemExit(7)" if mode == "nonzero" else
            "import time;time.sleep(10)" if mode in ("timeout", "cancel") else
            "from pathlib import Path\n"
            "for name in ['/source/no.txt','/attempt/source/no.txt']:\n"
            "    try: Path(name).write_text('forbidden')\n"
            "    except OSError: pass\n"
            "    else: raise RuntimeError('source mount is writable')\n")
    job_id = campaign.submit(request(runner="linux", argv=["python", "-c", code], seconds=seconds))
    root = Path(json.loads(campaign.state.attempt(job_id)["spec"])["root"])
    try:
        campaign.dispatch()
        if mode == "cancel":
            deadline = time.monotonic() + 10
            while not (root / "payload-handle.json").exists():
                assert time.monotonic() < deadline
                time.sleep(0.1)
            campaign.cancel(job_id)
        result = wait(campaign, job_id)
        assert result["status"] == expected, result["reason"]
        assert not campaign.ledger.rows()
    finally:
        if kind == "docker" and (root / "backend.json").exists():
            info = json.loads((root / "backend.json").read_text())
            subprocess.run(["docker", "--context", runner.context, "rm", "-f", info["container_id"]],
                           check=True, capture_output=True)


def test_results_export_does_not_overwrite_manual_csv(campaign):
    from xgenius.results import export_registered
    manual = campaign.project / "results" / "experiments.csv"
    manual.write_text("user,owned\n", encoding="utf-8")
    job_id = campaign.submit(request())
    campaign.cancel(job_id)
    path = Path(export_registered(campaign.config))
    first = path.read_bytes()
    assert export_registered(campaign.config) == str(path)
    assert path.read_bytes() == first
    assert manual.read_text() == "user,owned\n"


def test_slurm_ids_and_paths_are_cluster_specific(tmp_path):
    from xgenius.config import XGeniusConfig, ClusterConfig
    from xgenius.jobs import JobManager
    from xgenius.templates import build_params_from_cluster
    config = XGeniusConfig(config_path=str(tmp_path / "xgenius.toml"), clusters={
        name: ClusterConfig(name, "host", "user", "/project", "/scratch", "/images")
        for name in ("a", "b")
    })
    manager = JobManager(config)
    assert manager._record_job("42", "a", "exp", "", "python train.py") == "a:42"
    assert manager._record_job("42", "b", "exp", "", "python train.py") == "b:42"
    with pytest.raises(ValueError, match="Ambiguous"):
        manager.db.get_job("42")
    assert manager.db.get_job("42", "a")["job_id"] == "a:42"
    params = build_params_from_cluster(config.clusters["a"], "image.sif")
    assert params["IMAGE_PATH"] == "/images/image.sif"
    assert params["LOG_FILE"] == "/scratch/.xgenius/logs/{{EXPERIMENT_ID}}_%j.out"


def test_sqlite_budget_counts_pending_and_failed_jobs(campaign):
    from xgenius.safety import SafetyValidator
    db = campaign.state.db
    db.record_job("old", "cluster", "old", "", "python old.py", gpus=2, walltime="01:00:00")
    db.update_job_status("old", "failed", gpu_hours=2)
    db.record_job("pending", "cluster", "next", "", "python next.py", gpus=1, walltime="02:00:00")
    db.update_job_status("pending", "pending")
    budget = SafetyValidator(campaign.config).get_budget()
    assert budget.gpu_hours_used == 2 and budget.gpu_hours_reserved == 2
    assert budget.active_jobs == 1
    assert not db.is_hypothesis_complete("")
