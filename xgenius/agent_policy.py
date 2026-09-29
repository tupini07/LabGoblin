"""Provider-scoped trusted execution and optional Copilot sandbox preflight."""

import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time

from xgenius.workspace import atomic_json, read_json
from xgenius.processes import background_options


def sandbox_preflight(config) -> dict:
    home = Path(config.local.copilot_home).resolve()
    project = Path(config.config_path).parent.resolve()
    if not home.is_relative_to(project / ".xgenius") or not home.is_dir():
        raise ValueError("Sandbox requires a provisioned dedicated profile under this campaign's .xgenius")
    lock = home / "preflight.lock"
    try:
        with lock.open("x", encoding="utf-8") as f:
            from xgenius.backends import own_handle
            json.dump(own_handle("sandbox-preflight"), f)
    except FileExistsError as e:
        raise ValueError(f"Sandbox preflight is active or unresolved; inspect {lock}") from e
    try:
        return _sandbox_probe(config)
    finally:
        lock.unlink()


def _sandbox_probe(config) -> dict:
    local = config.local
    home = Path(local.copilot_home).resolve()
    project = Path(config.config_path).parent.resolve()
    if not home.is_relative_to(project / ".xgenius"):
        raise ValueError("Sandbox Copilot home must be a dedicated profile under this campaign's .xgenius")
    settings_path = home / "settings.json"
    if not settings_path.exists():
        raise ValueError("Provision/authenticate the dedicated Copilot profile before sandbox preflight")
    settings = read_json(settings_path)
    sandbox = settings.get("sandbox", {})
    if sandbox.get("enabled") is not True:
        raise ValueError("The dedicated Copilot profile must explicitly enable sandboxing")
    if sandbox.get("allowBypass") is not False:
        raise ValueError("Autonomous sandbox mode requires allowBypass=false")
    command = local.command or config.watcher.command_args()
    if Path(command[0]).stem.lower() != "copilot":
        raise ValueError("Optional command sandboxing is only supported for Copilot")
    canary_root = home / "preflight"
    canary_root.mkdir(exist_ok=True)
    denied = canary_root / "denied.txt"
    denied.write_text("unchanged", encoding="utf-8")
    result_path = canary_root / "result.json"
    result_path.unlink(missing_ok=True)
    filesystem = sandbox.setdefault("userPolicy", {}).setdefault("filesystem", {})
    original = settings_path.read_bytes()
    denied_paths = filesystem.setdefault("deniedPaths", [])
    denied_paths.append(str(denied))
    filesystem.setdefault("readwritePaths", []).append(str(canary_root))
    from xgenius.state import identifier
    challenge = identifier()
    script = canary_root / "probe.py"
    probes = []
    for runner in local.runners.values():
        if runner.kind == "wsl":
            probes.append(["wsl", "-d", runner.distro, "--exec", runner.python, "--version"])
        elif runner.kind == "docker":
            probes.append(["docker", "--context", runner.context, "version", "--format", "{{.Server.Os}}"])
    script.write_text(
        "import json,pathlib,subprocess\n"
        f"denied=pathlib.Path({str(denied)!r})\n"
        "blocked=False\n"
        "try: denied.write_text('changed',encoding='utf-8')\n"
        "except PermissionError: blocked=True\n"
        f"for argv in {probes!r}: subprocess.run(argv,check=True,timeout=30,capture_output=True)\n"
        f"pathlib.Path({str(result_path)!r}).write_text(json.dumps(dict("
        f"challenge={challenge!r},allowed_write=True,denied_write_blocked=blocked)),encoding='utf-8')\n",
        encoding="utf-8")
    prompt = (
        "This is a sandbox preflight, not a coding task. Do not bypass the sandbox. "
        f"Use only your shell tool to run the exact argv {json.dumps([sys.executable, str(script)])}. "
        "The pre-created script performs a disposable denied-write canary and writes its own receipt. "
        "Do not edit any files, write the receipt yourself, change policy, or run any other commands. "
        "If the sandbox fails, report the exact startup error and stop."
    )
    env = {**os.environ, "COPILOT_HOME": str(home)}
    try:
        atomic_json(settings_path, settings)
        result = subprocess.run(
            [*command, "--experimental", "--sandbox", "-p", prompt],
            cwd=project, env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=local.turn_timeout, **background_options())
        (canary_root / "stdout.log").write_text(result.stdout, encoding="utf-8")
        (canary_root / "stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise ValueError(f"Copilot sandbox startup/preflight failed ({result.returncode}): {result.stderr}")
        if not result_path.exists():
            raise ValueError(f"Sandbox preflight did not produce its receipt; see {canary_root}. "
                             f"Diagnostics: {result.stdout[-3000:]}")
        receipt = read_json(result_path)
        if (receipt != {"challenge": challenge, "allowed_write": True, "denied_write_blocked": True}
                or denied.read_text(encoding="utf-8") != "unchanged"):
            raise ValueError("Sandbox canary failed; refusing unsandboxed execution")
        return {"status": "passed", "scope": "shell canary only; file edits are best-effort"}
    finally:
        settings_path.write_bytes(original)


def require_idle_agent(config):
    if config.local is None:
        return
    from xgenius.db import _connect
    from xgenius.state import LocalState
    state = LocalState(config)
    with _connect(state.path) as c:
        running = c.execute("SELECT handle FROM turns WHERE state IN ('running','starting','maintenance')").fetchall()
    if running:
        raise ValueError("An agent turn is active or unresolved; finish/recover it before report/compact")


@contextmanager
def maintenance_lock(config):
    if config.local is None:
        yield
        return
    from xgenius.backends import alive, own_handle
    from xgenius.db import _connect
    from xgenius.state import LocalState
    state = LocalState(config)
    with _connect(state.path) as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute("SELECT agent FROM campaign WHERE id=?", (state.id,)).fetchone()[0]
        if existing and alive(json.loads(existing)):
            raise ValueError("Another maintenance operation is active")
        if c.execute("SELECT 1 FROM turns WHERE state IN ('starting','running','maintenance')").fetchone():
            raise ValueError("Finish or recover the active agent turn before maintenance")
        c.execute("UPDATE campaign SET agent=? WHERE id=?", (json.dumps(own_handle(state.id)), state.id))
    try:
        yield
    finally:
        with _connect(state.path) as c:
            c.execute("UPDATE campaign SET agent=NULL WHERE id=?", (state.id,))


class AgentSession:
    def __init__(self, handle, directory):
        self.handle = handle
        self.directory = directory
        self.returncode = None

    def poll(self):
        from xgenius.backends import alive
        receipt = self.directory / "agent-completion.json"
        if receipt.exists():
            data = read_json(receipt)
            if data.get("token") != self.handle["token"]:
                raise ValueError("Agent receipt ownership mismatch")
            self.returncode = data["returncode"] if not data["error"] else 1
        elif not alive(self.handle):
            self.returncode = 1
        return self.returncode


def start_session(config, prompt, turn_id, directory):
    from xgenius.backends import launch_independent
    import psutil
    atomic_json(directory / "agent-request.json", {
        "id": turn_id, "config_path": config.config_path, "prompt": prompt})
    process = launch_independent(
        [sys.executable, "-m", "xgenius.agent_worker", str(directory / "agent-request.json")], directory)
    handle = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(), "token": turn_id}
    from xgenius.db import _connect
    from xgenius.config import get_xgenius_dir
    with _connect(str(Path(get_xgenius_dir(config)) / "xgenius.db")) as c:
        c.execute("UPDATE turns SET handle=? WHERE id=?", (json.dumps(handle), turn_id))
    return AgentSession(handle, directory)


