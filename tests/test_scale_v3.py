from contextlib import contextmanager
import json
import time

from tests.test_controller import fixture
from tests.test_workspace import request
from labgoblin import workspace
from labgoblin.campaign import Campaign
from labgoblin.db import Database
from labgoblin.processes import CampaignLease


def test_terminal_history_does_not_reparse_specs_or_replay_releases(tmp_path, monkeypatch):
    measurements = []
    for count in (0, 1000, 10000):
        root = tmp_path / str(count)
        root.mkdir()
        config, state, ledger, _ = fixture(root)
        with state.db.write() as conn:
            conn.executemany("""INSERT INTO attempts(id,generation,idempotency_key,request_digest,spec,
                experiment_id,status,cpus,memory_mb,gpus,seconds,created,ended,collection,validation)
                VALUES(?,1,?,'history','intentionally not parseable',?,'completed',1,128,'[]',1,1,2,'complete','valid')""",
                [(f"old-{i}", f"old-{i}", f"old-{i}") for i in range(count)])
            conn.executemany("""INSERT INTO allocations(token,generation,work_id,kind,revision,request,digest,state,created,released)
                VALUES(?,1,?,'attempt',0,'{}','old','released',1,2)""",
                [(f"old-{i}", f"old-{i}") for i in range(count)])
        queued = workspace.submit(state, config, request())
        statements = []
        instructions = [0]
        original = state.db.read
        original_write = state.db.write

        @contextmanager
        def traced(open_connection):
            with open_connection() as conn:
                conn.set_trace_callback(statements.append)
                def progress():
                    instructions[0] += 1
                    return 0
                conn.set_progress_handler(progress, 1)
                yield conn

        with monkeypatch.context() as patch:
            patch.setattr(state.db, "read", lambda: traced(original))
            patch.setattr(state.db, "write", lambda: traced(original_write))
            started = time.perf_counter()
            with Campaign(config, state=state, ledger=ledger) as controller:
                result = controller.step(no_agent=True, admit=False)
            elapsed = time.perf_counter() - started
        assert not result["errors"] and not ledger.rows()
        assert state.attempt(queued["id"])["status"] == "queued"
        assert not any("UPDATE allocations" in sql for sql in statements)
        reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        measurements.append({"terminal": count, "read_queries": len(reads),
                             "vm_instructions": instructions[0], "seconds": elapsed})
        with original() as conn:
            plans = {sql: [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)] for sql in reads}
        assert not any("SCAN attempts" in detail or "SCAN allocations" in detail
                       for details in plans.values() for detail in details), plans
        for sql in reads:
            if "FROM attempts" in sql and "spec" in sql:
                assert "WHERE" in sql and ("queued" in sql or "collection" in sql)
    assert len({row["read_queries"] for row in measurements}) == 1, measurements
    assert max(row["vm_instructions"] for row in measurements) - min(
        row["vm_instructions"] for row in measurements) < 100, measurements
    (tmp_path / "measurements.json").write_text(json.dumps(measurements), encoding="utf-8")


def test_database_creation_excludes_readers_and_reset(tmp_path, monkeypatch):
    from labgoblin.config import initial_config, parse_config
    config = parse_config(initial_config("creation"), tmp_path / "labgoblin.toml")
    original = Database._create_owned
    import pytest

    def create(cls, config, ledger, **kwargs):
        with pytest.raises(ValueError, match="readers|archive"):
            with CampaignLease(config.state_dir):
                pass
        return original(config, ledger, **kwargs)

    monkeypatch.setattr(Database, "_create_owned", classmethod(create))
    db = Database.create(config, tmp_path / "ledger.db")
    with db.read() as conn:
        assert conn.execute("SELECT id FROM campaign").fetchone()[0] == db.id
