"""One-time machine grants with nonblocking, cross-campaign drain-to-fit admission."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sqlite3
import time

from labgoblin.db import connection
from labgoblin import credits
from labgoblin.paths import machine_ledger_path
from labgoblin.protocol import (
    LEDGER_VERSION, LaunchEnvelope, LaunchReceipt, Resources, canonical, fingerprint, identifier, integer,
    require_version, text,
)


APPLICATION_ID = 0x58474C33
SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE capacity(
    id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,
    cpus INTEGER NOT NULL,memory_mb INTEGER NOT NULL,gpus TEXT NOT NULL,
    headroom_mb INTEGER NOT NULL,native_cpus TEXT NOT NULL,placement_supported INTEGER NOT NULL
);
CREATE TABLE grants(
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL, work_id TEXT NOT NULL, kind TEXT NOT NULL,
    owner TEXT NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL,
    cpus INTEGER NOT NULL,memory_mb INTEGER NOT NULL,gpus TEXT NOT NULL,native INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','granted','released','rejected')),
    eligible INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL DEFAULT '',
    native_cpus TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL,granted REAL,released REAL
);
CREATE INDEX grants_pending ON grants(state,eligible,sequence);
CREATE INDEX grants_owner ON grants(owner_id,state,sequence);
CREATE TABLE consumer_runs(
    token TEXT PRIMARY KEY REFERENCES grants(token), envelope TEXT NOT NULL, digest TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('armed','executing','quiescent')),
    supervisor TEXT, receipt TEXT, created REAL NOT NULL, ended REAL
);
CREATE INDEX consumer_runs_phase ON consumer_runs(phase,created);
"""


def _wire_envelope(envelope, *, inspection=False):
    # Preserve the ledger's v3 exchange shape so installed legacy readers can observe pending runs.
    return {
        "protocol": 3,
        "key": {"campaign_id": envelope.project_id, "generation": 1, "work_id": envelope.id,
                "grant_id": envelope.id, "nonce": envelope.id},
        "kind": "observer" if inspection else "research", "argv": list(envelope.argv), "cwd": envelope.project, "root": envelope.root,
        "state_path": str(Path(envelope.project) / ".labgoblin" / "project.json"),
        "ledger_path": envelope.ledger_path, "ledger_id": envelope.ledger_id,
        "timeout_seconds": envelope.config["timeout_seconds"], "resources": asdict(envelope.resources),
        "config_revision": fingerprint(envelope.config), "environment": {},
        "metadata": {"simple_envelope": asdict(envelope), "credit_allowance_nano": envelope.allowance_nano},
    }


def _wire_receipt(receipt, envelope, *, inspection=False):
    wire = _wire_envelope(envelope, inspection=inspection)
    return {
        "protocol": 3, "key": wire["key"],
        "envelope_digest": fingerprint(wire),
        "status": receipt.status if receipt.status in
        ("completed", "failed", "cancelled", "timed_out", "interrupted", "not_started") else "failed",
        "quiescent": receipt.quiescent, "elapsed": receipt.elapsed, "returncode": receipt.returncode,
        "reason": receipt.reason, "executed": receipt.executed, "usage": receipt.usage,
        "metadata": {"simple_receipt": asdict(receipt), "partial_usage": receipt.metadata.get("partial_usage")},
    }


def _consumer(row):
    if row is None:
        return None
    row = dict(row)
    wire = json.loads(row["envelope"])
    if "simple_envelope" not in wire.get("metadata", {}):
        raise ValueError("This is not a simplified research execution; use its owning installed runtime")
    envelope = LaunchEnvelope.parse(wire["metadata"]["simple_envelope"])
    inspection = wire.get("kind") == "observer"
    expected = _wire_envelope(envelope, inspection=inspection)
    if canonical(wire) != canonical(expected) or row["digest"] != fingerprint(expected) or row["token"] != envelope.id:
        raise ValueError("Ledger envelope identity mismatch")
    row["envelope"], row["digest"] = canonical(asdict(envelope)).decode(), envelope.digest
    if row["receipt"]:
        wire_receipt = json.loads(row["receipt"])
        receipt = LaunchReceipt.parse(wire_receipt["metadata"]["simple_receipt"])
        if canonical(wire_receipt) != canonical(_wire_receipt(receipt, envelope, inspection=inspection)):
            raise ValueError("Ledger receipt identity mismatch")
        row["receipt"] = canonical(asdict(receipt)).decode()
    row["inspection"] = inspection
    return row