def run_local_agent(config, prompt, *, capture_output=False):
    """Bound maintenance sessions and serialize them with research sessions."""
    from xgenius.db import _connect
    from xgenius.state import LocalState, identifier
    state = LocalState(config)
    turn_id = identifier()
    directory = state.root / "turns" / turn_id
    directory.mkdir(parents=True)
    with _connect(state.path) as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM turns WHERE state IN ('starting','running','maintenance')").fetchone():
            raise ValueError("An agent turn is active; finish or recover it before report/compact")
        if c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] >= config.local.max_turns:
            raise ValueError("Campaign agent-turn budget exhausted")
        c.execute("INSERT INTO turns(id,events,started,state,kind) VALUES(?,'[]',?,'maintenance','maintenance')",
                  (turn_id, time.time()))
    error = None
    code = 1
    process = None
    launch_attempted = False
    try:
        if config.local.sandbox:
            sandbox_preflight(config)
        launch_attempted = True
        process = start_session(config, prompt, turn_id, directory)
        while process.poll() is None:
            time.sleep(0.1)
        code = process.returncode
    except (OSError, ValueError, RuntimeError) as e:
        error = e
    finally:
        stdout = (directory / "stdout.log").read_text(encoding="utf-8", errors="replace") \
            if (directory / "stdout.log").exists() else ""
        stderr = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace") \
            if (directory / "stderr.log").exists() else ""
        if not launch_attempted or (process is not None and process.poll() is not None):
            with _connect(state.path) as c:
                c.execute("UPDATE turns SET state=?,ended=?,result=? WHERE id=?",
                          ("failed" if error or code else "completed", time.time(),
                           json.dumps({"returncode": code, "error": str(error) if error else None}), turn_id))
    if error:
        raise error
    if not capture_output:
        print(stdout, end="")
        print(stderr, end="", file=sys.stderr)
    return subprocess.CompletedProcess(config.local.command, code, stdout if capture_output else None,
                                       stderr if capture_output else None)
