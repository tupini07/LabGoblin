"""Explicit SQLite creation and version-checked, non-creating connections."""

from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
import sqlite3
import time

from labgoblin.config import LabGoblinConfig
from labgoblin.paths import DATABASE_NAME, database_path
from labgoblin.protocol import ACTIVE, DATABASE_VERSION, canonical, identifier, require_version


APPLICATION_ID = 0x58474533
ACTIVE_STATUSES = ACTIVE

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE configs(id TEXT PRIMARY KEY, content TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE campaign(
    id TEXT PRIMARY KEY, generation INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
    operator_mode TEXT NOT NULL CHECK(operator_mode IN ('ready','running','paused','stopping','stopped')),
    progress TEXT NOT NULL CHECK(progress IN ('research','wait','blocked','finalize','closed')),
    reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL, started REAL,
    elapsed REAL NOT NULL DEFAULT 0, observed_wall REAL, controller TEXT,
    gpu_hours REAL NOT NULL DEFAULT 0 CHECK(gpu_hours>=0),
    invocations INTEGER NOT NULL DEFAULT 0 CHECK(invocations>=0),
    config_revision TEXT NOT NULL REFERENCES configs(id), failures INTEGER NOT NULL DEFAULT 0,
    wake_after INTEGER NOT NULL DEFAULT 0, authority_revision INTEGER NOT NULL DEFAULT 0,
    queue_sequence INTEGER NOT NULL DEFAULT 0, attempt_revision INTEGER NOT NULL DEFAULT 0,
    archive_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE generations(
    id INTEGER PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('open','sealed','closed')),
    created REAL NOT NULL, sealed REAL, outcome TEXT NOT NULL DEFAULT 'active',
    reason TEXT NOT NULL DEFAULT '', source_heads TEXT, final_turn TEXT, view_id TEXT,
    authority_revision INTEGER, assessment TEXT
);
CREATE TABLE control_requests(
    id TEXT PRIMARY KEY, digest TEXT NOT NULL, action TEXT NOT NULL,
    expected_revision INTEGER NOT NULL, result TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE hypotheses(
    id TEXT PRIMARY KEY, statement TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    supersedes TEXT REFERENCES hypotheses(id), frozen INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'proposed', conclusion TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE attempts(
    id TEXT PRIMARY KEY, generation INTEGER NOT NULL REFERENCES generations(id),
    idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL, spec TEXT NOT NULL,
    experiment_id TEXT NOT NULL, hypothesis_id TEXT REFERENCES hypotheses(id),
    status TEXT NOT NULL CHECK(status IN
        ('queued','starting','running','recovery_required','completed','failed',
         'cancelled','timed_out','interrupted','not_started')),
    cpus INTEGER NOT NULL, memory_mb INTEGER NOT NULL, gpus TEXT NOT NULL,
    seconds REAL NOT NULL, created REAL NOT NULL, started REAL, ended REAL,
    exit_code INTEGER, elapsed REAL NOT NULL DEFAULT 0, gpu_hours REAL NOT NULL DEFAULT 0,
    collection TEXT NOT NULL DEFAULT 'pending', collection_reason TEXT NOT NULL DEFAULT '',
    collection_event TEXT REFERENCES events(id),
    validation TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
    grant_id TEXT, nonce TEXT, cancel_requested REAL, admission_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(generation,idempotency_key)
);
CREATE INDEX attempts_active ON attempts(status,created,id);
CREATE INDEX attempts_generation ON attempts(generation,status,id);
CREATE INDEX attempts_hypothesis ON attempts(hypothesis_id,id);
CREATE INDEX attempts_collection ON attempts(collection,status,created,id);
CREATE INDEX attempts_cancel ON attempts(status,cancel_requested);
CREATE INDEX attempts_queue ON attempts(status,admission_order,id);
CREATE TABLE allocations(
    token TEXT PRIMARY KEY, generation INTEGER NOT NULL REFERENCES generations(id),
    work_id TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL,
    request TEXT NOT NULL, digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('requested','granted','attached','release_pending','released')),
    created REAL NOT NULL, released REAL, reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX allocations_pending ON allocations(state,created,token);
CREATE INDEX allocations_work ON allocations(work_id,created,token);
CREATE UNIQUE INDEX allocations_owned_work ON allocations(work_id)
    WHERE state IN ('requested','granted','attached');
CREATE TABLE launches(
    nonce TEXT PRIMARY KEY, work_id TEXT NOT NULL, kind TEXT NOT NULL,
    generation INTEGER NOT NULL REFERENCES generations(id),
    grant_id TEXT NOT NULL REFERENCES allocations(token), envelope TEXT NOT NULL,
    digest TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('prepared','armed','executing','quiescent','revoked')),
    launcher TEXT, supervisor TEXT, payload TEXT, receipt TEXT, created REAL NOT NULL, ended REAL
);
CREATE INDEX launches_active ON launches(phase,created,nonce);
CREATE INDEX launches_grant ON launches(grant_id,phase);
CREATE INDEX launches_work ON launches(work_id,created,nonce);
CREATE UNIQUE INDEX launches_owned_work ON launches(work_id)
    WHERE phase IN ('prepared','armed','executing');
CREATE TABLE blockers(
    id TEXT PRIMARY KEY, generation INTEGER NOT NULL, category TEXT NOT NULL,
    work_id TEXT, detail TEXT NOT NULL, created REAL NOT NULL, resolved REAL
);
CREATE INDEX blockers_unresolved ON blockers(generation,resolved,id);
CREATE TABLE events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL REFERENCES generations(id), kind TEXT NOT NULL,
    payload TEXT NOT NULL, payload_digest TEXT NOT NULL, created REAL NOT NULL, acknowledged_by TEXT
);
CREATE INDEX events_pending ON events(generation,acknowledged_by,seq);
CREATE INDEX events_evidence ON events(generation,acknowledged_by,kind,seq);
CREATE TABLE packets(
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL,
    watermark INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
    path TEXT, ready INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
);
CREATE TABLE packet_events(
    packet_id TEXT NOT NULL REFERENCES packets(id), event_id TEXT NOT NULL REFERENCES events(id),
    PRIMARY KEY(packet_id,event_id)
);
CREATE TABLE turns(
    id TEXT PRIMARY KEY, generation INTEGER NOT NULL REFERENCES generations(id),
    kind TEXT NOT NULL, packet_id TEXT UNIQUE REFERENCES packets(id),
    state TEXT NOT NULL CHECK(state IN ('prepared','running','accepted','failed','cancelled')),
    created REAL NOT NULL, ended REAL, result TEXT, reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL, source_revision TEXT, admission_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX turns_active ON turns(state,created,id);
CREATE UNIQUE INDEX turns_single_owner ON turns((1)) WHERE state IN ('prepared','running');
CREATE TABLE invocations(
    id TEXT PRIMARY KEY, turn_id TEXT REFERENCES turns(id), generation INTEGER NOT NULL,
    kind TEXT NOT NULL, bundle_id TEXT NOT NULL, bundle_position INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('reserved','armed','running','completed','failed','uncertain','not_started','cancelled')),
    created REAL NOT NULL, started REAL, ended REAL, usage TEXT,
    nonce TEXT, reason TEXT NOT NULL DEFAULT '', UNIQUE(bundle_id,bundle_position)
);
CREATE INDEX invocations_accounting ON invocations(state,generation,kind);
CREATE INDEX invocations_turn ON invocations(turn_id,bundle_position);
CREATE TABLE handoffs(
    turn_id TEXT PRIMARY KEY REFERENCES turns(id), content TEXT NOT NULL,
    digest TEXT NOT NULL, source_id TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE turn_references(
    turn_id TEXT NOT NULL REFERENCES turns(id), reference_id TEXT NOT NULL,
    PRIMARY KEY(turn_id,reference_id)
);
CREATE TABLE dispositions(
    turn_id TEXT NOT NULL REFERENCES turns(id), event_id TEXT NOT NULL REFERENCES events(id),
    disposition TEXT NOT NULL, reason TEXT NOT NULL, wake_condition TEXT NOT NULL,
    reference_ids TEXT NOT NULL, PRIMARY KEY(turn_id,event_id)
);
CREATE TABLE sources(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL, origin TEXT NOT NULL, body BLOB NOT NULL,
    digest TEXT NOT NULL, metadata TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE source_heads(
    name TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    revision INTEGER NOT NULL
);
CREATE INDEX sources_history ON sources(kind,seq);
CREATE TABLE directives(
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    origin TEXT NOT NULL, scope TEXT NOT NULL, supersedes TEXT REFERENCES directives(id),
    active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
CREATE TABLE observations(
    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
    path TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT,
    body BLOB, metadata TEXT NOT NULL, assurance TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX observations_attempt ON observations(attempt_id,created,id);
CREATE TABLE maintenance(
    id TEXT PRIMARY KEY, generation INTEGER NOT NULL, kind TEXT NOT NULL,
    source_revision TEXT NOT NULL, origin TEXT NOT NULL, revision INTEGER NOT NULL,
    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', turn_id TEXT,
    view_id TEXT, options TEXT NOT NULL DEFAULT '{}', options_digest TEXT NOT NULL,
    created REAL NOT NULL, UNIQUE(kind,source_revision,options_digest)
);
CREATE INDEX maintenance_pending ON maintenance(state,created,id);
CREATE UNIQUE INDEX maintenance_turn ON maintenance(turn_id) WHERE turn_id IS NOT NULL;
CREATE TABLE closure_members(
    generation INTEGER NOT NULL REFERENCES generations(id),
    attempt_id TEXT NOT NULL REFERENCES attempts(id), admitted INTEGER NOT NULL,
    grant_ids TEXT NOT NULL, PRIMARY KEY(generation,attempt_id)
);
CREATE TABLE views(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, generation INTEGER NOT NULL,
    watermark INTEGER NOT NULL, source_ids TEXT NOT NULL,
    metadata TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE view_members(
    view_id TEXT NOT NULL REFERENCES views(id), attempt_id TEXT NOT NULL REFERENCES attempts(id),
    ordinal INTEGER NOT NULL, outcome TEXT NOT NULL, observation_ids TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY(view_id,attempt_id), UNIQUE(view_id,ordinal)
);
CREATE TABLE view_sources(
    view_id TEXT NOT NULL REFERENCES views(id), source_id TEXT NOT NULL REFERENCES sources(id),
    PRIMARY KEY(view_id,source_id)
);
CREATE TABLE turn_view_pages(
    turn_id TEXT NOT NULL REFERENCES turns(id), view_id TEXT NOT NULL REFERENCES views(id),
    start INTEGER NOT NULL, end INTEGER NOT NULL,
    PRIMARY KEY(turn_id,view_id,start,end)
);
CREATE TABLE reports(
    id TEXT PRIMARY KEY, view_id TEXT NOT NULL REFERENCES views(id), turn_id TEXT,
    outputs TEXT NOT NULL, created REAL NOT NULL
);
CREATE UNIQUE INDEX reports_turn ON reports(turn_id) WHERE turn_id IS NOT NULL;
"""


@contextmanager
def connection(path: str | Path, *, write: bool = False, lease: bool = True):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Database is not a file: {path}")
    from labgoblin.processes import CampaignLease
    lock_path = path.parent.with_name(path.parent.name + ".lock")
    guard = CampaignLease(path.parent) if lease and path.name == DATABASE_NAME and lock_path.exists() else nullcontext()
    with guard:
        with _connection(path, write=write) as conn:
            yield conn


@contextmanager
def _connection(path, *, write):
    conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                           uri=True, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if not write:
            conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve(strict=True)
        with connection(self.path) as conn:
            require_version(conn.execute("PRAGMA user_version").fetchone()[0],
                            DATABASE_VERSION, "campaign database")
            if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("Database is not a LabGoblin campaign")
            row = conn.execute("SELECT id FROM campaign").fetchone()
            if row is None:
                raise ValueError("Campaign initialization is incomplete")
            self.id = row["id"]
        if not self.path.parent.with_name(self.path.parent.name + ".lock").is_file():
            raise ValueError("Campaign initialization lacks its reader lock; initialize fresh state")

    @contextmanager
    def read(self):
        with connection(self.path) as conn:
            if conn.execute("SELECT id FROM campaign").fetchone()[0] != self.id:
                raise ValueError("Campaign database identity changed after it was opened")
            yield conn

    @contextmanager
    def write(self):
        with connection(self.path, write=True) as conn:
            if conn.execute("SELECT id FROM campaign").fetchone()[0] != self.id:
                raise ValueError("Campaign database identity changed after it was opened")
            yield conn

    @classmethod
    def create(cls, config: LabGoblinConfig, ledger_path: str | Path) -> "Database":
        from labgoblin.processes import CampaignLease
        root = config.state_dir
        if not root.resolve().is_relative_to(config.root):
            raise ValueError("Campaign state escapes the project through a symlink/junction")
        root.parent.mkdir(parents=True, exist_ok=True)
        lock_path = root.with_name(root.name + ".lock")
        try:
            with lock_path.open("xb") as stream:
                stream.write(b"\0")
        except FileExistsError:
            with lock_path.open("rb") as stream:
                marker = stream.read(2)
            if lock_path.resolve() != lock_path or marker != b"\0":
                raise ValueError("Existing campaign lock is not the expected owned lock file")
        with CampaignLease(root, exclusive=True):
            path = cls._create_owned(config, ledger_path)
        return cls(path)

    @classmethod
    def _create_owned(cls, config: LabGoblinConfig, ledger_path: str | Path) -> Path:
        root = config.state_dir
        if root.exists() and any(root.iterdir()):
            raise FileExistsError("Campaign state is not empty; initialize a fresh local campaign")
        root.mkdir(parents=True, exist_ok=True)
        path = database_path(root)
        with path.open("xb"):
            pass
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            now = time.time()
            campaign_id = identifier()
            conn.executemany("INSERT INTO meta(key,value) VALUES(?,?)", [
                ("ledger_path", str(Path(ledger_path).resolve())), ("ledger_id", ""),
            ])
            conn.execute("INSERT INTO configs(id,content,created) VALUES(?,?,?)",
                         (config.revision, canonical(asdict(config)).decode(), now))
            conn.execute("INSERT INTO generations(id,state,created) VALUES(1,'open',?)", (now,))
            conn.execute("""INSERT INTO campaign(
                id,generation,operator_mode,progress,created,config_revision)
                VALUES(?,1,'ready','research',?,?)""", (campaign_id, now, config.revision))
            from labgoblin.protocol import fingerprint
            conn.execute("""INSERT INTO events(id,generation,kind,payload,payload_digest,created)
                VALUES(?,1,'initial','{}',?,?)""", (identifier(), fingerprint({}), now))
            conn.execute(f"PRAGMA user_version={DATABASE_VERSION}")
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return path
