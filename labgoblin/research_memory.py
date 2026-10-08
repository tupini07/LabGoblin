"""Editable working notes, explicitly published immutable research revisions."""

import hashlib
import json
from pathlib import Path
import time

from labgoblin.evidence import Capture, contained, parse_json, publish_bytes
from labgoblin.protocol import canonical, fingerprint, identifier, integer, strings, table, text
from labgoblin.state import _campaign, _json, _source


KINDS = ("knowledge", "question", "hypothesis", "reflection")
ACTIVITIES = ("active", "parked", "retired")
OUTCOMES = ("unassessed", "supported", "refuted", "inconclusive", "blocked")
RELATIONS = ("context", "informs", "supports", "refutes", "supersedes", "derived_from")
MAX_BYTES = 32 * 1024


def actor(conn, turn_id=None, *, write=False):
    if turn_id is None:
        return {"scope_id": "root", "kind": "operator", "id": None}
    row = conn.execute("""SELECT t.* FROM turns t WHERE t.id=? AND t.state IN ('prepared','running')
        AND EXISTS(SELECT 1 FROM invocations i WHERE i.turn_id=t.id
                   AND i.kind=t.kind AND i.state IN ('armed','running'))""", (turn_id,)).fetchone()
    if row is None:
        raise ValueError("Research access requires an active owned turn")
    if write:
        current = _campaign(conn)
        packet = json.loads(conn.execute("SELECT content FROM packets WHERE id=?", (row["packet_id"],)).fetchone()[0])
        if (row["kind"] not in ("research", "reflection", "worker")
                or row["revision"] != current["revision"] or row["generation"] != current["generation"]
                or packet.get("config_revision") != current["config_revision"]
                or packet.get("authority_revision") != current["authority_revision"]
                or current["operator_mode"] not in ("ready", "running")):
            raise ValueError("This role or stale turn cannot publish research")
        if row["scope_id"] != "root":
            from labgoblin.research_tasks import assert_owned
            assert_owned(conn, row["scope_id"])
    return dict(row)


def reference_exists(conn, ref):
    return any(conn.execute(f"SELECT 1 FROM {name} WHERE id=?", (ref,)).fetchone()
               for name in ("sources", "observations", "attempts", "events", "research_tasks"))


