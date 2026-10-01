"""Canonical-only branding, without weakening retained ownership checks."""

from dataclasses import asdict, replace
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import tomli_w

from labgoblin import agent, backends, briefing, cli, payload, worker
from labgoblin.config import initial_config, load_config, parse_config
from labgoblin.evidence import publish_bytes
from labgoblin.paths import database_path, environment_value, project_paths
from labgoblin.protocol import LaunchReceipt, PreExecutionError, canonical
from labgoblin.scheduler import MachineSample, ResourceLedger, ledger_path
from labgoblin.state import State
from tests.test_cli_v3 import call
from tests.test_dashboard import complete_job, dump, get, job, serve
from tests.test_dashboard_chat import FakeObserver, service
from tests.test_launch import admission
from tests.test_providers import fixture as provider_fixture
from tests.test_workspace import PROGRAM


@pytest.fixture
def campaign(tmp_path):
    project = tmp_path / "research"
    project.mkdir()
    path = project / "labgoblin.toml"
    raw = initial_config("local-research")
    raw["execution"]["source_files"] = ["experiment.py"]
    path.write_text(tomli_w.dumps(raw), encoding="utf-8")
    project.joinpath("experiment.py").write_text(PROGRAM, encoding="utf-8")
    config = parse_config(raw, path)
    ledger = ResourceLedger.create(
        tmp_path / "appdata" / "labgoblin" / "resources.db",
        sampler=lambda _: MachineSample((0, 1), 8192, 8192))
    ledger.configure(2, 4096, (), 0)
    state = State.create(config, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    state.source("goal", b"Retain exact evidence.", origin="operator", head="goal")
    return SimpleNamespace(config=config, state=state, ledger=ledger, project=project)


def snapshot(root):
    # SQLite mode=ro can create coordination sidecars; SQL dumps check retained data.
    return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file() and not path.name.endswith((".db-wal", ".db-shm"))}


def test_canonical_init_instructions_packet_and_no_legacy_stores(tmp_path, capsys):
    code, _ = call(capsys, "init", "--project", tmp_path, "--ledger", tmp_path / "resources.db",
                   "--install-copilot-instructions", "--json")
    assert code == 0
    paths = project_paths(tmp_path)
    assert paths.config.name == "labgoblin.toml" and paths.state.name == ".labgoblin"
    assert database_path(paths.state).name == "labgoblin.db"
    assert load_config(tmp_path).state_dir == paths.state
    for path in (tmp_path / "CLAUDE.md", tmp_path / ".github" / "copilot-instructions.md"):
        body = path.read_text(encoding="utf-8")
        assert "LabGoblin" in body and "labgoblin submit" in body and "LABGOBLIN_OUTPUT_DIR" in body
        assert "xgenius" not in body.lower()
    packet = briefing.prepare(State.open(paths.state))
    assert packet["content"]["cli_argv"] == [sys.executable, "-m", "labgoblin.cli"]
    assert ".labgoblin.lock" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert not (tmp_path / "resources.db").exists()
    assert not any("xgenius" in name for name in snapshot(tmp_path))


def test_status_dashboard_and_exact_evidence_do_not_write(campaign, capsys, monkeypatch):
    observation_id = complete_job(campaign, job(campaign))["observation_ids"][0]
    before_sql = dump(campaign.state)
    before_files = snapshot(campaign.project)
    before_ledger = snapshot(campaign.ledger.path.parent)

    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only surfaces must not probe or infer")

    monkeypatch.setattr(agent, "inspect_provider", forbidden)
    monkeypatch.setattr("labgoblin.processes.process_state", forbidden)
    monkeypatch.setenv("LABGOBLIN_RESOURCE_DB", str(campaign.project / "must-not-exist.db"))
    for args in (("status",), ("budget",), ("machine", "status"),
                 ("evidence", "observation", "--id", observation_id)):
        code, data = call(capsys, *args, "--project", campaign.project, "--json")
        assert code == 0, data
    with serve(campaign.config.config_path, chat=False) as base:
        for path in ("/", "/evidence", "/jobs", "/resources", "/journal",
                     "/observation?id=" + observation_id):
            status, _, body = get(base, path)
            assert status == 200, (path, body)
            assert b"LabGoblin" in body
        assert b"labgoblin-chat-token" in get(base)[2]
    assert dump(campaign.state) == before_sql
    assert snapshot(campaign.project) == before_files
    assert snapshot(campaign.ledger.path.parent) == before_ledger


