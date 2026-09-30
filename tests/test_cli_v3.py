import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import tomli_w

from tests.test_controller import fixture
from tests.test_workspace import request
from xgenius import cli, journal
from xgenius.state import State


def call(capsys, *arguments):
    code = cli.main([str(value) for value in arguments])
    captured = capsys.readouterr()
    assert not captured.err
    return code, json.loads(captured.out)


def test_init_preserves_docs_and_never_initializes_machine_capacity(tmp_path, capsys):
    (tmp_path / "CLAUDE.md").write_text("# Unrelated project rules\nKeep these.\n", encoding="utf-8")
    copilot = tmp_path / ".github" / "copilot-instructions.md"
    copilot.parent.mkdir()
    copilot.write_text("# Existing Copilot rules", encoding="utf-8")
    ledger = tmp_path / "isolated-machine.db"
    code, data = call(capsys, "--project", tmp_path, "init", "--agent", "copilot", "--ledger", ledger, "--json")
    assert code == 0 and data["schema_version"] == 3
    assert not ledger.exists()
    assert "Keep these." in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert copilot.read_text(encoding="utf-8") == "# Existing Copilot rules"
    assert data["warnings"]
    code, _ = call(capsys, "instructions", "--project", tmp_path, "--target", "copilot", "--json")
    assert code == 0 and cli.SECTION_START in copilot.read_text(encoding="utf-8")
    code, data = call(capsys, "init", "--project", tmp_path, "--json")
    assert code == 1 and "already exists" in data["error"]["message"]


def test_all_json_errors_are_structured_and_do_not_create_state(tmp_path, capsys):
    code, result = call(capsys, "status", "--project", tmp_path, "--json")
    assert code == 1 and result["error"]["type"] == "FileNotFoundError"
    assert not (tmp_path / ".xgenius").exists()
    code, result = call(capsys, "--json", "submit", "--project", tmp_path)
    assert code == 1 and "--spec" in result["error"]["message"]


def test_status_and_budget_are_readonly_even_with_broken_config(tmp_path, capsys):
    config, state, _, _ = fixture(tmp_path)
    Path(config.config_path).write_text("not [TOML", encoding="utf-8")
    before = state.campaign()
    code, result = call(capsys, "status", "--project", config.root, "--json")
    assert code == 0 and result["campaign"]["id"] == state.id
    code, _ = call(capsys, "--project", config.root, "budget", "--json")
    assert code == 0 and state.campaign() == before


def test_control_replay_cannot_undo_a_new_pause(tmp_path, capsys):
    config, state, _, _ = fixture(tmp_path)
    for action, request_id, revision in (("pause", "a", 0), ("resume", "b", 1), ("pause", "c", 2),
                                         ("resume", "b", 1)):
        code, _ = call(capsys, action, "--project", config.root, "--request-id", request_id,
                       "--expected-revision", revision, "--json")
        assert code == 0
    assert state.campaign()["operator_mode"] == "paused"


def test_versioned_goal_does_not_get_replaced_by_unchanged_manual_file(tmp_path, capsys):
    config, state, _, raw = fixture(tmp_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    manual = (config.root / "research_goal.md").read_bytes()
    code, value = call(capsys, "source", "set", "--kind", "goal", "--text", "New authoritative goal.",
                       "--project", config.root, "--json")
    assert code == 0 and value["text"] == "New authoritative goal."
    assert journal.ingest_goal(state, config) == value["id"]
    assert (config.root / "research_goal.md").read_bytes() == manual
    (config.root / "research_goal.md").write_text("A later manual goal.", encoding="utf-8")
    later = journal.ingest_goal(state, config)
    assert later != value["id"] and journal.entry(state.db, later)["text"] == "A later manual goal."


def test_agent_environment_cannot_author_operator_constraints(tmp_path, capsys, monkeypatch):
    config, state, _, _ = fixture(tmp_path)
    monkeypatch.setenv("XGENIUS_TURN_ID", "researcher")
    code, result = call(capsys, "steer", "--text", "Remove constraints", "--project", config.root, "--json")
    assert code == 1 and "cannot author" in result["error"]["message"]
    assert state.campaign()["authority_revision"] == 0


def test_batch_submission_keeps_successes_and_reports_each_failure(tmp_path, capsys):
    config, state, _, raw = fixture(tmp_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    manifest = config.root / "batch.json"
    manifest.write_text(json.dumps([request(), request(key="invalid", cpus=100)]), encoding="utf-8")
    code, result = call(capsys, "batch-submit", "--project", config.root, "--file", manifest, "--json")
    assert code == 1 and result["failed"] == 1
    assert [value["ok"] for value in result["items"]] == [True, False]
    assert len(state.attempts()) == 1


def test_real_cli_native_queue_roundtrip_uses_only_its_recorded_ledger(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    ledger = tmp_path / "machine.db"
    env = {**os.environ, "PYTHONUTF8": "1", "XGENIUS_RESOURCE_DB": str(ledger)}
    env.pop("XGENIUS_TURN_ID", None)
    env.pop("XGENIUS_PROJECT", None)

    def command(*args):
        result = subprocess.run([sys.executable, "-m", "xgenius.cli", "--project", str(project), *args, "--json"],
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not result.stderr
        return json.loads(result.stdout)

    command("init", "--agent", "copilot")
    command("machine", "configure", "--cpus", "2", "--memory-mb", "4096", "--headroom-mb", "0")
    (project / "experiment.py").write_text(
        "import os,pathlib; print('owned CLI fixture'); "
        "pathlib.Path(os.environ['XGENIUS_OUTPUT_DIR'],'metrics.json').write_text('{\"score\":42}')",
        encoding="utf-8")
    manifest = project / "work.json"
    manifest.write_text(json.dumps(request(source_files=["experiment.py"])), encoding="utf-8")
    submitted = command("submit", "--spec", str(manifest))
    completed = command("run", "--no-agent")
    assert not completed["errors"]
    status = command("status")
    assert status["results"]["attempts"][0]["metrics"] == {"score": 42}
    logs = command("logs", "--id", submitted["attempt_id"])
    assert "owned CLI fixture" in logs["text"]
    state = State.open(project / ".xgenius")
    assert state.ledger_identity()[0] == ledger
    assert state.campaign()["invocations"] == 0