def ledger_path() -> Path:
    return machine_ledger_path()


@dataclass(frozen=True)
class MachineSample:
    cpus: tuple[int, ...]
    total_mb: int
    available_mb: int
    visible_gpus: frozenset[str] = frozenset()
    busy_gpus: frozenset[str] = frozenset()
    placement_supported: bool = True


def sample_machine(gpus: tuple[str, ...] = ()) -> MachineSample:
    import psutil
    memory = psutil.virtual_memory()
    process = psutil.Process()
    cpus = tuple(process.cpu_affinity()) if hasattr(process, "cpu_affinity") else tuple(range(os.cpu_count() or 1))
    visible, busy = set(), set()
    if gpus:
        from labgoblin.payload import probe
        for query, target in (("--query-gpu=uuid", visible), ("--query-compute-apps=gpu_uuid", busy)):
            output = probe(["nvidia-smi", query, "--format=csv,noheader"], timeout=10)
            target.update(line.strip() for line in output.splitlines() if line.strip())
    return MachineSample(cpus, memory.total // (1024 * 1024), memory.available // (1024 * 1024),
                         frozenset(visible), frozenset(busy),
                         os.name != "nt" or ((os.cpu_count() or 1) <= 64 and all(cpu < 64 for cpu in cpus)))


def request_eligible(row: dict) -> tuple[bool, str]:
    """Observe owners outside the ledger writer transaction."""
    from labgoblin.processes import process_state
    try:
        owner = json.loads(row["owner"])
        if owner["kind"] not in ("research-run", "inspection-run"):
            # Other installed runtimes retain responsibility for their own admission intent.
            return bool(row["eligible"]), row["reason"]
        state = process_state(owner["handle"])
        return (True, "") if state == "alive" else (False, f"Research controller ownership is {state}")
    except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError) as error:
        return False, f"Admission owner cannot be verified: {error}"