def test_old_project_names_are_not_discovered_or_modified(tmp_path, capsys):
    (tmp_path / "xgenius.toml").write_text("old config", encoding="utf-8")
    (tmp_path / ".xgenius").mkdir()
    (tmp_path / ".xgenius" / "xgenius.db").write_bytes(b"old state")
    before = snapshot(tmp_path)
    assert project_paths(tmp_path).state == tmp_path / ".labgoblin"
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path)
    with pytest.raises(FileNotFoundError):
        State.open(tmp_path / ".xgenius")
    for args in (("status",), ("init", "--existing-config")):
        code, data = call(capsys, *args, "--project", tmp_path, "--json")
        assert code == 1 and data["error"]["type"] == "FileNotFoundError"
    assert snapshot(tmp_path) == before
    assert not (tmp_path / ".labgoblin").exists()


def test_missing_or_broken_config_does_not_block_recorded_status(campaign, capsys):
    path = Path(campaign.config.config_path)
    for exists in (True, False):
        if exists:
            path.write_text("broken [", encoding="utf-8")
        else:
            path.unlink()
        before = snapshot(campaign.project)
        before_sql = dump(campaign.state)
        code, data = call(capsys, "status", "--project", campaign.project, "--json")
        assert code == 0 and data["campaign"]["id"] == campaign.state.id
        with pytest.raises((ValueError, FileNotFoundError)):
            load_config(campaign.project)
        with serve(path, chat=False) as base:
            assert get(base)[0] == 200
        assert snapshot(campaign.project) == before
        assert dump(campaign.state) == before_sql


def test_default_ledger_and_environment_have_no_old_name_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("LABGOBLIN_RESOURCE_DB", raising=False)
    monkeypatch.setenv("XGENIUS_RESOURCE_DB", str(tmp_path / "ignored.db"))
    old = tmp_path / "xgenius" / "resources.db"
    old.parent.mkdir()
    old.write_bytes(b"retained-old-ledger")
    before = snapshot(tmp_path)
    assert ledger_path() == tmp_path / "labgoblin" / "resources.db"
    with pytest.raises(FileNotFoundError):
        ResourceLedger()
    assert snapshot(tmp_path) == before
    monkeypatch.setenv("LABGOBLIN_RESOURCE_DB", str(tmp_path / "explicit.db"))
    assert ledger_path() == tmp_path / "explicit.db"
    monkeypatch.setenv("LABGOBLIN_RESOURCE_DB", "")
    with pytest.raises(ValueError, match="LABGOBLIN_RESOURCE_DB must not be empty"):
        ledger_path()
    monkeypatch.delenv("LABGOBLIN_TURN_ID", raising=False)
    monkeypatch.setenv("XGENIUS_TURN_ID", "ignored")
    assert environment_value("TURN_ID") is None


def test_owned_instructions_preserve_unrelated_text_and_backup(campaign, capsys):
    path = campaign.project / "CLAUDE.md"
    old = f"# Unrelated rules\n\n{cli.SECTION_START}\nOld instructions\n{cli.SECTION_END}\nRetain this.\n"
    path.write_text(old, encoding="utf-8")
    old_bytes = path.read_bytes()
    assert call(capsys, "instructions", "--project", campaign.project, "--target", "claude", "--json")[0] == 0
    body = path.read_text(encoding="utf-8")
    assert body.startswith("# Unrelated rules") and body.endswith("Retain this.\n")
    assert body.count(cli.SECTION_START) == 1 and "labgoblin submit" in body
    with campaign.state.db.read() as conn:
        backup = conn.execute("SELECT body FROM sources WHERE kind='instruction_backup'").fetchone()
        assert backup and bytes(backup["body"]) == old_bytes


def test_payload_emits_only_canonical_variables(monkeypatch):
    for suffix in ("OUTPUT_DIR", "ATTEMPT_ID", "INPUT_DATA"):
        monkeypatch.delenv("XGENIUS_" + suffix, raising=False)
    env = payload.process_environment({"cpus": 1, "output": "exact-output", "work_id": "exact-attempt",
                                       "inputs": {"DATA": {"access_path": "exact-input"}}})
    for suffix, value in (("OUTPUT_DIR", "exact-output"), ("ATTEMPT_ID", "exact-attempt"), ("INPUT_DATA", "exact-input")):
        assert env["LABGOBLIN_" + suffix] == value
        assert "XGENIUS_" + suffix not in env


