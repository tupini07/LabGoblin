"""Cold whole-project relocation preserves original paths, snapshots and ownership."""

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil

import pytest
import tomli_w

from labgoblin import agent, reporting, worker, workspace
from labgoblin.campaign import Campaign
from labgoblin.config import load_config, parse_config, restore_config
from labgoblin.evidence import observation
from labgoblin.processes import CampaignLease, replace_file
from labgoblin.protocol import Handoff, LaunchEnvelope, canonical, fingerprint
from labgoblin.state import State
from tests.test_controller import fixture, forbid_provider
from tests.test_dashboard import dump
from tests.test_research_loop import owned
from tests.test_state import receipt
from tests.test_workspace import request


def alias(path, target):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(path))
    else:
        path.symlink_to(target, target_is_directory=True)


@contextmanager
def moved(config, destination):
    original = config.root
    assert not destination.exists()
    replace_file(original, destination)
    alias(original, destination)
    try:
        yield State.open(original / ".labgoblin")
    finally:
        if os.name == "nt":
            original.rmdir()
        else:
            original.unlink()


def configured(tmp_path):
    config, state, ledger, raw = fixture(tmp_path)
    raw["agent"]["resources"]["memory_mb"] = 128
    (config.root / "scratch").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    raw["storage"] = {"volumes": {
        "project": {"path": ".", "min_free_mb": 1},
        "scratch": {"path": "scratch", "min_free_mb": 1},
        "external": {"path": str(external), "min_free_mb": 1},
    }}
    raw["inputs"] = {"data": {"path": "experiment.py", "prompt_access": False}}
    config = parse_config(raw, config.config_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    with Campaign(config, state=state, ledger=ledger) as controller:
        state.configure(config, controller=controller.handle)
    return config, state, ledger, raw


def snapshots(state):
    with state.db.read() as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM configs ORDER BY id")]


def test_restoration_normalizes_only_project_paths_without_changing_saved_bytes(tmp_path):
    config, state, _, _ = configured(tmp_path)
    snapshot = json.loads(canonical(asdict(config)))
    before = canonical(snapshot), dump(state)
    with moved(config, tmp_path / "relocated") as relocated:
        current = load_config(config.config_path)
        for requested in (config.config_path, current.config_path):
            restored = restore_config(snapshot, revision=config.revision, path=requested)
            assert restored == current
            assert restored.root == (tmp_path / "relocated").resolve()
            assert restored.storage.volumes["project"].path == str(restored.root)
            assert restored.storage.volumes["scratch"].path == str(restored.root / "scratch")
            assert restored.storage.volumes["external"] == config.storage.volumes["external"]
            assert restored.agent == config.agent and restored.runners == config.runners
            assert restored.inputs == config.inputs and restored.revision == config.revision
        assert (canonical(snapshot), dump(relocated)) == before


def test_same_revision_activates_after_move_without_duplicate_or_rewritten_snapshot(tmp_path):
    config, state, ledger, _ = configured(tmp_path)
    before = snapshots(state)
    with moved(config, tmp_path / "relocated") as relocated:
        current = load_config(config.config_path)
        with Campaign(state=relocated, ledger=ledger) as controller:
            assert not controller.step(no_agent=True, admit=False)["errors"]
            assert relocated.configuration()[0] == current
            assert snapshots(relocated) == before
            Path(current.config_path).unlink()
            assert relocated.configuration()[0] == current
            assert not controller.step(no_agent=True, admit=False)["errors"]
        assert snapshots(relocated) == before


@pytest.mark.parametrize("change", ["bytes", "watermark", "external", "field", "type"])
def test_relocated_snapshot_still_rejects_nonpath_corruption(tmp_path, change):
    config, _, _, _ = configured(tmp_path)
    snapshot = json.loads(canonical(asdict(config)))
    with moved(config, tmp_path / "relocated"):
        if change == "bytes":
            snapshot["storage"]["unexpected_bytes"] = 1
        elif change == "watermark":
            snapshot["storage"]["volumes"]["project"]["unexpected"] = 1
        elif change == "external":
            # Existing strict round-trip behavior must not follow unrelated retargeted paths.
            external = tmp_path / "external"
            external.rename(tmp_path / "other")
            alias(external, tmp_path / "other")
        elif change == "field":
            snapshot["runners"]["native"]["image"] = "inapplicable"
        else:
            snapshot["storage"] = []
        try:
            with pytest.raises(ValueError, match="Unknown|round-trip|table"):
                restore_config(snapshot, revision=config.revision, path=config.config_path)
        finally:
            if change == "external":
                external.rmdir() if os.name == "nt" else external.unlink()


@pytest.mark.parametrize("kind", ["copy", "move", "state-only", "config-link"])
def test_same_bytes_without_whole_project_alias_are_not_a_relocated_identity(tmp_path, kind):
    config, _, _, _ = configured(tmp_path)
    snapshot = asdict(config)
    target = tmp_path / "not-an-alias"
    if kind == "copy":
        shutil.copytree(config.root, target)
    elif kind == "move":
        config.root.rename(target)
    elif kind == "state-only":
        target.mkdir()
        shutil.copy2(config.config_path, target / "labgoblin.toml")
        alias(target / ".labgoblin", config.root / ".labgoblin")
    else:
        target.mkdir()
        os.link(config.config_path, target / "labgoblin.toml")
    try:
        with pytest.raises(ValueError, match="identity/path"):
            restore_config(snapshot, revision=config.revision, path=target / "labgoblin.toml")
    finally:
        if kind == "state-only":
            link = target / ".labgoblin"
            link.rmdir() if os.name == "nt" else link.unlink()


def test_retargeted_original_alias_fails_closed_during_loaded_controller(tmp_path):
    config, _, ledger, _ = configured(tmp_path)
    with moved(config, tmp_path / "relocated") as state:
        with Campaign(state=state, ledger=ledger) as controller:
            state.configure(load_config(config.config_path), controller=controller.handle)
            other = tmp_path / "unrelated"
            other.mkdir()
            config.root.rmdir() if os.name == "nt" else config.root.unlink()
            alias(config.root, other)
            with pytest.raises(ValueError, match="identity/path"):
                state.configuration()


def test_old_and_new_paths_share_the_same_exclusive_reader_lock(tmp_path):
    config, _, _, _ = configured(tmp_path)
    with moved(config, tmp_path / "relocated") as state:
        with CampaignLease(Path(config.config_path).parent / ".labgoblin", exclusive=True):
            with pytest.raises(ValueError, match="live readers"):
                with state.db.read():
                    pass
        assert state.campaign()["controller"] is None


def test_cold_copy_cutover_locks_destination_and_retains_complete_rollback(tmp_path):
    config, state, _, _ = configured(tmp_path)
    target, backup = tmp_path / "relocated", tmp_path / "rollback"
    shutil.copytree(config.root, target)
    try:
        with CampaignLease(target / ".labgoblin", exclusive=True):
            with CampaignLease(state.root, exclusive=True):
                with pytest.raises(ValueError, match="live readers"):
                    State.open(state.root)
            # All processes must be stopped: Windows cannot rename the source with its lock open.
            replace_file(config.root, backup)
            alias(config.root, target)
            for root in (config.root, target):
                with pytest.raises(ValueError, match="live readers"):
                    State.open(root / ".labgoblin")
        restored = State.open(config.root / ".labgoblin")
        assert restored.id == state.id and restored.root == target / ".labgoblin"
        assert dump(State.open(backup / ".labgoblin")) == dump(restored)
        assert restore_config(asdict(config), revision=config.revision,
                              path=config.config_path) == load_config(config.config_path)
    finally:
        if config.root.is_symlink() or config.root.resolve() == target.resolve():
            config.root.rmdir() if os.name == "nt" else config.root.unlink()


def test_queued_work_keeps_spec_and_runs_with_local_watermarks_after_move(tmp_path, monkeypatch):
    config, state, ledger, raw = configured(tmp_path)
    # Source snapshots and declared inputs cannot overlap.
    raw["inputs"] = {}
    config = parse_config(raw, config.config_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    attempt = workspace.submit(state, config, request())
    frozen = attempt["spec"]
    monkeypatch.setattr(agent, "inspect_provider", forbid_provider)
    with moved(config, tmp_path / "relocated") as relocated:
        result = Campaign(state=relocated, ledger=ledger).run(no_agent=True)
        assert not result["errors"], result
        assert relocated.attempt(attempt["id"])["status"] == "completed"
        assert relocated.attempt(attempt["id"])["spec"] == frozen
        assert not ledger.rows()
        assert relocated.campaign()["invocations"] == 0
        observation_ids = relocated.collection(attempt["id"])["observation_ids"]
        assert len(observation_ids) == 1
        captured = observation(relocated.db, observation_ids[0])
        assert json.loads(captured["body"]) == {"score": 42}
        new = workspace.submit(relocated, load_config(config.config_path), request(key="after-move"))
        assert Path(json.loads(new["spec"])["root"]).is_relative_to(relocated.root)
        assert not Campaign(state=relocated, ledger=ledger).run(no_agent=True)["errors"]
        assert relocated.attempt(new["id"])["status"] == "completed"
        assert not ledger.rows() and relocated.campaign()["invocations"] == 0


def test_completed_launch_evidence_and_report_view_remain_exact_after_move(tmp_path, monkeypatch):
    config, state, ledger, raw = configured(tmp_path)
    raw["inputs"] = {}
    config = parse_config(raw, config.config_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    attempt = workspace.submit(state, config, request())
    monkeypatch.setattr(agent, "inspect_provider", forbid_provider)
    assert not Campaign(config, state=state, ledger=ledger).run(no_agent=True)["errors"]
    view = reporting.seal_view(state)
    with state.db.read() as conn:
        launch = dict(conn.execute("SELECT * FROM launches").fetchone())
    frozen = LaunchEnvelope.parse(json.loads(launch["envelope"]))
    bodies = {path: path.read_bytes() for path in (
        worker.launch_directory(frozen) / "envelope.json",
        worker.launch_directory(frozen) / "receipt.json")}
    original_view = reporting.page(state.db, view["id"])
    original_observation = observation(state.db, state.collection(attempt["id"])["observation_ids"][0])
    before = dump(state)
    with moved(config, tmp_path / "relocated") as relocated:
        worker.verify_runtime(relocated, frozen)
        assert not worker.reconcile(relocated)["unresolved"]
        current_view = reporting.page(relocated.db, view["id"])
        assert current_view.pop("retrieved_at") >= original_view.pop("retrieved_at")
        assert current_view == original_view
        assert observation(relocated.db, original_observation["id"]) == original_observation
        assert dump(relocated) == before
        assert all(path.read_bytes() == body for path, body in bodies.items())


def test_completed_owned_handoff_is_accepted_after_move_with_original_packet_and_receipt(tmp_path):
    config, state, _, _ = configured(tmp_path)
    packet, frozen = owned(config, state)
    handoff = Handoff(packet["turn_id"], packet["id"], "Preserved result", "Existing rationale",
                      "Assess the next result", "continue", "More work remains")
    outcome = receipt(frozen, usage={"totalNanoAiu": 1000000000},
                      metadata={"result_capture": {"handoff_digest": fingerprint(asdict(handoff))}})
    state.finish_launch(frozen.key.nonce, outcome)
    state.released(frozen.key.grant_id)
    before = state.launch(frozen.key.nonce)
    config_history = snapshots(state)
    with moved(config, tmp_path / "relocated") as relocated:
        worker.verify_runtime(relocated, frozen)
        relocated.accept_handoff(handoff, invocation_id=frozen.key.work_id)
        assert relocated.launch(frozen.key.nonce) == before
        assert snapshots(relocated) == config_history
        with relocated.db.read() as conn:
            assert conn.execute("SELECT state FROM turns WHERE id=?", (packet["turn_id"],)).fetchone()[0] == "accepted"
            stored = conn.execute("SELECT content FROM packets WHERE id=?", (packet["id"],)).fetchone()[0]
        assert json.loads(stored) == packet["content"]


def test_queued_watermark_edits_are_not_hidden_by_relocation(tmp_path):
    config, state, _, raw = configured(tmp_path)
    raw["inputs"] = {}
    config = parse_config(raw, config.config_path)
    attempt = workspace.submit(state, config, request())
    with moved(config, tmp_path / "relocated"):
        raw["storage"]["volumes"]["project"]["min_free_mb"] += 1
        changed = parse_config(raw, config.config_path)
        with pytest.raises(ValueError, match="storage settings changed"):
            workspace.queued_runner(changed, json.loads(attempt["spec"]))
