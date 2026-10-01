"""Transactional campaign authority, independent of mutable configuration files."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

from labgoblin.db import Database
from labgoblin.paths import database_path
from labgoblin.protocol import (
    ACTIVE, TERMINAL, AdmissionClosed, AdmissionWait, BudgetExhausted, Handoff, LaunchEnvelope, LaunchKey, LaunchReceipt, Limit, Resources,
    canonical, fingerprint, identifier, integer, number, table, text,
)


def _json(value) -> str:
    return canonical(value).decode("utf-8")


def _campaign(conn) -> dict:
    return dict(conn.execute("SELECT * FROM campaign").fetchone())


def _settings(conn, campaign: dict) -> dict:
    return json.loads(conn.execute("SELECT content FROM configs WHERE id=?",
                                   (campaign["config_revision"],)).fetchone()[0])


def _configuration_fence(conn) -> dict:
    campaign = _campaign(conn)
    epoch = conn.execute("SELECT value FROM meta WHERE key='controller_epoch'").fetchone()
    return {"controller": campaign["controller"], "config_revision": campaign["config_revision"],
            "controller_epoch": epoch[0] if epoch else ""}


def _check_configuration_fence(conn, expected):
    if expected != _configuration_fence(conn):
        raise ValueError("Controller/configuration changed during preparation; retry with the loaded configuration")


def _event(conn, generation: int, kind: str, payload: dict, event_id: str | None = None):
    event_id = event_id or identifier()
    if len(text(event_id, "event ID").encode("utf-8")) > 128 or len(text(kind, "event kind").encode("utf-8")) > 128:
        raise ValueError("Event IDs and kinds must fit 128 UTF-8 bytes")
    encoded = _json(payload)
    old = conn.execute("SELECT generation,kind,payload FROM events WHERE id=?", (event_id,)).fetchone()
    if old:
        if (old["generation"], old["kind"], old["payload"]) != (generation, kind, encoded):
            raise ValueError("Event identity already belongs to different evidence")
        return event_id
    conn.execute("INSERT INTO events(id,generation,kind,payload,payload_digest,created) VALUES(?,?,?,?,?,?)",
                 (event_id, generation, kind, encoded, fingerprint(payload), time.time()))
    return event_id


def _blocker(conn, generation: int, blocker_id: str, category: str, detail: str, work_id: str | None):
    conn.execute("""INSERT INTO blockers(id,generation,category,work_id,detail,created)
        VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET detail=excluded.detail,resolved=NULL""",
                 (blocker_id, generation, category, work_id, detail, time.time()))


def _collection(conn, attempt) -> dict:
    event = conn.execute("SELECT payload FROM events WHERE id=?", (attempt["collection_event"],)).fetchone()
    return {"attempt_id": attempt["id"], "collection": attempt["collection"],
            "validation": attempt["validation"], "reason": attempt["collection_reason"],
            "event_id": attempt["collection_event"],
            "observation_ids": json.loads(event[0])["observation_ids"] if event else []}


def _source(conn, kind: str, body: bytes, origin: str, metadata: dict,
            head: str | None = None) -> str:
    source_id = identifier()
    conn.execute("INSERT INTO sources(id,kind,origin,body,digest,metadata,created) VALUES(?,?,?,?,?,?,?)",
                 (source_id, kind, origin, body, hashlib.sha256(body).hexdigest(),
                  _json(metadata), time.time()))
    if kind in ("handoff", "journal_import"):
        conn.execute("UPDATE campaign SET archive_bytes=archive_bytes+?", (len(body),))
    if head:
        conn.execute("""INSERT INTO source_heads(name,source_id,revision) VALUES(?,?,1)
            ON CONFLICT(name) DO UPDATE SET source_id=excluded.source_id,revision=revision+1""",
                     (head, source_id))
    return source_id


def _authority_changed(conn):
    conn.execute("UPDATE campaign SET revision=revision+1,authority_revision=authority_revision+1")
    conn.execute("UPDATE allocations SET state='release_pending' WHERE state IN ('requested','granted')")
    conn.execute("""UPDATE maintenance SET state='cancelled',reason='Governing authority changed'
        WHERE state='pending'""")


def _queue_order(conn) -> int:
    conn.execute("UPDATE campaign SET queue_sequence=queue_sequence+1")
    return conn.execute("SELECT queue_sequence FROM campaign").fetchone()[0]


def _active_directives(conn, generation):
    return conn.execute("""SELECT id,source_id,origin,scope FROM directives WHERE active=1
        AND (scope='campaign' OR scope=? OR scope LIKE 'hypothesis:%') ORDER BY created,id""",
                        (f"generation:{generation}",))


def _source_revision(conn) -> str:
    source = conn.execute("""SELECT COALESCE(MAX(seq),0) FROM sources
        WHERE kind NOT IN ('summary','instruction_backup')""").fetchone()[0]
    event = conn.execute("""SELECT COALESCE(MAX(seq),0) FROM events
        WHERE kind NOT IN ('maintenance','continue','protocol_failure','control','reopened')""").fetchone()[0]
    campaign = _campaign(conn)
    return fingerprint({"source": source, "event": event, "generation": campaign["generation"],
                        "attempt_revision": campaign["attempt_revision"]})



def _elapsed(conn, campaign: dict, now: float) -> float:
    elapsed = campaign["elapsed"]
    if campaign["started"] is not None:
        previous = campaign["observed_wall"]
        elapsed += max(0, now - previous)
        conn.execute("UPDATE campaign SET elapsed=?,observed_wall=? WHERE id=?",
                     (elapsed, max(now, previous), campaign["id"]))
    return elapsed


def _seal(conn, generation: int, reason: str):
    heads = dict(conn.execute("SELECT name,source_id FROM source_heads WHERE name IN ('goal','protocol','rationale','summary')"))
    heads.update({f"directive:{r['id']}": r["source_id"] for r in _active_directives(conn, generation)})
    changed = conn.execute("""UPDATE generations SET state='sealed',sealed=?,reason=?,
        source_heads=?,authority_revision=(SELECT authority_revision FROM campaign) WHERE id=? AND state='open'""",
                           (time.time(), reason, _json(heads), generation)).rowcount
    if not changed:
        return
    conn.execute("""INSERT INTO closure_members(generation,attempt_id,admitted,grant_ids)
        SELECT generation,id,EXISTS(SELECT 1 FROM launches l WHERE l.work_id=attempts.id
               AND l.phase IN ('armed','executing','quiescent')),
               COALESCE((SELECT json_group_array(token) FROM allocations a
                         WHERE a.work_id=attempts.id),'[]')
        FROM attempts WHERE generation=?""", (generation,))


class State:
    def __init__(self, db: Database):
        self.db = db
        self.path = db.path
        self.root = self.path.parent
        self.id = db.id

    @classmethod
    def open(cls, state_dir: str | Path) -> "State":
        marker = Path(state_dir).with_name(Path(state_dir).name + ".initializing")
        if marker.exists():
            raise ValueError(f"Campaign initialization is incomplete or in progress; inspect {marker}")
        return cls(Database(database_path(state_dir)))

    @classmethod
    def create(cls, config, ledger_path: str | Path, *, initialization_token=None) -> "State":
        return cls(Database.create(config, ledger_path, initialization_token=initialization_token))

    def campaign(self, *, connection=None) -> dict:
        from contextlib import nullcontext
        with self.db.read() if connection is None else nullcontext(connection) as conn:
            value = _campaign(conn)
            generation = dict(conn.execute("SELECT * FROM generations WHERE id=?",
                                           (value["generation"],)).fetchone())
            blockers = [dict(r) for r in conn.execute(
                "SELECT * FROM blockers WHERE generation=? AND resolved IS NULL ORDER BY created,id",
                (value["generation"],))]
            if value["operator_mode"] in ("paused", "stopping", "stopped"):
                display = value["operator_mode"]
            elif blockers:
                display = "recovery_required"
            elif generation["state"] == "closed":
                display = "completed" if generation["outcome"] == "assessed" else generation["outcome"]
            elif value["progress"] == "research":
                display = value["operator_mode"]
            else:
                display = value["progress"]
            return {**value, "state": display, "research_outcome": generation["outcome"],
                    "generation_state": generation["state"], "blockers": blockers,
                    "closure": json.loads(generation["assessment"]) if generation["assessment"] else None,
                    "assessment_scope_stale": generation["authority_revision"] is not None
                    and generation["authority_revision"] != value["authority_revision"]}

    def ledger_identity(self) -> tuple[Path, str]:
        with self.db.read() as conn:
            metadata = dict(conn.execute("SELECT key,value FROM meta"))
            return Path(metadata["ledger_path"]), metadata["ledger_id"]

    def bind_ledger(self, path: str | Path, ledger_id: str):
        text(ledger_id, "ledger identity")
        with self.db.write() as conn:
            metadata = dict(conn.execute("SELECT key,value FROM meta"))
            if Path(metadata["ledger_path"]) != Path(path).resolve():
                raise ValueError("Resource ledger path differs from this campaign's recorded ledger")
            if metadata["ledger_id"] and metadata["ledger_id"] != ledger_id:
                raise ValueError("Resource ledger identity changed; recovery cannot use replacement capacity")
            conn.execute("UPDATE meta SET value=? WHERE key='ledger_id'", (ledger_id,))

    def configuration(self, candidate=None):
        from labgoblin.config import load_config, restore_config
        with self.db.read() as conn:
            campaign = _campaign(conn)
            fence = _configuration_fence(conn)
            if campaign["controller"]:
                owner = conn.execute("SELECT value FROM meta WHERE key='config_owner'").fetchone()
                if not owner or owner[0] != campaign["controller"]:
                    raise ValueError("Controller has not activated a valid startup configuration; retry after startup/recovery")
                config = restore_config(_settings(conn, campaign), revision=campaign["config_revision"],
                                        path=self.root.parent / "labgoblin.toml")
            else:
                config = candidate if candidate is not None else load_config(self.root.parent)
            if config.state_dir.resolve() != self.root.resolve():
                raise ValueError("Configuration belongs to another campaign directory")
        return config, fence

    def configure(self, config, *, controller):
        if config.state_dir.resolve() != self.root.resolve():
            raise ValueError("Configuration belongs to another campaign directory")
        with self.db.write() as conn:
            campaign = _campaign(conn)
            owner = _json(controller)
            if not controller or campaign["controller"] != owner:
                raise ValueError("Configuration activation requires exact controller ownership")
            active = conn.execute("SELECT value FROM meta WHERE key='config_owner'").fetchone()
            if active and active[0] == owner:
                raise ValueError("This controller already loaded its configuration; restart to apply edits")
            old = conn.execute("SELECT content FROM configs WHERE id=?", (config.revision,)).fetchone()
            if old and old[0] != _json(asdict(config)):
                raise ValueError("Configuration revision already belongs to different snapshot contents")
            conn.execute("INSERT OR IGNORE INTO configs(id,content,created) VALUES(?,?,?)",
                         (config.revision, _json(asdict(config)), time.time()))
            conn.execute("UPDATE campaign SET config_revision=? WHERE id=?", (config.revision, self.id))
            conn.execute("INSERT INTO meta(key,value) VALUES('config_owner',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (owner,))

    def tick(self, now: float | None = None, *, minimum_elapsed: float | None = None) -> float:
        if minimum_elapsed is not None:
            number(minimum_elapsed, "monotonic elapsed high-water", zero=True)
        with self.db.write() as conn:
            campaign = _campaign(conn)
            elapsed = _elapsed(conn, campaign, time.time() if now is None else now)
            if campaign["started"] is not None and minimum_elapsed is not None and minimum_elapsed > elapsed:
                elapsed = minimum_elapsed
                conn.execute("UPDATE campaign SET elapsed=? WHERE id=?", (elapsed, self.id))
            return elapsed

    def control(self, action: str, request_id: str, expected_revision: int) -> dict:
        if action not in ("run", "pause", "resume", "stop", "reopen"):
            raise ValueError(f"Unknown control action: {action}")
        text(request_id, "request_id")
        integer(expected_revision, "expected_revision", zero=True)
        digest = fingerprint({"action": action, "expected_revision": expected_revision})
        with self.db.write() as conn:
            prior = conn.execute("SELECT digest,result FROM control_requests WHERE id=?",
                                 (request_id,)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise ValueError("Control request ID was reused with a different payload")
                return json.loads(prior["result"])
            campaign = _campaign(conn)
            if campaign["revision"] != expected_revision:
                raise ValueError(f"Control revision conflict: expected {expected_revision}, "
                                 f"current {campaign['revision']}")
            generation = conn.execute("SELECT state FROM generations WHERE id=?",
                                      (campaign["generation"],)).fetchone()[0]
            mode, progress, gen = campaign["operator_mode"], campaign["progress"], campaign["generation"]
            if action in ("run", "resume"):
                if mode in ("stopping", "stopped") or generation == "closed":
                    raise ValueError("Closed/stopped work requires explicit reopen, not run/resume")
                if action == "run" and mode == "paused":
                    raise ValueError("Campaign is paused; use resume")
                if conn.execute("SELECT 1 FROM blockers WHERE resolved IS NULL").fetchone():
                    raise ValueError("Resolve recorded recovery blockers before resuming admission")
                mode = "running"
                if progress in ("blocked", "wait") and action == "resume":
                    progress = "research"
                _event(conn, gen, "control", {"action": action}, f"control-{request_id}")
            elif action == "pause":
                if mode in ("stopping", "stopped"):
                    raise ValueError("Pause cannot undo a stop")
                mode = "paused"
            elif action == "stop":
                mode, progress = "stopping", "closed"
                _seal(conn, gen, "Operator stop")
            else:
                if generation != "closed" and mode != "stopped":
                    raise ValueError("Reopen requires a closed or stopped generation")
                if not self._quiescent(conn):
                    raise ValueError("Reopen requires verified quiescence and released allocations")
                gen += 1
                conn.execute("INSERT INTO generations(id,state,created) VALUES(?,'open',?)",
                             (gen, time.time()))
                mode, progress = "running", "research"
                _event(conn, gen, "reopened", {"previous_generation": gen - 1})
            result = {"request_id": request_id, "generation": gen,
                      "revision": expected_revision + 1, "operator_mode": mode, "progress": progress}
            conn.execute("""UPDATE campaign SET generation=?,revision=?,operator_mode=?,progress=?,
                reason=? WHERE id=?""",
                         (gen, result["revision"], mode, progress, f"Operator {action}", self.id))
            conn.execute("""UPDATE allocations SET state='release_pending' WHERE state IN ('requested','granted')
                AND revision<=?""", (expected_revision,))
            if action in ("run", "resume"):
                conn.execute("UPDATE maintenance SET revision=? WHERE state='pending' AND revision=?",
                             (result["revision"], expected_revision))
            else:
                conn.execute("""UPDATE maintenance SET state='cancelled',reason='Superseded by control revision'
                    WHERE state='pending' AND revision<=?""", (expected_revision,))
            conn.execute("""INSERT INTO control_requests(id,digest,action,expected_revision,result,created)
                VALUES(?,?,?,?,?,?)""", (request_id, digest, action, expected_revision, _json(result), time.time()))
            return result

    @staticmethod
    def _quiescent(conn) -> bool:
        queries = (
            "SELECT 1 FROM attempts WHERE status IN ('starting','running','recovery_required')",
            "SELECT 1 FROM launches WHERE phase IN ('armed','executing')",
            "SELECT 1 FROM turns WHERE state IN ('prepared','running')",
            "SELECT 1 FROM allocations WHERE state IN ('requested','granted','attached','release_pending')",
        )
        return not any(conn.execute(query).fetchone() for query in queries)

    def event(self, kind: str, payload: dict, event_id: str | None = None) -> str:
        text(kind, "event kind")
        table(payload, "event payload")
        with self.db.write() as conn:
            return _event(conn, _campaign(conn)["generation"], kind, payload, event_id)

    def pending_events(self, *, limit: int = 64) -> list[dict]:
        integer(limit, "event limit")
        if limit > 1000:
            raise ValueError("Event page cannot exceed 1000 records")
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute("""SELECT * FROM events
                WHERE generation=? AND acknowledged_by IS NULL ORDER BY seq LIMIT ?""",
                (_campaign(conn)["generation"], limit))]

    def hypothesis(self, hypothesis_id: str, statement: str, *, label: str = "",
                   supersedes: str | None = None):
        text(hypothesis_id, "hypothesis_id")
        text(statement, "hypothesis statement")
        if statement.strip() == hypothesis_id:
            raise ValueError("A hypothesis statement must describe the claim, not repeat its ID")
        with self.db.write() as conn:
            old = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
            now = time.time()
            if old:
                if old["statement"] != statement and old["frozen"]:
                    raise ValueError("Evaluated hypothesis statements are immutable; use a new ID with supersedes")
                if old["supersedes"] != supersedes:
                    raise ValueError("A hypothesis supersedes link cannot be changed")
                conn.execute("""UPDATE hypotheses SET statement=?,label=?,updated=?,
                    status=CASE WHEN statement=? THEN status ELSE 'proposed' END,
                    conclusion=CASE WHEN statement=? THEN conclusion ELSE '' END WHERE id=?""",
                             (statement, label, now, statement, statement, hypothesis_id))
            else:
                conn.execute("""INSERT INTO hypotheses(id,statement,label,supersedes,created,updated)
                    VALUES(?,?,?,?,?,?)""", (hypothesis_id, statement, label, supersedes, now, now))

    def enqueue(self, spec: dict) -> str:
        text(spec.get("id"), "attempt id")
        text(spec.get("key"), "idempotency key")
        resources = Resources.parse({k: spec[k] for k in ("cpus", "memory_mb", "gpus")})
        number(spec.get("seconds"), "seconds")
        if len(canonical(spec)) > 1024 * 1024:
            raise ValueError("Frozen work spec exceeds 1 MiB")
        request_digest = fingerprint(spec["request"])
        with self.db.write() as conn:
            campaign = _campaign(conn)
            if "configuration_fence" in spec:
                _check_configuration_fence(conn, spec["configuration_fence"])
            old = conn.execute("""SELECT id,request_digest FROM attempts
                WHERE generation=? AND idempotency_key=?""", (campaign["generation"], spec["key"])).fetchone()
            if old:
                if old["request_digest"] != request_digest:
                    raise ValueError("Idempotency key already belongs to a different request")
                return old["id"]
            if spec.get("generation", campaign["generation"]) != campaign["generation"]:
                raise ValueError("Submission generation changed during preparation; resubmit explicitly")
            if spec.get("authority_revision", campaign["authority_revision"]) != campaign["authority_revision"]:
                raise ValueError("Governing authority changed during submission preparation; resubmit explicitly")
            if spec.get("submitted_by"):
                turn = conn.execute("""SELECT t.generation,t.kind,t.state,p.content FROM turns t
                    JOIN packets p ON p.id=t.packet_id WHERE t.id=?""", (spec["submitted_by"],)).fetchone()
                if (not turn or turn["generation"] != campaign["generation"] or turn["kind"] != "research"
                        or turn["state"] not in ("prepared", "running")):
                    raise ValueError("Only the current owned research turn may submit work")
                if json.loads(turn["content"]).get("config_revision") != spec.get("configuration_revision"):
                    raise ValueError("Research turn configuration changed; return a handoff before submitting")
            if (campaign["operator_mode"] in ("stopping", "stopped")
                    or campaign["progress"] in ("closed", "finalize")):
                raise ValueError("Campaign is not accepting work; explicitly reopen closed research")
            hid = spec.get("hypothesis_id") or None
            if hid:
                hypothesis = conn.execute("SELECT statement FROM hypotheses WHERE id=?", (hid,)).fetchone()
                statement = text(spec.get("hypothesis_description"), "hypothesis_description")
                if statement.strip() == hid:
                    raise ValueError("Hypothesis statement must not repeat its ID")
                if hypothesis and hypothesis["statement"] != statement:
                    raise ValueError("Hypothesis has a different statement; use the correct claim identity")
                if not hypothesis:
                    now = time.time()
                    conn.execute("INSERT INTO hypotheses(id,statement,created,updated) VALUES(?,?,?,?)",
                                 (hid, statement, now, now))
            conn.execute("""INSERT INTO attempts(id,generation,idempotency_key,request_digest,spec,
                experiment_id,hypothesis_id,status,cpus,memory_mb,gpus,seconds,created,admission_order)
                VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?,?)""",
                         (spec["id"], campaign["generation"], spec["key"], request_digest, _json(spec),
                          spec.get("experiment_id", spec["key"]), hid, resources.cpus, resources.memory_mb,
                          _json(resources.gpus), spec["seconds"], time.time(), _queue_order(conn)))
            conn.execute("UPDATE campaign SET attempt_revision=attempt_revision+1 WHERE id=?", (self.id,))
            return spec["id"]

    def attempt(self, attempt_id: str) -> dict:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown attempt: {attempt_id}")
            return dict(row)

    def attempts(self, *, statuses: tuple[str, ...] | None = None,
                 limit: int = 100, offset: int = 0) -> list[dict]:
        integer(limit, "limit")
        integer(offset, "offset", zero=True)
        if limit > 1000:
            raise ValueError("Attempt pages cannot exceed 1000 records")
        params = []
        condition = ""
        if statuses is not None:
            if not statuses or set(statuses) - set(ACTIVE + TERMINAL):
                raise ValueError("Invalid attempt status filter")
            condition = f"WHERE status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM attempts {condition} ORDER BY created,id LIMIT ? OFFSET ?",
                (*params, limit, offset))]

    def source(self, kind: str, body: bytes, *, origin: str, head: str | None = None,
               metadata: dict | None = None, expected_revision: int | None = None, notify=False,
               configuration_fence=None) -> str:
        text(kind, "source kind")
        text(origin, "source origin")
        if not isinstance(body, bytes) or len(body) > 1024 * 1024:
            raise ValueError("Authoritative source must be bytes no larger than 1 MiB")
        if len(kind.encode()) > 128 or len(origin.encode()) > 128 or len(canonical(metadata or {})) > 16384:
            raise ValueError("Source kind/origin/metadata exceed their bounded provenance allowance")
        if head and (head == "rationale" or head.startswith("checkpoint:")):
            raise ValueError("Only an accepted owned handoff can replace governing rationale")
        with self.db.write() as conn:
            if configuration_fence is not None:
                _check_configuration_fence(conn, configuration_fence)
            if expected_revision is not None:
                current = conn.execute("SELECT revision FROM source_heads WHERE name=?", (head,)).fetchone()
                if (current[0] if current else 0) != expected_revision:
                    raise ValueError("Source revision conflict; preserve and retry the observed edit")
            source_id = _source(conn, kind, body, origin, metadata or {}, head)
            if notify:
                if head in ("goal", "protocol"):
                    _authority_changed(conn)
                _event(conn, _campaign(conn)["generation"], "source", {"source_id": source_id, "head": head})
            return source_id

    def directive(self, body: str, *, scope="campaign", supersedes=None, request_id=None) -> dict:
        encoded = text(body, "operator directive").encode("utf-8")
        if len(text(scope, "directive scope").encode("utf-8")) > 256:
            raise ValueError("Directive scope exceeds 256 bytes")
        if len(encoded) > 65536:
            raise ValueError("An operator directive cannot exceed 64 KiB")
        directive_id = request_id or identifier()
        text(directive_id, "directive request ID")
        if len(directive_id.encode()) > 128:
            raise ValueError("Directive request ID cannot exceed 128 bytes")
        with self.db.write() as conn:
            prior = conn.execute("""SELECT d.*,s.body FROM directives d JOIN sources s ON s.id=d.source_id
                WHERE d.id=?""", (directive_id,)).fetchone()
            if prior:
                if (bytes(prior["body"]) != encoded or prior["scope"] != scope or prior["supersedes"] != supersedes):
                    raise ValueError("Directive request ID belongs to a different payload")
                return {key: prior[key] for key in prior.keys() if key != "body"}
            if scope != "campaign":
                group, separator, target = scope.partition(":")
                if not separator or group not in ("generation", "hypothesis"):
                    raise ValueError("Directive scope must be campaign, generation:ID or hypothesis:ID")
                if group == "generation" and (not target.isdecimal() or str(int(target)) != target):
                    raise ValueError("Generation scope must use the exact recorded integer ID")
                table_name = "generations" if group == "generation" else "hypotheses"
                if not conn.execute(f"SELECT 1 FROM {table_name} WHERE id=?", (target,)).fetchone():
                    raise ValueError("Directive scope refers to an unknown generation or hypothesis")
            if supersedes:
                previous = conn.execute("SELECT active FROM directives WHERE id=?", (supersedes,)).fetchone()
                if not previous or not previous["active"]:
                    raise ValueError("Supersession requires an active existing directive")
                conn.execute("UPDATE directives SET active=0 WHERE id=?", (supersedes,))
            source_id = _source(conn, "directive", encoded, "operator-command",
                                {"scope": scope, "supersedes": supersedes})
            conn.execute("""INSERT INTO directives(id,source_id,origin,scope,supersedes,created)
                VALUES(?,?,'operator-command',?,?,?)""", (directive_id, source_id, scope, supersedes, time.time()))
            _authority_changed(conn)
            _event(conn, _campaign(conn)["generation"], "directive",
                   {"directive_id": directive_id, "source_id": source_id, "scope": scope, "supersedes": supersedes})
            return dict(conn.execute("SELECT * FROM directives WHERE id=?", (directive_id,)).fetchone())

    def request_maintenance(self, kind: str, *, origin="operator", options=None) -> dict:
        if kind not in ("report", "compact") or origin not in ("operator", "automatic"):
            raise ValueError("Only explicit report/compact or automatic compaction requests are supported")
        if origin == "automatic" and kind != "compact":
            raise ValueError("Reports are never automatic paid requests")
        options = options or {}
        if kind == "report":
            from labgoblin.reporting import selection_options
            options = selection_options(**table(options, "report options", {"selected", "selection_reason"}))
        elif options:
            raise ValueError("Compaction has no arbitrary operation options")
        with self.db.write() as conn:
            return self._maintenance(conn, kind, origin, options)

    @staticmethod
    def _maintenance(conn, kind, origin, options=None):
        if kind == "report":
            from labgoblin.reporting import selection_options
            options = selection_options(**(options or {}))
        options = options or {}
        revision = _source_revision(conn)
        campaign = _campaign(conn)
        prior = conn.execute("SELECT * FROM maintenance WHERE kind=? AND source_revision=? AND options_digest=?",
                             (kind, revision, fingerprint(options))).fetchone()
        if prior:
            if prior["state"] == "cancelled" and origin == "operator" and not conn.execute(
                    """SELECT 1 FROM invocations WHERE turn_id=?
                    AND state NOT IN ('not_started','cancelled','reserved')""", (prior["turn_id"],)).fetchone():
                conn.execute("""UPDATE maintenance SET state='pending',origin='operator',reason='',turn_id=NULL,view_id=NULL,revision=?
                    WHERE id=?""", (campaign["revision"], prior["id"]))
                return dict(conn.execute("SELECT * FROM maintenance WHERE id=?", (prior["id"],)).fetchone())
            return dict(prior)
        maintenance_id = identifier()
        conn.execute("""INSERT INTO maintenance(id,generation,kind,source_revision,origin,revision,state,created,options,options_digest)
            VALUES(?,?,?,?,?,?,'pending',?,?,?)""",
                     (maintenance_id, campaign["generation"], kind, revision, origin, campaign["revision"], time.time(),
                      _json(options), fingerprint(options)))
        _event(conn, campaign["generation"], "maintenance", {"request_id": maintenance_id, "kind": kind, "origin": origin})
        return dict(conn.execute("SELECT * FROM maintenance WHERE id=?", (maintenance_id,)).fetchone())

    def pending_maintenance(self) -> dict | None:
        with self.db.write() as conn:
            campaign = _campaign(conn)
            row = conn.execute("""SELECT * FROM maintenance WHERE state='pending' AND generation=? AND revision=?
                AND (origin='operator' OR EXISTS(SELECT 1 FROM generations WHERE id=? AND state='open'))
                ORDER BY created,id LIMIT 1""",
                               (campaign["generation"], campaign["revision"], campaign["generation"])).fetchone()
            if row is None:
                return None
            if row["source_revision"] != _source_revision(conn):
                conn.execute("UPDATE maintenance SET state='cancelled',reason='Coalesced to newer sources' WHERE id=?",
                             (row["id"],))
                return self._maintenance(conn, row["kind"], row["origin"], json.loads(row["options"]))
            return dict(row)

    def fail_maintenance(self, request_id: str, reason: str):
        with self.db.write() as conn:
            conn.execute("UPDATE maintenance SET state='failed',reason=? WHERE id=? AND state='pending'",
                         (text(reason, "maintenance failure reason"), request_id))

    def blocker(self, blocker_id: str, category: str, detail: str, work_id: str | None = None):
        text(detail, "blocker detail")
        with self.db.write() as conn:
            generation = _campaign(conn)["generation"]
            _blocker(conn, generation, blocker_id, category, detail, work_id)

    def allocation(self, work_id: str, kind: str, request: dict, expected_revision: int) -> dict:
        if kind not in ("attempt", "research", "final_analysis", "report", "compact"):
            raise ValueError("Unknown campaign allocation kind")
        resources = Resources.parse(request["resources"])
        with self.db.write() as conn:
            campaign = _campaign(conn)
            if campaign["revision"] != expected_revision:
                raise ValueError("Admission control revision changed")
            self._admissible(conn, campaign, kind, work_id=work_id)
            old = conn.execute("""SELECT * FROM allocations WHERE work_id=?
                AND state IN ('requested','granted','attached')""", (work_id,)).fetchone()
            if old:
                if old["digest"] != fingerprint(request):
                    raise ValueError("Admission request changed while allocation is pending")
                return dict(old)
            settings = _settings(conn, campaign)
            if kind == "attempt":
                work = conn.execute("SELECT status FROM attempts WHERE id=?", (work_id,)).fetchone()
                if not work or work["status"] != "queued":
                    raise ValueError("Attempt allocation requires unlaunched queued work")
                count = conn.execute("""SELECT COUNT(*) FROM attempts
                    WHERE status IN ('starting','running','recovery_required')""").fetchone()[0]
                if count >= settings["campaign"]["max_jobs"]:
                    raise AdmissionWait("Waiting for campaign experiment concurrency")
            else:
                work = conn.execute("SELECT kind,state,revision FROM turns WHERE id=?", (work_id,)).fetchone()
                if (not work or work["kind"] != kind or work["state"] not in ("prepared", "running")
                        or work["revision"] != campaign["revision"]):
                    raise AdmissionClosed("Turn allocation requires current owned work")
            envelope = settings["campaign"]["resources"]
            if (resources.cpus > envelope["cpus"] or resources.memory_mb > envelope["memory_mb"]
                    or not set(resources.gpus).issubset(envelope["gpus"])):
                raise ValueError("Resource request cannot fit the campaign envelope")
            active = [json.loads(r[0])["resources"] for r in conn.execute(
                "SELECT request FROM allocations WHERE state IN ('granted','attached','release_pending')")]
            if (sum(r["cpus"] for r in active) + resources.cpus > envelope["cpus"]
                    or sum(r["memory_mb"] for r in active) + resources.memory_mb > envelope["memory_mb"]
                    or not set(resources.gpus).issubset(envelope["gpus"])):
                raise AdmissionWait("Waiting for the campaign resource envelope")
            token = identifier()
            conn.execute("""INSERT INTO allocations(token,generation,work_id,kind,revision,request,digest,state,created)
                VALUES(?,?,?,?,?,?,?,'requested',?)""",
                         (token, campaign["generation"], work_id, kind, expected_revision,
                          _json(request), fingerprint(request), time.time()))
            return dict(conn.execute("SELECT * FROM allocations WHERE token=?", (token,)).fetchone())

    @staticmethod
    def _admissible(conn, campaign: dict, kind: str, *, work_id=None, maintenance_id=None):
        maintenance = None
        if kind in ("report", "compact"):
            maintenance = conn.execute("""SELECT * FROM maintenance WHERE kind=? AND generation=? AND revision=?
                AND ((id=? AND state='pending') OR (turn_id=? AND state='running'))""",
                                       (kind, campaign["generation"], campaign["revision"], maintenance_id, work_id)).fetchone()
            if maintenance is None:
                raise AdmissionClosed("Maintenance requires its exact scoped request authorization")
        explicit = maintenance is not None and maintenance["origin"] == "operator"
        if campaign["operator_mode"] not in (("ready", "running", "stopped") if explicit else ("ready", "running")):
            raise AdmissionClosed("Operator intent prevents new admission")
        generation = conn.execute("SELECT state FROM generations WHERE id=?",
                                  (campaign["generation"],)).fetchone()[0]
        if generation != ("sealed" if kind == "final_analysis" else "open") and not explicit:
            raise AdmissionClosed("Generation does not admit this operation")
        if explicit and generation != "open":
            if (conn.execute("SELECT 1 FROM attempts WHERE status IN ('starting','running','recovery_required') LIMIT 1").fetchone()
                    or conn.execute("""SELECT 1 FROM allocations WHERE state!='released'
                        AND work_id!=COALESCE(?,'') LIMIT 1""", (work_id,)).fetchone()
                    or conn.execute("""SELECT 1 FROM turns WHERE state IN ('prepared','running')
                        AND id!=COALESCE(?,'') LIMIT 1""", (work_id,)).fetchone()):
                raise AdmissionWait("Closed-campaign operator maintenance requires quiescence")
        if conn.execute("SELECT 1 FROM blockers WHERE resolved IS NULL").fetchone():
            raise AdmissionClosed("Unresolved recovery blocks admission")
        settings = _settings(conn, campaign)
        resources = settings["campaign"]["resources"]
        committed = [json.loads(r[0])["resources"] for r in conn.execute(
            "SELECT request FROM allocations WHERE state IN ('granted','attached','release_pending')")]
        if (sum(r["cpus"] for r in committed) > resources["cpus"]
                or sum(r["memory_mb"] for r in committed) > resources["memory_mb"]):
            raise AdmissionWait("Current campaign envelope is below existing commitments")
        elapsed = _elapsed(conn, campaign, time.time())
        if Limit(settings["campaign"]["max_seconds"]["value"]).exhausted(elapsed):
            raise BudgetExhausted("elapsed", "Campaign elapsed-admission budget exhausted")

    def granted(self, token: str):
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM allocations WHERE token=?", (token,)).fetchone()
            if row is None:
                raise ValueError("Unknown allocation token")
            if row["state"] == "requested":
                settings = _settings(conn, _campaign(conn))["campaign"]["resources"]
                requested = json.loads(row["request"])["resources"]
                existing = [json.loads(r[0])["resources"] for r in conn.execute(
                    "SELECT request FROM allocations WHERE state IN ('granted','attached','release_pending')")]
                if (sum(r["cpus"] for r in existing) + requested["cpus"] > settings["cpus"]
                        or sum(r["memory_mb"] for r in existing) + requested["memory_mb"] > settings["memory_mb"]):
                    conn.execute("""UPDATE allocations SET state='release_pending',
                        reason='Concurrent admission filled the campaign envelope' WHERE token=?""", (token,))
                    return False
                conn.execute("UPDATE allocations SET state='granted' WHERE token=?", (token,))
            return row["state"] in ("requested", "granted", "attached")

    def release_pending(self, token: str):
        with self.db.write() as conn:
            if conn.execute("SELECT 1 FROM launches WHERE grant_id=? AND phase IN ('armed','executing')",
                            (token,)).fetchone():
                raise ValueError("Cannot release an armed or executing allocation")
            if not conn.execute("SELECT 1 FROM allocations WHERE token=?", (token,)).fetchone():
                raise ValueError("Unknown allocation token")
            conn.execute("UPDATE allocations SET state='release_pending' WHERE token=? AND state!='released'",
                         (token,))

    def released(self, token: str):
        with self.db.write() as conn:
            changed = conn.execute("""UPDATE allocations SET state='released',released=?
                WHERE token=? AND state='release_pending'""", (time.time(), token)).rowcount
            if not changed:
                row = conn.execute("SELECT state FROM allocations WHERE token=?", (token,)).fetchone()
                if row is None or row["state"] != "released":
                    raise ValueError("Release acknowledgement has no matching release intent")

    def arm(self, envelope: LaunchEnvelope):
        value = asdict(envelope)
        with self.db.write() as conn:
            campaign = _campaign(conn)
            allocation = conn.execute("SELECT * FROM allocations WHERE token=?",
                                      (envelope.key.grant_id,)).fetchone()
            if not allocation or allocation["state"] not in ("granted", "attached"):
                raise ValueError("Launch requires its exact unreleased allocation")
            if (envelope.key.campaign_id != self.id or envelope.key.generation != campaign["generation"]
                    or allocation["revision"] != campaign["revision"]
                    or allocation["generation"] != campaign["generation"]):
                raise ValueError("Launch authorization lost to a control revision or generation change")
            admission_kind = envelope.kind
            if admission_kind == "canary":
                owner = conn.execute("""SELECT t.kind FROM invocations i JOIN turns t ON t.id=i.turn_id
                    WHERE i.id=?""", (envelope.key.work_id,)).fetchone()
                if not owner:
                    raise ValueError("Canary has no owning operation")
                admission_kind = owner["kind"]
            self._admissible(conn, campaign, admission_kind, work_id=allocation["work_id"])
            if admission_kind == "final_analysis":
                generation = conn.execute("SELECT final_turn FROM generations WHERE id=?", (campaign["generation"],)).fetchone()
                if generation["final_turn"] not in (None, allocation["work_id"]):
                    raise AdmissionClosed("Generation already spent its one final-analysis authorization")
                conn.execute("UPDATE generations SET final_turn=? WHERE id=?",
                             (allocation["work_id"], campaign["generation"]))
            metadata = dict(conn.execute("SELECT key,value FROM meta"))
            if (not metadata["ledger_id"] or metadata["ledger_id"] != envelope.ledger_id
                    or Path(metadata["ledger_path"]) != Path(envelope.ledger_path)
                    or Path(envelope.state_path) != self.path
                    or envelope.config_revision != campaign["config_revision"]):
                raise ValueError("Launch envelope identity differs from the authorized campaign")
            requested = json.loads(allocation["request"])
            if Resources.parse(requested["resources"]) != envelope.resources:
                raise ValueError("Launch resources differ from the granted request")
            if envelope.kind == "attempt":
                attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (envelope.key.work_id,)).fetchone()
                if (not attempt or attempt["status"] != "queued"
                        or allocation["work_id"] != attempt["id"]):
                    raise ValueError("Attempt is no longer queued for this allocation")
                if (Resources(attempt["cpus"], attempt["memory_mb"], tuple(json.loads(attempt["gpus"]))) != envelope.resources
                        or envelope.timeout_seconds > attempt["seconds"]):
                    raise ValueError("Launch exceeds or changes the attempt's admitted execution request")
                if envelope.metadata.get("spec_digest") and envelope.metadata["spec_digest"] != fingerprint(json.loads(attempt["spec"])):
                    raise ValueError("Launch scientific spec differs from its queued request")
                if json.loads(attempt["spec"]).get("authority_revision", campaign["authority_revision"]) != campaign["authority_revision"]:
                    raise ValueError("Queued request predates current governing authority; resubmit against its intended scope")
                settings = _settings(conn, campaign)
                running = conn.execute("""SELECT COUNT(*),COALESCE(SUM(seconds*json_array_length(gpus)/3600),0)
                    FROM attempts WHERE status IN ('starting','running','recovery_required')""").fetchone()
                used = campaign["gpu_hours"]
                if running[0] >= settings["campaign"]["max_jobs"]:
                    raise AdmissionWait("Campaign experiment concurrency exhausted")
                if used + running[1] + attempt["seconds"] * len(json.loads(attempt["gpus"])) / 3600 > \
                        settings["campaign"]["max_gpu_hours"]:
                    raise BudgetExhausted("gpu_hours", "Campaign GPU-hour admission budget exhausted")
                if attempt["hypothesis_id"]:
                    statement = conn.execute("SELECT statement FROM hypotheses WHERE id=?",
                                             (attempt["hypothesis_id"],)).fetchone()[0]
                    if statement != json.loads(attempt["spec"])["hypothesis_description"]:
                        raise ValueError("Queued claim changed; resubmit against its intended statement")
                    conn.execute("UPDATE hypotheses SET frozen=1 WHERE id=?", (attempt["hypothesis_id"],))
                conn.execute("UPDATE attempts SET status='starting',grant_id=?,nonce=? WHERE id=?",
                             (envelope.key.grant_id, envelope.key.nonce, attempt["id"]))
            else:
                invocation = conn.execute("SELECT * FROM invocations WHERE id=?",
                                          (envelope.key.work_id,)).fetchone()
                if (not invocation or invocation["state"] != "reserved" or invocation["kind"] != envelope.kind
                        or allocation["work_id"] != invocation["turn_id"]):
                    raise ValueError("Provider launch lacks its reserved invocation and turn allocation")
                turn = conn.execute("SELECT state,revision FROM turns WHERE id=?", (invocation["turn_id"],)).fetchone()
                if turn["state"] not in ("prepared", "running") or turn["revision"] != campaign["revision"]:
                    raise AdmissionClosed("Provider turn was superseded by control or acceptance")
                settings = _settings(conn, campaign)
                committed = campaign["invocations"] + conn.execute(
                    "SELECT COUNT(*) FROM invocations WHERE state='reserved'").fetchone()[0]
                if not Limit(settings["campaign"]["max_invocations"]["value"]).allows(committed):
                    raise BudgetExhausted("invocations", "Current invocation budget is below existing commitments")
                conn.execute("UPDATE invocations SET state='armed',nonce=? WHERE id=?",
                             (envelope.key.nonce, envelope.key.work_id))
                conn.execute("UPDATE campaign SET invocations=invocations+1 WHERE id=?", (self.id,))
            conn.execute("""INSERT INTO launches(nonce,work_id,kind,generation,grant_id,envelope,digest,phase,created)
                VALUES(?,?,?,?,?,?,?,'armed',?)""",
                         (envelope.key.nonce, envelope.key.work_id, envelope.kind,
                          envelope.key.generation, envelope.key.grant_id, _json(value), envelope.digest, time.time()))
            conn.execute("UPDATE allocations SET state='attached' WHERE token=?", (envelope.key.grant_id,))
            now = time.time()
            conn.execute("""UPDATE campaign SET started=COALESCE(started,?),
                observed_wall=COALESCE(observed_wall,?) WHERE id=?""", (now, now, self.id))

    def claim_launch(self, envelope: LaunchEnvelope, supervisor: dict) -> bool:
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM launches WHERE nonce=?", (envelope.key.nonce,)).fetchone()
            if not row or row["digest"] != envelope.digest:
                raise ValueError("Supervisor has no matching launch authorization")
            if row["phase"] != "armed":
                return False
            if supervisor.get("token") != envelope.key.nonce:
                raise ValueError("Supervisor identity does not match the launch nonce")
            if row["supervisor"] and json.loads(row["supervisor"]) != supervisor:
                raise ValueError("Launch already has a different supervisor identity")
            conn.execute("UPDATE launches SET phase='executing',supervisor=? WHERE nonce=?",
                         (_json(supervisor), envelope.key.nonce))
            if envelope.kind == "attempt":
                conn.execute("UPDATE attempts SET status='running',started=? WHERE id=?",
                             (time.time(), envelope.key.work_id))
            else:
                conn.execute("UPDATE invocations SET state='running',started=? WHERE id=?",
                             (time.time(), envelope.key.work_id))
                conn.execute("""UPDATE turns SET state='running' WHERE id=(
                    SELECT turn_id FROM invocations WHERE id=?)""", (envelope.key.work_id,))
            conn.execute("UPDATE blockers SET resolved=? WHERE id=?",
                         (time.time(), f"launch-{envelope.key.nonce}"))
            return True

    def attach_supervisor(self, envelope: LaunchEnvelope, supervisor: dict):
        with self.db.write() as conn:
            row = conn.execute("SELECT digest,supervisor FROM launches WHERE nonce=?",
                               (envelope.key.nonce,)).fetchone()
            if not row or row["digest"] != envelope.digest or supervisor.get("token") != envelope.key.nonce:
                raise ValueError("Supervisor attachment does not match its launch")
            if row["supervisor"] and json.loads(row["supervisor"]) != supervisor:
                raise ValueError("Conflicting supervisor attachment")
            conn.execute("UPDATE launches SET supervisor=? WHERE nonce=?",
                         (_json(supervisor), envelope.key.nonce))

    def attach_launcher(self, envelope: LaunchEnvelope, launcher: dict):
        with self.db.write() as conn:
            row = conn.execute("SELECT digest,launcher FROM launches WHERE nonce=?",
                               (envelope.key.nonce,)).fetchone()
            if not row or row["digest"] != envelope.digest or launcher.get("token") != envelope.key.nonce:
                raise ValueError("Launcher attachment does not match its launch")
            if row["launcher"] and json.loads(row["launcher"]) != launcher:
                raise ValueError("Conflicting launcher attachment")
            conn.execute("UPDATE launches SET launcher=? WHERE nonce=?",
                         (_json(launcher), envelope.key.nonce))

    def active_launches(self) -> list[dict]:
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM launches WHERE phase IN ('armed','executing') ORDER BY created,nonce")]

    def launch(self, nonce: str) -> dict:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM launches WHERE nonce=?", (nonce,)).fetchone()
            if row is None:
                raise ValueError("Unknown launch nonce")
            return dict(row)

    def launch_problem(self, nonce: str, detail: str) -> bool:
        text(detail, "launch problem")
        with self.db.write() as conn:
            row = conn.execute("SELECT phase,generation,work_id FROM launches WHERE nonce=?", (nonce,)).fetchone()
            if row is None:
                raise ValueError("Unknown launch nonce")
            if row["phase"] == "quiescent":
                return False
            _blocker(conn, row["generation"], f"launch-{nonce}", "launch", detail, row["work_id"])
            return True

    def finish_launch(self, nonce: str, receipt: dict):
        receipt = asdict(LaunchReceipt.parse(receipt))
        with self.db.write() as conn:
            launch = conn.execute("SELECT * FROM launches WHERE nonce=?", (nonce,)).fetchone()
            if not launch:
                raise ValueError("Receipt refers to an unknown launch")
            envelope = LaunchEnvelope.parse(json.loads(launch["envelope"]))
            if (receipt.get("protocol") != envelope.protocol
                    or LaunchKey.parse(receipt.get("key")) != envelope.key
                    or receipt.get("envelope_digest") != envelope.digest):
                raise ValueError("Receipt does not match its launch incarnation")
            if receipt.get("quiescent") is not True or receipt.get("status") not in TERMINAL:
                raise ValueError("Receipt does not establish a terminal, quiescent owned operation")
            number(receipt.get("elapsed"), "receipt elapsed", zero=True)
            if launch["phase"] == "quiescent":
                if launch["receipt"] != _json(receipt):
                    raise ValueError("Conflicting receipt for a completed launch")
                return
            if launch["phase"] not in ("armed", "executing"):
                raise ValueError("Receipt cannot complete a revoked launch")
            now = time.time()
            conn.execute("UPDATE launches SET phase='quiescent',receipt=?,ended=? WHERE nonce=?",
                         (_json(receipt), now, nonce))
            if envelope.kind == "attempt":
                gpu_hours = receipt["elapsed"] * len(envelope.resources.gpus) / 3600 if receipt["executed"] else 0
                conn.execute("""UPDATE attempts SET status=?,ended=?,exit_code=?,elapsed=?,gpu_hours=?,
                    reason=? WHERE id=? AND nonce=?""",
                             (receipt["status"], now, receipt.get("returncode"), receipt["elapsed"],
                              gpu_hours, receipt.get("reason", ""), envelope.key.work_id, nonce))
                conn.execute("UPDATE campaign SET gpu_hours=gpu_hours+? WHERE id=?", (gpu_hours, self.id))
                _event(conn, envelope.key.generation, "execution",
                       {"attempt_id": envelope.key.work_id, "status": receipt["status"]},
                       f"execution-{envelope.key.work_id}")
            else:
                status = ("not_started" if not receipt["executed"] else
                          "completed" if receipt["status"] == "completed" else "failed")
                conn.execute("UPDATE invocations SET state=?,ended=?,usage=?,reason=? WHERE id=? AND nonce=?",
                             (status, now, _json(receipt.get("usage")), receipt.get("reason", ""),
                              envelope.key.work_id, nonce))
                if not receipt["executed"]:
                    conn.execute("UPDATE campaign SET invocations=invocations-1 WHERE id=?", (self.id,))
                if receipt["status"] != "completed":
                    conn.execute("""UPDATE invocations SET state='cancelled',ended=?,reason=?
                        WHERE turn_id=(SELECT turn_id FROM invocations WHERE id=?) AND state='reserved'""",
                                 (now, "Earlier bundle invocation did not complete", envelope.key.work_id))
            if envelope.metadata.get("last_in_bundle", True) or receipt["status"] != "completed":
                conn.execute("UPDATE allocations SET state='release_pending' WHERE token=?",
                             (envelope.key.grant_id,))
            conn.execute("UPDATE blockers SET resolved=? WHERE id=?", (now, f"launch-{nonce}"))
            conn.execute("UPDATE blockers SET resolved=? WHERE id=?", (now, f"cancel-{envelope.key.work_id}"))

    def reserve_invocations(self, turn_id: str, kind: str, kinds: tuple[str, ...]) -> list[str]:
        if kind not in ("research", "final_analysis", "report", "compact") or kinds not in ((kind,), ("canary", kind)):
            raise ValueError("Invalid provider invocation bundle")
        with self.db.write() as conn:
            campaign = _campaign(conn)
            self._admissible(conn, campaign, kind, work_id=turn_id)
            turn = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if (not turn or turn["generation"] != campaign["generation"] or turn["kind"] != kind
                    or turn["state"] not in ("prepared", "running") or turn["revision"] != campaign["revision"]):
                raise ValueError("Invocation bundle requires its exact prepared turn")
            old = list(conn.execute("SELECT id,kind FROM invocations WHERE bundle_id=? ORDER BY bundle_position",
                                    (turn_id,)))
            if old:
                if tuple(r["kind"] for r in old) != kinds:
                    raise ValueError("Invocation bundle changed after reservation")
                return [r["id"] for r in old]
            settings = _settings(conn, campaign)
            expected = ("canary", kind) if settings["agent"]["sandbox"] else (kind,)
            if kinds != expected:
                raise ValueError("Invocation bundle differs from the configured sandbox policy")
            limit = Limit(settings["campaign"]["max_invocations"]["value"])
            committed = campaign["invocations"] + conn.execute(
                "SELECT COUNT(*) FROM invocations WHERE state='reserved'").fetchone()[0]
            generation_state = conn.execute("SELECT state FROM generations WHERE id=?", (campaign["generation"],)).fetchone()[0]
            reserve = (2 if settings["agent"]["sandbox"] else 1) if kind != "final_analysis" and generation_state != "closed" else 0
            if not limit.allows(committed, len(kinds) + reserve):
                raise BudgetExhausted("invocations", "Invocation budget exhausted after reserving final analysis")
            ids = []
            for index, invocation_kind in enumerate(kinds):
                invocation_id = identifier()
                conn.execute("""INSERT INTO invocations(id,turn_id,generation,kind,bundle_id,bundle_position,state,created)
                    VALUES(?,?,?,?,?,?,'reserved',?)""",
                             (invocation_id, turn_id, campaign["generation"], invocation_kind, turn_id,
                              index, time.time()))
                ids.append(invocation_id)
            return ids

    def record_collection(self, attempt_id: str, observations: list[dict], collection: str,
                          validation: str, reason: str, *, recollect: bool = False) -> dict:
        if collection not in ("complete", "failed", "not_performed"):
            raise ValueError("Invalid collection outcome")
        if validation not in ("valid", "invalid", "unchecked", "not_performed"):
            raise ValueError("Invalid evidence validation outcome")
        with self.db.write() as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["status"] not in TERMINAL:
                raise ValueError("Collection cannot publish for an active or unknown attempt")
            if attempt["collection"] != "pending" and not recollect:
                return _collection(conn, attempt)
            ids = []
            for observation in observations:
                conn.execute("""INSERT INTO observations(id,attempt_id,path,kind,size,digest,body,metadata,assurance,created)
                    VALUES(?,?,?,?,?,?,NULL,?,?,?)""",
                             (observation["id"], attempt_id, observation["path"], observation["kind"],
                              observation["size"], observation["digest"], _json(observation["metadata"]),
                              observation["assurance"], time.time()))
                ids.append(observation["id"])
            event_id = _event(conn, attempt["generation"], "evidence",
                              {"attempt_id": attempt_id, "observation_ids": ids, "collection": collection,
                               "validation": validation, "reason": reason},
                              f"collection-{attempt_id}" if not recollect else f"recollection-{identifier()}")
            conn.execute("UPDATE attempts SET collection=?,collection_reason=?,collection_event=?,validation=? WHERE id=?",
                         (collection, reason, event_id, validation, attempt_id))
            return {"attempt_id": attempt_id, "collection": collection, "validation": validation,
                    "observation_ids": ids, "reason": reason, "event_id": event_id}

    def collection(self, attempt_id: str) -> dict:
        with self.db.read() as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise ValueError("Unknown attempt")
            return _collection(conn, attempt)

    def retrieved(self, turn_id: str, reference_id: str):
        with self.db.write() as conn:
            if not conn.execute("""SELECT 1 FROM turns t JOIN invocations i ON i.turn_id=t.id
                WHERE t.id=? AND t.state IN ('prepared','running') AND i.state IN ('armed','running')
                AND i.kind=t.kind""", (turn_id,)).fetchone():
                raise ValueError("Retrieval registration requires a currently owned provider turn")
            exists = any(conn.execute(f"SELECT 1 FROM {table_name} WHERE id=?", (reference_id,)).fetchone()
                         for table_name in ("sources", "observations", "events", "views"))
            if not exists:
                raise ValueError("Cannot register an unavailable source reference")
            conn.execute("INSERT OR IGNORE INTO turn_references(turn_id,reference_id) VALUES(?,?)",
                         (turn_id, reference_id))

    def retrieved_view(self, turn_id: str, value: dict):
        with self.db.write() as conn:
            if not conn.execute("""SELECT 1 FROM turns t JOIN invocations i ON i.turn_id=t.id
                WHERE t.id=? AND t.state IN ('prepared','running') AND i.state IN ('armed','running')
                AND i.kind=t.kind""", (turn_id,)).fetchone():
                raise ValueError("View retrieval requires a currently owned provider turn")
            row = conn.execute("SELECT metadata FROM views WHERE id=?", (value["id"],)).fetchone()
            if not row or json.loads(row["metadata"])["inventory_digest"] != value["metadata"]["inventory_digest"]:
                raise ValueError("Retrieved page differs from its retained source view")
            coverage = value["coverage"]
            if not 0 <= coverage["offset"] <= coverage["end"] <= json.loads(row["metadata"])["attempts"]:
                raise ValueError("Invalid inventory page coverage")
            conn.execute("INSERT OR IGNORE INTO turn_view_pages(turn_id,view_id,start,end) VALUES(?,?,?,?)",
                         (turn_id, value["id"], coverage["offset"], coverage["end"]))
            for ref in [value["id"], *(o["id"] for member in value["attempts"] for o in member["observations"])]:
                conn.execute("INSERT OR IGNORE INTO turn_references(turn_id,reference_id) VALUES(?,?)", (turn_id, ref))

    def accept_handoff(self, handoff: Handoff, *, invocation_id: str):
        encoded = _json(asdict(handoff))
        with self.db.write() as conn:
            turn = conn.execute("SELECT * FROM turns WHERE id=?", (handoff.turn_id,)).fetchone()
            if not turn or turn["kind"] not in ("research", "final_analysis") or turn["packet_id"] != handoff.packet_id:
                raise ValueError("Handoff does not belong to its turn and packet")
            if turn["state"] == "accepted":
                if turn["result"] != encoded:
                    raise ValueError("Accepted handoff cannot be replaced")
                return
            if turn["state"] not in ("prepared", "running"):
                raise ValueError("Turn is not eligible for handoff acceptance")
            owned = conn.execute("""SELECT l.receipt FROM invocations i JOIN launches l ON l.nonce=i.nonce
                WHERE i.id=? AND i.turn_id=? AND i.kind=? AND i.state='completed' AND l.phase='quiescent'""",
                                 (invocation_id, handoff.turn_id, turn["kind"])).fetchone()
            if (not owned or json.loads(owned["receipt"]).get("metadata", {}).get(
                    "result_capture", {}).get("handoff_digest") != fingerprint(asdict(handoff))):
                raise ValueError("Handoff requires its exact successfully owned provider result")
            packet = conn.execute("SELECT * FROM packets WHERE id=?", (handoff.packet_id,)).fetchone()
            if not packet or not packet["ready"]:
                raise ValueError("Turn packet was not durably published")
            if hashlib.sha256(packet["content"].encode("utf-8")).hexdigest() != packet["digest"]:
                raise ValueError("Committed turn packet integrity failed")
            delivered = {r[0] for r in conn.execute(
                "SELECT event_id FROM packet_events WHERE packet_id=?", (handoff.packet_id,))}
            if {e.event_id for e in handoff.evidence} - delivered:
                raise ValueError("Cannot acknowledge evidence outside the turn packet")
            allowed_refs = set(json.loads(packet["content"]).get("references", []))
            allowed_refs.update(o["id"] for member in json.loads(packet["content"]).get("inventory", {}).get("attempts", [])
                                for o in member["observations"])
            allowed_refs.update(r[0] for r in conn.execute(
                "SELECT reference_id FROM turn_references WHERE turn_id=?", (handoff.turn_id,)))
            if {ref for e in handoff.evidence for ref in e.references} - allowed_refs:
                raise ValueError("Handoff cites evidence not delivered or retrieved by this turn")
            if handoff.assessment is not None:
                from labgoblin.reporting import read_all
                assessment = handoff.assessment
                if turn["kind"] != "final_analysis" or json.loads(packet["content"]).get("view_id") != assessment["view_id"]:
                    raise ValueError("Assessment must name the final turn's exact source view")
                view = conn.execute("SELECT metadata FROM views WHERE id=?", (assessment["view_id"],)).fetchone()
                if (not view or json.loads(view[0])["inventory_digest"] != assessment["inventory_digest"]
                        or not read_all(conn, handoff.turn_id, assessment["view_id"])):
                    raise ValueError("Assessment has not received every page of its exact immutable inventory")
                for item in assessment.get("exclusions", []):
                    if not conn.execute("SELECT 1 FROM view_members WHERE view_id=? AND attempt_id=?",
                                        (assessment["view_id"], item["attempt_id"])).fetchone():
                        raise ValueError("Assessment exclusion is outside its sealed inventory")
            now = time.time()
            for evidence in handoff.evidence:
                conn.execute("""INSERT INTO dispositions(turn_id,event_id,disposition,reason,wake_condition,reference_ids)
                    VALUES(?,?,?,?,?,?)""",
                             (handoff.turn_id, evidence.event_id, evidence.disposition, evidence.reason,
                              evidence.wake_condition, _json(evidence.references)))
                if evidence.disposition != "deferred":
                    conn.execute("UPDATE events SET acknowledged_by=? WHERE id=? AND acknowledged_by IS NULL",
                                 (handoff.turn_id, evidence.event_id))
            source_id = _source(conn, "handoff", encoded.encode(), "researcher",
                                {"turn_id": handoff.turn_id, "generation": turn["generation"]},
                                f"checkpoint:{turn['generation']}")
            conn.execute("""INSERT INTO source_heads(name,source_id,revision) VALUES('rationale',?,1)
                ON CONFLICT(name) DO UPDATE SET source_id=excluded.source_id,revision=revision+1""", (source_id,))
            conn.execute("INSERT INTO handoffs(turn_id,content,digest,source_id,created) VALUES(?,?,?,?,?)",
                         (handoff.turn_id, encoded, fingerprint(asdict(handoff)), source_id, now))
            conn.execute("UPDATE turns SET state='accepted',ended=?,result=? WHERE id=?",
                         (now, encoded, handoff.turn_id))
            campaign = _campaign(conn)
            if (campaign["generation"] == turn["generation"]
                    and campaign["operator_mode"] not in ("stopping", "stopped")
                    and turn["kind"] == "research" and conn.execute(
                        "SELECT state FROM generations WHERE id=?", (turn["generation"],)).fetchone()[0] == "open"):
                progress = {"continue": "research", "wait": "wait", "blocked": "blocked",
                            "finalize": "finalize"}[handoff.disposition]
                reason = handoff.reason
                if (progress == "finalize" and json.loads(packet["content"]).get("authority_revision", 0)
                        != campaign["authority_revision"]):
                    progress = "research"
                    reason = "Governing goal/protocol/constraints changed after this turn; reassessment is required"
                conn.execute("UPDATE campaign SET progress=?,reason=?,failures=0,wake_after=? WHERE id=?",
                             (progress, reason, packet["watermark"], self.id))
                if progress == "finalize":
                    _seal(conn, campaign["generation"], handoff.reason)
                elif progress == "research":
                    _event(conn, campaign["generation"], "continue",
                           {"turn_id": handoff.turn_id, "reason": handoff.reason},
                           f"continue-{handoff.turn_id}")
            if (turn["kind"] == "research" and campaign["generation"] == turn["generation"]
                    and campaign["operator_mode"] not in ("stopping", "stopped")):
                for kind in handoff.maintenance:
                    self._maintenance(conn, kind, "researcher")

    @staticmethod
    def _owned_maintenance(conn, value, invocation_id, kind):
        turn = conn.execute("SELECT * FROM turns WHERE id=? AND kind=?", (value["turn_id"], kind)).fetchone()
        if not turn or turn["packet_id"] != value["packet_id"]:
            raise ValueError("Maintenance result does not belong to its turn and packet")
        if turn["state"] == "accepted" and turn["result"] == _json(value):
            return turn, None, None
        if turn["state"] not in ("prepared", "running"):
            raise ValueError("Maintenance turn is not eligible for acceptance")
        owned = conn.execute("""SELECT l.receipt FROM invocations i JOIN launches l ON l.nonce=i.nonce
            WHERE i.id=? AND i.turn_id=? AND i.kind=? AND i.state='completed' AND l.phase='quiescent'""",
                             (invocation_id, turn["id"], kind)).fetchone()
        if (not owned or json.loads(owned[0]).get("metadata", {}).get("result_capture", {}).get("result_digest")
                != fingerprint(value)):
            raise ValueError("Maintenance requires its exact successfully owned provider result")
        row = conn.execute("SELECT content,digest,ready FROM packets WHERE id=?", (turn["packet_id"],)).fetchone()
        if not row["ready"] or hashlib.sha256(row["content"].encode()).hexdigest() != row["digest"]:
            raise ValueError("Committed maintenance packet integrity failed")
        request = conn.execute("SELECT * FROM maintenance WHERE turn_id=?", (turn["id"],)).fetchone()
        if not request or request["state"] != "running":
            raise ValueError("Result has no owned maintenance request")
        return turn, json.loads(row["content"]), request

    def accept_compaction(self, value: dict, *, invocation_id: str):
        table(value, "compaction result", {"turn_id", "packet_id", "summary", "source_ids"})
        for key in ("turn_id", "packet_id", "summary"):
            text(value.get(key), key)
        with self.db.write() as conn:
            turn, packet, request = self._owned_maintenance(conn, value, invocation_id, "compact")
            if turn["state"] == "accepted":
                return
            scope = packet["compaction"]
            if value.get("source_ids") != scope["source_ids"]:
                raise ValueError("Summary must identify exactly its committed source scope")
            body = value["summary"].encode("utf-8")
            if len(canonical(value)) > 16384 or len(body) >= scope["input_bytes"]:
                raise ValueError("Derived summary is oversized or did not shrink its committed input")
            _source(conn, "summary", body, "owned-compactor",
                    {**scope, "sources": [{k: v for k, v in item.items() if k != "text"} for item in scope["sources"]],
                     "turn_id": turn["id"], "source_revision": request["source_revision"],
                     "authority": "Derived preview summary, not verified semantic equivalence or research authority"},
                    "summary")
            conn.execute("UPDATE turns SET state='accepted',ended=?,result=? WHERE id=?",
                         (time.time(), _json(value), turn["id"]))
            conn.execute("UPDATE maintenance SET state='completed' WHERE id=?", (request["id"],))

    def resolve_blocker(self, blocker_id: str):
        with self.db.write() as conn:
            conn.execute("UPDATE blockers SET resolved=? WHERE id=? AND resolved IS NULL",
                         (time.time(), blocker_id))

    def controller(self, handle: dict | None, expected: str | None):
        with self.db.write() as conn:
            if conn.execute("SELECT controller FROM campaign WHERE id=?", (self.id,)).fetchone()[0] != expected:
                raise ValueError("Controller ownership changed concurrently")
            conn.execute("UPDATE campaign SET controller=? WHERE id=?", (_json(handle) if handle else None, self.id))
            conn.execute("INSERT INTO meta(key,value) VALUES('controller_epoch',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (identifier(),))

    def cancel_attempt(self, attempt_id: str, reason="Operator cancellation") -> dict:
        with self.db.write() as conn:
            row = conn.execute("SELECT status,nonce FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown attempt")
            if row["status"] == "queued":
                self._unlaunched_attempt(conn, attempt_id, "cancelled", reason)
            elif row["status"] not in TERMINAL:
                conn.execute("UPDATE attempts SET cancel_requested=COALESCE(cancel_requested,?),reason=? WHERE id=?",
                             (time.time(), reason, attempt_id))
            return dict(conn.execute("SELECT id,status,cancel_requested,nonce,reason FROM attempts WHERE id=?",
                                     (attempt_id,)).fetchone())

    @staticmethod
    def _unlaunched_attempt(conn, attempt_id, status, reason):
        row = conn.execute("SELECT generation,status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not row or row["status"] != "queued":
            raise ValueError("Only unlaunched queued work may receive a pre-execution outcome")
        conn.execute("UPDATE attempts SET status=?,ended=?,reason=?,validation='not_performed' WHERE id=?",
                     (status, time.time(), reason, attempt_id))
        conn.execute("""UPDATE allocations SET state='release_pending' WHERE work_id=?
            AND state IN ('requested','granted')""", (attempt_id,))
        _event(conn, row["generation"], "execution", {"attempt_id": attempt_id, "status": status, "reason": reason},
               f"execution-{attempt_id}")

    def fail_unlaunched_attempt(self, attempt_id: str, reason: str):
        with self.db.write() as conn:
            self._unlaunched_attempt(conn, attempt_id, "not_started", reason)

    def finish_turn(self, turn_id: str, reason: str, *, cancelled=False) -> bool:
        text(reason, "turn failure reason")
        with self.db.write() as conn:
            turn = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if not turn:
                raise ValueError("Unknown turn")
            if turn["state"] not in ("prepared", "running"):
                return False
            if conn.execute("""SELECT 1 FROM invocations i JOIN launches l ON l.nonce=i.nonce
                WHERE i.turn_id=? AND l.phase IN ('armed','executing')""", (turn_id,)).fetchone():
                raise ValueError("Cannot retire a turn with armed or unresolved execution")
            now = time.time()
            conn.execute("UPDATE invocations SET state='cancelled',ended=?,reason=? WHERE turn_id=? AND state='reserved'",
                         (now, reason, turn_id))
            conn.execute("UPDATE turns SET state=?,ended=?,reason=? WHERE id=?",
                         ("cancelled" if cancelled else "failed", now, reason, turn_id))
            conn.execute("UPDATE allocations SET state='release_pending' WHERE work_id=? AND state!='released'",
                         (turn_id,))
            conn.execute("UPDATE maintenance SET state=?,reason=? WHERE turn_id=? AND state='running'",
                         ("cancelled" if cancelled else "failed", reason, turn_id))
            if not cancelled:
                campaign = _campaign(conn)
                event_kind = "maintenance" if turn["kind"] in ("compact", "report") else "protocol_failure"
                event_id = _event(conn, turn["generation"], event_kind, {"turn_id": turn_id, "reason": reason})
                if turn["generation"] == campaign["generation"] and turn["kind"] == "research":
                    failures = campaign["failures"] + 1
                    retry = failures <= _settings(conn, campaign)["agent"]["retries"]
                    sequence = conn.execute("SELECT seq FROM events WHERE id=?", (event_id,)).fetchone()[0]
                    if campaign["operator_mode"] not in ("stopping", "stopped"):
                        conn.execute("""UPDATE campaign SET failures=?,progress=?,reason=?,wake_after=? WHERE id=?""",
                                     (failures, "research" if retry else "blocked", reason,
                                      sequence - 1 if retry else sequence, self.id))
            return True

    def close_admission(self, reason: str):
        text(reason, "closure reason")
        with self.db.write() as conn:
            campaign = _campaign(conn)
            if campaign["progress"] == "closed":
                return
            _seal(conn, campaign["generation"], reason)
            conn.execute("UPDATE campaign SET progress='finalize',reason=? WHERE id=?", (reason, self.id))
            conn.execute("""UPDATE allocations SET state='release_pending' WHERE state IN ('requested','granted')
                AND kind!='final_analysis'""")

    def close_generation(self, view_id: str, outcome: str, reason: str, *, assessed_turn=None):
        if outcome not in ("assessed", "incomplete", "unassessed", "needs_more_work"):
            raise ValueError("Invalid research closure outcome")
        with self.db.write() as conn:
            campaign = _campaign(conn)
            generation = conn.execute("SELECT * FROM generations WHERE id=?", (campaign["generation"],)).fetchone()
            if generation["state"] == "open" or generation["view_id"] != view_id:
                raise ValueError("Closure does not belong to this sealed generation source view")
            view = conn.execute("SELECT metadata FROM views WHERE id=?", (view_id,)).fetchone()
            metadata = json.loads(view[0])
            quiescent = self._quiescent(conn) and metadata["operationally_ready"]
            if outcome == "assessed":
                from labgoblin.reporting import valid_assessed_turn
                if not quiescent or not valid_assessed_turn(conn, view_id, assessed_turn):
                    raise ValueError("Assessed closure requires an owned complete assessment and operational quiescence")
            assessment = {"view_id": view_id, "inventory_digest": metadata["inventory_digest"], "outcome": outcome,
                          "reason": text(reason, "closure reason"), "assessed_turn": assessed_turn,
                          "attempts": metadata["attempts"], "operational_quiescent": quiescent}
            conn.execute("UPDATE generations SET outcome=?,reason=?,assessment=?,state=? WHERE id=?",
                         (outcome, reason, _json(assessment), "closed" if quiescent else "sealed", campaign["generation"]))
            if quiescent:
                conn.execute("UPDATE campaign SET progress='closed',reason=? WHERE id=?", (reason, self.id))
            return assessment
    def reasoning_due(self) -> bool:
        with self.db.read() as conn:
            campaign = _campaign(conn)
            if campaign["operator_mode"] not in ("ready", "running") or campaign["progress"] in ("closed", "finalize"):
                return False
            if conn.execute("SELECT 1 FROM blockers WHERE resolved IS NULL").fetchone():
                return False
            return bool(conn.execute("""SELECT 1 FROM events WHERE generation=? AND acknowledged_by IS NULL
                AND kind!='maintenance' AND seq>? LIMIT 1""", (campaign["generation"], campaign["wake_after"])).fetchone())

    def budget(self, now: float | None = None, *, connection=None) -> dict:
        from contextlib import nullcontext
        with self.db.read() if connection is None else nullcontext(connection) as conn:
            campaign = _campaign(conn)
            settings = _settings(conn, campaign)["campaign"]
            reserved = conn.execute("SELECT COUNT(*) FROM invocations WHERE state='reserved'").fetchone()[0]
            active = conn.execute("""SELECT COUNT(*),COALESCE(SUM(seconds*json_array_length(gpus)/3600),0)
                FROM attempts WHERE status IN ('starting','running','recovery_required')""").fetchone()
            requests = [json.loads(row[0])["resources"] for row in conn.execute(
                "SELECT request FROM allocations WHERE state IN ('granted','attached','release_pending')")]
            elapsed = campaign["elapsed"]
            if campaign["started"] is not None:
                elapsed += max(0, (time.time() if now is None else now) - campaign["observed_wall"])
            return {
                "configuration": {"revision": campaign["config_revision"],
                                  "scope": "Loaded controller snapshot; TOML edits require controller restart"},
                "elapsed_admission_seconds": Limit(settings["max_seconds"]["value"]).view(elapsed),
                "managed_invocations": Limit(settings["max_invocations"]["value"]).view(campaign["invocations"], reserved),
                "gpu_hours": {"used": campaign["gpu_hours"], "reserved": active[1], "configured": settings["max_gpu_hours"]},
                "active_experiments": active[0], "maximum_experiments": settings["max_jobs"],
                "resources": {"configured": settings["resources"],
                              "committed_cpus": sum(row["cpus"] for row in requests),
                              "committed_memory_mb": sum(row["memory_mb"] for row in requests)},
                "usage_semantics": "Invocation commitments include armed/uncertain launches, not tokens or API calls",
            }

    def converge_stop(self):
        with self.db.write() as conn:
            campaign = _campaign(conn)
            generation = conn.execute("SELECT state FROM generations WHERE id=?",
                                      (campaign["generation"],)).fetchone()[0]
            if campaign["operator_mode"] != "stopping" and generation != "sealed":
                return
            queued = list(conn.execute("SELECT id FROM attempts WHERE generation=? AND status='queued'",
                                       (campaign["generation"],)))
            for row in queued:
                attempt_id = row["id"]
                conn.execute("""UPDATE allocations SET state='release_pending' WHERE work_id=?
                    AND state IN ('requested','granted')""", (attempt_id,))
                conn.execute("""UPDATE attempts SET status='cancelled',validation='not_performed',
                    ended=?,reason='Admission closed before execution' WHERE id=? AND status='queued'""",
                             (time.time(), attempt_id))
                _event(conn, campaign["generation"], "execution",
                       {"attempt_id": attempt_id, "status": "cancelled"},
                       f"execution-{attempt_id}")
            if campaign["operator_mode"] == "stopping" and self._quiescent(conn):
                conn.execute("UPDATE campaign SET operator_mode='stopped',progress='closed' WHERE id=?", (self.id,))
                conn.execute("""UPDATE generations SET state='closed',outcome='incomplete',
                    reason='Stopped without final model analysis' WHERE id=?""", (campaign["generation"],))
