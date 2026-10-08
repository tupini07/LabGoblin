"""Researcher-authored communication, separate from scientific authority."""

import hashlib
import json
from pathlib import Path

from labgoblin.evidence import Capture, contained, parse_json
from labgoblin.protocol import canonical, fingerprint, integer, strings, table, text
from labgoblin.state import _campaign, _source


MAX_BYTES = 8192
FIELDS = ("bottom_line", "changes", "uncertainty", "next_step", "decision_needed")
KINDS = ("research", "reflection", "final_analysis")


def parse(value):
    value = dict(table(value, "supervisor memo", {*FIELDS, "turn_id", "packet_id", "references"}))
    for key in (*FIELDS, "turn_id", "packet_id"):
        text(value.get(key), key)
    value["references"] = list(strings(value.get("references"), "memo references"))
    if len(value["references"]) > 12 or any(len(ref) > 128 for ref in value["references"]):
        raise ValueError("A supervisor memo permits at most 12 exact references of at most 128 characters")
    if len(canonical(value)) > MAX_BYTES or sum(len(value[key].split()) for key in FIELDS) > 300:
        raise ValueError("Keep supervisor memos within 300 words and 8192 UTF-8 bytes")
    return value


def capture(envelope):
    """Freeze the optional sidecar into the receipt, independently of the handoff."""
    try:
        path = contained(Path(envelope.root), envelope.metadata["supervisor_memo_path"])
        captured = Capture.read(path, MAX_BYTES)
        value = parse(parse_json(captured.body))
        if (value["turn_id"] != envelope.metadata["turn_id"]
                or value["packet_id"] != envelope.metadata["packet_id"]):
            raise ValueError("Supervisor memo does not belong to its exact turn and packet")
        return {"status": "captured", "content": value, "digest": captured.digest,
                "bytes": len(captured.body), "content_digest": fingerprint(value)}
    except FileNotFoundError:
        return {"status": "missing", "error": "No supervisor memo was supplied with this result"}
    except (OSError, ValueError) as error:
        return {"status": "invalid", "error": f"{type(error).__name__}: {str(error)[:1000]}"}


def request_in(conn, reason, *, key, reference=None, turn_id=None):
    reason = text(reason, "memo request reason")
    if len(reason.encode("utf-8")) > 2000:
        raise ValueError("Memo request reason exceeds 2000 UTF-8 bytes")
    generation = _campaign(conn)["generation"]
    head = f"supervisor_memo_request:{generation}:{fingerprint(key)}"
    old = conn.execute("SELECT source_id FROM source_heads WHERE name=?", (head,)).fetchone()
    if old:
        if _read(conn, old[0])["content"] != {"reason": reason, "reference": reference}:
            raise ValueError("Memo request ID already has different content")
        return old[0]
    return _source(conn, "supervisor_memo_request", canonical({"reason": reason, "reference": reference}),
                   "harness", {"generation": generation, "turn_id": turn_id}, head)


def request(state, reason, *, request_id):
    text(request_id, "memo request ID")
    if len(request_id) > 128:
        raise ValueError("Memo request ID exceeds 128 characters")
    with state.db.write() as conn:
        source = request_in(conn, reason, key=["operator", request_id])
    return {"request_id": source, "pending": True,
            "note": "Requested for the next ordinary root turn; no invocation, resume or approval was issued"}


def record_changed_in(conn, old, content, source_id, owner):
    if owner["scope_id"] != "root":
        return
    previous = json.loads(conn.execute("SELECT body FROM sources WHERE id=?", (old["head"],)).fetchone()[0]) if old else {}
    assessed = content["outcome"] != "unassessed" and any(
        content[key] != previous.get(key) for key in ("body", "outcome", "links"))
    retired = content["kind"] == "hypothesis" and content["activity"] == "retired" and previous.get("activity") != "retired"
    strategy = content["kind"] == "reflection" and content["body"] != previous.get("body")
    if assessed or retired or strategy:
        label = "Assessment" if assessed else "Hypothesis retirement (not necessarily refutation)" if retired else "Strategy/reflection"
        request_in(conn, f"{label}: {content['title']}", key=source_id,
                   reference=source_id, turn_id=content["author_turn"])


