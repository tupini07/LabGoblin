from dataclasses import asdict
import json
import sqlite3
import time

import pytest

from labgoblin.config import initial_config, parse_config
from labgoblin.protocol import Handoff, LaunchEnvelope, LaunchKey, Resources, canonical, fingerprint, identifier
from labgoblin.state import State


@pytest.fixture
def state(tmp_path):
    cfg = parse_config(initial_config("fixture", "copilot"), tmp_path / "labgoblin.toml")
    result = State.create(cfg, tmp_path / "machine.db")
    result.bind_ledger(tmp_path / "machine.db", "machine")
    return result


def spec(state, key="work", **changes):
    work_id = identifier()
    return {"id": work_id, "key": key, "request": {"key": key},
            "cpus": 1, "memory_mb": 128, "gpus": [], "seconds": 10,
            "root": str(state.root / "attempts" / work_id),
            "experiment_id": key, **changes}


def turn(state, *, kind="research", owned=True):
    campaign = state.campaign()
    turn_id, packet_id = identifier(), identifier()
    events = state.pending_events()
    content = {"references": [], "authority_revision": campaign["authority_revision"]}
    with state.db.write() as conn:
        conn.execute("""INSERT INTO packets(id,turn_id,generation,watermark,content,digest,ready,created)
            VALUES(?,?,?,?,?,?,1,?)""",
                     (packet_id, turn_id, campaign["generation"], max((e["seq"] for e in events), default=0),
                      canonical(content).decode(), fingerprint(content), time.time()))
        conn.executemany("INSERT INTO packet_events(packet_id,event_id) VALUES(?,?)",
                         [(packet_id, e["id"]) for e in events])
        conn.execute("""INSERT INTO turns(id,generation,kind,packet_id,state,created,revision)
            VALUES(?,?,?,?,'prepared',?,?)""",
                     (turn_id, campaign["generation"], kind, packet_id, time.time(), campaign["revision"]))
    value = Handoff(turn_id, packet_id, "Compared controls.", "Need replication.", "Inspect evidence.",
                    "continue", "More work remains.")
    if owned:
        invocation = state.reserve_invocations(turn_id, kind, (kind,))[0]
        resources = Resources(1, 128)
        allocation = state.allocation(turn_id, kind, {"resources": asdict(resources)}, campaign["revision"])
        assert state.granted(allocation["token"])
        state.arm(LaunchEnvelope(
            LaunchKey(state.id, campaign["generation"], invocation, allocation["token"], identifier()),
            kind, ("provider-double",), str(state.root.parent), str(state.root / "turns" / turn_id),
            str(state.path), str(state.ledger_identity()[0]), "machine", 10, resources,
            campaign["config_revision"]))
    return value


def handoff(value, **changes):
    return Handoff.parse({**asdict(value), **changes, "evidence": changes.get("evidence", [])})


def allocate(state, work):
    value = state.allocation(work["id"], "attempt",
                             {"resources": {"cpus": work["cpus"], "memory_mb": work["memory_mb"], "gpus": []}},
                             state.campaign()["revision"])
    assert state.granted(value["token"])
    return value


def envelope(state, work, allocation):
    campaign = state.campaign()
    return LaunchEnvelope(
        LaunchKey(state.id, campaign["generation"], work["id"], allocation["token"], identifier()),
        "attempt", ("program",), str(state.root.parent), work["root"], str(state.path),
        str(state.ledger_identity()[0]), "machine", 10, Resources(work["cpus"], work["memory_mb"]),
        campaign["config_revision"])


def receipt(value, **changes):
    return {"protocol": 3, "key": asdict(value.key), "envelope_digest": value.digest,
            "status": "completed", "quiescent": True, "returncode": 0, "elapsed": 1, **changes}


