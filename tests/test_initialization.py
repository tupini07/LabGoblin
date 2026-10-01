import json
from pathlib import Path
import sys
import subprocess

import pytest

from labgoblin import cli, initialization as init
from labgoblin.config import initial_config
from labgoblin.state import State


def proposal(root, **changes):
    return init.InitDraft.parse({"configuration": initial_config(root.name),
                                "goal": "Measure a synthetic baseline; unknown inputs remain unverified.",
                                "protocol": "Compare held-out means, including negative results.",
                                "constraints": ["No private data uploads."], **changes}, root)


@pytest.mark.parametrize("flag", ["--non-interactive", "--json"])
def test_basic_modes_never_import_sdk(tmp_path, monkeypatch, capsys, flag):
    import builtins
    original = builtins.__import__

    def guard(name, *args, **kwargs):
        assert name != "copilot" and name != "labgoblin.setup_assistant"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    assert cli.main(["init", "--project", str(tmp_path), "--ledger", str(tmp_path / "unused.db"), flag]) == 0
    assert not capsys.readouterr().err
    assert not (tmp_path / "unused.db").exists()


def test_non_terminal_fails_before_writes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["init", "--project", str(tmp_path)]) == 1
    assert "--non-interactive" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_preview_has_no_writes_and_publishes_final_paths_and_provenance(tmp_path):
    root = tmp_path / "campaign"
    reviewed = init.prepare(root, tmp_path / "unused.db", proposal(root))
    assert not root.exists()
    result = init.apply(reviewed)
    state = State.open(root / ".labgoblin")
    assert result["assisted"] and result["approval"] == reviewed.digest
    assert state.campaign()["invocations"] == 0
    with state.db.read() as conn:
        config = json.loads(conn.execute("SELECT content FROM configs").fetchone()[0])
        assert config["config_path"] == str(root / "labgoblin.toml")
        sources = {r["kind"]: dict(r) for r in conn.execute("SELECT * FROM sources")}
        assert sources["goal"]["origin"] == "setup-assistant"
        assert json.loads(sources["protocol"]["metadata"])["operator_approved"]
        assert "setup_approval" in sources and "handoff" not in sources
    assert not (tmp_path / "unused.db").exists()
    assert not init.initialization_marker(root).exists()


@pytest.mark.parametrize("target", ["CLAUDE.md", "research_goal.md", ".gitignore"])
def test_changed_preimage_invalidates_approval(tmp_path, target):
    reviewed = init.prepare(tmp_path, tmp_path / "unused.db", proposal(tmp_path))
    (tmp_path / target).write_text("Concurrent human content", encoding="utf-8")
    with pytest.raises(ValueError, match="review initialization again"):
        init.apply(reviewed)
    assert (tmp_path / target).read_text() == "Concurrent human content"
    assert not (tmp_path / ".labgoblin").exists()


def test_mutated_draft_invalidates_approval(tmp_path):
    reviewed = init.prepare(tmp_path, tmp_path / "unused.db", proposal(tmp_path))
    reviewed.draft.configuration["campaign"]["max_invocations"] = 0
    with pytest.raises(ValueError, match="review initialization again"):
        init.apply(reviewed)
    assert not list(tmp_path.iterdir())