def _read(conn, source_id):
    row = conn.execute("SELECT id,seq,body,digest,metadata,created FROM sources WHERE id=?", (source_id,)).fetchone()
    if not row or hashlib.sha256(bytes(row["body"])).hexdigest() != row["digest"]:
        raise ValueError("Supervisor memo source integrity failed")
    return {"id": row["id"], "seq": row["seq"], "created": row["created"],
            "content": json.loads(row["body"]), "metadata": json.loads(row["metadata"])}


def _head(conn, name):
    row = conn.execute("SELECT source_id FROM source_heads WHERE name=?", (name,)).fetchone()
    return _read(conn, row[0]) if row else None


def _pending(generation, covered):
    return ("""FROM sources r WHERE r.kind='supervisor_memo_request'
        AND json_extract(r.metadata,'$.generation')=? AND r.seq>?
        AND NOT EXISTS (SELECT 1 FROM sources m WHERE m.kind='supervisor_memo'
            AND json_extract(m.metadata,'$.generation')=?
            AND json_extract(m.metadata,'$.turn_id')=json_extract(r.metadata,'$.turn_id'))""",
            (generation, covered, generation))


def status(conn, *, offset=0, limit=8):
    integer(offset, "memo request offset", zero=True)
    integer(limit, "memo request page size")
    if limit > 20:
        raise ValueError("Memo request pages cannot exceed 20 entries")
    current = _campaign(conn)
    latest = _head(conn, "supervisor_memo")
    same_generation = latest and latest["metadata"]["generation"] == current["generation"]
    covered = latest["metadata"]["request_cutoff"] if same_generation else 0
    where, args = _pending(current["generation"], covered)
    count = conn.execute("SELECT COUNT(*) " + where, args).fetchone()[0]
    rows = conn.execute("SELECT r.id " + where + " ORDER BY r.seq LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    cutoff = conn.execute("""SELECT COALESCE(MAX(seq),0) FROM sources
        WHERE kind='supervisor_memo_request' AND json_extract(metadata,'$.generation')=?""",
                          (current["generation"],)).fetchone()[0]
    last_result = _head(conn, "supervisor_memo_status")
    if last_result and last_result["metadata"]["generation"] != current["generation"]:
        last_result = None
    changed = bool(same_generation and latest["metadata"]["authority_revision"] != current["authority_revision"])
    failed = last_result and last_result["content"]["status"] != "published"
    return {"latest": latest, "pending": bool(count or not same_generation or changed or failed), "pending_count": count,
            "first_memo_due": not bool(same_generation), "request_cutoff": cutoff,
            "requests": [_read(conn, row["id"]) for row in rows], "offset": offset,
            "has_more": offset + len(rows) < count, "next_offset": offset + len(rows),
            "last_result": last_result,
            "authority_changed": changed,
            "coverage": "Coalesced communication requests, not a complete inventory or live operational health"}


def packet_context(conn, path):
    value = status(conn)
    latest = value["latest"]
    return {
        "due": value["pending"], "request_cutoff": value["request_cutoff"], "path": str(path),
        "pending_count": value["pending_count"], "first_memo_due": value["first_memo_due"],
        "requests": [{"id": row["id"], "reason": row["content"]["reason"][:240],
                      "truncated": len(row["content"]["reason"]) > 240,
                      "reference": row["content"]["reference"]} for row in value["requests"]],
        "has_more": value["has_more"],
        "previous": ({"source_id": latest["id"], "generation": latest["metadata"]["generation"],
                      "bottom_line_preview": latest["content"]["bottom_line"][:500]} if latest else None),
        "last_error": (value["last_result"]["content"].get("error") if value["last_result"] else None),
        "format": {"turn_id": "this turn ID", "packet_id": "this packet ID",
                   **{key: "short plain text" for key in FIELDS},
                   "references": ["exact delivered/retrieved evidence ID"]},
        "maximum_bytes": MAX_BYTES,
    }


def accept_in(conn, turn, packet, receipt, allowed_refs, *, stale):
    if turn["scope_id"] != "root" or turn["kind"] not in KINDS or "supervisor_memo" not in packet:
        return
    captured = receipt.get("metadata", {}).get("supervisor_memo_capture", {"status": "missing"})
    captured = captured if isinstance(captured, dict) else {"status": "invalid", "error": "Memo capture must be an object"}
    pending = status(conn)["pending"]
    if captured.get("status") == "missing" and not pending:
        return
    result = {"status": "missing", "error": "Supervisor update pending: no memo in the owned result"}
    metadata = {"turn_id": turn["id"], "generation": turn["generation"]}
    try:
        if captured.get("status") != "captured":
            raise ValueError(captured.get("error") or result["error"])
        if stale:
            raise ValueError("Memo is stale: generation or governing authority changed during its turn")
        value = parse(captured.get("content"))
        if (value["turn_id"] != turn["id"] or value["packet_id"] != turn["packet_id"]
                or fingerprint(value) != captured.get("content_digest")):
            raise ValueError("Memo differs from its exact owned capture")
        if set(value["references"]) - allowed_refs:
            raise ValueError("Memo cites evidence not delivered or retrieved by its root turn")
        if conn.execute("SELECT 1 FROM sources WHERE kind LIKE 'supervisor_memo%' AND id IN ("
                        + ",".join("?" for _ in value["references"]) + ")", value["references"]).fetchone():
            raise ValueError("Memo references must cite underlying evidence, not other memos")
        digest = text(captured.get("digest"), "memo capture digest")
        byte_count = integer(captured.get("bytes"), "memo capture bytes")
        if len(digest) != 64 or byte_count > MAX_BYTES:
            raise ValueError("Invalid memo capture identity or size")
        previous = status(conn)["latest"]
        same_generation = previous and previous["metadata"]["generation"] == turn["generation"]
        covered = previous["metadata"]["request_cutoff"] if same_generation else 0
        cutoff = packet["supervisor_memo"]["request_cutoff"]
        where, args = _pending(turn["generation"], covered)
        requests = conn.execute("SELECT r.id,r.seq " + where + """
            AND r.seq<=? AND COALESCE(json_extract(r.metadata,'$.turn_id'),'')!=? ORDER BY r.seq""",
                               (*args, cutoff, turn["id"]))
        unread = next((row["seq"] for row in requests if row["id"] not in allowed_refs), None)
        if unread is not None:
            cutoff = unread - 1
        metadata.update(packet_id=turn["packet_id"], authority_revision=packet["authority_revision"],
                        request_cutoff=cutoff, event_cutoff=packet["watermark"], capture_digest=digest,
                        capture_bytes=byte_count, authority="Derived researcher communication, not scientific evidence or operator approval")
        source = _source(conn, "supervisor_memo", canonical(value), "owned-root-researcher", metadata, "supervisor_memo")
        result = {"status": "published", "source_id": source}
    except ValueError as error:
        result = {"status": "missing" if captured.get("status") == "missing" else "invalid", "error": str(error)[:1200]}
    _source(conn, "supervisor_memo_status", canonical(result), "harness", metadata, "supervisor_memo_status")


def get(conn, source_id):
    if not conn.execute("SELECT 1 FROM sources WHERE id=? AND kind='supervisor_memo'", (source_id,)).fetchone():
        raise ValueError("Unknown exact supervisor memo")
    return _read(conn, source_id)


def history(conn, *, offset=0, limit=20):
    integer(offset, "memo history offset", zero=True)
    integer(limit, "memo history limit")
    if limit > 50:
        raise ValueError("Memo history pages cannot exceed 50 entries")
    count = conn.execute("SELECT COUNT(*) FROM sources WHERE kind='supervisor_memo'").fetchone()[0]
    rows = conn.execute("""SELECT id,created,metadata,
        json_extract(CAST(body AS TEXT),'$.bottom_line') bottom_line FROM sources
        WHERE kind='supervisor_memo' ORDER BY seq DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
    return {"memos": [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows],
            "total": count, "offset": offset, "has_more": offset + len(rows) < count,
            "next_offset": offset + len(rows)}
