from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import venv

import pytest

from labgoblin import guidance, research_environment, runtime
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
    ("evaluation_spec", ""), ("evaluation_spec", "relative.json"), ("evaluation_spec", True),
    ("archive_workspace", "yes"), ("max_event_log_bytes", 0), ("max_event_log_bytes", True),
    ("max_event_log_bytes", "unlimited"), ("max_event_log_bytes", 65 * 1024 ** 3),
])
def test_strict_restart_configuration(field, value):
    with pytest.raises(ValueError):
        Config.parse({**Config().to_dict(), field: value})


@pytest.mark.parametrize("coach,credits", [(False, 30), (True, 90), (True, "unlimited")])
def test_configuration_roundtrip(tmp_path, coach, credits):
    config = Config(coach=coach, max_ai_credits=credits)
    (tmp_path / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    assert Config.load(tmp_path) == config


@pytest.mark.parametrize("expression,seconds", [
    ("48 * 60 * 60", 172800), (" (24 + 24) * 3600 ", 172800),
    ("3600 / 2", 1800), ("60.5 + -(2 - 3)", 61.5),
])
def test_timeout_expression_loads_and_freezes_numeric_seconds(tmp_path, expression, seconds):
    body = Config().toml().replace("timeout_seconds = 5400",
                                  f"timeout_seconds = {json.dumps(expression)}")
    (tmp_path / "labgoblin.toml").write_text(body, encoding="utf-8")
    config = Config.load(tmp_path)
    assert config.timeout_seconds == seconds
    assert config.to_dict()["timeout_seconds"] == seconds
    assert Config.parse(config.to_dict()) == config
    assert (tmp_path / "labgoblin.toml").read_text(encoding="utf-8") == body


@pytest.mark.parametrize("expression", [
    "60 / 0", "1e308 * 2", "59", "-60", "True", "'60'", "hours * 3600",
    "__import__('os').getcwd()", "(60).__class__", "2 ** 1000000", "60 +",
    " " * 257, "+".join(["60"] * 30),
])
def test_timeout_expression_rejects_non_arithmetic_and_unbounded_values(expression):
    with pytest.raises(ValueError, match="timeout_seconds"):
        Config.parse({**Config().to_dict(), "timeout_seconds": expression})


def test_optional_evaluation_cli_is_nonexecuting_and_roundtrips(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Initialization must not start a provider or evaluator")
    monkeypatch.setattr(runtime.provider, "capabilities", forbidden)
    project = tmp_path / "research"
    kit = tmp_path / "reviewed-kit.json"
    assert main(["init", str(project), "--evaluation-spec", str(kit)]) == 0
    assert Config.load(project).evaluation_spec == str(kit)
    assert "evaluation_spec" not in Config().to_dict()


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


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("reformatted", [False, True])
def test_unfilled_init_brief_never_admits_research(tmp_path, ledger, capsys, monkeypatch, previous, reformatted):
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Unexpected provider call"))
    root = tmp_path / "new"
    assert main(["init", str(root), "--ledger", str(ledger.path), "--no-research-venv"]) == 0
    record = json.loads(capsys.readouterr().out)
    goal = root / "research_goal.md"
    body = goal.read_bytes()
    if previous:
        body = b"# Research goal\n\nDescribe the question, available inputs and evidence that would answer it.\n"
    if reformatted:
        body = b" \r\n" + body.replace(b"\n", b"\r\n").replace(b" ", b"  ") + b"\t\r\n"
    goal.write_bytes(body)
    assert main(["run", str(root)]) == 1
    assert "Write a real research question" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert goal.read_bytes() == body
    assert not list((root / ".labgoblin" / "runs").iterdir())
    assert not ledger.runs(record["id"])
    assert not ledger.rows()


@pytest.mark.parametrize("existing", ["absent", "matching", "conflicting"])
def test_cli_goal_file_preserves_operator_scope_and_bytes(tmp_path, capsys, monkeypatch, existing):
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Unexpected provider call"))
    source = tmp_path / "question.md"
    body = b"Only compare prototypes; do not train models.\r\nKeep the original inputs unchanged.\r\n"
    source.write_bytes(body)
    root = tmp_path / "research"
    instructions = root / ".github" / "copilot-instructions.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_bytes(b"Use only local synthetic data.\r\n")
    goal = root / "research_goal.md"
    if existing != "absent":
        goal.write_bytes(body if existing == "matching" else b"A different operator goal.\r\n")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = main(["init", str(root), "--goal-file", str(source), "--no-research-venv"])
    output = capsys.readouterr()
    if existing == "conflicting":
        assert result == 1
        assert "differs from the existing" in json.loads(output.err)["error"]["message"]
        assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    else:
        assert result == 0
        assert goal.read_bytes() == body
        assert not (root / "research-notes.md").exists()
    assert all(path.read_bytes() == original for path, original in before.items())
    assert source.read_bytes() == body


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


@pytest.mark.parametrize("enabled", [True, False])
def test_init_records_local_research_authority_without_downloading(tmp_path, capsys, monkeypatch, enabled):
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Unexpected provider call"))
    args = ["init", str(tmp_path / "new")]
    if not enabled:
        args.append("--no-research-venv")
    assert main(args) == 0
    record = json.loads(capsys.readouterr().out)
    root = Path(record["root"])
    if not enabled:
        assert "research_environment" not in record
        assert not (root / ".venv").exists()
        return
    environment = record["research_environment"]
    assert environment["authorization"] == research_environment.AUTHORIZATION
    assert "not an exhaustive whitelist" in environment["authorization"]
    assert "training and fine-tuning" in environment["authorization"]
    observed = json.loads(runtime.payload.probe([
        environment["python"], "-I", "-B", "-c",
        "import importlib.util,json,sys; print(json.dumps({'prefix':sys.prefix,"
        "'pip':importlib.util.find_spec('pip') is not None}))",
    ]))
    assert Path(observed["prefix"]) == root / ".venv"
    assert observed["pip"] is False
    monkeypatch.setattr(research_environment, "inspect", lambda *args: pytest.fail("Status probed Python"))
    before = (root / ".labgoblin" / "project.json").read_bytes()
    (root / ".venv").rename(root / "retained-environment")
    assert runtime.status(root)["state"] == "initialized"
    assert (root / ".labgoblin" / "project.json").read_bytes() == before
    assert not (root / ".venv").exists()


def test_initialization_reuses_environment_without_overwriting_operator_material(tmp_path):
    root = tmp_path / "research"
    environment, _ = research_environment.paths(root)
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    (environment / "keep.txt").write_text("Existing environment contents", encoding="utf-8")
    goal = "Use local fixtures only. Do not download models or access the network."
    (root / "research_goal.md").write_text(goal, encoding="utf-8")
    instructions = root / ".github" / "copilot-instructions.md"
    instructions.parent.mkdir()
    instructions.write_text("Do not install third-party packages.", encoding="utf-8")
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    record = runtime.initialize(root, Config())
    assert "research_environment" in record
    assert all(Path(path).read_bytes() == body for path, body in before.items())
    assert "Explicit operator restrictions" in record["research_environment"]["authorization"]


@pytest.mark.parametrize("problem", ["alias", "shared-packages", "harness"])
def test_initialization_refuses_unsafe_existing_environment(tmp_path, monkeypatch, problem):
    root = tmp_path / "research"
    root.mkdir()
    directory, _ = research_environment.paths(root)
    target = tmp_path / "external" if problem == "alias" else directory
    venv.EnvBuilder(with_pip=False).create(target)
    if problem == "alias":
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(directory))
        else:
            directory.symlink_to(target, target_is_directory=True)
    elif problem == "shared-packages":
        config = target / "pyvenv.cfg"
        config.write_text(config.read_text().replace("include-system-site-packages = false",
                                                    "include-system-site-packages = true"))
    else:
        monkeypatch.setattr(research_environment.sys, "prefix", str(target))
    before = {str(path): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="aliases|shared site-packages|harness runtime"):
        runtime.initialize(root, Config(), goal="Preserve isolation.")
    assert not (root / ".labgoblin" / "project.json").exists()
    assert all(Path(path).read_bytes() == body for path, body in before.items())


def test_environment_preparation_failure_never_publishes_a_ready_project(tmp_path, monkeypatch):
    goal = tmp_path / "research_goal.md"
    goal.write_text("Retain the original goal", encoding="utf-8")
    def fail(self, directory):
        (directory / "partial.txt").write_text("Incomplete preparation", encoding="utf-8")
        raise OSError("deliberate venv creation failure")
    monkeypatch.setattr(venv.EnvBuilder, "create", fail)
    with pytest.raises(OSError, match="deliberate venv creation failure"):
        runtime.initialize(tmp_path, Config())
    assert goal.read_text() == "Retain the original goal"
    assert not (tmp_path / ".labgoblin" / "project.json").exists()
    assert not (tmp_path / "labgoblin.toml").exists()
    assert (tmp_path / ".venv" / "partial.txt").read_text() == "Incomplete preparation"


def git(root, *arguments, check=True):
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root),
                           "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "core.hooksPath=",
                           "-c", "commit.gpgSign=false", "-c", "user.name=Fixture",
                           "-c", "user.email=fixture@example.invalid", *arguments],
                          check=check, capture_output=True, text=True, encoding="utf-8", timeout=30)


