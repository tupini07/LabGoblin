"""Non-creating, bounded SQLite transactions for the shared machine ledger only."""

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import sqlite3
import time


BUSY_TIMEOUT_SECONDS = 30


@contextmanager
def connection(path, *, write=False):
    path = Path(path).resolve(strict=True)
    conn = None
    stage = "connect"
    started = time.monotonic()
    try:
        conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                               uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if not write:
            conn.execute("PRAGMA query_only=ON")
        stage = "BEGIN IMMEDIATE" if write else "BEGIN"
        conn.execute(stage)
        stage = "transaction body"
        yield conn
        stage = "commit"
        conn.commit()
    except BaseException as error:
        if isinstance(error, sqlite3.Error):
            error.add_note(f"database={path}; stage={stage}; busy_timeout={BUSY_TIMEOUT_SECONDS}s")
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            logging.getLogger(__name__).warning(
                "Slow ledger transaction [database=%s; stage=%s; write=%s; elapsed=%.3fs; pid=%s]",
                path, stage, write, elapsed, os.getpid())
