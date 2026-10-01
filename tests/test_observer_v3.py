import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import time
import uuid

import pytest

from tests.test_controller import fixture
from tests.test_workspace import request
from labgoblin import journal, workspace
from labgoblin.campaign import Campaign
from labgoblin.config import ChatSettings, parse_config
from labgoblin.dashboard_chat import ObserverService, SDKObserver, worker_main
from labgoblin.dashboard_data import EvidenceReader
from labgoblin.evidence import publish_bytes
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchEnvelope, LaunchKey, LaunchReceipt, Resources, canonical, fingerprint
from labgoblin.worker import prepare_runtime


def snapshot(state):
    with state.db.read() as conn:
        return list(conn.iterdump())


def test_reader_retains_history_and_does_not_write_or_expose_payloads(tmp_path):
    config, state, ledger, raw = fixture(tmp_path)
    state.hypothesis("claim", "Controlled replication preserves the score.")
    attempt = workspace.submit(state, config, request())
    Campaign(config, state=state, ledger=ledger).run(no_agent=True)
    source = state.source("journal_import", b"Negative finding: predictor did not improve.", origin="operator")
    old_goal = state.source("goal", b"Historical goal", origin="operator", head="goal")
    state.source("goal", b"Different current goal", origin="operator", head="goal")
    config.root.joinpath("labgoblin.toml").write_text("broken TOML [", encoding="utf-8")
    reader = EvidenceReader(config.config_path)
    before = snapshot(state)
    data = reader.read("campaign_status", {})
    assert data["data"]["campaign"]["generation"] == 1
    assert data["data"]["budgets"]["managed_invocations"]["used"] == 0
    assert reader.read("get_experiment", {"id": attempt["id"]})["data"]["experiments"][0]["metrics"] == {"score": 42}
    assert reader.read("hypothesis_detail", {"id": "claim"})["data"]["statement"].startswith("Controlled")
    history = reader.read("archive_search", {"query": "predictor"})["data"]
    assert history["matches"][0]["id"] == source and history["searched"]
    assert reader.read("source_entry", {"id": old_goal})["data"]["text"] == "Historical goal"
    assert reader.read("research_document", {"document": "goal"})["data"]["text"] == "Different current goal"
    assert "zero matches do not prove" in reader.read("archive_search", {"query": "no-such-term"})["data"]["coverage"]
    assert reader.read("agent_activity", {})["data"]["events"]
    observation = state.collection(attempt["id"])["observation_ids"][0]
    detail = reader.read("evidence_observation", {"id": observation})["data"]
    assert detail["metrics"] == {"score": 42}
    text = json.dumps(reader.read("get_experiment", {"id": attempt["id"]}))
    assert '"environment":' not in text and '"argv":' not in text and '"access_path":' not in text
    assert snapshot(state) == before
    for name, args in [("shell", {}), ("source_entry", {}), ("archive_search", {"query": "a", "after": -1}),
                       ("get_experiment", {"id": attempt["id"], "path": "secret"}), ("source_entry", {"id": "missing"})]:
        with pytest.raises(ValueError):
            reader.read(name, args)


def test_source_byte_pages_and_non_scientific_backup_boundary(tmp_path):
    config, state, _, _ = fixture(tmp_path)
    body = ("\u00e9" * 10000).encode()
    source = state.source("journal_import", body, origin="operator")
    reader = EvidenceReader(config.config_path)
    first = reader.read("source_entry", {"id": source})["data"]
    second = reader.read("source_entry", {"id": source, "offset": first["returned_bytes"]})["data"]
    import base64
    assert base64.b64decode(first["base64"]) + base64.b64decode(second["base64"]) == body
    assert first["has_more"] and not second["has_more"]
    backup = state.source("instruction_backup", b"Private instructions", origin="operator")
    with pytest.raises(ValueError, match="unavailable"):
        reader.read("source_entry", {"id": backup})
    assert not reader.read("archive_search", {"query": "Private"})["data"]["matches"]


def test_strict_observer_config_is_separate_from_research_envelope(tmp_path):
    config, _, _, raw = fixture(tmp_path)
    raw["dashboard"] = {"chat": {"enabled": True, "cpus": 2, "memory_mb": 4096, "max_invocations": 3}}
    settings = parse_config(raw, config.config_path).chat
    assert settings.cpus == 2 and settings.max_invocations == 3
    for change in ({"cpus": 0}, {"memory_mb": False}, {"max_invocations": 0}, {"timeout_seconds": 0}, {"unknown": True}):
        raw["dashboard"]["chat"] = change
        with pytest.raises(ValueError):
            parse_config(raw, config.config_path)


