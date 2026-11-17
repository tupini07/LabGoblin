import json
import os
from pathlib import Path
import sys

import pytest

from labgoblin import guidance, operator_profile, runtime
from labgoblin.cli import main
from labgoblin.config import Config
from labgoblin.storage import read_json


def contents(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("explicit_directory", [False, True])
def test_init_operator_is_default_external_and_model_free(tmp_path, monkeypatch, capsys, explicit_directory):
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Unexpected provider call"))
    campaign = tmp_path / "campaign's space"
    root = campaign / "research"
    cli = tmp_path / "bin" / ("labgoblin.exe" if os.name == "nt" else "labgoblin")
    cli.parent.mkdir()
    cli.write_bytes(b"Model-free console-script fixture")
    monkeypatch.setattr(operator_profile.sysconfig, "get_path", lambda _: str(cli.parent))
    directory = tmp_path / "custom-operator" if explicit_directory else campaign / "operator"
    args = ["init", str(campaign), "--no-git", "--no-research-venv"]
    if explicit_directory:
        args += ["--operator-dir", str(directory), "--operator-cli", sys.executable]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    operator = result.pop("operator")
    assert result == read_json(root / ".labgoblin" / "project.json")
    assert operator["project_id"] == result["id"]
    assert operator["project"] == str(root)
    assert operator["cli"] == str(Path(sys.executable) if explicit_directory else cli)
    assert Path(operator["profile"]).is_relative_to(directory / ".github" / "agents")
    assert Path(operator["handoff"]) == directory / "labgoblin-operator.md"
    assert operator["notes_directory"] == str(directory)
    assert (root / ".github" / "copilot-instructions.md").read_bytes() == guidance.RESEARCH.encode()
    assert not (root / ".github" / "agents").exists()
    assert not (directory / ".github" / "copilot-instructions.md").exists()
    assert runtime.status(root)["runs"] == []
    before = contents(campaign)
    assert main(["status", str(campaign)]) == 0
    assert json.loads(capsys.readouterr().out)["project_id"] == result["id"]
    assert main(["ideas", "status", str(campaign)]) == 0
    capsys.readouterr()
    assert contents(campaign) == before
    assert runtime.project_record(campaign) == runtime.project_record(root)
    assert Config.load(campaign) == Config.load(root)
    assert {path.name for path in campaign.iterdir()} == (
        {"research"} if explicit_directory else {"research", "operator"})
    handoff = Path(operator["handoff"]).read_text(encoding="utf-8")
    assert "copilot --agent labgoblin-operator" in handoff
    assert str(root).replace("\\", "\\\\") in handoff
    assert "campaign''s space" in handoff or """campaign'"'"'s space""" in handoff
    assert not (tmp_path / ".github").exists()


@pytest.mark.parametrize("entrypoint", ["cli", "api"])
def test_no_operator_needs_no_installed_cli_and_creates_only_research(tmp_path, monkeypatch, capsys, entrypoint):
    campaign = tmp_path / "campaign"
    root = campaign / "research"
    monkeypatch.setattr(operator_profile, "prepare", lambda *args: pytest.fail("Operator was disabled"))
    if entrypoint == "cli":
        assert main(["init", str(campaign), "--no-operator", "--no-git", "--no-research-venv"]) == 0
        record = json.loads(capsys.readouterr().out)
    else:
        record = runtime.initialize_campaign(campaign, Config(), operator=False, research_venv=False, git=False)
    assert record == read_json(root / ".labgoblin" / "project.json")
    assert list(campaign.iterdir()) == [root]
    assert list(tmp_path.iterdir()) == [campaign]
    assert runtime.status(root)["state"] == "initialized"


@pytest.mark.parametrize("option", ["--operator-dir", "--operator-cli"])
def test_no_operator_rejects_conflicting_options_before_writes(tmp_path, capsys, option):
    assert main(["init", str(tmp_path / "research"), "--no-operator", option, str(tmp_path)]) == 1
    assert "--no-operator cannot be combined" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert list(tmp_path.iterdir()) == []


def test_campaigns_get_separate_default_operator_bindings(tmp_path):
    results = [runtime.initialize_campaign(tmp_path / name, Config(), research_venv=False, git=False,
                                          operator_cli=sys.executable)
               for name in ("first", "second")]
    for record, name in zip(results, ("first", "second")):
        operator = record["operator"]
        assert operator["operator_directory"] == str(tmp_path / name / "operator")
        assert record["id"] in Path(operator["handoff"]).read_text(encoding="utf-8")
        other = results[1] if name == "first" else results[0]
        assert other["id"] not in Path(operator["profile"]).read_text(encoding="utf-8")
    assert not (tmp_path / ".github").exists()


def test_campaign_reinit_cannot_create_a_second_project_and_companion_can_be_reused(tmp_path, capsys):
    record = runtime.initialize_campaign(tmp_path, Config(), research_venv=False, git=False,
                                         operator_cli=sys.executable)
    before = contents(tmp_path)
    assert main(["init", str(tmp_path), "--no-git", "--no-research-venv"]) == 1
    assert "already exists" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert main(["init", record["root"], "--no-git", "--no-research-venv"]) == 1
    assert "already exist" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert main(["operator-init", str(tmp_path), "--operator-dir", str(tmp_path / "operator"),
                 "--operator-cli", sys.executable]) == 0
    assert json.loads(capsys.readouterr().out) == record["operator"]
    assert contents(tmp_path) == before


def test_existing_operator_init_preserves_research_and_user_files(project, tmp_path, monkeypatch, capsys):
    root, _, record = project
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Unexpected provider call"))
    directory = tmp_path / "operator-workspace"
    profile_directory = directory / ".github" / "agents"
    profile_directory.mkdir(parents=True)
    (profile_directory / "other.agent.md").write_bytes(b"Human-owned agent\r\n")
    (directory / "README.md").write_bytes(b"Human-owned handoff\r\n")
    before = contents(tmp_path)
    args = ["operator-init", str(root), "--operator-dir", str(directory), "--operator-cli", sys.executable]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["project_id"] == record["id"]
    after = contents(tmp_path)
    assert all(after[name] == body for name, body in before.items())
    assert set(after) - set(before) == {
        Path(first["profile"]).relative_to(tmp_path), Path(first["handoff"]).relative_to(tmp_path)}
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert contents(tmp_path) == after


@pytest.mark.parametrize("relative", [".", "operator", ".github"])
def test_operator_cannot_be_published_inside_research(project, relative):
    root, _, _ = project
    before = contents(root)
    with pytest.raises(ValueError, match="outside"):
        runtime.initialize_operator(root, root / relative, executable=sys.executable)
    assert contents(root) == before


@pytest.mark.parametrize("target", [
    (".github", "agents", "labgoblin-operator.agent.md"), ("labgoblin-operator.md",)])
@pytest.mark.parametrize("explicit_directory", [False, True])
def test_conflicting_operator_file_refuses_before_project_publication(tmp_path, target, explicit_directory):
    campaign = tmp_path / "campaign"
    directory = tmp_path / "custom-operator" if explicit_directory else campaign / "operator"
    existing = directory.joinpath(*target)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"Existing user instructions\r\n")
    before = contents(tmp_path)
    with pytest.raises(ValueError, match="different content"):
        runtime.initialize_campaign(campaign, Config(), research_venv=False, git=False,
                                    operator_dir=directory if explicit_directory else None, operator_cli=sys.executable)
    assert contents(tmp_path) == before
    assert not (campaign / "research" / ".labgoblin").exists()


