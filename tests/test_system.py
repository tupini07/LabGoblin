"""Opt-in, real CLI system tests on a prepared Windows/WSL/Docker workstation."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid

import pytest
import tomli_w


pytestmark = pytest.mark.skipif(
    os.environ.get("XGENIUS_SYSTEM_E2E") != "1" or os.name != "nt",
    reason="Explicit full-system opt-in requires prepared Windows, Ubuntu and Docker Desktop",
)

TASK = """import json,os,sys,time
from pathlib import Path
out=Path(os.environ['XGENIUS_OUTPUT_DIR'])
assert 'XGENIUS_TEST_CREDENTIAL' not in os.environ
assert os.environ['EXPLICIT_VALUE']=='preserved'
values=json.loads(Path(os.environ['XGENIUS_INPUT_DATA']).read_text())['values']
ready={'pid':os.getpid(),'label':sys.argv[1:],'platform':os.name}
if os.name!='nt':
    ready['start_ticks']=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
    ready['boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
(out/'ready.json').write_text(json.dumps(ready))
if 'hold' in sys.argv:
    deadline=time.monotonic()+45
    while not (out/'release').exists():
        assert time.monotonic()<deadline,'fixture release was never signalled'
        time.sleep(.1)
if 'orphan' in sys.argv:
    time.sleep(8)
metrics={'mean':sum(values)/len(values),'count':len(values)}
(out/'metrics.json').write_text(json.dumps(metrics))
print(json.dumps(metrics),flush=True)
"""


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2), encoding="utf-8")


class System:
    def __init__(self, root):
        self.root = root
        self.env = {**os.environ, "XGENIUS_RESOURCE_DB": str(root / "resources.db"),
                    "XGENIUS_TEST_CREDENTIAL": "synthetic-not-a-real-credential", "PYTHONUTF8": "1"}
        self.processes = []
        self.projects = []
        self.commands = []
        self.observations = []
        self.images = []
        self.data = root / "approved data.json"
        write(self.data, {"values": [2, 4, 6]})

    def sql(self, project, query, parameters=()):
        with closing(sqlite3.connect(project / ".xgenius" / "xgenius.db", timeout=30)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(query, parameters)]

    def cli(self, project, *args, expected=0, timeout=40):
        result = subprocess.run(
            [sys.executable, "-m", "xgenius.cli", *args, "--json"], cwd=project,
            env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        self.commands.append({"project": str(project), "argv": list(args), "returncode": result.returncode,
                              "stdout": result.stdout, "stderr": result.stderr})
        write(self.root / "commands.json", self.commands)
        assert result.returncode == expected, self.commands[-1]
        return json.loads(result.stdout)

    def start(self, project, *args):
        name = f"controller-{len(self.processes)}"
        with (project / (name + ".out")).open("wb") as out, (project / (name + ".err")).open("wb") as err:
            process = subprocess.Popen(
                [sys.executable, "-m", "xgenius.cli", *args, "--json"],
                cwd=project, env=self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        self.processes.append(process)
        return process

    def project(self, name):
        root = self.root / name
        root.mkdir()
        self.projects.append(root)
        self.cli(root, "init", "--agent", "copilot")
        path = root / "xgenius.toml"
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        raw["runners"].update(
            ubuntu={"kind": "wsl", "distro": "Ubuntu", "python": "python3"},
            container={"kind": "docker", "context": "desktop-linux",
                       "image": "python:3.11-slim", "python": "python", "network": False})
        raw["campaign"].update(cpus=2, memory_mb=2048, max_jobs=2, max_seconds=1200)
        raw["agent"].update(max_turns=8, timeout_seconds=240, retries=0)
        raw["inputs"] = {"data": {"path": str(self.data), "identity": "synthetic-data-v1",
                                 "sha256": hashlib.sha256(self.data.read_bytes()).hexdigest(),
                                 "prompt_access": False}}
        path.write_bytes(tomli_w.dumps(raw).encode("utf-8"))
        (root / "task.py").write_text(TASK, encoding="utf-8")
        if len(self.projects) == 1:
            self.cli(root, "machine", "configure", "--cpus", "2", "--memory-mb", "4096",
                     "--headroom-mb", "1024")
        self.cli(root, "doctor")
        return root

    def manifest(self, project, key, runner, mode="", **extra):
        spec = {"key": key, "runner": runner, "argv": ["python", "task.py", mode],
                "source_files": ["task.py"], "cpus": 2, "memory_mb": 128, "seconds": 60,
                "environment": {"EXPLICIT_VALUE": "preserved"}, "artifacts": ["metrics.json"], **extra}
        path = project / (key + ".json")
        write(path, spec)
        return path

    def submit(self, project, key, runner, mode="", **extra):
        path = self.manifest(project, key, runner, mode, **extra)
        return self.cli(project, "submit", "--spec", str(path))["job_id"]

    def attempt(self, project, job_id):
        return self.sql(project, "SELECT a.*,j.status FROM attempts a JOIN jobs j ON a.id=j.job_id WHERE a.id=?",
                        (job_id,))[0]

    def output(self, project, job_id):
        return Path(json.loads(self.attempt(project, job_id)["spec"])["output"])

    def observe(self):
        with closing(sqlite3.connect(self.env["XGENIUS_RESOURCE_DB"], timeout=30)) as c:
            specs = [json.loads(r[0]) for r in c.execute(
                "SELECT spec FROM reservations WHERE state IN ('reserved','running')")]
        value = {"at": time.time(), "attempts": [s["id"] for s in specs],
                 "cpus": sum(s["cpus"] for s in specs), "memory_mb": sum(s["memory_mb"] for s in specs)}
        self.observations.append(value)
        write(self.root / "reservations.json", self.observations)
        assert value["cpus"] <= 2 and value["memory_mb"] <= 4096, value
        return value

    def wait(self, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.observe()
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        raise AssertionError("System condition not reached; inspect " + str(self.root))

    def finished(self, project, job_id):
        self.cli(project, "reconcile")
        return self.attempt(project, job_id)["status"] in (
            "completed", "failed", "timed_out", "cancelled", "interrupted")

    def cleanup(self):
        for project in self.projects:
            for path in (project / ".xgenius" / "attempts").glob("*/spec.json"):
                spec = read(path)
                (Path(spec["root"]) / "cancel").touch()
                (Path(spec["output"]) / "release").touch()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for project in self.projects:
            for path in project.rglob("backend.json"):
                info = read(path)
                result = subprocess.run(["docker", "--context", "desktop-linux", "inspect", info["container_id"]],
                                        capture_output=True, text=True, timeout=20)
                if result.returncode == 0:
                    container = json.loads(result.stdout)[0]
                    spec = read(path.parent / "spec.json")
                    assert container["Config"]["Labels"]["xgenius.attempt"] == spec["id"]
                    subprocess.run(["docker", "--context", "desktop-linux", "rm", "-f", info["container_id"]],
                                   capture_output=True, check=True, timeout=20)
        for image, token in self.images:
            result = subprocess.run(
                ["docker", "--context", "desktop-linux", "image", "inspect", image],
                capture_output=True, text=True, timeout=20)
            if result.returncode == 0:
                assert json.loads(result.stdout)[0]["Config"]["Labels"]["xgenius.system_test"] == token
                subprocess.run(["docker", "--context", "desktop-linux", "image", "rm", image],
                               capture_output=True, check=True, timeout=20)
        self.wait(lambda: self._supervisors_stopped(), timeout=70)
        for project in self.projects:
            self.cli(project, "reconcile")

    def _supervisors_stopped(self):
        from xgenius.backends import alive
        return not any(alive(read(path)) for project in self.projects
                       for name in ("supervisor.json", "agent-handle.json")
                       for path in project.rglob(name))


@pytest.fixture
def system(tmp_path):
    instance = System(tmp_path)
    try:
        yield instance
    finally:
        instance.cleanup()


def test_cli_campaigns_share_resources_and_recover(system):
    a, b = system.project("campaign A"), system.project("campaign B")
    native = system.submit(a, "native", "native", "hold")
    wsl = system.submit(a, "wsl", "ubuntu")
    docker = system.submit(b, "docker", "container")
    assert system.cli(a, "submit", "--spec", str(a / "native.json"))["job_id"] == native
    (a / "task.py").write_text("raise RuntimeError('mutated source must not run')", encoding="utf-8")
    controller_a = system.start(a, "run", "--no-agent")
    system.wait(lambda: (system.output(a, native) / "ready.json").exists())
    controller_b = system.start(b, "run", "--no-agent")
    system.wait(lambda: any(row["state"] == "queued" for row in system.cli(b, "machine", "status")["reservations"]))
    assert system.attempt(b, docker)["status"] == "queued"
    system.cli(b, "pause")
    assert controller_b.wait(timeout=15) == 0
    system.cli(a, "reset", expected=1)
    controller_a.kill()
    controller_a.wait(timeout=10)
    assert system.attempt(a, native)["status"] == "running"
    (system.output(a, native) / "release").touch()
    system.wait(lambda: system.finished(a, native))
    assert system.attempt(a, wsl)["status"] == "queued"
    assert system.attempt(b, docker)["status"] == "queued"
    assert system.start(a, "resume", "--no-agent").wait(timeout=60) == 0
    assert system.start(b, "resume", "--no-agent").wait(timeout=60) == 0
    for project, job_id, platform in ((a, native, "nt"), (a, wsl, "posix"), (b, docker, "posix")):
        assert system.attempt(project, job_id)["status"] == "completed"
        assert read(system.output(project, job_id) / "metrics.json") == {"mean": 4, "count": 3}
        assert read(system.output(project, job_id) / "ready.json")["platform"] == platform
        assert '"mean": 4.0' in system.cli(project, "logs", "--job-id", job_id)["output"]
        assert not system.cli(project, "errors", "--job-id", job_id)["output"]
    assert not system.cli(a, "machine", "status")["reservations"]
    assert len(system.cli(a, "results", "attempts")) == 2
    export = Path(system.cli(a, "results", "export")["path"])
    assert native in export.read_text() and wsl in export.read_text()
    system.cli(a, "steer", "SYSTEM_TEST_DIRECTIVE: preserve all accepted evidence")
    assert "SYSTEM_TEST_DIRECTIVE" in (a / ".xgenius" / "journal.md").read_text()
    assert any(e["kind"] == "directive" for e in system.cli(a, "status")["events"])

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    launcher = ("import sys,webbrowser;webbrowser.open=lambda *a,**k:False;"
                f"sys.argv=['xgenius','dashboard','--port','{port}'];"
                "from xgenius.cli import main;main()")
    server = subprocess.Popen([sys.executable, "-c", launcher], cwd=a, env=system.env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    system.processes.append(server)
    base = f"http://127.0.0.1:{port}"
    def ready():
        try:
            with urllib.request.urlopen(base, timeout=1) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError):
            return False
    system.wait(ready, timeout=15)
    for endpoint in ("/", "/jobs", "/hypotheses", "/journal", "/debug"):
        with urllib.request.urlopen(base + endpoint) as response:
            assert response.status == 200
    artifact = system.sql(a, "SELECT id,metadata FROM artifacts WHERE attempt_id=?", (native,))[0]
    with urllib.request.urlopen(base + "/artifact?id=" + artifact["id"]) as response:
        assert response.headers["Content-Disposition"] == "attachment"
        assert hashlib.sha256(response.read()).hexdigest() == json.loads(artifact["metadata"])["sha256"]
    server.kill()
    server.wait(timeout=10)
    old_id = system.cli(a, "status")["campaign"]["id"]
    reset = system.cli(a, "reset")
    assert reset["campaign_id"] != old_id
    assert (Path(reset["archive"]) / "xgenius.db").is_file()
    assert not system.cli(a, "status")["jobs"]


def test_stop_drains_only_admitted_work(system):
    a = system.project("drain")
    running = system.submit(a, "admitted", "native", "hold")
    queued = system.submit(a, "not-admitted", "ubuntu")
    controller = system.start(a, "run", "--no-agent")
    system.wait(lambda: (system.output(a, running) / "ready.json").exists())
    system.cli(a, "stop")
    assert system.attempt(a, queued)["status"] == "cancelled"
    assert system.attempt(a, running)["status"] == "running"
    (system.output(a, running) / "release").touch()
    assert controller.wait(timeout=30) == 0
    assert system.cli(a, "status")["campaign"]["state"] == "stopped"
    assert system.attempt(a, running)["status"] == "completed"
    assert not system.cli(a, "machine", "status")["reservations"]


def test_cli_partial_batch_errors_timeout_and_cancel(system):
    a = system.project("failure outcomes")
    specs = [
        read(system.manifest(a, "success", "native")),
        {"key": "bad-shape", "argv": []},
        read(system.manifest(a, "invalid-artifact", "native", artifacts=["absent.json"])),
        read(system.manifest(a, "nonzero", "native", argv=["python", "-c", "raise SystemExit(9)"], artifacts=[])),
        read(system.manifest(a, "timeout", "ubuntu", "hold", seconds=1)),
        read(system.manifest(a, "cancel", "container", "hold")),
    ]
    batch = a / "batch.json"
    write(batch, specs)
    results = system.cli(a, "batch-submit", "--file", str(batch), expected=1)
    assert len(results) == 6 and not results[1]["success"]
    assert all(results[i]["success"] for i in (0, 2, 3, 4, 5))
    controller = system.start(a, "run", "--no-agent")
    cancelled = results[5]["job_id"]
    system.wait(lambda: (system.output(a, cancelled) / "ready.json").exists(), timeout=60)
    system.cli(a, "cancel", "--job-ids", cancelled)
    assert controller.wait(timeout=30) == 1
    status = system.cli(a, "status")
    jobs = {j["id"]: j for j in status["jobs"]}
    assert jobs[results[0]["job_id"]]["status"] == "completed"
    invalid = jobs[results[2]["job_id"]]
    assert invalid["status"] == "completed" and "Artifact validation failed" in invalid["reason"]
    assert jobs[results[3]["job_id"]]["exit_code"] == 9
    assert jobs[results[4]["job_id"]]["status"] == "timed_out"
    assert jobs[cancelled]["status"] == "cancelled"
    assert any(e["kind"] == "validation_failed" for e in status["events"])
    assert not system.cli(a, "machine", "status")["reservations"]


def test_local_docker_build_and_execution(system):
    a = system.project("local build")
    token = uuid.uuid4().hex
    image = "xgenius-system:" + token
    system.images.append((image, token))
    path = a / "xgenius.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    raw["runners"]["container"]["image"] = image
    path.write_bytes(tomli_w.dumps(raw).encode("utf-8"))
    (a / "fixture-image.txt").write_text(token, encoding="utf-8")
    (a / "Dockerfile").write_text(
        f"FROM python:3.11-slim\nLABEL xgenius.system_test={token}\n"
        "COPY fixture-image.txt /fixture-image.txt\n", encoding="utf-8")
    (a / ".dockerignore").write_text("**\n!Dockerfile\n!fixture-image.txt\n", encoding="utf-8")
    assert system.cli(a, "build", "--runner", "container", timeout=120)["success"]
    system.cli(a, "doctor")
    job_id = system.submit(a, "built-image", "container", validators=[
        ["python", "-c", f"from pathlib import Path;assert Path('/fixture-image.txt').read_text()=={token!r}"]])
    system.cli(a, "run", "--no-agent", timeout=60)
    row = system.attempt(a, job_id)
    assert row["status"] == "completed" and not row["reason"]


def guest_alive(handle):
    script = (
        "from pathlib import Path;import sys;"
        "p=Path('/proc/'+sys.argv[1]+'/stat');"
        "print(int(p.exists() and p.read_text().rsplit(')',1)[1].split()[19]==sys.argv[2]"
        " and p.read_text().rsplit(')',1)[1].split()[0]!='Z'"
        " and Path('/proc/sys/kernel/random/boot_id').read_text().strip()==sys.argv[3]))"
    )
    return subprocess.check_output(
        ["wsl", "-d", "Ubuntu", "--exec", "python3", "-c", script,
         str(handle["pid"]), handle["start_ticks"], handle["boot_id"]], text=True).strip() == "1"


@pytest.mark.parametrize("phase", ["workload", "validator"])
def test_wsl_supervisor_loss_does_not_release_live_work(system, phase):
    a = system.project("lost supervisor")
    extra = {}
    if phase == "validator":
        extra["validators"] = [["python", "-c",
            "import json,os,time;from pathlib import Path;"
            "root=Path(os.environ['XGENIUS_OUTPUT_DIR']);"
            "handle=dict(pid=os.getpid(),start_ticks=Path('/proc/self/stat').read_text()"
            ".rsplit(')',1)[1].split()[19],boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip());"
            "(root/'validator-ready.json').write_text(json.dumps(handle));time.sleep(8)"]]
    job_id = system.submit(a, "guest", "ubuntu", "orphan" if phase == "workload" else "", **extra)
    system.cli(a, "run", "--once", "--no-agent")
    out = system.output(a, job_id)
    ready = out / ("ready.json" if phase == "workload" else "validator-ready.json")
    system.wait(ready.exists)
    child = read(ready)
    supervisor = read(out.parent / "payload-handle.json")
    assert guest_alive(child) and guest_alive(supervisor)
    script = (
        "from pathlib import Path;import os,signal,sys;"
        "p=Path('/proc/'+sys.argv[1]+'/stat');"
        "assert p.read_text().rsplit(')',1)[1].split()[19]==sys.argv[2];"
        "assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==sys.argv[3];"
        "os.kill(int(sys.argv[1]),signal.SIGKILL)"
    )
    subprocess.run(["wsl", "-d", "Ubuntu", "--exec", "python3", "-c", script,
                    str(supervisor["pid"]), supervisor["start_ticks"], supervisor["boot_id"]],
                   check=True, capture_output=True, timeout=10)
    try:
        system.wait(lambda: not guest_alive(supervisor), timeout=10)
        observed_live_child = False
        for _ in range(3):
            status = system.cli(a, "reconcile")
            if guest_alive(child):
                observed_live_child = True
                assert any(r["id"] == job_id and r["state"] in ("reserved", "running")
                           for r in status["reservations"]), status
                assert status["jobs"][0]["status"] in ("starting", "running", "recovery_required"), status
            time.sleep(0.3)
        assert observed_live_child, "Fixture must observe an orphan still alive during reconciliation"
    finally:
        system.wait(lambda: not guest_alive(child), timeout=15)
    system.wait(lambda: system.finished(a, job_id), timeout=15)
    assert system.attempt(a, job_id)["status"] == "interrupted"


def test_docker_kill_is_interrupted_not_success(system):
    a = system.project("container loss")
    job_id = system.submit(a, "container", "container", "hold")
    system.cli(a, "run", "--once", "--no-agent")
    out = system.output(a, job_id)
    system.wait(lambda: (out / "ready.json").exists())
    handle = read(out.parent / "backend.json")
    info = json.loads(subprocess.check_output(
        ["docker", "--context", "desktop-linux", "inspect", handle["container_id"]], text=True))[0]
    assert info["Config"]["Labels"]["xgenius.attempt"] == job_id
    subprocess.run(["docker", "--context", "desktop-linux", "kill", handle["container_id"]],
                   check=True, capture_output=True, timeout=15)
    system.wait(lambda: system.finished(a, job_id))
    assert system.attempt(a, job_id)["status"] == "interrupted"
    assert not (out.parent / "completion.json").exists()
    assert not system.cli(a, "machine", "status")["reservations"]


@pytest.mark.skipif(os.environ.get("XGENIUS_LIVE_AGENT") != "1",
                    reason="Separate opt-in for live Copilot inference and report/compact turns")
def test_live_copilot_controls_all_runners(system):
    a = system.project("live Copilot")
    keys = ["native-probe", "ubuntu-probe", "container-probe"]
    for key, runner in zip(keys, ("native", "ubuntu", "container")):
        system.manifest(a, key, runner)
    (a / "research_goal.md").write_text(
        "# Fully specified synthetic system test\n"
        "Submit each existing *-probe.json manifest unchanged and exactly once through xgenius. "
        "There are three manifests: native-probe.json, ubuntu-probe.json, container-probe.json. "
        "Handle their real completion events and inspect registered numeric metrics: all must have "
        "mean=4 and count=3. Do not inspect raw declared inputs. Never run the payload directly. "
        "Return wait only while work is active; otherwise continue to handle pending events. "
        "Complete only after verifying all three attempts, updating the journal with their IDs, "
        "and acknowledging all three completion events. Acknowledge only IDs in your assigned "
        "turn batch: if status shows a completion outside that batch, return continue so it is "
        "delivered in a subsequent turn rather than completing early. Do not edit the database.\n"
        "Do not change source/specs/config or access anything outside this synthetic fixture. "
        "No issues, PRs, commits, pushes, uploads, subagents, schedules, package installs or remote compute. "
        "Do not ask questions. Keep the report proportional to three observations.\n",
        encoding="utf-8")
    result = system.cli(a, "run", timeout=850)
    assert result["state"] == "completed", result
    status = system.cli(a, "status")
    assert len(status["jobs"]) == 3 and not status["events"], status
    for job in status["jobs"]:
        assert job["status"] == "completed"
        assert read(system.output(a, job["id"]) / "metrics.json") == {"mean": 4, "count": 3}
    assert not system.cli(a, "machine", "status")["reservations"]
    report = system.cli(a, "report", timeout=300)
    folder = Path(report["report_dir"])
    assert (folder / "report.md").stat().st_size and (folder / "report.html").stat().st_size
    journal = a / ".xgenius" / "journal.md"
    before = journal.read_bytes()
    compact = system.cli(a, "compact", timeout=300)
    assert Path(compact["backup"]).read_bytes() == before
    assert all(job["id"] in journal.read_text(encoding="utf-8") for job in status["jobs"])
    assert (folder / "report.html").is_file()