def test_provider_envelope_emits_only_canonical_variables(tmp_path):
    config, state, _, turn_id, invocations, grant = provider_fixture(tmp_path)
    envelope = agent.prepare(state, config, turn_id, invocations[0], grant)
    for suffix in ("PROJECT", "TURN_ID", "PACKET_ID", "INVOCATION_ID", "RESOURCE_DB"):
        assert envelope.environment["LABGOBLIN_" + suffix]
    assert not any(key.startswith("XGENIUS_") for key in envelope.environment)


def test_frozen_manifest_and_late_receipt_recover_without_rewrite(admission):
    state, ledger, envelope = admission
    frozen = worker.prepare_runtime(state, envelope)
    root = Path(frozen.metadata["runtime"]["root"])
    before = snapshot(root)
    worker.verify_runtime(state, frozen)
    state.arm(frozen)
    assert worker.reconcile(state)["unresolved"]
    assert ledger.grant(frozen.key.grant_id)["state"] == "granted"
    wrong = replace(frozen, key=replace(frozen.key, nonce="wrong-owner"))
    receipt_path = worker.launch_directory(frozen) / "receipt.json"
    publish_bytes(receipt_path, canonical(asdict(LaunchReceipt(
        wrong.key, wrong.digest, "completed", True, 1, returncode=0))))
    assert worker.reconcile(state)["unresolved"]
    assert ledger.grant(frozen.key.grant_id)["state"] == "granted"
    receipt_path.unlink()
    publish_bytes(receipt_path, canonical(asdict(LaunchReceipt(
        frozen.key, frozen.digest, "completed", True, 1, returncode=0))))
    assert worker.reconcile(state) == {"recovered": [frozen.key.work_id], "unresolved": []}
    assert ledger.grant(frozen.key.grant_id)["state"] == "released"
    assert canonical(json.loads(state.launch(frozen.key.nonce)["envelope"])) == canonical(asdict(frozen))
    assert snapshot(root) == before
    assert not (root / "xgenius").exists()


@pytest.mark.parametrize("namespace", [None, "xgenius", "unrecognized"])
def test_frozen_runtime_rejects_missing_and_noncanonical_namespace(admission, namespace):
    state, _, envelope = admission
    frozen = worker.prepare_runtime(state, envelope)
    runtime = dict(frozen.metadata["runtime"])
    if namespace is None:
        runtime.pop("namespace")
    else:
        runtime["namespace"] = namespace
    corrupt = replace(frozen, metadata={**frozen.metadata, "runtime": runtime})
    with pytest.raises(PreExecutionError, match="namespace"):
        worker.verify_runtime(state, corrupt)


def test_docker_ownership_uses_exact_canonical_labels(admission, monkeypatch):
    _, _, envelope = admission
    envelope = replace(envelope, metadata={"runner": {
        "kind": "docker", "context": "prepared-fixture", "endpoint": "local-fixture", "image_id": "exact-image"}})
    calls = []
    value = {"id": "exact-container", "image": "exact-image", "restart": "no",
             "labels": {"labgoblin.nonce": envelope.key.nonce, "labgoblin.envelope": envelope.digest,
                        "labgoblin.campaign": envelope.key.campaign_id}, "state": {"Running": True}}
    monkeypatch.setattr(backends, "validate_docker_endpoint", lambda _: "local-fixture")
    monkeypatch.setattr(backends, "command", lambda argv: calls.append(argv) or json.dumps(value))
    assert backends.inspect_payload(envelope) == "alive"
    assert f"labgoblin-{envelope.key.nonce}" in calls[0]
    value["labels"]["labgoblin.nonce"] = "wrong-owner"
    with pytest.raises(ValueError, match="incarnation labels"):
        backends.inspect_payload(envelope)
    value["labels"] = {key.replace("labgoblin.", "xgenius."): entry for key, entry in value["labels"].items()}
    with pytest.raises(ValueError, match="incarnation labels"):
        backends.inspect_payload(envelope)