def junction(path, target):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(path))
    else:
        path.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("inside_research", [False, True])
def test_default_operator_directory_never_follows_an_alias(tmp_path, inside_research):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    root = campaign / "research"
    target = root if inside_research else tmp_path / "unrelated"
    target.mkdir()
    junction(campaign / "operator", target)
    with pytest.raises(ValueError, match="default operator directory cannot be an alias"):
        runtime.initialize_campaign(campaign, Config(), research_venv=False, git=False, operator_cli=sys.executable)
    assert list(target.iterdir()) == []
    assert not (root / ".labgoblin").exists()


@pytest.mark.parametrize("target", ["destination", "profile-parent"])
def test_operator_rejects_directory_aliases_into_research(project, tmp_path, target):
    root, _, _ = project
    directory = tmp_path / "external"
    if target == "destination":
        junction(directory, root)
    else:
        directory.mkdir()
        junction(directory / ".github", root)
    before = contents(root)
    with pytest.raises(ValueError, match="outside|alias"):
        runtime.initialize_operator(root, directory, executable=sys.executable)
    assert contents(root) == before


@pytest.mark.parametrize("relative", [(".github",), (".github", "agents")])
def test_parent_operator_target_cannot_place_files_in_a_research_child(tmp_path, relative):
    root = tmp_path.joinpath(*relative)
    with pytest.raises(ValueError, match="outside"):
        runtime.initialize(root, Config(), research_venv=False, git=False,
                           operator_dir=tmp_path, operator_cli=sys.executable)
    assert contents(tmp_path) == {}


