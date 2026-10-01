import asyncio
from pathlib import Path
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import copilot
import pytest

from labgoblin import cli, initialization, setup_assistant as setup
from labgoblin.processes import own_handle, process_state
from labgoblin.protocol import canonical


class Terminal:
    def __init__(self, answers=()):
        self.answers = iter(answers)
        self.output = []

    def write(self, value):
        self.output.append(value)

    async def ask(self, prompt):
        self.output.append(prompt)
        value = next(self.answers, "/cancel")
        if value == "APPLY":
            return prompt.split("Type ", 1)[1].split(",", 1)[0]
        return value

    async def approve(self, prompt):
        return await self.ask(prompt) == "yes"


def tools(root, answers=()):
    return setup.SetupTools(root, root / "unused.db", "copilot", False, Terminal(answers))


def sdk(monkeypatch, *, failure="", tool_calls=0):
    captured = {"prompts": [], "calls": []}

    class Session:
        session_id = "setup-test"

        def on(self, callback):
            self.callback = callback

        async def send(self, prompt):
            captured["prompts"].append(prompt)
            if failure == "send":
                raise RuntimeError("send failed")
            if failure == "tool_eof":
                tool = next(t for t in captured["session"]["tools"] if t.name == "ask_operator")
                await tool.handler(SimpleNamespace(tool_name="ask_operator", arguments={"question": "Clarify?"}))
            for _ in range(tool_calls):
                tool = next(t for t in captured["session"]["tools"] if t.name == "input_metadata")
                result = await tool.handler(SimpleNamespace(tool_name="input_metadata", arguments={"path": "unselected"}))
                assert result.result_type == "failure"
                captured["calls"].append(result)
            self.callback(SimpleNamespace(type=SimpleNamespace(value="assistant.message_delta"),
                                          data=SimpleNamespace(message_id="m", delta_content="draft ")))
            self.callback(SimpleNamespace(type=SimpleNamespace(value="assistant.message"),
                                          data=SimpleNamespace(message_id="m", content="draft answer")))
            if failure == "model":
                self.callback(SimpleNamespace(type=SimpleNamespace(value="session.error"),
                                              data=SimpleNamespace(message="model failed")))
            self.callback(SimpleNamespace(type=SimpleNamespace(value="session.idle"), data=None))

        async def abort(self):
            captured["aborted"] = True

        async def disconnect(self):
            captured["disconnected"] = True
            if failure == "disconnect":
                raise RuntimeError("disconnect failed")

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def start(self):
            if failure == "startup":
                raise RuntimeError("startup failed")

        async def get_auth_status(self):
            return SimpleNamespace(isAuthenticated=failure != "auth")

        async def create_session(self, **kwargs):
            captured["session"] = kwargs
            return Session()

        async def delete_session(self, session_id):
            captured["deleted"] = session_id
            if failure == "delete":
                raise RuntimeError("delete failed")

        async def stop(self):
            captured["stopped"] = True
            if failure == "stop":
                raise RuntimeError("stop failed")

        async def force_stop(self):
            captured["forced"] = True

    monkeypatch.setattr(copilot, "CopilotClient", Client)
    monkeypatch.setattr(setup, "local_runtime", lambda: sys.executable)
    return captured


def test_sdk_scoped_multi_turn_no_observer_quota_and_exact_host_approval(tmp_path, monkeypatch):
    captured = sdk(monkeypatch, tool_calls=15)
    terminal = Terminal(["First intent", *(f"Correction {i}" for i in range(25)), "/review", "APPLY"])
    reviewed = asyncio.run(setup.assist(tmp_path, tmp_path / "unused.db", terminal=terminal, model="chosen-setup"))
    assert len(captured["prompts"]) == 26 and len(captured["calls"]) == 26 * 15
    assert not list(tmp_path.iterdir())
    assert reviewed.draft.configuration["agent"]["model"] == ""
    session = captured["session"]
    assert session["model"] == "chosen-setup"
    assert "session_limits" not in session
    assert session["enable_managed_settings"]
    assert captured["client"]["mode"] == "empty"
    connection = captured["client"]["connection"]
    assert connection.path == sys.executable
    assert "--disable-builtin-mcps" in connection.args and "--no-custom-instructions" in connection.args
    assert "setup_runtime.py" in connection.args[0]
    assert captured["client"]["session_idle_timeout_seconds"] == 0
    assert set(session["available_tools"].to_list()) == {"custom:" + name for name in setup.TOOLS}
    for key in ("enable_config_discovery", "enable_file_hooks", "enable_host_git_operations", "enable_skills",
                "enable_session_store", "manage_schedule_enabled", "request_extensions"):
        assert session[key] is False
    assert session["hooks"]["on_pre_tool_use"]({"toolName": "powershell"}, {})["permissionDecision"] == "deny"
    assert session["hooks"]["on_pre_tool_use"]({"toolName": "propose_draft"}, {})["permissionDecision"] == "allow"
    assert captured["disconnected"] and captured["deleted"] and captured["stopped"]