def test_init_prepares_git_without_commits_and_supports_selected_rollback(tmp_path, capsys):
    root = tmp_path / "research"
    assert main(["init", str(root), "--no-research-venv", "--max-event-log-bytes", "2048"]) == 0
    capsys.readouterr()
    assert Path(git(root, "rev-parse", "--show-toplevel").stdout.strip()) == root
    assert git(root, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    assert not git(root, "diff", "--cached", "--name-only").stdout.strip()
    for path in (".labgoblin/project.json", ".labgoblin.lock", ".venv/package.py",
                 "inputs/data.json", ".env.local"):
        assert git(root, "check-ignore", "--", path, check=False).returncode == 0
    code = root / "candidate.py"
    code.write_bytes(b"value = 1\n")
    notes = root / "my-experiment" / "result.txt"
    notes.parent.mkdir()
    notes.write_bytes(b"Uncommitted evidence\n")
    assert git(root, "check-ignore", "--", "my-experiment/result.txt", check=False).returncode == 1
    git(root, "add", "--", "candidate.py")
    git(root, "commit", "-m", "A meaningful local checkpoint")
    code.write_bytes(b"value = 2\n")
    git(root, "restore", "--source=HEAD", "--", "candidate.py")
    assert code.read_bytes() == b"value = 1\n"
    assert notes.read_bytes() == b"Uncommitted evidence\n"
    assert Config.load(root).max_event_log_bytes == 2048
    assert not Config.load(root).archive_workspace


@pytest.mark.parametrize("nested", [False, True])
def test_init_preserves_existing_git_history_index_configuration_and_ignores(tmp_path, nested):
    git(tmp_path, "init", "--quiet", "--template=")
    (tmp_path / "existing.txt").write_bytes(b"Retained staged work\n")
    git(tmp_path, "add", "--", "existing.txt")
    original = {name: (tmp_path / ".git" / name).read_bytes() for name in ("HEAD", "config", "index")}
    root = tmp_path / "research" if nested else tmp_path
    root.mkdir(exist_ok=True)
    ignore = root / ".gitignore"
    previous = b"# My rules\r\ncustom-output/\r\n"
    ignore.write_bytes(previous)
    runtime.initialize(root, Config(), research_venv=False)
    assert {name: (tmp_path / ".git" / name).read_bytes() for name in original} == original
    assert ignore.read_bytes().startswith(previous)
    assert git(root, "check-ignore", "--", "custom-output/data.bin", check=False).returncode == 0
    assert Path(git(root, "rev-parse", "--show-toplevel").stdout.strip()) == tmp_path
    if nested:
        assert not (root / ".git").exists()


def test_missing_git_is_explicit_and_opt_out_is_model_free(tmp_path, monkeypatch):
    original = runtime.shutil.which
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None if name == "git" else original(name))
    failed = tmp_path / "missing"
    with pytest.raises(ValueError, match="init --no-git"):
        runtime.initialize(failed, Config(), research_venv=False)
    assert not (failed / ".labgoblin" / "project.json").exists()
    assert not (failed / "labgoblin.toml").exists()
    root = tmp_path / "opt-out"
    assert main(["init", str(root), "--no-git", "--no-research-venv"]) == 0
    assert not (root / ".git").exists() and not (root / ".gitignore").exists()
    assert runtime.status(root)["state"] == "initialized"