def publish_in(conn, request, owner):
    request = dict(table(request, "research publication", {
        "id", "request_id", "base", "kind", "title", "body", "activity", "outcome", "links", "provenance"}))
    request_id = text(request.get("request_id"), "request_id")
    digest = fingerprint(request)
    replay = conn.execute("SELECT source_id,request_digest FROM research_revisions WHERE request_id=?",
                          (request_id,)).fetchone()
    if replay:
        if replay["request_digest"] != digest:
            raise ValueError("Publication request ID already has different content")
        return replay["source_id"]
    record_id = text(request.get("id"), "record id")
    if len(record_id) > 128:
        raise ValueError("Record ID exceeds 128 characters")
    old = conn.execute("SELECT * FROM research_records WHERE id=?", (record_id,)).fetchone()
    if request.get("base") != (old["head"] if old else None):
        raise ValueError("Research revision conflict; read the current head before publishing")
    if old and owner["scope_id"] != old["scope_id"] and owner["scope_id"] != "root":
        raise ValueError("Cannot edit another research scope")
    kind = request.get("kind", old["kind"] if old else None)
    if kind not in KINDS or (old and old["kind"] != kind and (old["kind"], kind) != ("question", "hypothesis")):
        raise ValueError("Invalid research kind or promotion")
    activity = request.get("activity", "active")
    outcome = request.get("outcome", "unassessed")
    if activity not in ACTIVITIES or outcome not in OUTCOMES:
        raise ValueError("Invalid activity or assessed outcome")
    title = text(request.get("title"), "title")
    body = text(request.get("body"), "body")
    if len(title) > 240 or len(canonical(request)) > MAX_BYTES:
        raise ValueError("Research publication exceeds its bounded size")
    provenance = table(request.get("provenance", {}), "provenance")
    links = request.get("links", [])
    if not isinstance(links, list) or len(links) > 100:
        raise ValueError("links must be an array of at most 100 references")
    for link in links:
        table(link, "research link", {"target", "relation"})
        if link.get("relation") not in RELATIONS or not reference_exists(conn, text(link.get("target"), "link target")):
            raise ValueError("Invalid research link or unknown exact reference")
    if outcome != "unassessed" and not links:
        raise ValueError("An assessment requires evidence references")
    if owner["kind"] not in ("accepted", "operator") and (
            outcome != "unassessed" or any(link["relation"] in ("supports", "refutes") for link in links)):
        raise ValueError("Scientific assessments must be published in the accepted owned handoff")
    version = old["version"] + 1 if old else 1
    content = {**request, "kind": kind, "activity": activity, "outcome": outcome, "links": links,
               "provenance": provenance, "version": version, "scope_id": old["scope_id"] if old else owner["scope_id"],
               "author_turn": owner["id"]}
    associations = []
    for link in links:
        attempt = conn.execute("""SELECT a.id FROM attempts a WHERE a.id=? OR a.id=(
            SELECT attempt_id FROM observations WHERE id=?)""", (link["target"], link["target"])).fetchone()
        if attempt:
            revisions = list(conn.execute("""SELECT ar.source_id,s.body FROM attempt_research_refs ar
                JOIN sources s ON s.id=ar.source_id WHERE ar.attempt_id=?""", (attempt["id"],)))
            pinned = [row["source_id"] for row in revisions]
            claims = [json.loads(row["body"]) for row in revisions]
            same_claim = kind == "hypothesis" and any(claim["kind"] == "hypothesis"
                and claim["id"] == record_id and claim["body"] == body for claim in claims)
            associations.append({"target": link["target"], "attempt_id": attempt["id"],
                                 "association": "interpretation_of_pinned_revision" if same_claim else "post_hoc",
                                 "pinned_research_refs": pinned})
    content["evidence_associations"] = associations
    source_id = _source(conn, "research_record", canonical(content), owner["kind"],
                        {"record_id": record_id, "version": version, "scope_id": content["scope_id"]})
    conn.execute("""INSERT INTO research_records(id,kind,scope_id,head,version,activity,title,updated)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,head=excluded.head,
        version=excluded.version,activity=excluded.activity,title=excluded.title,updated=excluded.updated""",
                 (record_id, kind, content["scope_id"], source_id, version, activity, title, time.time()))
    conn.execute("INSERT INTO research_revisions VALUES(?,?,?,?,?)",
                 (source_id, record_id, version, request_id, digest))
    conn.executemany("INSERT OR IGNORE INTO research_links VALUES(?,?,?)",
                     [(source_id, item["target"], item["relation"]) for item in links])
    if kind == "hypothesis":
        conn.execute("""INSERT INTO hypotheses(id,statement,label,status,conclusion,created,updated)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET statement=excluded.statement,
            label=excluded.label,status=excluded.status,conclusion=excluded.conclusion,updated=excluded.updated""",
                     (record_id, body, title, "proposed" if outcome == "unassessed" else outcome,
                      "" if outcome == "unassessed" else body, time.time(), time.time()))
    from labgoblin.supervisor_memos import record_changed_in
    record_changed_in(conn, old, content, source_id, owner)
    return source_id


def publish(state, request, *, turn_id=None):
    with state.db.write() as conn:
        owner = actor(conn, turn_id, write=True)
        source_id = publish_in(conn, request, owner)
        if turn_id:
            conn.execute("INSERT OR IGNORE INTO turn_references VALUES(?,?)", (turn_id, source_id))
    return read(state, source_id)


def publish_file(state, path, *, turn_id=None):
    path = contained(state.root.parent / "research", state.root.parent / path)
    with state.db.read() as conn:
        owner = actor(conn, turn_id, write=True)
        if owner["scope_id"] != "root":
            task = conn.execute("SELECT root FROM research_tasks WHERE id=?", (owner["scope_id"],)).fetchone()
            contained(Path(task["root"]) / "work", path)
    capture = Capture.read(path, MAX_BYTES)
    request = dict(table(parse_json(capture.body), "research draft"))
    request["provenance"] = {**request.get("provenance", {}), "draft_path": str(path), "draft_digest": capture.digest}
    return publish(state, request, turn_id=turn_id)


def read(state, reference):
    with state.db.read() as conn:
        row = conn.execute("""SELECT s.id,s.body,s.digest,s.metadata FROM sources s WHERE s.kind='research_record'
            AND (s.id=? OR s.id=(SELECT head FROM research_records WHERE id=?))""", (reference, reference)).fetchone()
        if not row:
            raise ValueError("Unknown research record or revision")
        body = bytes(row["body"])
        if hashlib.sha256(body).hexdigest() != row["digest"]:
            raise ValueError("Research source integrity failed")
        return {"source_id": row["id"], "digest": row["digest"], "content": json.loads(body)}