def test_operator_never_follows_existing_profile_hardlink(project, tmp_path):
    root, _, _ = project
    directory = tmp_path / "external"
    result = runtime.initialize_operator(root, directory, executable=sys.executable)
    profile = Path(result["profile"])
    linked = tmp_path / "retained-profile"
    os.link(profile, linked)
    before = contents(tmp_path)
    with pytest.raises(ValueError, match="different content"):
        runtime.initialize_operator(root, directory, executable=sys.executable)
    assert contents(tmp_path) == before


@pytest.mark.parametrize("concurrent_edit", [False, True])
def test_failed_operator_publication_only_removes_unchanged_owned_files(project, tmp_path, monkeypatch, concurrent_edit):
    root, _, _ = project
    directory = tmp_path / "external"
    original = operator_profile.publish_bytes
    handoff = directory / "labgoblin-operator.md"
    def fail(path, body):
        if path.suffix == ".md" and path.name.endswith(".agent.md"):
            if concurrent_edit:
                handoff.write_bytes(b"Concurrent user edit")
            raise OSError("Injected profile publication failure")
        original(path, body)
    monkeypatch.setattr(operator_profile, "publish_bytes", fail)
    before = contents(root)
    with pytest.raises(OSError, match="Injected"):
        runtime.initialize_operator(root, directory, executable=sys.executable)
    assert contents(root) == before
    assert handoff.exists() == concurrent_edit
    if concurrent_edit:
        assert handoff.read_bytes() == b"Concurrent user edit"


@pytest.mark.parametrize("explicit_directory", [False, True])
def test_init_failure_does_not_leave_an_operator_for_an_uninitialized_project(tmp_path, monkeypatch, explicit_directory):
    def fail(_):
        raise ValueError("Injected environment preparation failure")
    monkeypatch.setattr(runtime.research_environment, "prepare", fail)
    campaign = tmp_path / "campaign"
    root = campaign / "research"
    with pytest.raises(ValueError, match="Injected"):
        runtime.initialize_campaign(campaign, Config(), git=False,
                                    operator_dir=tmp_path if explicit_directory else None, operator_cli=sys.executable)
    assert contents(tmp_path) == {}
    assert not (root / ".labgoblin" / "project.json").exists()


@pytest.mark.parametrize("fresh", [False, True])
def test_changed_profile_parent_cannot_escape_after_preflight(tmp_path, monkeypatch, fresh):
    root = tmp_path / "research"
    if not fresh:
        runtime.initialize(root, Config(), research_venv=False, git=False)
    original = runtime.publish_bytes if fresh else operator_profile.publish_bytes
    before = contents(root) if root.exists() else {}
    def replace_parent(path, body):
        original(path, body)
        if path.name == "labgoblin-operator.md":
            junction(tmp_path / ".github", root)
    monkeypatch.setattr(runtime if fresh else operator_profile, "publish_bytes", replace_parent)
    with pytest.raises(ValueError, match="path changed to an alias"):
        if fresh:
            runtime.initialize(root, Config(), research_venv=False, git=False,
                               operator_dir=tmp_path, operator_cli=sys.executable)
        else:
            runtime.initialize_operator(root, tmp_path, executable=sys.executable)
    assert contents(root) == before
    assert not (tmp_path / "labgoblin-operator.md").exists()


