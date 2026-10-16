from dataclasses import asdict
import json
from pathlib import Path

import pytest

from labgoblin import guidance, runtime
from labgoblin.cli import credit_value, main
from labgoblin.config import Config
from labgoblin.storage import read_json, read_text


@pytest.mark.parametrize("field,value", [
    ("version", 4), ("coach", "yes"), ("cpus", True), ("cpus", 0), ("memory_mb", -1),
    ("gpus", "gpu"), ("gpus", ["gpu", "gpu"]), ("timeout_seconds", 0),
    ("timeout_seconds", float("inf")), ("max_ai_credits", None), ("max_ai_credits", True),
    ("max_ai_credits", 29), ("max_ai_credits", 30.0000000001), ("reasoning_effort", "guess"),
    ("model", ""), ("max_snapshot_bytes", 0), ("old_research_schema", {}),
    ("max_stalled_reviews", 0), ("max_stalled_reviews", -1), ("max_stalled_reviews", True),
    ("max_stalled_reviews", 1.5), ("max_stalled_reviews", "unlimited"),
])
def test_strict_restart_configuration(field, value):
    with pytest.raises(ValueError):
        Config.parse({**Config().to_dict(), field: value})


@pytest.mark.parametrize("coach,credits", [(False, 30), (True, 90), (True, "unlimited")])
def test_configuration_roundtrip(tmp_path, coach, credits):
    config = Config(coach=coach, max_ai_credits=credits)
    (tmp_path / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    assert Config.load(tmp_path) == config


def test_initialize_preserves_existing_goal_and_instructions(tmp_path):
    goal = "Human-owned question"
    (tmp_path / "research_goal.md").write_text(goal, encoding="utf-8")
    instructions = tmp_path / ".github" / "copilot-instructions.md"
    instructions.parent.mkdir()
    instructions.write_text("Human-owned instructions", encoding="utf-8")
    record = runtime.initialize(tmp_path, Config())
    assert read_text(tmp_path / "research_goal.md") == goal
    assert read_text(instructions) == "Human-owned instructions"
    assert read_json(tmp_path / ".labgoblin" / "project.json") == record
    with pytest.raises(ValueError, match="already exists"):
        runtime.initialize(tmp_path, Config())


def test_initialize_writes_current_research_guidance(tmp_path):
    runtime.initialize(tmp_path, Config(), goal="Investigate the stated question.")
    assert (tmp_path / ".github" / "copilot-instructions.md").read_bytes() == guidance.RESEARCH.encode()


def test_old_campaign_fails_without_mutation(tmp_path):
    state = tmp_path / ".labgoblin"
    state.mkdir()
    old = state / "labgoblin.db"
    old.write_bytes(b"old campaign bytes")
    with pytest.raises(ValueError, match="legacy campaign"):
        runtime.status(tmp_path)
    assert old.read_bytes() == b"old campaign bytes"
    assert list(state.iterdir()) == [old]


def test_status_is_read_only_and_uses_no_provider(project, monkeypatch):
    root, config, record = project
    def forbidden(*args, **kwargs):
        raise AssertionError("Status must not start a provider or acquire resources")
    monkeypatch.setattr(runtime.provider, "capabilities", forbidden)
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = runtime.status(root)
    assert result["state"] == "initialized"
    assert result["budget"]["remaining"] == 300
    assert result["project_id"] == record["id"]
    assert before == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0.0000000001", "no"])
def test_cli_rejects_bad_credit_values(value):
    with pytest.raises(ValueError):
        credit_value(value)


def test_cli_init_never_starts_a_provider(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Unexpected provider call"))
    assert main(["init", str(tmp_path / "new"), "--no-coach", "--credits", "30"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["version"] == 5
    assert main(["status", str(tmp_path / "new")]) == 0
    assert json.loads(capsys.readouterr().out)["budget"]["used_nano"] == 0


def test_coach_is_opt_in(tmp_path, capsys):
    assert main(["init", str(tmp_path / "plain"), "--credits", "30"]) == 0
    capsys.readouterr()
    assert Config.load(tmp_path / "plain").coach is False
    assert main(["init", str(tmp_path / "coached"), "--coach", "--credits", "60"]) == 0
    capsys.readouterr()
    assert Config.load(tmp_path / "coached").coach is True


def test_stalled_review_configuration_defaults_and_cli(tmp_path, capsys):
    old = Config().to_dict()
    del old["max_stalled_reviews"]
    assert Config.parse(old).max_stalled_reviews == 10
    assert main(["init", str(tmp_path / "new"), "--coach", "--max-stalled-reviews", "3"]) == 0
    capsys.readouterr()
    assert Config.load(tmp_path / "new").max_stalled_reviews == 3
