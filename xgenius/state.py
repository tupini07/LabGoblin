"""Durable local campaign state layered on the existing research database."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import uuid

from xgenius.db import XGeniusDB, _connect, hypothesis_statement


ACTIVE = ("queued", "starting", "running", "recovery_required")
TERMINAL = ("completed", "failed", "cancelled", "timed_out", "interrupted")


def identifier() -> str:
    return uuid.uuid4().hex


class LocalState:
    def __init__(self, config):
        self.config = config
        self.db = XGeniusDB(config)
        self.path = self.db.db_path
        self.root = Path(self.path).parent
        with _connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='local_schema'").fetchone()
            version = c.execute("SELECT MAX(version) FROM local_schema").fetchone()[0] if exists else 0
            if version > 2:
                raise ValueError(f"Local database schema {version} is newer than this xgenius")
            if version < 2:
                backup = self.root / f"xgenius.db.before-local-v2-{identifier()}"
                with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
            schema = """
                CREATE TABLE IF NOT EXISTS local_schema(version INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS campaign(
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, controller TEXT, agent TEXT,
                    failures INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                    spec TEXT NOT NULL, handle TEXT, reason TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, started REAL, ended REAL);
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    created REAL NOT NULL, acknowledged_by TEXT);
                CREATE TABLE IF NOT EXISTS turns(
                    id TEXT PRIMARY KEY, events TEXT NOT NULL, started REAL NOT NULL,
                    ended REAL, state TEXT NOT NULL, result TEXT, handle TEXT);
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, path TEXT NOT NULL,
                    metadata TEXT NOT NULL, UNIQUE(attempt_id, path));
            """
            for statement in schema.split(";"):
                if statement.strip():
                    c.execute(statement)
            for table, name, definition in [
                ("campaign", "started", "REAL"),
                ("turns", "journal_before", "TEXT"),
                ("turns", "kind", "TEXT NOT NULL DEFAULT 'research'"),
                ("turns", "usage", "TEXT"),
            ]:
                if name not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            c.execute("DELETE FROM local_schema")
            c.execute("INSERT INTO local_schema VALUES(2)")
            row = c.execute("SELECT id FROM campaign").fetchone()
            if row is None:
                self.id = identifier()
                c.execute("INSERT INTO campaign(id,state,created) VALUES(?,?,?)",
                          (self.id, "ready", time.time()))
                c.execute("INSERT INTO events VALUES(?,?,?,?,NULL)",
                          (identifier(), "initial", "{}", time.time()))
            else:
                self.id = row["id"]

    def campaign(self) -> dict:
        with _connect(self.path) as c:
            return dict(c.execute("SELECT * FROM campaign WHERE id=?", (self.id,)).fetchone())

    def set_campaign(self, state: str, reason: str = ""):
        with _connect(self.path) as c:
            c.execute("UPDATE campaign SET state=?,reason=? WHERE id=?", (state, reason, self.id))

    def claim(self, attempt_id: str) -> bool:
        with _connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            state = c.execute("SELECT state FROM campaign WHERE id=?", (self.id,)).fetchone()[0]
            if state not in ("ready", "running", "waiting"):
                return False
            return c.execute("UPDATE jobs SET status='starting' WHERE job_id=? AND status='queued'",
                             (attempt_id,)).rowcount == 1

    def event(self, kind: str, payload: dict, event_id: str | None = None):
        with _connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,NULL)",
                      (event_id or identifier(), kind, json.dumps(payload), time.time()))

    def pending_events(self) -> list[dict]:
        with _connect(self.path) as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events WHERE acknowledged_by IS NULL ORDER BY created")]

    def attempts(self) -> list[dict]:
        with _connect(self.path) as c:
            return [dict(r) for r in c.execute("""
                SELECT a.*, j.status, j.exit_code, j.gpu_hours
                FROM attempts a JOIN jobs j ON a.id=j.job_id ORDER BY a.created
            """)]

    def attempt(self, attempt_id: str) -> dict:
        with _connect(self.path) as c:
            row = c.execute("""
                SELECT a.*, j.status, j.exit_code, j.gpu_hours
                FROM attempts a JOIN jobs j ON a.id=j.job_id WHERE a.id=?
            """, (attempt_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown local attempt: {attempt_id}")
            return dict(row)

    def enqueue(self, spec: dict) -> str:
        with _connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT state FROM campaign WHERE id=?", (self.id,)).fetchone()[0] in (
                    "stopping", "stopped", "finishing", "completed"):
                raise ValueError("Campaign no longer accepts new work")
            row = c.execute("SELECT id,spec FROM attempts WHERE idempotency_key=?",
                            (spec["key"],)).fetchone()
            if row:
                old = json.loads(row["spec"])
                if old["request"] != spec["request"]:
                    raise ValueError("Idempotency key already used for a different job request")
                return row["id"]
            attempt_id = spec["id"]
            c.execute("INSERT INTO attempts(id,idempotency_key,spec,created) VALUES(?,?,?,?)",
                      (attempt_id, spec["key"], json.dumps(spec), time.time()))
            c.execute("""INSERT INTO jobs(
                job_id,cluster,experiment_id,hypothesis_id,command,status,submitted_at,
                gpus,cpus,memory,walltime_requested,output_dir)
                VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)""",
                      (attempt_id, spec["runner_name"], spec["experiment_id"],
                       spec.get("hypothesis_id", ""), json.dumps(spec["argv"]),
                       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       len(spec["gpus"]), spec["cpus"], f'{spec["memory_mb"]}M',
                       str(spec["seconds"]), spec["output"]))
            hid = spec.get("hypothesis_id", "")
            if hid:
                description = spec.get("hypothesis_description", "")
                hypothesis = c.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hid,)).fetchone()
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if hypothesis is None:
                    c.execute("INSERT INTO hypotheses(hypothesis_id,description,created_at,updated_at) VALUES(?,?,?,?)",
                              (hid, description, now, now))
                elif description:
                    existing = hypothesis_statement(dict(hypothesis))
                    if existing and existing != description:
                        raise ValueError("Hypothesis already has a different statement; use "
                                         "xgenius db hypothesis-update --description for an intentional revision")
                    if not existing:
                        c.execute("UPDATE hypotheses SET description=?,updated_at=? WHERE hypothesis_id=?",
                                  (description, now, hid))
        return attempt_id

    def transition(self, attempt_id: str, status: str, *, reason: str = "",
                   receipt: dict | None = None, handle: dict | None = None,
                   expected: str | None = None) -> bool:
        if status not in ACTIVE + TERMINAL:
            raise ValueError(f"Invalid attempt status: {status}")
        with _connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM jobs WHERE job_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown local attempt: {attempt_id}")
            if expected is not None and row["status"] != expected:
                return False
            if row["status"] in TERMINAL:
                return False
            if status == "starting" and row["status"] not in ("queued", "starting"):
                return False
            if status == "queued" and row["status"] != "queued":
                raise ValueError("An attempt cannot be requeued; retry with a new key")
            c.execute("UPDATE jobs SET status=?,error_message=? WHERE job_id=?",
                      (status, reason, attempt_id))
            c.execute("UPDATE attempts SET reason=? WHERE id=?", (reason, attempt_id))
            if handle is not None:
                c.execute("UPDATE attempts SET handle=? WHERE id=?", (json.dumps(handle), attempt_id))
            if status == "running":
                c.execute("UPDATE attempts SET started=COALESCE(started,?) WHERE id=?",
                          (time.time(), attempt_id))
            if status in TERMINAL:
                data = receipt or {}
                if not receipt:
                    attempt = c.execute("SELECT spec,started FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                    spec = json.loads(attempt["spec"])
                    elapsed = min(spec["seconds"], max(0, time.time() - attempt["started"])) \
                        if attempt["started"] else 0
                    data = {"elapsed": elapsed, "gpu_hours": elapsed * len(spec["gpus"]) / 3600}
                c.execute("UPDATE attempts SET ended=? WHERE id=?", (time.time(), attempt_id))
                c.execute("""UPDATE jobs SET exit_code=?,walltime_seconds=?,gpu_hours=?,
                    completed_at=? WHERE job_id=?""",
                          (data.get("returncode"), data.get("elapsed", 0),
                           data.get("gpu_hours", 0),
                           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), attempt_id))
                c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,NULL)",
                          (f"completion-{attempt_id}", "completion",
                           json.dumps({"attempt_id": attempt_id, "status": status, "reason": reason}),
                           time.time()))
            return True

    def accept_turn(self, turn_id: str, result: dict):
        if not isinstance(result, dict):
            raise ValueError("Turn result must be a JSON object")
        if result.get("turn_id") != turn_id or result.get("disposition") not in (
                "continue", "wait", "blocked", "complete"):
            raise ValueError("Turn result needs the current turn_id and a valid disposition")
        if not isinstance(result.get("reason"), str) or not result["reason"].strip():
            raise ValueError("Turn result needs a non-empty reason")
        with _connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            turn = c.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if turn is None:
                raise ValueError("Unknown turn")
            if turn["state"] == "completed":
                return
            from xgenius.workspace import digest
            journal = self.root / "journal.md"
            if (result.get("journal") != ".xgenius/journal.md" or not journal.is_file()
                    or digest(journal) == turn["journal_before"]):
                raise ValueError("Turn must update the journal and reference .xgenius/journal.md")
            requested = result.get("acknowledged_events")
            if not isinstance(requested, list) or not all(isinstance(x, str) for x in requested):
                raise ValueError("acknowledged_events must be an array of event IDs")
            if set(requested) - set(json.loads(turn["events"])):
                raise ValueError("Cannot acknowledge events outside this turn")
            for event_id in requested:
                c.execute("UPDATE events SET acknowledged_by=? WHERE id=? AND acknowledged_by IS NULL",
                          (turn_id, event_id))
            c.execute("UPDATE turns SET state='completed',ended=?,result=? WHERE id=?",
                      (time.time(), json.dumps(result), turn_id))
            c.execute("UPDATE campaign SET failures=0 WHERE id=?", (self.id,))