def accept(state, value):
    with state.db.read() as conn:
        row = conn.execute("""SELECT l.envelope,i.id FROM invocations i JOIN launches l ON l.nonce=i.nonce
            WHERE i.turn_id=?""", (value.turn_id,)).fetchone()
    launched = LaunchEnvelope.parse(json.loads(row["envelope"]))
    state.finish_launch(launched.key.nonce, receipt(launched, metadata={
        "result_capture": {"handoff_digest": fingerprint(asdict(value))}}))
    state.released(launched.key.grant_id)
    state.accept_handoff(value, invocation_id=row["id"])


def test_open_missing_state_never_creates_directory(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        State.open(path)
    assert not path.exists()


def test_reject_old_database_without_migration(tmp_path):
    root = tmp_path / ".labgoblin"
    root.mkdir()
    path = root / "labgoblin.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=2")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported campaign database"):
        State.open(root)
    assert path.read_bytes() == before
    assert [p.name for p in root.iterdir()] == ["labgoblin.db"]


def test_read_connections_are_read_only_and_init_is_exclusive(state):
    with state.db.read() as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE campaign SET revision=99")
    cfg = parse_config(initial_config("fixture"), state.root.parent / "labgoblin.toml")
    with pytest.raises(FileExistsError):
        State.create(cfg, state.root.parent / "machine.db")
    assert state.campaign()["revision"] == 0


def test_duplicate_control_cannot_undo_new_intent(state):
    state.control("pause", "pause-one", 0)
    resumed = state.control("resume", "resume-one", 1)
    state.control("pause", "pause-two", 2)
    assert state.control("resume", "resume-one", 1) == resumed
    assert state.campaign()["operator_mode"] == "paused"
    assert state.campaign()["revision"] == 3
    with pytest.raises(ValueError, match="different payload"):
        state.control("stop", "resume-one", 1)
    with pytest.raises(ValueError, match="revision conflict"):
        state.control("resume", "stale", 1)


@pytest.mark.parametrize("disposition,progress", [
    ("continue", "research"), ("wait", "wait"), ("blocked", "blocked"), ("finalize", "finalize"),
])
def test_paused_result_retains_owned_progression(state, disposition, progress):
    current = turn(state)
    state.control("pause", "pause", 0)
    value = handoff(current, disposition=disposition,
                    stopping_criterion="All controls assessed." if disposition == "finalize" else "")
    accept(state, value)
    assert state.campaign()["operator_mode"] == "paused"
    assert state.campaign()["progress"] == progress
    state.control("resume", "resume", 1)
    assert state.campaign()["progress"] == ("research" if progress in ("blocked", "wait") else progress)
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1
    accept(state, value)
    assert state.campaign()["revision"] == 2


def test_stop_converges_after_crash_with_queued_attempt(state):
    work = spec(state)
    state.enqueue(work)
    state.control("stop", "stop", 0)
    reopened_state = State.open(state.root)
    for _ in range(3):
        reopened_state.converge_stop()
    assert reopened_state.attempt(work["id"])["status"] == "cancelled"
    assert reopened_state.campaign()["operator_mode"] == "stopped"
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM closure_members").fetchone()[0] == 1
        assert conn.execute("SELECT admitted FROM closure_members").fetchone()[0] == 0
    with pytest.raises(ValueError, match="explicit reopen"):
        state.control("resume", "resume", 1)
    state.control("reopen", "reopen", 1)
    assert state.campaign()["generation"] == 2
    state.enqueue(spec(state))


def test_late_old_handoff_does_not_reopen_stop_or_new_generation(state):
    current = turn(state)
    state.control("stop", "stop", 0)
    value = handoff(current, disposition="finalize", stopping_criterion="Observed.")
    accept(state, value)
    state.converge_stop()
    state.control("reopen", "reopen", 1)
    accept(state, value)
    assert state.campaign()["generation"] == 2
    assert state.campaign()["progress"] == "research"