def authorization(config, state, ledger, *, owner=None, command=None):
    owner = owner or uuid.uuid4().hex
    token = uuid.uuid4().hex
    resources = Resources(1, 256)
    ledger.request(token, owner, token, "observer", resources,
                   {"kind": "observer", "handle": own_handle(owner)}, native=True)
    grant = ledger.reserve(token)
    root = ledger.path.parent / "observers" / owner / token
    root.mkdir(parents=True)
    envelope = LaunchEnvelope(
        LaunchKey(owner, 1, token, token, token), "observer", tuple(command or (sys.executable, "-c", "pass")),
        str(root), str(root), str(state.path), str(ledger.path), ledger.id, 10, resources, config.revision,
        metadata={"cpu_ids": json.loads(grant["native_cpus"])})
    envelope = prepare_runtime(state, envelope, root=root.parent)
    return envelope


def test_owned_observer_real_native_payload_separate_usage_and_quiescence(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    command = [sys.executable, "-c", "import psutil; print(psutil.Process().cpu_affinity())"]
    envelope = authorization(config, state, ledger, command=command)
    before = snapshot(state)
    ledger.arm_consumer(envelope, max_invocations=1)
    with pytest.raises(ValueError, match="quiescent"):
        ledger.release(envelope.key.grant_id, owner_id=envelope.key.campaign_id)
    target = Path(envelope.root) / "envelope.json"
    publish_bytes(target, canonical(asdict(envelope)))
    assert worker_main("--observer-supervisor", target) == 0
    row = ledger.consumer_run(envelope.key.grant_id)
    assert row["phase"] == "quiescent" and ledger.grant(envelope.key.grant_id)["state"] == "released"
    receipt = LaunchReceipt.parse(json.loads(row["receipt"]))
    assert receipt.metadata["cpu_ids"] == envelope.metadata["cpu_ids"]
    assert ledger.observer_usage(envelope.key.campaign_id)["committed"] == 1
    assert snapshot(state) == before
    other = authorization(config, state, ledger, owner=envelope.key.campaign_id)
    with pytest.raises(ValueError, match="allowance"):
        ledger.arm_consumer(other, max_invocations=1)
    ledger.release(other.key.grant_id, owner_id=other.key.campaign_id)


def test_uncertain_observer_stays_reserved_and_late_receipt_recovers(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    envelope = authorization(config, state, ledger)
    ledger.arm_consumer(envelope, max_invocations=1)
    assert ledger.claim_consumer(envelope, own_handle(envelope.key.nonce))
    assert not ledger.claim_consumer(envelope, own_handle(envelope.key.nonce))
    assert ledger.recover_consumers()[0]["state"] == "awaiting_owned_receipt"
    receipt = LaunchReceipt(envelope.key, envelope.digest, "completed", True, 0.1, returncode=0)
    with pytest.raises(ValueError, match="ownership"):
        ledger.finish_consumer(replace(receipt, envelope_digest="wrong"))
    publish_bytes(Path(envelope.root) / "backend-receipt.json", canonical(asdict(receipt)))
    assert ledger.recover_consumers()[0]["state"] == "released"


def test_capacity_wait_cancels_without_arming_or_research_writes(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    owner = uuid.uuid4().hex
    ledger.request("occupier", owner, "work", "observer", Resources(2, 2048),
                   {"kind": "observer", "handle": own_handle(owner)}, native=True)
    ledger.reserve("occupier")
    driver = SDKObserver()
    reader = EvidenceReader(config.config_path)
    before = snapshot(state)
    emissions = []

    async def check():
        work = asyncio.create_task(driver.answer(ChatSettings(cpus=1, memory_mb=256), reader, "No inference", lambda *args: emissions.append(args)))
        await asyncio.sleep(0.4)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work

    asyncio.run(check())
    assert emissions and "Waiting" in emissions[0][1]["text"]
    assert ledger.observer_usage(driver.id)["committed"] == 0
    assert all(row["owner_id"] == owner for row in ledger.rows())
    ledger.release("occupier", owner_id=owner)
    assert snapshot(state) == before