def index(state, *, query="", offset=0, limit=20, include_retired=True, cutoff=None):
    integer(offset, "offset", zero=True)
    integer(limit, "limit")
    text(query, "query", empty=True)
    if limit > 50 or len(query) > 256:
        raise ValueError("Narrow the research search")
    with state.db.read() as conn:
        cutoff = conn.execute("SELECT COALESCE(MAX(seq),0) FROM sources").fetchone()[0] if cutoff is None else integer(
            cutoff, "cutoff", zero=True)
        where = """FROM research_revisions v JOIN sources s ON s.id=v.source_id
            WHERE s.seq<=? AND NOT EXISTS(
                SELECT 1 FROM research_revisions n JOIN sources ns ON ns.id=n.source_id
                WHERE n.record_id=v.record_id AND ns.seq<=? AND n.version>v.version)
            AND (? OR CASE WHEN s.kind='research_record'
                THEN json_extract(CAST(s.body AS TEXT),'$.activity') END!='retired')
            AND instr(lower(CAST(s.body AS TEXT)),lower(?))>0"""
        args = (cutoff, cutoff, int(include_retired), query)
        count = conn.execute("SELECT COUNT(*) " + where, args).fetchone()[0]
        rows = conn.execute("""SELECT v.record_id,v.source_id,v.version,
            json_extract(CAST(s.body AS TEXT),'$.title') title,
            json_extract(CAST(s.body AS TEXT),'$.kind') kind,
            json_extract(CAST(s.body AS TEXT),'$.activity') activity """ + where +
                            " ORDER BY s.seq DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    return {"records": [dict(row) for row in rows], "total": count, "offset": offset, "cutoff": cutoff,
            "has_more": offset + len(rows) < count, "next_offset": offset + len(rows),
            "coverage": "All published record bodies at cutoff; drafts and raw artifacts are not searched"}


def history(state, record_id, *, offset=0, limit=20):
    integer(offset, "offset", zero=True)
    integer(limit, "limit")
    if limit > 50:
        raise ValueError("History pages cannot exceed 50 revisions")
    with state.db.read() as conn:
        if not conn.execute("SELECT 1 FROM research_records WHERE id=?", (record_id,)).fetchone():
            raise ValueError("Unknown research record")
        total = conn.execute("SELECT COUNT(*) FROM research_revisions WHERE record_id=?", (record_id,)).fetchone()[0]
        rows = [dict(row) for row in conn.execute("""SELECT r.source_id,r.version,s.digest,s.created
            FROM research_revisions r JOIN sources s ON s.id=r.source_id
            WHERE r.record_id=? ORDER BY version LIMIT ? OFFSET ?""", (record_id, limit, offset))]
    return {"record_id": record_id, "revisions": rows, "total": total, "offset": offset,
            "next_offset": offset + len(rows), "has_more": offset + len(rows) < total,
            "coverage": "Immutable version order; new revisions append after existing pages"}


def validate_refs(conn, refs, purpose):
    refs = strings(refs, "research_refs", empty=True)
    if purpose not in ("evaluation", "exploration", "support"):
        raise ValueError("Research purpose must be evaluation, exploration or support")
    hypotheses = 0
    for ref in refs:
        row = conn.execute("""SELECT s.body FROM research_revisions r JOIN sources s ON s.id=r.source_id
            WHERE r.source_id=?""", (ref,)).fetchone()
        if not row:
            raise ValueError("research_refs must name exact published revisions")
        hypotheses += json.loads(bytes(row["body"]))["kind"] == "hypothesis"
    if purpose == "evaluation" and not hypotheses:
        raise ValueError("Evaluation requires at least one published hypothesis revision")
    return refs


def export(state, reference, destination, *, turn_id=None):
    value = read(state, reference)
    target = contained(state.root.parent / "research", state.root.parent / destination)
    with state.db.read() as conn:
        owner = actor(conn, turn_id, write=True)
        if owner["scope_id"] != "root":
            task = conn.execute("SELECT root FROM research_tasks WHERE id=?", (owner["scope_id"],)).fetchone()
            contained(Path(task["root"]) / "work", target)
    publish_bytes(target, canonical(value))
    return {"path": str(target), "source_id": value["source_id"], "authority": "Read-only projection; edit a new draft"}