def test_missing_default_operator_cli_does_not_create_project_state(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_profile.sysconfig, "get_path", lambda _: str(tmp_path / "missing-bin"))
    with pytest.raises(ValueError, match="Installed LabGoblin CLI"):
        runtime.initialize_campaign(tmp_path / "campaign", Config(), research_venv=False, git=False)
    assert contents(tmp_path) == {}
    assert not (tmp_path / "campaign" / "research" / ".labgoblin").exists()


@pytest.mark.parametrize("marker", [".labgoblin", ".labgoblin.lock", "labgoblin.toml"])
def test_init_never_nests_or_redirects_an_existing_or_incomplete_project(tmp_path, marker):
    runtime.initialize(tmp_path / "research", Config(), research_venv=False, git=False)
    if marker == ".labgoblin":
        (tmp_path / marker).mkdir()
    else:
        (tmp_path / marker).write_bytes(b"Retained")
    before = contents(tmp_path)
    with pytest.raises(ValueError, match="already exist"):
        runtime.initialize_campaign(tmp_path, Config(), operator_cli=sys.executable)
    with pytest.raises((OSError, ValueError)):
        runtime.project_record(tmp_path)
    assert contents(tmp_path) == before


def test_two_project_roots_are_ambiguous_without_mutation(tmp_path):
    runtime.initialize(tmp_path, Config(), research_venv=False, git=False)
    runtime.initialize(tmp_path / "research", Config(), research_venv=False, git=False)
    before = contents(tmp_path)
    with pytest.raises(ValueError, match="Ambiguous campaign directory"):
        runtime.status(tmp_path)
    assert runtime.status(tmp_path / "research")["state"] == "initialized"
    assert contents(tmp_path) == before


def test_incomplete_nested_project_is_not_ready_and_cannot_be_reinitialized(tmp_path, monkeypatch):
    def fail(_):
        raise ValueError("Injected environment preparation failure")
    monkeypatch.setattr(runtime.research_environment, "prepare", fail)
    with pytest.raises(ValueError, match="Injected"):
        runtime.initialize_campaign(tmp_path, Config(), git=False, operator_cli=sys.executable)
    before = contents(tmp_path)
    with pytest.raises(FileNotFoundError):
        runtime.status(tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        runtime.initialize_campaign(tmp_path, Config(), operator_cli=sys.executable)
    assert contents(tmp_path) == before


def test_outer_goal_is_not_silently_ignored_or_moved(tmp_path):
    (tmp_path / "research_goal.md").write_bytes(b"Original goal")
    with pytest.raises(ValueError, match="--goal-file"):
        runtime.initialize_campaign(tmp_path, Config())
    assert contents(tmp_path) == {Path("research_goal.md"): b"Original goal"}


def test_campaign_research_alias_is_refused_without_writes(tmp_path):
    other = tmp_path / "other"
    runtime.initialize(other, Config(), research_venv=False, git=False)
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    junction(campaign / "research", other)
    before = contents(other)
    with pytest.raises(ValueError, match="research directory cannot be an alias"):
        runtime.initialize_campaign(campaign, Config())
    with pytest.raises(ValueError, match="research directory cannot be an alias"):
        runtime.status(campaign)
    assert contents(other) == before


def test_operator_rejects_missing_or_legacy_identity_without_writes(tmp_path):
    root = tmp_path / "research"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        runtime.initialize_operator(root, tmp_path, executable=sys.executable)
    (root / ".labgoblin").mkdir()
    (root / ".labgoblin" / "labgoblin.db").write_bytes(b"Legacy identity sentinel")
    before = contents(tmp_path)
    with pytest.raises(ValueError, match="legacy campaign"):
        runtime.initialize_operator(root, tmp_path, executable=sys.executable)
    assert contents(tmp_path) == before
