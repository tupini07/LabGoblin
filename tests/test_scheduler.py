from contextlib import contextmanager
from dataclasses import replace
import json
import sqlite3

import pytest

from labgoblin.protocol import Resources
from labgoblin.scheduler import MachineSample, ResourceLedger


@pytest.fixture
def ledger(tmp_path):
    sample = MachineSample(tuple(range(8)), 32768, 32768, frozenset({"GPU-one", "GPU-two"}))
    value = ResourceLedger.create(tmp_path / "machine.db", sampler=lambda gpus: sample,
                                  eligibility=lambda row: (True, ""))
    value.configure(4, 4096, (), 0)
    return value


def request(ledger, token, *, owner="campaign", cpus=1, memory=128, gpus=(), native=True):
    return ledger.request(token, owner, token, "attempt", Resources(cpus, memory, gpus),
                          {"kind": "campaign", "state_dir": "unused", "generation": 1, "revision": 0},
                          native=native)


def test_ledger_open_never_creates_or_upgrades(tmp_path):
    with pytest.raises(FileNotFoundError):
        ResourceLedger(tmp_path / "missing" / "machine.db")
    assert not (tmp_path / "missing").exists()
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE capacity(cpus INTEGER)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported machine ledger"):
        ResourceLedger(path)
    assert path.read_bytes() == before


def test_replacement_ledger_rejected_and_creation_exclusive(ledger):
    with pytest.raises(ValueError, match="identity differs"):
        ResourceLedger(ledger.path, expected_id="another")
    with pytest.raises(FileExistsError):
        ResourceLedger.create(ledger.path)
    assert ResourceLedger(ledger.path, expected_id=ledger.id).id == ledger.id


def test_capacity_change_between_sample_and_transaction_cannot_overadmit(ledger):
    request(ledger, "two-cpus", cpus=2)
    original = ledger.sampler
    changed = False

    def observe(gpus):
        nonlocal changed
        if not changed:
            changed = True
            ledger.sampler = original
            ledger.configure(1, 4096, (), 0)
        return original(gpus)

    ledger.sampler = observe
    result = ledger.reserve("two-cpus")
    assert result["state"] == "pending"
    assert "Capacity changed" in result["reason"]
    result = ledger.reserve("two-cpus")
    assert result["state"] == "rejected"
    assert "cannot fit" in result["reason"]
    assert ledger.capacity()["cpus"] == 1


def test_capacity_cannot_shrink_after_grant(ledger):
    request(ledger, "job", cpus=2)
    assert ledger.reserve("job")["state"] == "granted"
    with pytest.raises(ValueError, match="while an allocation"):
        ledger.configure(1, 4096, (), 0)
    assert ledger.capacity()["cpus"] == 4


def test_delayed_release_r1_never_releases_live_r2(ledger):
    request(ledger, "r1")
    assert ledger.reserve("r1")["state"] == "granted"
    ledger.release("r1", owner_id="campaign")
    request(ledger, "r2")
    assert ledger.reserve("r2")["state"] == "granted"
    for _ in range(3):
        ledger.release("r1", owner_id="campaign")
    assert ledger.grant("r2")["state"] == "granted"
    assert request(ledger, "r1")["state"] == "released"
    with pytest.raises(ValueError, match="another owner"):
        ledger.release("r2", owner_id="other")


def test_compensation_tombstone_fences_late_reservation(ledger):
    ledger.release("revoked-before-registration", owner_id="campaign")
    row = request(ledger, "revoked-before-registration")
    assert row["state"] == "released"
    assert ledger.reserve("revoked-before-registration")["state"] == "released"
    with pytest.raises(ValueError, match="different request"):
        request(ledger, "revoked-before-registration", owner="other")


def test_native_cpu_sets_do_not_overlap_and_are_retained_on_reopen(ledger):
    for token in ("a", "b"):
        request(ledger, token, cpus=2)
    first, second = ledger.reserve("a"), ledger.reserve("b")
    assert first["state"] == second["state"] == "granted"
    assert json.loads(first["native_cpus"]) == [0, 1]
    assert json.loads(second["native_cpus"]) == [2, 3]
    reopened = ResourceLedger(ledger.path, expected_id=ledger.id)
    assert reopened.grant("b")["native_cpus"] == second["native_cpus"]
    ledger.release("a", owner_id="campaign")
    request(ledger, "c", cpus=2)
    assert json.loads(ledger.reserve("c")["native_cpus"]) == [0, 1]


