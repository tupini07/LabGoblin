"""Atomic per-user reservations shared by independent local campaigns."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import psutil

from xgenius.local_config import positive


def ledger_path() -> Path:
    override = os.environ.get("XGENIUS_RESOURCE_DB")
    if override:
        return Path(override).resolve()
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "state"))
    return root / "xgenius" / "resources.db"


class ResourceLedger:
    def __init__(self):
        self.path = ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS capacity(
                    id INTEGER PRIMARY KEY CHECK(id=1), cpus INTEGER,
                    memory_mb INTEGER, gpus TEXT, headroom_mb INTEGER);
                CREATE TABLE IF NOT EXISTS reservations(
                    id TEXT PRIMARY KEY, campaign TEXT, config_path TEXT, spec TEXT,
                    state TEXT, created REAL, reason TEXT DEFAULT '');
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        try:
            c.row_factory = sqlite3.Row
            with c:
                yield c
        finally:
            c.close()

    def configure(self, cpus: int, memory_mb: int, gpus: list[str], headroom_mb: int):
        for name, value in [("cpus", cpus), ("memory_mb", memory_mb), ("headroom_mb", headroom_mb)]:
            positive(value, name, zero=name == "headroom_mb")
            if type(value) is not int:
                raise ValueError(f"{name} must be an integer")
        if cpus > (os.cpu_count() or 1) or memory_mb + headroom_mb > psutil.virtual_memory().total // (1024 * 1024):
            raise ValueError("Configured capacity and headroom exceed physical CPU/RAM")
        if len(set(gpus)) != len(gpus):
            raise ValueError("GPU identities must be unique")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM reservations WHERE state IN ('reserved','running')").fetchone():
                raise ValueError("Cannot change machine capacity while reservations are live")
            c.execute("INSERT OR REPLACE INTO capacity VALUES(1,?,?,?,?)",
                      (cpus, memory_mb, json.dumps(gpus), headroom_mb))

    def capacity(self) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT * FROM capacity WHERE id=1").fetchone()
            if row is None:
                raise ValueError("Machine capacity not configured; run xgenius machine configure")
            return dict(row)

    def validate(self, spec: dict):
        capacity = self.capacity()
        if (spec["cpus"] > capacity["cpus"] or spec["memory_mb"] > capacity["memory_mb"]
                or not set(spec["gpus"]).issubset(json.loads(capacity["gpus"]))):
            raise ValueError("Job cannot fit the configured machine capacity/devices")

    def register(self, state, spec: dict):
        self.validate(spec)
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO reservations VALUES(?,?,?,?,?,?,?)",
                      (spec["id"], state.id, state.config.config_path, json.dumps(spec),
                       "queued", time.time(), ""))
            c.execute("UPDATE reservations SET state='queued' WHERE id=? AND state='paused'",
                      (spec["id"],))

    def reserve(self, attempt_id: str) -> bool:
        capacity = self.capacity()
        free_mb = psutil.virtual_memory().available // (1024 * 1024)
        external_gpus = set()
        if json.loads(capacity["gpus"]):
            result = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=gpu_uuid", "--format=csv,noheader"],
                capture_output=True, text=True, encoding="utf-8", timeout=10)
            if result.returncode:
                raise RuntimeError(f"Cannot observe GPU activity: {result.stderr.strip()}")
            external_gpus = set(result.stdout.strip().splitlines())
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in c.execute("SELECT * FROM reservations ORDER BY created,id")]
            active = [json.loads(r["spec"]) for r in rows if r["state"] in ("reserved", "running")]
            available_cpus = capacity["cpus"] - sum(s["cpus"] for s in active)
            available_ram = capacity["memory_mb"] - sum(s["memory_mb"] for s in active)
            occupied = {gpu for s in active for gpu in s["gpus"]} | external_gpus
            for row in rows:
                if row["id"] == attempt_id and row["state"] in ("reserved", "running"):
                    return True
            for row in rows:
                if row["state"] != "queued":
                    continue
                spec = json.loads(row["spec"])
                if row["id"] != attempt_id:
                    from xgenius.backends import alive
                    from xgenius.db import _connect
                    path = Path(spec["root"]).parents[1] / "xgenius.db"
                    if not path.is_file():
                        c.execute("UPDATE reservations SET state='paused',reason=? WHERE id=?",
                                  ("Queued campaign database is missing", row["id"]))
                        continue
                    try:
                        with _connect(str(path)) as project:
                            campaign = project.execute("SELECT controller,state FROM campaign WHERE id=?",
                                                       (row["campaign"],)).fetchone()
                    except sqlite3.Error as e:
                        c.execute("UPDATE reservations SET state='paused',reason=? WHERE id=?",
                                  (f"Queued campaign database is unavailable: {e}", row["id"]))
                        continue
                    if not campaign or campaign["state"] not in ("ready", "running", "waiting") \
                            or not campaign["controller"] or not alive(json.loads(campaign["controller"])):
                        continue
                fits = (spec["cpus"] <= available_cpus and spec["memory_mb"] <= available_ram
                        and spec["memory_mb"] + sum(s["memory_mb"] for s in active)
                            + capacity["headroom_mb"] <= free_mb
                        and not occupied.intersection(spec["gpus"]))
                if fits:
                    if row["id"] != attempt_id:
                        return False
                    c.execute("UPDATE reservations SET state='reserved',reason='' WHERE id=?",
                              (attempt_id,))
                    return True
            c.execute("UPDATE reservations SET reason=? WHERE id=? AND state='queued'",
                      ("Waiting for CPU/RAM/GPU capacity, external GPU activity, or host RAM headroom", attempt_id))
            return False

    def finish(self, attempt_id: str):
        with self.connect() as c:
            c.execute("UPDATE reservations SET state='released' WHERE id=?", (attempt_id,))

    def rows(self) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT id,campaign,state,created,reason FROM reservations WHERE state!='released'")]