class ResourceLedger:
    def __init__(self, path: str | Path | None = None, *, expected_id: str | None = None,
                 sampler=None, eligibility=None):
        self.path = Path(path or ledger_path()).resolve(strict=True)
        self.sampler = sampler or sample_machine
        self.eligibility = eligibility or request_eligible
        with connection(self.path) as conn:
            require_version(conn.execute("PRAGMA user_version").fetchone()[0], LEDGER_VERSION, "machine ledger")
            if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("File is not a LabGoblin resource ledger")
            row = conn.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if not row:
                raise ValueError("Machine ledger initialization is incomplete")
            self.id = row[0]
        if expected_id is not None and self.id != expected_id:
            raise ValueError("Machine ledger identity differs from the recorded allocation owner")

    @classmethod
    def create(cls, path: str | Path, *, sampler=None, eligibility=None) -> "ResourceLedger":
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO meta(key,value) VALUES('identity',?)", (identifier(),))
            conn.execute(f"PRAGMA user_version={LEDGER_VERSION}")
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return cls(path, sampler=sampler, eligibility=eligibility)

    def read(self):
        return connection(self.path)

    def write(self):
        return connection(self.path, write=True)

    def configure(self, cpus: int, memory_mb: int, gpus: tuple[str, ...] = (), headroom_mb: int = 2048):
        resources = Resources(cpus, memory_mb, tuple(gpus))
        integer(headroom_mb, "headroom_mb", zero=True)
        sample = self.sampler(resources.gpus)
        if cpus > len(sample.cpus) or memory_mb + headroom_mb > sample.total_mb:
            raise ValueError("Machine capacity and headroom exceed observed physical capacity")
        if not set(resources.gpus).issubset(sample.visible_gpus):
            raise ValueError("Configured physical GPUs are not visible")
        with self.write() as conn:
            if conn.execute("SELECT 1 FROM grants WHERE state='granted'").fetchone():
                raise ValueError("Cannot reconfigure capacity while an allocation is granted or uncertain")
            old = conn.execute("SELECT revision FROM capacity WHERE id=1").fetchone()
            revision = old[0] + 1 if old else 1
            conn.execute("""INSERT INTO capacity(id,revision,cpus,memory_mb,gpus,headroom_mb,native_cpus,placement_supported)
                VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                revision=excluded.revision,cpus=excluded.cpus,memory_mb=excluded.memory_mb,
                gpus=excluded.gpus,headroom_mb=excluded.headroom_mb,native_cpus=excluded.native_cpus,
                placement_supported=excluded.placement_supported""",
                         (revision, cpus, memory_mb, canonical(resources.gpus).decode(), headroom_mb,
                          canonical(sample.cpus).decode(), int(sample.placement_supported)))

    @staticmethod
    def _capacity(conn) -> dict:
        row = conn.execute("SELECT * FROM capacity WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Machine capacity is not configured; use machine configure explicitly")
        return dict(row)

    def capacity(self) -> dict:
        with self.read() as conn:
            return self._capacity(conn)

    @staticmethod
    def _validate(resources: Resources, capacity: dict, native: bool):
        if (resources.cpus > capacity["cpus"] or resources.memory_mb > capacity["memory_mb"]
                or not set(resources.gpus).issubset(json.loads(capacity["gpus"]))):
            raise ValueError("Resource request cannot fit configured machine capacity")
        if native and not capacity["placement_supported"]:
            raise ValueError("Native CPU placement is unsupported on this processor topology")

    def request(self, token: str, owner_id: str, work_id: str, kind: str, resources: Resources,
                owner: dict, *, native: bool) -> dict:
        for label, value in (("token", token), ("owner_id", owner_id), ("work_id", work_id), ("kind", kind)):
            text(value, label)
        if kind not in ("research-run", "inspection-run") or owner.get("kind") != kind or native is not True:
            raise ValueError("This runtime admits only native research or read-only inspection runs")
        handle = owner.get("handle")
        if (not isinstance(handle, dict) or type(handle.get("pid")) is not int
                or type(handle.get("created")) not in (int, float) or handle.get("token") != owner_id):
            raise ValueError("Research owner requires an exact PID, creation time and project identity")
        request = {"owner_id": owner_id, "work_id": work_id, "kind": kind,
                   "resources": asdict(resources), "owner": owner, "native": native}
        digest = fingerprint(request)
        with self.write() as conn:
            old = conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone()
            if old:
                if old["owner_id"] != owner_id or (old["digest"] != digest and old["digest"]):
                    raise ValueError("Allocation token already belongs to a different request")
                return dict(old)
            self._validate(resources, self._capacity(conn), native)
            conn.execute("""INSERT INTO grants(
                token,owner_id,work_id,kind,owner,request,digest,cpus,memory_mb,gpus,native,state,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                         (token, owner_id, work_id, kind, canonical(owner).decode(), canonical(request).decode(),
                          digest, resources.cpus, resources.memory_mb, canonical(resources.gpus).decode(),
                          int(native), time.time()))
            return dict(conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone())

    def reserve(self, token: str) -> dict:
        capacity = self.capacity()
        with self.read() as conn:
            requested = conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone()
            if not requested:
                raise ValueError("Unknown allocation token")
            if requested["state"] != "pending":
                return dict(requested)
            candidates = [dict(r) for r in conn.execute(
                "SELECT * FROM grants WHERE state='pending' ORDER BY eligible DESC,sequence LIMIT 256")]
            if all(row["token"] != token for row in candidates):
                candidates.append(dict(requested))
        eligibility = {row["token"]: self.eligibility(row) for row in candidates}
        sample = self.sampler(tuple(json.loads(capacity["gpus"])))
        with self.write() as conn:
            current = self._capacity(conn)
            requested = conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone()
            if requested["state"] != "pending":
                return dict(requested)
            if current["revision"] != capacity["revision"]:
                conn.execute("UPDATE grants SET reason='Capacity changed during observation; retry admission' WHERE token=?",
                             (token,))
                return dict(conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone())
            for candidate, (eligible, reason) in eligibility.items():
                conn.execute("UPDATE grants SET eligible=?,reason=? WHERE token=? AND state='pending'",
                             (int(eligible), reason, candidate))
            pending = list(conn.execute("SELECT * FROM grants WHERE state='pending' AND eligible=1 ORDER BY sequence"))
            valid = []
            for row in pending:
                try:
                    self._validate(Resources(row["cpus"], row["memory_mb"], tuple(json.loads(row["gpus"]))),
                                   current, bool(row["native"]))
                except ValueError as error:
                    conn.execute("UPDATE grants SET state='rejected',reason=? WHERE token=?", (str(error), row["token"]))
                else:
                    valid.append(row)
            target = next((row for row in valid if row["token"] == token), None)
            if target is None:
                return dict(conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone())
            active = list(conn.execute("SELECT cpus,memory_mb,gpus,native_cpus FROM grants WHERE state='granted'"))
            used_cpus = sum(row["cpus"] for row in active)
            used_mb = sum(row["memory_mb"] for row in active)
            used_gpus = {gpu for row in active for gpu in json.loads(row["gpus"])} | set(sample.busy_gpus)
            used_native = {cpu for row in active for cpu in json.loads(row["native_cpus"])}
            eligible_cpus = sorted(set(json.loads(current["native_cpus"])) & set(sample.cpus) - used_native)
            barrier = valid[0]
            held_cpus = barrier["cpus"] if barrier["token"] != token else 0
            held_mb = barrier["memory_mb"] if barrier["token"] != token else 0
            barrier_gpus = set(json.loads(barrier["gpus"])) if barrier["token"] != token else set()
            target_gpus = set(json.loads(target["gpus"]))
            fits = (used_cpus + target["cpus"] + held_cpus <= current["cpus"]
                    and used_mb + target["memory_mb"] + held_mb <= current["memory_mb"]
                    and used_mb + target["memory_mb"] + held_mb + current["headroom_mb"] <= sample.available_mb
                    and not target_gpus.intersection(used_gpus | barrier_gpus)
                    and target_gpus.issubset(sample.visible_gpus)
                    and (not target["native"] or len(eligible_cpus) >= target["cpus"]
                         + (held_cpus if barrier["native"] else 0)))
            if fits:
                selected = eligible_cpus[:target["cpus"]] if target["native"] else []
                conn.execute("""UPDATE grants SET state='granted',granted=?,native_cpus=?,reason=''
                    WHERE token=? AND state='pending'""", (time.time(), canonical(selected).decode(), token))
            else:
                reason = ("Waiting for oldest eligible request to drain-to-fit"
                          if barrier["token"] != token else
                          "Waiting for CPU/RAM/GPU capacity or conservative available-RAM headroom")
                conn.execute("UPDATE grants SET reason=? WHERE token=?", (reason, token))
            return dict(conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone())

    def release(self, token: str, *, owner_id: str):
        text(token, "token")
        text(owner_id, "owner_id")
        with self.write() as conn:
            row = conn.execute("SELECT owner_id,state FROM grants WHERE token=?", (token,)).fetchone()
            if row:
                if row["owner_id"] != owner_id:
                    raise ValueError("Cannot release another owner's allocation")
                consumer = conn.execute("SELECT phase FROM consumer_runs WHERE token=?", (token,)).fetchone()
                if consumer and consumer["phase"] != "quiescent":
                    raise ValueError("Armed consumer requires a matching quiescent receipt before release")
                if row["state"] != "released":
                    conn.execute("UPDATE grants SET state='released',released=?,reason='' WHERE token=?",
                                 (time.time(), token))
            else:
                # A tombstone fences a delayed request after cross-store compensation.
                conn.execute("""INSERT INTO grants(token,owner_id,work_id,kind,owner,request,digest,
                    cpus,memory_mb,gpus,native,state,created,released)
                    VALUES(?,?,'','revoked','{}','{}','',0,0,'[]',0,'released',?,?)""",
                             (token, owner_id, time.time(), time.time()))

    def grant(self, token: str) -> dict | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone()
            return dict(row) if row else None

    def arm_consumer(self, envelope, *, adjustments=None, inspection=False):
        from labgoblin.config import Config
        config = Config.parse(envelope.config)
        kind = "inspection-run" if inspection else "research-run"
        if inspection and (config.coach or config.consultant or config.gpus or config.credit_limit is None):
            raise ValueError("Inspection requires a finite separate allowance and no research roles or GPUs")
        if envelope.ledger_id != self.id or Path(envelope.ledger_path) != self.path:
            raise ValueError("Research authorization has the wrong ledger identity")
        expected_root = Path(envelope.project) / ".labgoblin" / "runs" / envelope.id
        if Path(envelope.root).resolve() != expected_root.resolve():
            raise ValueError("Execution directory must belong to its exact project and grant")
        with self.write() as conn:
            grant = conn.execute("SELECT * FROM grants WHERE token=?", (envelope.id,)).fetchone()
            if (not grant or grant["state"] != "granted" or grant["owner_id"] != envelope.project_id
                    or grant["work_id"] != envelope.id or grant["kind"] != kind or not grant["native"]
                    or json.loads(grant["owner"]) != {"kind": kind, "handle": envelope.launcher}
                    or grant["cpus"] != envelope.resources.cpus or grant["memory_mb"] != envelope.resources.memory_mb
                    or json.loads(grant["gpus"]) != list(envelope.resources.gpus)
                    or json.loads(grant["native_cpus"]) != list(envelope.cpu_ids)):
                raise ValueError("Research authorization differs from its machine grant")
            old = conn.execute("SELECT digest FROM consumer_runs WHERE token=?", (envelope.id,)).fetchone()
            if old:
                raise ValueError("A research grant is one-use and was already armed")
            rows = self._runs(conn, envelope.project_id, inspection=inspection)
            budget = credits.summarize(rows, config.credit_limit, adjustments=adjustments)
            if envelope.allowance_nano != credits.available(budget, coach=config.coach, consultant=config.consultant):
                raise ValueError("Research allowance changed before arming")
            conn.execute("INSERT INTO consumer_runs(token,envelope,digest,phase,created) VALUES(?,?,?,'armed',?)",
                         (envelope.id, canonical(_wire_envelope(envelope, inspection=inspection)).decode(),
                          fingerprint(_wire_envelope(envelope, inspection=inspection)), time.time()))

    def claim_consumer(self, envelope, handle: dict) -> bool:
        if (type(handle.get("pid")) is not int or type(handle.get("created")) not in (int, float)
                or handle.get("token") != envelope.id):
            raise ValueError("Consumer claim requires its exact supervisor incarnation")
        with self.write() as conn:
            row = _consumer(conn.execute("SELECT * FROM consumer_runs WHERE token=?", (envelope.id,)).fetchone())
            grant = conn.execute("SELECT state FROM grants WHERE token=?", (envelope.id,)).fetchone()
            if not row or row["digest"] != envelope.digest or not grant or grant["state"] != "granted":
                raise ValueError("Consumer launch lacks its matching armed grant")
            if row["phase"] != "armed":
                return False
            conn.execute("UPDATE consumer_runs SET phase='executing',supervisor=? WHERE token=?",
                         (canonical(handle).decode(), envelope.id))
            return True

    def finish_consumer(self, receipt):
        with self.write() as conn:
            row = _consumer(conn.execute("SELECT * FROM consumer_runs WHERE token=?", (receipt.id,)).fetchone())
            if row is None:
                raise ValueError("No armed consumer owns this receipt")
            envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
            if receipt.id != envelope.id or receipt.envelope_digest != envelope.digest:
                raise ValueError("Consumer receipt ownership mismatch")
            encoded = canonical(asdict(receipt)).decode()
            if row["receipt"] and row["receipt"] != encoded:
                raise ValueError("Consumer already has a different terminal receipt")
            if row["phase"] == "armed":
                from labgoblin.processes import process_state
                if (receipt.executed or receipt.status != "not_started" or row["supervisor"] is not None
                        or process_state(envelope.launcher) != "dead"):
                    raise ValueError("Pre-claim recovery requires the exact dead launcher and no committed claim")
            elif row["phase"] == "executing" and receipt.metadata.get("supervisor") != json.loads(row["supervisor"]):
                raise ValueError("Receipt does not identify its committed supervisor")
            conn.execute("UPDATE consumer_runs SET phase='quiescent',receipt=?,ended=? WHERE token=?",
                         (canonical(_wire_receipt(receipt, envelope, inspection=row["inspection"])).decode(), time.time(), receipt.id))
            conn.execute("UPDATE grants SET state='released',released=?,reason='' WHERE token=? AND state!='released'",
                         (time.time(), receipt.id))

    def consumer_run(self, token) -> dict | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM consumer_runs WHERE token=?", (token,)).fetchone()
            return _consumer(row)

    @staticmethod
    def _runs(conn, project_id, *, inspection=False):
        return [_consumer(row) for row in conn.execute("""SELECT r.* FROM consumer_runs r
            JOIN grants g ON g.token=r.token WHERE g.owner_id=? AND g.kind=?
            ORDER BY r.created,r.token""", (project_id, "inspection-run" if inspection else "research-run"))]

    def runs(self, project_id, *, inspection=False):
        with self.read() as conn:
            return self._runs(conn, project_id, inspection=inspection)

    def rows(self, *, limit: int = 100, offset: int = 0, history: bool = False) -> list[dict]:
        integer(limit, "limit")
        integer(offset, "offset", zero=True)
        if limit > 1000:
            raise ValueError("Ledger pages cannot exceed 1000 records")
        where = "" if history else "WHERE state IN ('pending','granted','rejected')"
        with self.read() as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM grants {where} ORDER BY sequence LIMIT ? OFFSET ?", (limit, offset))]