def test_acceptance_rolls_back_acknowledgement_and_sources_together(state, monkeypatch):
    import labgoblin.state as implementation
    current = turn(state)
    event = state.pending_events()[0]
    value = handoff(current, evidence=[{"event_id": event["id"], "disposition": "assessed", "reason": "Read."}])
    original = implementation._source

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected crash after source insertion")

    monkeypatch.setattr(implementation, "_source", interrupted)
    with pytest.raises(RuntimeError, match="injected crash"):
        accept(state, value)
    assert state.pending_events()[0]["id"] == event["id"]
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM dispositions").fetchone()[0] == 0
    monkeypatch.setattr(implementation, "_source", original)
    accept(state, value)
    assert event["id"] not in {e["id"] for e in state.pending_events()}


def test_nonprefix_acknowledgements_leave_holes(state):
    initial = state.pending_events()[0]["id"]
    later = state.event("evidence", {"score": 1})
    current = turn(state)
    accept(state, handoff(current, evidence=[
        {"event_id": later, "disposition": "assessed", "reason": "Read"}]))
    assert initial in {e["id"] for e in state.pending_events()}
    assert later not in {e["id"] for e in state.pending_events()}


def test_unowned_references_cannot_be_accepted(state):
    event_id = state.pending_events()[0]["id"]
    current = turn(state)
    with pytest.raises(ValueError, match="not delivered"):
        accept(state, handoff(current, evidence=[
            {"event_id": event_id, "disposition": "assessed", "reason": "Read", "references": ["invented"]}]))


def test_prepared_human_handoff_is_not_an_owned_provider_result(state):
    current = turn(state, owned=False)
    with pytest.raises(ValueError, match="owned provider result"):
        state.accept_handoff(current, invocation_id="not-an-invocation")
    assert state.campaign()["progress"] == "research"


def test_pause_wins_before_arm_but_not_after_authorization(state):
    work = spec(state)
    state.enqueue(work)
    allocation = allocate(state, work)
    value = envelope(state, work, allocation)
    state.control("pause", "pause", 0)
    with pytest.raises(ValueError, match="allocation"):
        state.arm(value)
    state.released(allocation["token"])
    state.control("resume", "resume", 1)
    second = allocate(state, work)
    value = envelope(state, work, second)
    state.arm(value)
    state.control("pause", "pause-two", 2)
    assert state.claim_launch(value, {"pid": 1, "created": 1, "token": value.key.nonce})
    assert not state.claim_launch(value, {"pid": 2, "created": 1, "token": value.key.nonce})
    state.finish_launch(value.key.nonce, receipt(value))
    state.released(allocation["token"])
    assert state.campaign()["operator_mode"] == "paused"
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM allocations WHERE token=?", (second["token"],)).fetchone()[0] \
            == "release_pending"


def test_armed_without_receipt_never_releases_and_late_identity_only_clears_own_blocker(state):
    work = spec(state)
    state.enqueue(work)
    allocation = allocate(state, work)
    value = envelope(state, work, allocation)
    state.arm(value)
    with pytest.raises(ValueError, match="armed"):
        state.release_pending(allocation["token"])
    state.blocker(f"launch-{value.key.nonce}", "launch", "No handle yet", work["id"])
    state.blocker("storage", "storage", "Volume unavailable")
    assert state.claim_launch(value, {"pid": 123, "created": 456, "token": value.key.nonce})
    assert [r["id"] for r in state.campaign()["blockers"]] == ["storage"]
    altered = receipt(value)
    altered["key"]["nonce"] = "stale"
    with pytest.raises(ValueError, match="incarnation"):
        state.finish_launch(value.key.nonce, altered)
    assert state.attempt(work["id"])["status"] == "running"


def test_receipt_recovery_does_not_load_current_toml(state):
    work = spec(state)
    state.enqueue(work)
    allocation = allocate(state, work)
    value = envelope(state, work, allocation)
    state.arm(value)
    (state.root.parent / "labgoblin.toml").write_text("not [valid toml", encoding="utf-8")
    recovered = State.open(state.root)
    recovered.finish_launch(value.key.nonce, receipt(value))
    recovered.finish_launch(value.key.nonce, receipt(value))
    assert recovered.attempt(work["id"])["status"] == "completed"
    assert len([e for e in recovered.pending_events() if e["kind"] == "execution"]) == 1
    with pytest.raises(ValueError, match="identity changed"):
        recovered.bind_ledger(state.ledger_identity()[0], "replacement")


