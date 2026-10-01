import json
from pathlib import Path

import pytest
import tomli_w

from tests.test_cli_v3 import call
from tests.test_controller import fixture
from tests.test_dashboard import serve
from tests.test_workspace import request
from labgoblin import cli, workspace
from labgoblin.campaign import Campaign
from labgoblin.evidence import read_json, retention_candidates
from labgoblin.processes import CampaignLease
from labgoblin.state import State


def prepared(tmp_path):
    config, state, ledger, raw = fixture(tmp_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    return config, state, ledger, raw


def test_stop_converges_without_controller_or_valid_config_and_retains_diagnostics(tmp_path, capsys):
    config, state, ledger, _ = prepared(tmp_path)
    attempt = workspace.submit(state, config, request())
    state.blocker("configuration", "configuration", "Current configuration cannot be parsed")
    Path(config.config_path).write_text("broken [TOML", encoding="utf-8")
    code, value = call(capsys, "stop", "--project", config.root, "--json")
    assert code == 1
    assert value["campaign"]["operator_mode"] == "stopped"
    assert value["campaign"]["blockers"][0]["id"] == "configuration"
    assert state.attempt(attempt["id"])["status"] == "cancelled"
    assert state.collection(attempt["id"])["collection"] == "not_performed"
    assert not ledger.rows() and state.campaign()["invocations"] == 0


def test_no_agent_run_returns_failure_for_failed_native_work_without_stale_controller(tmp_path, capsys):
    config, state, _, _ = prepared(tmp_path)
    config.root.joinpath("experiment.py").write_text("raise SystemExit(7)", encoding="utf-8")
    workspace.submit(state, config, request())
    code, value = call(capsys, "run", "--project", config.root, "--no-agent", "--json")
    assert code == 1 and value["failed"] == 1
    assert value["failed_work"][0]["status"] == "failed"
    assert value["campaign"]["controller"] is None


def test_validate_and_doctor_do_not_copy_or_spend_inference(tmp_path, capsys):
    config, state, _, _ = prepared(tmp_path)
    manifest = config.root / "work.json"
    manifest.write_text(json.dumps(request()), encoding="utf-8")
    code, value = call(capsys, "validate", "--project", config.root, "--spec", manifest, "--json")
    assert code == 0 and value["valid"]
    assert not (state.root / "attempts").exists()
    code, value = call(capsys, "doctor", "--project", config.root, "--provider", "--json")
    assert code == 0 and value["inference"] == "never"
    assert {r["check"] for r in value["checks"]} >= {"state", "machine", "configuration", "runner:native", "provider-help-only"}
    assert state.campaign()["invocations"] == 0


def test_machine_defaults_to_recorded_ledger_and_never_recreates_missing_bound_ledger(tmp_path, capsys, monkeypatch):
    config, state, ledger, _ = prepared(tmp_path)
    unrelated = tmp_path / "unrelated.db"
    monkeypatch.setenv("LABGOBLIN_RESOURCE_DB", str(unrelated))
    code, value = call(capsys, "machine", "status", "--project", config.root, "--json")
    assert code == 0 and value["ledger_id"] == ledger.id
    ledger.path.unlink()
    code, value = call(capsys, "machine", "configure", "--project", config.root, "--cpus", "1", "--memory-mb", "512", "--json")
    assert code == 1 and "will not be replaced" in value["error"]["message"]
    assert not ledger.path.exists() and not unrelated.exists()


def test_reset_refuses_live_readers_then_archives_and_fresh_init_is_explicit(tmp_path, capsys):
    config, state, ledger, _ = prepared(tmp_path)
    before = Path(config.config_path).read_bytes()
    with serve(config.config_path):
        code, value = call(capsys, "reset", "--project", config.root, "--confirm", state.id, "--json")
        assert code == 1 and "live readers" in value["error"]["message"]
    code, value = call(capsys, "reset", "--project", config.root, "--confirm", "wrong", "--json")
    assert code == 1
    code, value = call(capsys, "reset", "--project", config.root, "--confirm", state.id, "--json")
    assert code == 0, value
    archive = Path(value["archive"])
    assert (archive / "labgoblin.db").exists() and not state.root.exists()
    assert ledger.path.exists() and Path(config.config_path).read_bytes() == before
    code, value = call(capsys, "init", "--project", config.root, "--existing-config", "--ledger", ledger.path, "--json")
    assert code == 0 and value["campaign_id"] != state.id
    assert Path(config.config_path).read_bytes() == before
    with pytest.raises(ValueError, match="identity changed"):
        state.campaign()


def test_reset_refuses_pending_or_unknown_owned_work(tmp_path, capsys):
    config, state, _, _ = prepared(tmp_path)
    attempt = workspace.submit(state, config, request())
    code, value = call(capsys, "reset", "--project", config.root, "--confirm", state.id, "--json")
    assert code == 1 and "collection" in value["error"]["message"]
    with state.db.write() as conn:
        conn.execute("UPDATE attempts SET status='recovery_required' WHERE id=?", (attempt["id"],))
    code, value = call(capsys, "reset", "--project", config.root, "--confirm", state.id, "--json")
    assert code == 1 and "quiescent" in value["error"]["message"]


@pytest.mark.parametrize("race", ["generation", "authority"])
def test_submission_rechecks_generation_and_authority_after_copy(tmp_path, monkeypatch, race):
    config, state, _, _ = prepared(tmp_path)
    original = workspace.prepare_spec

    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        if race == "generation":
            state.control("stop", "stop", 0)
            state.converge_stop()
            state.control("reopen", "reopen", 1)
        else:
            state.directive("New mandatory evaluation requirement.")
        return value

    monkeypatch.setattr(workspace, "prepare_spec", changed)
    with pytest.raises((ValueError, RuntimeError), match="generation|authority"):
        workspace.submit(state, config, request())
    assert not state.attempts()
    markers = list((state.root / "attempts").glob("*/preparation.json"))
    assert len(markers) == 1 and read_json(markers[0])["owner_id"] == state.id
    monkeypatch.setattr("labgoblin.processes.process_state", lambda _: "dead")
    retained = retention_candidates(state)
    assert retained["dry_run"] and retained["candidates"][0]["candidate"]
    assert markers[0].exists()


def test_retention_is_dry_run_and_reference_inventory_is_bounded(tmp_path, capsys):
    config, state, ledger, _ = prepared(tmp_path)
    workspace.submit(state, config, request())
    Campaign(config, state=state, ledger=ledger).run(no_agent=True)
    code, value = call(capsys, "storage", "inventory", "--project", config.root, "--limit", "2", "--json")
    assert code == 0 and len(value["objects"]) == 2 and value["has_more"]
    assert all(item["referenced"] and item["logical_bytes"] >= 0 for item in value["objects"])
    code, value = call(capsys, "storage", "retention", "--project", config.root, "--json")
    assert code == 1 and "dry-run only" in value["error"]["message"]
    code, value = call(capsys, "storage", "retention", "--dry-run", "--project", config.root, "--json")
    assert code == 0 and not any(row["candidate"] for row in value["candidates"])