def test_canonical_module_command(campaign):
    result = subprocess.run([sys.executable, "-m", "labgoblin.cli", "--project", str(campaign.project), "status", "--json"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["campaign"]["id"] == campaign.state.id


def test_assets_original_credit_and_no_runtime_aliases():
    root = Path(__file__).resolve().parent.parent
    for name in ("dashboard.css", "dashboard.js", "dashboard-chat.js"):
        assert files("labgoblin").joinpath("static", name).read_bytes()
    readme = root.joinpath("README.md").read_text(encoding="utf-8")
    assert "# LabGoblin" in readme and "not a drop-in replacement" in readme
    assert "https://github.com/tupini07/LabGoblin" in readme
    assert "https://github.com/roger-creus/xgenius" in readme
    assert "Roger Creus Castanyer" in root.joinpath("LICENSE").read_text(encoding="utf-8")
    assert "10.5281/zenodo.19038735" in root.joinpath("CITATION.cff").read_text(encoding="utf-8")
    assert not (root / "xgenius" / "__init__.py").exists()
    for path in root.joinpath("labgoblin").rglob("*"):
        if path.suffix in (".py", ".js", ".css"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "xgenius" in line.lower():
                    assert (path.name == "__init__.py" and "derived from" in line
                            or path.name == "backends.py" and '".xgenius"' in line)


def test_supported_installation_uses_checkout_not_assumed_pypi_release(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    guide = (root / "docs" / "local-research.md").read_text(encoding="utf-8")
    assert "git clone https://github.com/tupini07/LabGoblin.git" in readme
    assert "pinned SDK is a normal LabGoblin dependency" in readme
    assert 'python -m pip install -e ".[docker-build]"' in guide
    for path in (root / "README.md", root / "CLAUDE.md", *root.joinpath("docs").glob("*.md"),
                 root / "examples" / "local-synthetic" / "README.md"):
        body = path.read_text(encoding="utf-8")
        assert "labgoblin[" not in body
        assert "pip install labgoblin" not in body
    monkeypatch.setattr("importlib.metadata.version", lambda _: "incompatible-fixture-version")
    with pytest.raises(ValueError, match="from the LabGoblin checkout") as error:
        backends.build(None, None, "container", tmp_path, [])
    assert 'python -m pip install -e ".[docker-build]"' in str(error.value)


@pytest.mark.skipif(environment_value("BROWSER_TESTS") != "1", reason="Opt in to prepared Edge")
def test_browser_canonical_preferences_preserve_conversation(campaign):
    from playwright.sync_api import expect, sync_playwright

    driver = FakeObserver()
    observer = service(campaign, driver)
    with serve(campaign.config.config_path, observer=observer) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=environment_value("BROWSER_EXECUTABLE"))
        try:
            page = browser.new_page()
            page.goto(base)
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator("#chat-send")).to_be_enabled()
            page.get_by_label("Ask about this campaign").fill("What is retained?")
            page.locator("#chat-send").click()
            expect(page.locator("#chat-send")).to_be_enabled()
            expect(page.locator(".chat-answer")).to_contain_text("Recorded state")
            cid = next(iter(observer.conversations))
            page.get_by_role("button", name="Maximize chat").click()
            page.reload()
            expect(page.locator("#chat-panel")).to_be_visible()
            expect(page.locator("#chat-panel")).to_have_attribute("aria-modal", "true")
            expect(page.locator(".chat-question")).to_have_text("What is retained?")
            assert list(observer.conversations) == [cid] and len(driver.calls) == 1
            page.get_by_role("button", name="Restore sidebar").click()
            page.get_by_role("button", name="Close chat").click()
            page.locator("#catchup-save").click()
            page.reload()
            expect(page.locator("#catchup-status")).to_contain_text("Since your saved checkpoint")
            page.locator("#catchup-clear").click()
            page.reload()
            expect(page.locator("#catchup-status")).to_contain_text("No saved viewing checkpoint")
            page.get_by_role("button", name="Ask Copilot").click()
            expect(page.locator(".chat-question")).to_have_text("What is retained?")
            page.locator("#chat-clear").click()
            expect(page.locator(".chat-exchange")).to_have_count(0)
            page.reload()
            expect(page.locator(".chat-exchange")).to_have_count(0)
            assert not observer.conversations and len(driver.calls) == 1
        finally:
            browser.close()