@pytest.mark.parametrize("failure", ["startup", "auth", "send", "model", "disconnect", "delete", "stop"])
def test_sdk_failures_never_create_campaign_and_cleanup_is_explicit(tmp_path, monkeypatch, failure):
    captured = sdk(monkeypatch, failure=failure)
    with pytest.raises(RuntimeError):
        asyncio.run(setup.assist(tmp_path, tmp_path / "unused.db", terminal=Terminal(["Intent", "/cancel"])))
    assert not list(tmp_path.iterdir()) and captured["stopped"]
    if failure not in ("startup", "auth"):
        assert captured["disconnected"] and captured["deleted"]
    if failure in ("send", "model"):
        assert captured["aborted"]
    if failure in ("disconnect", "delete", "stop"):
        assert captured["forced"]


def test_cancel_before_inference_is_inert(tmp_path, monkeypatch):
    captured = sdk(monkeypatch)
    with pytest.raises(EOFError):
        asyncio.run(setup.assist(tmp_path, tmp_path / "unused.db", terminal=Terminal(["/cancel"])))
    assert not captured["prompts"] and captured["deleted"] and not list(tmp_path.iterdir())


def test_eof_during_tool_question_cancels_session(tmp_path, monkeypatch):
    captured = sdk(monkeypatch, failure="tool_eof")

    class CancelDuringQuestion(Terminal):
        async def ask(self, prompt):
            if prompt == "Clarify?":
                raise EOFError()
            return "Intent"

    with pytest.raises(EOFError):
        asyncio.run(setup.assist(tmp_path, tmp_path / "unused.db", terminal=CancelDuringQuestion()))
    assert captured["aborted"] and captured["deleted"] and captured["stopped"]
    assert not list(tmp_path.iterdir())


def test_terminal_mode_routes_to_assistant(tmp_path, monkeypatch, capsys):
    calls = []

    async def assist(root, ledger, **kwargs):
        calls.append(kwargs)
        raise EOFError()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(setup, "assist", assist)
    assert cli.main(["init", "--project", str(tmp_path), "--setup-model", "chosen"]) == 130
    assert calls[0]["model"] == "chosen" and not list(tmp_path.iterdir())
    assert "cancelled" in capsys.readouterr().err


def test_auth_dependency_and_local_binary_failures_are_distinct(monkeypatch):
    monkeypatch.setattr(setup.importlib.metadata, "version", lambda _: "wrong")
    with pytest.raises(RuntimeError, match="requires github-copilot-sdk"):
        setup.local_runtime()
    monkeypatch.setattr(setup.importlib.metadata, "version", lambda _: setup.SDK_VERSION)
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="does not download"):
        setup.local_runtime()


def test_runtime_accepts_windows_app_execution_alias_without_resolving(monkeypatch):
    monkeypatch.setattr(setup.importlib.metadata, "version", lambda _: setup.SDK_VERSION)
    monkeypatch.setattr(setup.shutil, "which", lambda _: str(Path(sys.executable).absolute()))
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("App aliases cannot be resolved"))
    assert setup.local_runtime() == str(Path(sys.executable).absolute())


def test_read_requires_selected_root_and_per_file_consent(tmp_path):
    document = tmp_path / "research.md"
    document.write_text("Benign selected context", encoding="utf-8")
    host = tools(tmp_path, ["no", "yes"])
    with pytest.raises(ValueError, match="operator-selected"):
        asyncio.run(host.invoke("read_context", {"path": str(document)}))
    host.select(str(tmp_path))
    with pytest.raises(ValueError, match="declined"):
        asyncio.run(host.invoke("read_context", {"path": str(document)}))
    assert asyncio.run(host.invoke("read_context", {"path": str(document)}))["text"] == "Benign selected context"