def test_same_bytes_at_replaced_file_identity_invalidate_approval(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(b"Original\n")
    reviewed = init.prepare(tmp_path, tmp_path / "unused.db", proposal(tmp_path))
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    with pytest.raises(ValueError, match="review initialization again"):
        init.apply(reviewed)


@pytest.mark.parametrize("goal", [b"# Real human goal\n", b""])
def test_basic_preserves_human_goal_and_instruction_sections(tmp_path, goal):
    (tmp_path / "research_goal.md").write_bytes(goal)
    (tmp_path / "CLAUDE.md").write_bytes(b"Human instructions\n")
    reviewed = init.basic(tmp_path, tmp_path / "unused.db")
    init.apply(reviewed)
    assert (tmp_path / "research_goal.md").read_bytes() == goal
    assert (tmp_path / "CLAUDE.md").read_text().startswith("Human instructions\n")
    with State.open(tmp_path / ".labgoblin").db.read() as conn:
        assert bytes(conn.execute("SELECT body FROM sources WHERE kind='instruction_backup'").fetchone()[0]) == b"Human instructions\n"


@pytest.mark.parametrize("boundary", ["state", "goal", "protocol", "directive", "approval", "completion"])
def test_publication_failure_rolls_back_only_owned_writes(tmp_path, monkeypatch, boundary):
    (tmp_path / "CLAUDE.md").write_text("Keep this\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    reviewed = init.prepare(tmp_path, tmp_path / "unused.db", proposal(tmp_path))
    create, source, directive, unlink = State.create, State.source, State.directive, init.unlink_file

    def failure():
        raise OSError("injected publication failure")

    def create_or_fail(*args, **kwargs):
        state = create(*args, **kwargs)
        if boundary == "state":
            failure()
        return state

    def source_or_fail(self, kind, *args, **kwargs):
        if boundary == kind or boundary == "approval" and kind == "setup_approval":
            failure()
        return source(self, kind, *args, **kwargs)

    def directive_or_fail(self, *args, **kwargs):
        if boundary == "directive":
            failure()
        return directive(self, *args, **kwargs)

    def unlink_or_fail(path, **kwargs):
        if boundary == "completion" and path == init.initialization_marker(tmp_path):
            monkeypatch.setattr(init, "unlink_file", unlink)
            failure()
        return unlink(path, **kwargs)

    monkeypatch.setattr(State, "create", create_or_fail)
    monkeypatch.setattr(State, "source", source_or_fail)
    monkeypatch.setattr(State, "directive", directive_or_fail)
    monkeypatch.setattr(init, "unlink_file", unlink_or_fail)
    with pytest.raises(OSError, match="injected"):
        init.apply(reviewed)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_concurrent_edit_during_failure_is_not_rolled_back(tmp_path, monkeypatch):
    reviewed = init.prepare(tmp_path, tmp_path / "unused.db", proposal(tmp_path))

    def failure(*args, **kwargs):
        (tmp_path / "CLAUDE.md").write_text("Concurrent human changes", encoding="utf-8")
        raise OSError("injected source failure")

    monkeypatch.setattr(State, "source", failure)
    with pytest.raises(RuntimeError, match="rollback incomplete"):
        init.apply(reviewed)
    assert (tmp_path / "CLAUDE.md").read_text() == "Concurrent human changes"
    assert json.loads(init.initialization_marker(tmp_path).read_text())["phase"] == "incomplete"
    with pytest.raises(ValueError, match="incomplete"):
        State.open(tmp_path / ".labgoblin")


def test_concurrently_created_database_is_never_deleted(tmp_path, monkeypatch):
    reviewed = init.prepare(tmp_path, tmp_path / "unused.db", proposal(tmp_path))
    create = State.create

    def race(*args, **kwargs):
        state_dir = tmp_path / ".labgoblin"
        state_dir.mkdir()
        (state_dir / "labgoblin.db").write_bytes(b"Concurrent human-owned database")
        return create(*args, **kwargs)

    monkeypatch.setattr(State, "create", race)
    with pytest.raises(RuntimeError, match="ownership is unproven"):
        init.apply(reviewed)
    assert (tmp_path / ".labgoblin" / "labgoblin.db").read_bytes() == b"Concurrent human-owned database"
    assert init.initialization_marker(tmp_path).exists()


@pytest.mark.parametrize("goal", ["CLAUDE.md", "labgoblin.toml", ".labgoblin.lock", ".labgoblin/goal.md"])
def test_conflicting_document_destinations_fail_before_creation(tmp_path, goal):
    raw = initial_config(tmp_path.name)
    raw["project"]["research_goal"] = goal
    with pytest.raises(ValueError, match="conflict|runtime state"):
        init.prepare(tmp_path, tmp_path / "unused.db", proposal(tmp_path, configuration=raw))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("boundary", ["files", "database", "completion"])
def test_abrupt_exit_cannot_leave_a_falsely_ready_campaign(tmp_path, boundary):
    script = """
import os, sys
from pathlib import Path
from labgoblin import initialization as init
from labgoblin.state import State
root=Path(sys.argv[1])
reviewed=init.basic(root,root/'unused.db')
if sys.argv[2]=='files':
    State.create=lambda *a,**k: os._exit(77)
elif sys.argv[2]=='database':
    State.source=lambda *a,**k: os._exit(77)
else:
    original=init.unlink_file
    def stop(path,**kw):
        if path==init.initialization_marker(root): os._exit(77)
        return original(path,**kw)
    init.unlink_file=stop
init.apply(reviewed)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), boundary], timeout=20,
                            capture_output=True, text=True)
    assert result.returncode == 77, result.stdout + result.stderr
    assert init.initialization_marker(tmp_path).is_file()
    with pytest.raises(ValueError, match="incomplete"):
        State.open(tmp_path / ".labgoblin")