def test_waiter_prevents_younger_experiments_refilling_capacity(ledger):
    ledger.configure(2, 4096, (), 0)
    request(ledger, "running", owner="a")
    ledger.reserve("running")
    request(ledger, "reasoning", owner="b", cpus=2)
    assert ledger.reserve("reasoning")["state"] == "pending"
    for index in range(5):
        token = f"younger-{index}"
        request(ledger, token, owner="a")
        result = ledger.reserve(token)
        assert result["state"] == "pending"
        assert "drain-to-fit" in result["reason"]
    ledger.release("running", owner_id="a")
    assert ledger.reserve("reasoning")["state"] == "granted"
    assert ledger.reserve("younger-0")["state"] == "pending"
    ledger.release("reasoning", owner_id="b")
    assert ledger.reserve("younger-0")["state"] == "granted"


def test_one_slot_alternates_without_permanent_reasoning_reserve(ledger):
    ledger.configure(1, 512, (), 0)
    for token in ("reasoning", "experiment", "next-reasoning"):
        request(ledger, token)
        assert ledger.reserve(token)["state"] == "granted"
        ledger.release(token, owner_id="campaign")
    assert not ledger.rows()


def test_ineligible_waiter_does_not_block_but_live_grant_is_not_released(ledger):
    ledger.configure(1, 512, (), 0)
    request(ledger, "old")
    request(ledger, "new")
    ledger.eligibility = lambda row: (row["token"] != "old", "Paused owner")
    assert ledger.reserve("new")["state"] == "granted"
    ledger.eligibility = lambda row: (False, "Owner unavailable")
    assert ledger.reserve("new")["state"] == "granted"
    assert ledger.grant("old")["state"] == "pending"
    assert ledger.grant("old")["reason"] == "Paused owner"


def test_eligibility_probe_holds_no_machine_writer_lock(ledger):
    request(ledger, "job")

    def eligible(row):
        with ledger.write() as conn:
            conn.execute("UPDATE grants SET reason='probe acquired independent writer' WHERE token=?", (row["token"],))
        return True, ""

    ledger.eligibility = eligible
    assert ledger.reserve("job")["state"] == "granted"


def test_conservative_ram_counts_reserved_maxima_against_available_memory(ledger):
    request(ledger, "resident", memory=1536)
    ledger.reserve("resident")
    sample = ledger.sampler(())
    ledger.sampler = lambda gpus: replace(sample, available_mb=2560)
    request(ledger, "new", memory=1536)
    row = ledger.reserve("new")
    assert row["state"] == "pending"
    assert "conservative" in row["reason"]


def test_external_gpu_activity_and_guest_identity_do_not_become_native_cpu_claims(ledger):
    ledger.configure(4, 4096, ("GPU-one", "GPU-two"), 0)
    sample = ledger.sampler(())
    ledger.sampler = lambda gpus: replace(sample, busy_gpus=frozenset({"GPU-one"}))
    request(ledger, "gpu", gpus=("GPU-one",), native=False)
    assert ledger.reserve("gpu")["state"] == "pending"
    ledger.sampler = lambda gpus: sample
    row = ledger.reserve("gpu")
    assert row["state"] == "granted" and json.loads(row["native_cpus"]) == []


def test_unsupported_placement_is_refused_not_silently_downgraded(ledger):
    sample = ledger.sampler(())
    ledger.sampler = lambda gpus: replace(sample, placement_supported=False)
    ledger.configure(4, 4096, (), 0)
    with pytest.raises(ValueError, match="unsupported"):
        request(ledger, "native")
    request(ledger, "guest", native=False)
    assert ledger.reserve("guest")["state"] == "granted"


def test_already_released_history_receives_no_repeated_updates(ledger, monkeypatch):
    ledger.release("old", owner_id="campaign")
    statements = []
    original = ledger.write

    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(ledger, "write", traced)
    for _ in range(10):
        ledger.release("old", owner_id="campaign")
    assert not any(statement.startswith("UPDATE") for statement in statements)
    with ledger.read() as conn:
        query_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM grants WHERE state='pending' AND eligible=1 ORDER BY sequence").fetchall()
    assert any("grants_pending" in row["detail"] for row in query_plan)