@pytest.mark.parametrize("name", [".env", ".env.local", ".ssh/key.md", ".labgoblin/goal.md", "data.csv", "logs/run.txt"])
def test_credentials_state_logs_and_dataset_bodies_are_not_shareable(tmp_path, name):
    path = tmp_path.joinpath(*name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not for the model", encoding="utf-8")
    host = tools(tmp_path, ["yes"])
    host.select(str(tmp_path))
    with pytest.raises(ValueError):
        asyncio.run(host.invoke("read_context", {"path": str(path)}))


def test_selected_folder_does_not_allow_escape_or_oversized_text(tmp_path):
    folder = tmp_path / "selected"
    folder.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    large = folder / "large.md"
    large.write_bytes(b"x" * 32769)
    host = tools(tmp_path)
    host.select(str(folder))
    with pytest.raises(ValueError, match="operator-selected"):
        asyncio.run(host.invoke("read_context", {"path": str(folder / ".." / outside.name)}))
    with pytest.raises(ValueError, match="read limit"):
        asyncio.run(host.invoke("read_context", {"path": str(large)}))
    with pytest.raises(ValueError):
        host.select("")


def test_declared_input_cannot_be_shared_as_documentation(tmp_path):
    path = tmp_path / "dataset.md"
    path.write_text("Private dataset in a text extension", encoding="utf-8")
    host = tools(tmp_path, ["yes"])
    host.select(str(tmp_path))
    host.draft.configuration["inputs"] = {"DATA": {"path": str(path)}}
    with pytest.raises(ValueError, match="Declared input bodies"):
        asyncio.run(host.invoke("read_context", {"path": str(path)}))


def test_declined_probe_never_executes_and_custom_provider_command_is_refused(tmp_path, monkeypatch):
    host = tools(tmp_path, ["no"])
    monkeypatch.setattr("labgoblin.backends.validate_runner", lambda *_: pytest.fail("Declined probe ran"))
    result = asyncio.run(host.invoke("check_readiness", {"check": "runner"}))
    assert result["status"] == "declined"
    host.draft.configuration["agent"]["command"] = [sys.executable, "-c", "raise Exception('forbidden')"]
    with pytest.raises(ValueError, match="not custom"):
        asyncio.run(host.invoke("check_readiness", {"check": "provider"}))
    with pytest.raises(ValueError, match="Unknown setup tool"):
        asyncio.run(host.invoke("shell", {"command": "anything"}))


def test_invalid_proposal_preserves_draft_and_checks_until_valid_correction(tmp_path):
    host = tools(tmp_path, ["no", "APPLY"])
    original = host.draft
    declined = asyncio.run(host.invoke("check_readiness", {"check": "runner"}))
    proposal = {"configuration": json.loads(canonical(original.configuration)),
                "goal": "Plan an arithmetic rehearsal, not completed evidence.",
                "protocol": "Plan exactly five paired replications.",
                "constraints": ["No network access by experiments."]}
    proposal["configuration"]["campaign"]["max_invocations"] = 4
    with pytest.raises(ValueError, match="Unknown propose_draft fields"):
        asyncio.run(host.invoke("propose_draft", {**proposal, "constraints_placeholder": []}))
    assert host.draft is original and host.checks == [declined]
    assert not list(tmp_path.iterdir())

    result = asyncio.run(host.invoke("propose_draft", proposal))
    assert result["valid"] and not result["applied"]
    assert host.draft.configuration["campaign"]["max_invocations"] == 4
    assert host.draft.protocol == proposal["protocol"]
    assert host.draft.constraints == tuple(proposal["constraints"])
    assert not host.checks and not list(tmp_path.iterdir())
    prepared = asyncio.run(host.review())
    assert prepared.digest == result["approval_digest"] and prepared.draft == host.draft
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows owned Job Object acceptance")
@pytest.mark.parametrize("mode", ["parent_exit", "relay_killed"])
def test_stdio_runtime_relay_retires_tree_after_parent_exit(tmp_path, mode):
    child = tmp_path / "child.py"
    child.write_text("import time; time.sleep(90)", encoding="utf-8")
    runtime = tmp_path / "runtime.py"
    runtime.write_text(
        "import json,subprocess,sys,time,psutil,pathlib\n"
        "p=subprocess.Popen([sys.executable,sys.argv[1]])\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({'pid':p.pid,'created':psutil.Process(p.pid).create_time()}))\n"
        "time.sleep(90)\n", encoding="utf-8")
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
    import psutil
    handle = {"pid": parent.pid, "created": psutil.Process(parent.pid).create_time()}
    record = tmp_path / "child.json"
    relay = subprocess.Popen([sys.executable, "-m", "labgoblin.setup_runtime", "--parent", canonical(handle).decode(),
                              "--", sys.executable, str(runtime), str(child), str(record)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    descendant = None
    try:
        deadline = time.monotonic() + 15
        while not record.exists() and relay.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert record.exists(), relay.communicate(timeout=5)
        descendant = json.loads(record.read_text())
        assert process_state(descendant) == "alive"
        if mode == "parent_exit":
            parent.terminate()
            parent.wait(timeout=5)
        else:
            relay.kill()
        _, error = relay.communicate(timeout=10)
        if mode == "parent_exit":
            assert relay.returncode == 1 and b"Setup parent exited" in error
        deadline = time.monotonic() + 5
        while process_state(descendant) == "alive" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_state(descendant) == "dead"
    finally:
        if parent.poll() is None:
            parent.terminate()
        parent.wait(timeout=5)
        if relay.poll() is None:
            relay.kill()
        relay.communicate(timeout=10)
        if descendant and process_state(descendant) == "alive":
            psutil.Process(descendant["pid"]).kill()
