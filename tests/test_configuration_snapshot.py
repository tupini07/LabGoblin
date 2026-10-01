from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest
import tomli_w

from labgoblin import agent, briefing, cli, workspace
from labgoblin.campaign import Campaign
from labgoblin.config import load_config, parse_config, restore_config
from labgoblin.protocol import canonical, identifier
from tests.test_controller import fixture, drain
from tests.test_workspace import request


def configured(tmp_path):
    config, state, ledger, raw = fixture(tmp_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    return config, state, ledger, raw


def test_typed_snapshot_roundtrips_all_categories_and_rejects_corruption(tmp_path):
    config, _, _, raw = configured(tmp_path)
    raw["runners"].update(wsl={"kind": "wsl", "python": "python3", "distro": "Ubuntu"},
                          docker={"kind": "docker", "python": "python3", "image": "prepared", "context": "default"})
    raw["inputs"] = {"DATA": {"path": str(tmp_path / "data"), "identity": "unknown", "prompt_access": False}}
    raw["storage"] = {"volumes": {"work": {"path": ".", "min_free_mb": 1}}}
    raw["dashboard"] = {"chat": {"model": "observer-model", "enabled": False}}
    config = parse_config(raw, config.config_path)
    snapshot = json.loads(canonical(asdict(config)))
    assert restore_config(snapshot, revision=config.revision, path=config.config_path) == config
    snapshot["runners"]["native"]["distro"] = "inapplicable field"
    with pytest.raises(ValueError, match="round-trip"):
        restore_config(snapshot, revision=config.revision, path=config.config_path)
    snapshot = asdict(config)
    snapshot["campaign"]["max_invocations"]["extra"] = 1
    with pytest.raises(ValueError, match="Unknown"):
        restore_config(snapshot, revision=config.revision, path=config.config_path)


def test_every_toml_category_stays_loaded_until_new_controller_run(tmp_path):
    original, state, ledger, raw = configured(tmp_path)
    controller = Campaign(state=state, ledger=ledger)
    with controller:
        controller.step(no_agent=True, admit=False)
        loaded, _ = state.configuration()
        assert loaded == original
        raw["project"].update(name="new title", research_goal="other-goal.md")
        raw["execution"].update(source_files=[], environment={"CHOICE": "new"})
        raw["runners"]["native"]["python"] = "changed-interpreter"
        raw["inputs"] = {"DATA": {"path": str(tmp_path / "new-data"), "identity": "declared"}}
        raw["storage"] = {"capture_bytes": 512000, "log_bytes": 1024}
        raw["agent"].update(model="changed-model", reasoning_effort="high", timeout_seconds=30, retries=2)
        raw["campaign"].update(cpus=1, memory_mb=3072, max_jobs=2, max_gpu_hours=4,
                               max_seconds=0, max_invocations=0, gpus=["GPU-declared"])
        raw["dashboard"] = {"chat": {"model": "changed-observer", "enabled": False}}
        Path(original.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
        candidate = load_config(original.config_path)
        controller.step(no_agent=True, admit=False)
        assert state.configuration()[0] == original
        assert state.budget()["managed_invocations"]["configured"] == 10
        assert controller.config == original
        Path(original.config_path).write_text("bad [TOML", encoding="utf-8")
        assert not controller.step(no_agent=True, admit=False)["errors"]
        Path(original.config_path).unlink()
        assert not controller.step(no_agent=True, admit=False)["errors"]
        with state.db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM configs").fetchone()[0] == 1
    Path(original.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    with controller:
        assert not controller.step(no_agent=True, admit=False)["errors"]
        assert state.configuration()[0] == candidate
        assert state.budget()["managed_invocations"]["unlimited"]
        assert state.budget()["elapsed_admission_seconds"]["unlimited"]
        assert controller.config == candidate


def test_invalid_startup_is_not_hot_retried_but_next_run_activates(tmp_path):
    config, state, ledger, raw = configured(tmp_path)
    Path(config.config_path).write_text("invalid [toml", encoding="utf-8")
    controller = Campaign(state=state, ledger=ledger)
    with controller:
        first = controller.step(no_agent=True)
        assert first["errors"][0]["category"] == "configuration"
        Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
        assert controller.step(no_agent=True)["errors"]
        with pytest.raises(ValueError, match="startup configuration"):
            state.configuration()
    with controller:
        assert not controller.step(no_agent=True, admit=False)["errors"]


def test_activation_requires_exact_owner_and_only_once(tmp_path):
    config, state, _, raw = configured(tmp_path)
    with pytest.raises(ValueError, match="exact controller"):
        state.configure(config, controller={})
    with Campaign(config) as controller:
        controller.step(no_agent=True, admit=False)
        raw["campaign"]["max_invocations"] = 0
        with pytest.raises(ValueError, match="restart"):
            state.configure(parse_config(raw, config.config_path), controller=controller.handle)
        assert state.budget()["managed_invocations"]["configured"] == 10


def test_separate_cli_process_uses_loaded_settings_with_broken_disk_toml(tmp_path):
    config, state, _, _ = configured(tmp_path)
    manifest = config.root / "submit.json"
    manifest.write_text(json.dumps(request()), encoding="utf-8")
    with Campaign(config) as controller:
        controller.step(no_agent=True, admit=False)
        Path(config.config_path).write_text("broken [TOML", encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "labgoblin.cli", "submit", "--project", str(config.root),
                                 "--spec", str(manifest), "--json"], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        attempt = state.attempt(json.loads(result.stdout)["attempt_id"])
        spec = json.loads(attempt["spec"])
        assert spec["configuration_revision"] == config.revision and spec["source_hashes"]


def test_submission_is_fenced_even_if_controller_starts_and_closes_during_copy(tmp_path, monkeypatch):
    config, state, _, _ = configured(tmp_path)
    original = workspace.prepare_spec

    def race(*args, **kwargs):
        spec = original(*args, **kwargs)
        with Campaign(config) as controller:
            controller.step(no_agent=True, admit=False)
        return spec

    monkeypatch.setattr(workspace, "prepare_spec", race)
    with pytest.raises(ValueError, match="changed during preparation"):
        workspace.submit(state, config, request())
    assert not state.attempts()


def test_stale_research_packet_cannot_submit_under_new_config(tmp_path):
    config, state, _, raw = configured(tmp_path)
    packet = briefing.prepare(state)
    turn_id = packet["turn_id"]
    invocation = state.reserve_invocations(turn_id, "research", ("research",))[0]
    with state.db.write() as conn:
        conn.execute("UPDATE invocations SET state='armed' WHERE id=?", (invocation,))
    raw["agent"]["model"] = "new research model"
    new = parse_config(raw, config.config_path)
    with Campaign(new) as controller:
        # Only activate; the synthetic armed slot is not a real process to recover.
        state.configure(new, controller=controller.handle)
        with pytest.raises(ValueError, match="Research turn configuration changed"):
            workspace.submit(state, config, request(), turn_id=turn_id)
    assert not state.attempts()


@pytest.mark.parametrize("change", ["runner", "resources", "environment", "inputs", "storage", "source_files"])
def test_queued_work_is_rejected_not_rewritten_under_incompatible_snapshot(tmp_path, change):
    config, state, ledger, raw = configured(tmp_path)
    attempt = workspace.submit(state, config, request(cpus=2))
    before = state.attempt(attempt["id"])["spec"]
    if change == "runner":
        raw["runners"]["native"]["python"] = "different-python"
    elif change == "resources":
        raw["campaign"]["cpus"] = 1
    elif change == "environment":
        raw["execution"]["environment"] = {"DIFFERENT": "1"}
    elif change == "inputs":
        raw["inputs"] = {"DATA": {"path": "different.csv", "identity": "not checked"}}
    elif change == "storage":
        raw["storage"] = {"capture_bytes": 512000}
    else:
        raw["execution"]["source_files"] = []
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    result = Campaign(state=state, ledger=ledger).run(no_agent=True)
    assert result["failed"] == 1 and state.attempt(attempt["id"])["status"] == "not_started"
    assert "Queued" in state.attempt(attempt["id"])["reason"]
    assert state.attempt(attempt["id"])["spec"] == before
    assert not ledger.rows()


def test_admitted_work_recovers_without_valid_new_configuration(tmp_path):
    config, state, ledger, _ = configured(tmp_path)
    attempt = workspace.submit(state, config, request())
    with Campaign(config, ledger=ledger) as first:
        assert first.step(no_agent=True)["started"] == [attempt["id"]]
        with state.db.read() as conn:
            frozen = conn.execute("SELECT envelope FROM launches").fetchone()[0]
    Path(config.config_path).write_text("broken [TOML", encoding="utf-8")
    with Campaign(state=state, ledger=ledger) as second:
        result = drain(second, lambda _: state.attempt(attempt["id"])["collection"] != "pending")
        assert result["errors"][0]["category"] == "configuration"
        assert state.attempt(attempt["id"])["status"] == "completed"
        with state.db.read() as conn:
            assert conn.execute("SELECT envelope FROM launches").fetchone()[0] == frozen
        assert not ledger.rows()


def test_restarting_changes_limits_without_resetting_accounting(tmp_path):
    config, state, ledger, raw = configured(tmp_path)
    with state.db.write() as conn:
        conn.execute("UPDATE campaign SET invocations=3,gpu_hours=1.5,elapsed=27")
    with Campaign(config, ledger=ledger) as first:
        first.step(no_agent=True, admit=False)
    raw["campaign"].update(max_seconds=0, max_invocations=0)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    before = state.campaign()
    with Campaign(state=state, ledger=ledger) as second:
        second.step(no_agent=True, admit=False)
        current = state.campaign()
        for key in ("id", "generation", "invocations", "gpu_hours", "elapsed", "authority_revision"):
            assert current[key] == before[key]
        assert state.budget()["managed_invocations"]["unlimited"]
    raw["campaign"].update(max_seconds=100, max_invocations=4)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    with Campaign(state=state, ledger=ledger) as third:
        third.step(no_agent=True, admit=False)
        budget = state.budget()
        assert budget["managed_invocations"]["remaining"] == 1
        assert budget["elapsed_admission_seconds"]["used"] == 27


def test_goal_source_uses_loaded_path_and_candidate_validation_does_not_activate(tmp_path, capsys):
    config, state, _, raw = configured(tmp_path)
    with Campaign(config) as controller:
        controller.step(no_agent=True, admit=False)
        raw["project"]["research_goal"] = "missing-other-goal.md"
        Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
        assert cli.main(["source", "set", "--kind", "goal", "--text", "New goal",
                         "--project", str(config.root), "--json"]) == 0
        capsys.readouterr()
        assert cli.main(["validate", "--project", str(config.root), "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["configuration_scope"] == "Disk candidate; not activated"
        assert state.configuration()[0].project.research_goal == "research_goal.md"
