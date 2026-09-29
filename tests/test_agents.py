"""Agent selection and non-interactive research workflow regression tests."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import Mock

import pytest

from xgenius import agent, cli, watcher
from xgenius.config import AGENT_COMMANDS, WatcherConfig, XGeniusConfig, load_config
from xgenius.db import XGeniusDB
from xgenius.journal import ResearchJournal


@pytest.fixture(params=["claude", "copilot"])
def config(request, tmp_path):
    path = tmp_path / "xgenius.toml"
    path.write_text(
        "[watcher]\ntrigger_command = "
        + json.dumps(AGENT_COMMANDS[request.param])
        + "\n",
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest.mark.parametrize("contents", ["", "[watcher]\npoll_interval_seconds = 15\n"])
def test_default_command_matches_dataclass(tmp_path, contents):
    path = tmp_path / "xgenius.toml"
    path.write_text(contents, encoding="utf-8")
    assert load_config(str(path)).watcher.trigger_command == WatcherConfig().trigger_command


def test_existing_explicit_command_is_preserved(tmp_path):
    path = tmp_path / "xgenius.toml"
    path.write_text('[watcher]\ntrigger_command = "claude --continue"\n', encoding="utf-8")
    assert load_config(str(path)).watcher.command_args() == ["claude", "--continue"]


@pytest.mark.parametrize("command", ["", "  ", '""', '"unfinished', 42])
def test_invalid_commands_fail_at_load(tmp_path, command):
    path = tmp_path / "xgenius.toml"
    path.write_text(
        "[watcher]\ntrigger_command = " + json.dumps(command) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="watcher.trigger_command"):
        load_config(str(path))


@pytest.mark.parametrize("provider", [None, "claude", "copilot"])
def test_init_selects_agent_and_preserves_shared_instructions(tmp_path, monkeypatch, provider):
    monkeypatch.chdir(tmp_path)
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text("# Existing project guidance\n\nKeep this.\n", encoding="utf-8")
    argv = ["xgenius", "init", "--backend", "slurm"]
    if provider:
        argv.extend(["--agent", provider])
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()

    config = load_config(str(tmp_path / "xgenius.toml"))
    assert config.watcher.trigger_command == AGENT_COMMANDS[provider or "claude"]
    text = instructions.read_text(encoding="utf-8")
    assert text.startswith("# Existing project guidance\n\nKeep this.")
    assert "xgenius submit" in text
    assert "xgenius journal read" in text
    assert (tmp_path / "research_goal.md").exists()
    assert (tmp_path / ".xgenius" / "templates").is_dir()
    cli._write_claude_md(str(tmp_path))
    assert instructions.read_text(encoding="utf-8") == text


def test_new_instructions_are_shared(tmp_path):
    cli._write_claude_md(str(tmp_path))
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Claude Code and GitHub Copilot CLI" in text


def test_run_agent_arguments_and_environment(config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(agent.subprocess, "run", run)
    prompt = 'Read the journal.\nKeep "quoted text", $variables, and ; intact.'
    result = agent.run_agent(config, prompt)

    assert result.returncode == 0
    args, kwargs = run.call_args
    assert args[0] == [*config.watcher.command_args(), "-p", prompt]
    assert kwargs["cwd"] == str(Path(config.config_path).parent)
    assert not kwargs.get("shell", False)
    assert kwargs["capture_output"] is False
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["env"]["GH_TOKEN"] == "test-token"
    if args[0][0] == "claude":
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    else:
        assert kwargs["env"]["ANTHROPIC_API_KEY"] == "test-key"
    assert os.environ["ANTHROPIC_API_KEY"] == "test-key"


def test_quoted_executable_and_options(tmp_path, monkeypatch):
    config = XGeniusConfig(
        config_path=str(tmp_path / "xgenius.toml"),
        watcher=WatcherConfig(
            trigger_command=r'''"C:\Program Files\Copilot\copilot.exe" --allow-all --model "model name"'''
        ),
    )
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(agent.subprocess, "run", run)
    agent.run_agent(config, "prompt", capture_output=True)
    assert run.call_args.args[0] == [
        r"C:\Program Files\Copilot\copilot.exe", "--allow-all",
        "--model", "model name", "-p", "prompt",
    ]
    assert run.call_args.kwargs["capture_output"] is True


def test_real_subprocess_receives_prompt_and_project_directory(tmp_path):
    script = tmp_path / "fake agent.py"
    script.write_text(
        "import json, os, sys\n"
        "sys.stdout.buffer.write(json.dumps("
        "{'args': sys.argv[1:], 'cwd': os.getcwd()}, ensure_ascii=False).encode('utf-8'))\n",
        encoding="utf-8",
    )
    config = XGeniusConfig(
        config_path=str(tmp_path / "xgenius.toml"),
        watcher=WatcherConfig(
            trigger_command=f'"{sys.executable}" "{script}" --model "model name"'
        ),
    )
    prompt = 'A multiline prompt\nwith "quotes"; no shell expansion: $HOME; Unicode: \u03bb'
    result = agent.run_agent(config, prompt, capture_output=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output == {
        "args": ["--model", "model name", "-p", prompt],
        "cwd": str(tmp_path),
    }


def test_wakeup_prompt_retains_shared_instructions(config):
    prompt = XGeniusDB(config).build_wakeup_prompt()
    assert "Read CLAUDE.md and research_goal.md" in prompt
    assert "fresh session" in prompt
    assert "xgenius journal read" in prompt


@pytest.mark.parametrize("outcome", [0, 1, "missing"])
def test_watcher_uses_configured_agent_and_retries_failures(config, monkeypatch, outcome):
    db = Mock()
    db.get_active_job_ids.return_value = []
    db.get_completed_not_pulled.return_value = [{
        "job_id": "123", "experiment_id": "exp1", "exit_code": 0, "cluster": "test",
    }]
    db.build_wakeup_prompt.return_value = "Fresh research session"
    manager = Mock(db=db)
    manager.check_completions.return_value = []
    monkeypatch.setattr(watcher, "JobManager", Mock(return_value=manager))
    run = Mock(
        side_effect=FileNotFoundError("agent executable missing") if outcome == "missing" else None,
        return_value=subprocess.CompletedProcess([], outcome),
    )
    monkeypatch.setattr(agent.subprocess, "run", run)
    # Stop after one polling cycle, including the outer error handler's sleep.
    monkeypatch.setattr(watcher.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    try:
        watcher.run_watcher(config.config_path)
    except KeyboardInterrupt:
        pass

    run.assert_called_once()
    assert run.call_args.args[0] == [
        *config.watcher.command_args(), "-p", "Fresh research session",
    ]
    state_dir = Path(config.config_path).parent / ".xgenius"
    assert not (state_dir / "watcher.lock").exists()
    log = (state_dir / "watcher.log").read_text(encoding="utf-8")
    if outcome == 0:
        db.mark_results_pulled.assert_called_once_with("123")
    else:
        db.mark_results_pulled.assert_not_called()
        assert "agent executable missing" in log if outcome == "missing" else "code 1" in log


@pytest.mark.parametrize("outcome", [0, 1, "missing"])
def test_report_uses_configured_agent(config, monkeypatch, capsys, outcome):
    run = Mock(
        side_effect=FileNotFoundError("agent executable missing") if outcome == "missing" else None,
        return_value=subprocess.CompletedProcess([], outcome, stdout="agent chatter", stderr="detail"),
    )
    monkeypatch.setattr(agent.subprocess, "run", run)
    args = argparse.Namespace(config=config.config_path, json=True)
    if outcome == 0:
        cli.cmd_report(args)
    else:
        with pytest.raises(SystemExit) as error:
            cli.cmd_report(args)
        assert error.value.code == 1
    command = run.call_args.args[0]
    assert command[:-2] == config.watcher.command_args()
    assert command[-2] == "-p"
    assert "research report" in command[-1]
    assert run.call_args.kwargs["capture_output"] is True
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == ("generated" if outcome == 0 else "error")
    if outcome == 1:
        assert output["stdout"] == "agent chatter"
        assert output["stderr"] == "detail"


@pytest.mark.parametrize("outcome", ["success", "failed", "empty", "deleted", "missing"])
def test_compact_uses_configured_agent_and_preserves_journal(config, monkeypatch, capsys, outcome):
    journal = ResearchJournal(config)
    original = "Important original research findings.\n" * 20
    journal.replace(original)
    compacted = "# Research Journal (compacted)\n\nImportant original research findings.\n"

    def run(command, **kwargs):
        assert command[:-2] == config.watcher.command_args()
        assert command[-2] == "-p"
        assert kwargs["cwd"] == str(Path(config.config_path).parent)
        assert kwargs["capture_output"] is True
        if outcome == "missing":
            raise FileNotFoundError("agent executable missing")
        path = Path(re.search(r"Write the compacted journal to: `([^`]+)`", command[-1]).group(1))
        if outcome == "success":
            path.write_text(compacted, encoding="utf-8")
        elif outcome == "deleted":
            path.unlink()
        return subprocess.CompletedProcess([], 1 if outcome == "failed" else 0, stderr="detail")

    monkeypatch.setattr(agent.subprocess, "run", run)
    args = argparse.Namespace(config=config.config_path, json=True)
    if outcome == "success":
        cli.cmd_compact(args)
    else:
        with pytest.raises(SystemExit) as error:
            cli.cmd_compact(args)
        assert error.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert journal.read() == (compacted if outcome == "success" else original)
    if outcome == "success":
        assert output["status"] == "compacted"
        assert Path(output["backup"]).read_text(encoding="utf-8") == original
    else:
        assert output["status"] == "error"
    assert not list(Path(config.config_path).parent.glob("journal_*.md"))
