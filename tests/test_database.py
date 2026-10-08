from contextlib import closing
import sqlite3
import time

import pytest

from labgoblin import db


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE counter(value INTEGER NOT NULL)")
        conn.execute("INSERT INTO counter VALUES(0)")
    return path


def test_connections_preserve_durability_and_read_only_access(database):
    for write in (False, True):
        with db.connection(database, write=write) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("SELECT value FROM counter").fetchone()[0] == 0
            if not write:
                conn.execute("PRAGMA query_only=OFF")
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    conn.execute("UPDATE counter SET value=1")


def test_exhausted_write_wait_is_bounded_and_reports_database_and_stage(database, monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.05)
    with closing(sqlite3.connect(database, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked") as failure:
            with db.connection(database, write=True):
                pytest.fail("A blocked transaction body must not run")
        assert time.monotonic() - started < 5
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert failure.value.sqlite_errorname == "SQLITE_BUSY"
        assert str(database) in str(failure.value)
        assert "stage=BEGIN IMMEDIATE" in str(failure.value)
        assert "busy_timeout=0.05s" in str(failure.value)
        blocker.rollback()
    with db.connection(database, write=True) as conn:
        conn.execute("UPDATE counter SET value=1")


def test_transaction_body_failure_rolls_back_without_replaying(database):
    calls = []
    with pytest.raises(sqlite3.OperationalError, match="stage=transaction body") as failure:
        with db.connection(database, write=True) as conn:
            calls.append(True)
            conn.execute("UPDATE counter SET value=1")
            conn.execute("SELECT * FROM missing_table")
    assert failure.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert calls == [True]
    with db.connection(database) as conn:
        assert conn.execute("SELECT value FROM counter").fetchone()[0] == 0


def test_read_contention_is_explicit_without_running_a_write(database, monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.05)
    with closing(sqlite3.connect(database, isolation_level=None)) as blocker:
        blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError, match="stage=transaction body") as failure:
            with db.connection(database) as conn:
                conn.execute("SELECT value FROM counter")
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert str(database) in str(failure.value)
        blocker.rollback()
    with db.connection(database) as conn:
        assert conn.execute("SELECT value FROM counter").fetchone()[0] == 0