def test_git_failure_preserves_user_files_and_never_publishes_ready_state(tmp_path, monkeypatch):
    (tmp_path / "research_goal.md").write_bytes(b"The operator's goal\n")
    (tmp_path / ".gitignore").write_bytes(b"My original rules\n")
    def fail(*args, **kwargs):
        raise RuntimeError("Deliberate git init failure")
    monkeypatch.setattr(runtime.payload, "probe", fail)
    with pytest.raises(RuntimeError, match="Deliberate git init failure"):
        runtime.initialize(tmp_path, Config(), research_venv=False)
    assert (tmp_path / ".gitignore").read_bytes() == b"My original rules\n"
    assert (tmp_path / "research_goal.md").read_bytes() == b"The operator's goal\n"
    assert not (tmp_path / ".labgoblin" / "project.json").exists()


def test_git_ignore_hardlink_is_rejected_without_changing_its_target(tmp_path):
    original = tmp_path / "original-ignore"
    original.write_bytes(b"Retain these exact rules\n")
    root = tmp_path / "research"
    root.mkdir()
    (root / ".gitignore").hardlink_to(original)
    with pytest.raises(ValueError, match="file alias"):
        runtime.initialize(root, Config(), research_venv=False)
    assert original.read_bytes() == b"Retain these exact rules\n"
    assert not (root / ".git").exists() and not (root / ".labgoblin" / "project.json").exists()


def test_frozen_retention_defaults_preserve_admitted_policy_without_rewriting(tmp_path):
    old = Config().to_dict()
    old.pop("archive_workspace")
    old.pop("max_event_log_bytes")
    original = json.dumps(old)
    historical = Config.parse(old, frozen=True)
    assert historical.archive_workspace and historical.max_event_log_bytes == 1024 ** 3
    assert json.dumps(old) == original
    (tmp_path / "labgoblin.toml").write_text(Config().toml(), encoding="utf-8")
    current = Config.load(tmp_path)
    assert not current.archive_workspace and current.max_event_log_bytes == 8 * 1024 ** 3
    assert Config.parse(current.to_dict(), frozen=True) == current