def test_claim_statement_frozen_at_admission(state):
    work = spec(state, hypothesis_id="h1", hypothesis_description="The control reduces variance.")
    state.enqueue(work)
    allocation = allocate(state, work)
    state.arm(envelope(state, work, allocation))
    with pytest.raises(ValueError, match="immutable"):
        state.hypothesis("h1", "A different claim.")
    state.hypothesis("h2", "A different claim.", supersedes="h1")
    state.hypothesis("h1", "The control reduces variance.", label="Readable label")


def test_concurrent_provisional_grants_do_not_overbook_campaign(state):
    first, second = spec(state, cpus=2), spec(state, key="second", cpus=2)
    requests = []
    for work in (first, second):
        state.enqueue(work)
        requests.append(state.allocation(work["id"], "attempt",
                                         {"resources": {"cpus": 2, "memory_mb": 128, "gpus": []}}, 0))
    assert state.granted(requests[0]["token"])
    assert not state.granted(requests[1]["token"])
    with state.db.read() as conn:
        row = conn.execute("SELECT state,reason FROM allocations WHERE token=?",
                           (requests[1]["token"],)).fetchone()
        assert row["state"] == "release_pending" and "Concurrent" in row["reason"]


def test_elapsed_clock_does_not_go_backwards_or_reset(state):
    with state.db.write() as conn:
        conn.execute("UPDATE campaign SET started=100,observed_wall=100")
    assert state.tick(110) == 10
    assert state.tick(90) == 10
    assert state.tick(120) == 20
    state.control("pause", "pause", 0)
    assert state.tick(130) == 30
    assert State.open(state.root).tick(150) == 50


def test_monotonic_controller_high_water_counts_time_during_clock_rollback(state):
    assert state.tick(90, minimum_elapsed=500) == 0
    with state.db.write() as conn:
        conn.execute("UPDATE campaign SET started=100,observed_wall=100")
    assert state.tick(110) == 10
    assert state.tick(90, minimum_elapsed=15) == 15
    assert state.tick(91, minimum_elapsed=16) == 16


def test_invocation_budget_reserves_closure_and_counts_canaries(state):
    raw = initial_config("fixture", "copilot")
    raw["campaign"]["max_invocations"] = 1
    state.configure(parse_config(raw, state.root.parent / "labgoblin.toml"))
    current = turn(state, owned=False)
    with pytest.raises(ValueError, match="final analysis"):
        state.reserve_invocations(current.turn_id, "research", ("research",))
    raw["campaign"]["max_invocations"] = 4
    raw["agent"].update(sandbox=True, copilot_home=str(state.root / "sandbox"))
    state.configure(parse_config(raw, state.root.parent / "labgoblin.toml"))
    ids = state.reserve_invocations(current.turn_id, "research", ("canary", "research"))
    assert len(ids) == 2
    assert state.reserve_invocations(current.turn_id, "research", ("canary", "research")) == ids
    with pytest.raises(ValueError, match="bundle changed"):
        state.reserve_invocations(current.turn_id, "research", ("research",))


def test_late_continue_cannot_undo_budget_closure(state):
    current = turn(state)
    state.close_admission("Elapsed budget exhausted.")
    accept(state, current)
    assert state.campaign()["progress"] == "finalize"
    assert state.campaign()["generation_state"] == "sealed"


def test_new_goal_fences_old_finalize_but_keeps_its_record(state):
    current = turn(state)
    state.source("goal", b"A new goal with different stopping criteria.", origin="operator", head="goal", notify=True)
    accept(state, handoff(current, disposition="finalize", stopping_criterion="Old criterion met."))
    assert state.campaign()["progress"] == "research"
    assert state.campaign()["generation_state"] == "open"
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1


def test_only_one_turn_can_own_research_or_maintenance(state):
    turn(state)
    with pytest.raises(sqlite3.IntegrityError):
        turn(state, kind="compact")
