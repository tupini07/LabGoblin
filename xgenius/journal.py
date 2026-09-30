"""Bounded journal projections over retained sources, never a handoff authority."""

import hashlib
import time

from xgenius.evidence import Capture, SizeLimitError, contained, parse_json
from xgenius.protocol import HANDOFF_BYTES, Handoff, integer, text


COMPACTION_THRESHOLD = 32 * 1024


def automatic_compaction(state) -> dict | None:
    with state.db.read() as conn:
        campaign = conn.execute("SELECT archive_bytes,operator_mode,generation FROM campaign").fetchone()
        if (campaign["operator_mode"] not in ("ready", "running")
                or conn.execute("SELECT state FROM generations WHERE id=?", (campaign["generation"],)).fetchone()[0] != "open"):
            return None
        row = conn.execute("""SELECT s.metadata FROM source_heads h JOIN sources s ON s.id=h.source_id
            WHERE h.name='summary'""").fetchone()
        prior = parse_json(row[0].encode()) if row else {}
        if campaign["archive_bytes"] - prior.get("archive_index_bytes", 0) < COMPACTION_THRESHOLD:
            return None
    return state.request_maintenance("compact", origin="automatic")


def render_handoff(value: Handoff) -> str:
    lines = [f"## {value.summary}", "", "### Governing rationale", value.rationale,
             "", "### Next step", value.next_step, "", f"**{value.disposition}:** {value.reason}"]
    if value.stopping_criterion:
        lines.extend(["", "### Stopping criterion", value.stopping_criterion])
    if value.evidence:
        lines.extend(["", "### Evidence dispositions"])
        for item in value.evidence:
            lines.extend(["", f"- `{item.event_id}` **{item.disposition}**: {item.reason}"])
            if item.references:
                lines.append("  References: " + ", ".join(f"`{ref}`" for ref in item.references))
            if item.wake_condition:
                lines.append(f"  Wake condition: {item.wake_condition}")
    return "\n".join(lines) + "\n"


def entry(db, source_id: str, *, limit=1024 * 1024) -> dict:
    integer(limit, "source byte limit")
    if limit > 1024 * 1024:
        raise ValueError("Source retrieval cannot exceed 1 MiB")
    with db.read() as conn:
        row = conn.execute("""SELECT id,seq,kind,origin,digest,metadata,created,length(body) AS bytes
            FROM sources WHERE id=?""", (source_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown retained source revision")
        if row["bytes"] > limit:
            raise SizeLimitError("Source exceeds the explicit retrieval byte limit")
        body = bytes(conn.execute("SELECT body FROM sources WHERE id=?", (source_id,)).fetchone()[0])
    if hashlib.sha256(body).hexdigest() != row["digest"]:
        raise ValueError("Retained source revision was modified")
    result = {**dict(row), "text": body.decode("utf-8"), "metadata": parse_json(row["metadata"].encode()),
              "retrieved_at": time.time(), "truncated": False}
    if row["kind"] == "handoff":
        content = parse_json(body)
        handoff = Handoff.parse(content)
        result.update(handoff=content, markdown=render_handoff(handoff))
    else:
        result["markdown"] = result["text"]
    return result


def entry_page(db, source_id: str, *, offset=0, limit=16384) -> dict:
    import base64
    integer(offset, "source byte offset", zero=True)
    integer(limit, "source page byte limit")
    if limit > 65536:
        raise ValueError("Source byte pages cannot exceed 64 KiB")
    with db.read() as conn:
        row = conn.execute("""SELECT id,seq,kind,origin,digest,created,length(body) AS bytes,
            substr(body,?,?) AS body FROM sources WHERE id=?""", (offset + 1, limit, source_id)).fetchone()
    if row is None:
        raise ValueError("Unknown retained source revision")
    result = dict(row)
    body = bytes(result.pop("body"))
    if offset > result["bytes"]:
        raise ValueError("Source byte offset exceeds the retained revision")
    if offset == 0 and len(body) == result["bytes"] and hashlib.sha256(body).hexdigest() != result["digest"]:
        raise ValueError("Retained source revision was modified")
    return {**result, "offset": offset, "returned_bytes": len(body), "has_more": offset + len(body) < result["bytes"],
            "text": body.decode("utf-8", errors="replace"), "base64": base64.b64encode(body).decode("ascii"),
            "retrieved_at": time.time()}


def page(db, *, limit=20, offset=0, cutoff=None) -> dict:
    integer(limit, "journal page size")
    integer(offset, "journal offset", zero=True)
    if limit > 100:
        raise ValueError("Journal pages cannot exceed 100 entries")
    with db.read() as conn:
        cutoff = conn.execute("SELECT COALESCE(MAX(seq),0) FROM sources").fetchone()[0] if cutoff is None else integer(
            cutoff, "journal source cutoff", zero=True)
        where = "WHERE kind IN ('handoff','journal_import','summary','directive') AND seq<=?"
        total = conn.execute(f"SELECT COUNT(*) FROM sources {where}", (cutoff,)).fetchone()[0]
        rows = conn.execute(f"""SELECT id,seq,kind,origin,digest,created,length(body) AS bytes,
            substr(CAST(body AS TEXT),1,240) AS preview FROM sources {where}
            ORDER BY seq DESC LIMIT ? OFFSET ?""", (cutoff, limit, offset)).fetchall()
    return {"entries": [dict(row) for row in rows], "total": total, "offset": offset, "limit": limit,
            "cutoff": cutoff, "has_more": offset + len(rows) < total, "retrieved_at": time.time()}


def ingest_goal(state, config) -> str:
    path = contained(config.root, config.project.research_goal)
    with state.db.read() as conn:
        previous = conn.execute("""SELECT h.source_id,h.revision,s.digest,
            json_extract(s.metadata,'$.observed_file_digest') AS file_digest FROM source_heads h
            JOIN sources s ON s.id=h.source_id WHERE h.name='goal'""").fetchone()
    capture = Capture.read(path, 1024 * 1024)
    capture.body.decode("utf-8")
    if previous and capture.digest in (previous["digest"], previous["file_digest"]):
        return previous["source_id"]
    return state.source("goal", capture.body, origin="observed-file-edit", head="goal",
                        expected_revision=previous["revision"] if previous else 0, notify=True,
                        metadata={"path": str(path), "observed_at": time.time(),
                                  "attribution": "Observed bytes; original editor and edit time are not inferred"})


def ingest_notes(state) -> str | None:
    path = state.root / "journal.md"
    if not path.exists():
        return None
    with state.db.read() as conn:
        prior = conn.execute("""SELECT h.source_id,h.revision,s.digest FROM source_heads h
            JOIN sources s ON s.id=h.source_id WHERE h.name='journal-file'""").fetchone()
    capture = Capture.read(path, 1024 * 1024)
    capture.body.decode("utf-8")
    if prior and prior["digest"] == capture.digest:
        return prior["source_id"]
    return state.source("journal_import", capture.body, origin="observed-file-edit", head="journal-file",
                        expected_revision=prior["revision"] if prior else 0, notify=True,
                        metadata={"path": str(path), "observed_at": time.time(),
                                  "authority": "Manual notes; not an owned handoff or operator constraint"})


def search(db, query: str, *, after=0, cutoff=None, scan_limit=64, result_limit=20) -> dict:
    text(query, "archive search query")
    if len(query.encode("utf-8")) > 256:
        raise ValueError("Archive query cannot exceed 256 bytes")
    integer(after, "archive cursor", zero=True)
    integer(scan_limit, "archive scan limit")
    integer(result_limit, "archive result limit")
    if scan_limit > 64 or result_limit > 20:
        raise ValueError("Archive requests scan at most 64 sources and return at most 20 matches")
    with db.read() as conn:
        cutoff = conn.execute("SELECT COALESCE(MAX(seq),0) FROM sources").fetchone()[0] if cutoff is None else integer(
            cutoff, "archive source cutoff", zero=True)
        rows = conn.execute("""SELECT id,seq,kind,origin,digest,created,length(body) AS bytes,
            substr(body,1,?) AS preview FROM sources WHERE seq>? AND seq<=?
            AND kind IN ('goal','protocol','handoff','journal_import','summary','directive')
            ORDER BY seq LIMIT ?""", (HANDOFF_BYTES, after, cutoff, scan_limit + 1)).fetchall()
    matches, searched = [], []
    cursor = after
    for row in rows[:scan_limit]:
        if len(matches) == result_limit:
            break
        body = bytes(row["preview"])
        content = body.decode("utf-8", errors="replace")
        position = content.casefold().find(query.casefold())
        record = {key: row[key] for key in ("id", "seq", "kind", "origin", "digest", "created", "bytes")}
        searched.append({**record, "searched_bytes": len(body), "truncated": len(body) < row["bytes"]})
        cursor = row["seq"]
        if position >= 0:
            matches.append({**record, "snippet": content[max(0, position - 80):position + 240],
                            "truncated": len(body) < row["bytes"]})
    has_more = bool(rows and cursor < rows[-1]["seq"])
    return {"query": query, "matches": matches, "searched": searched, "after": after, "next_after": cursor,
            "cutoff": cutoff, "has_more": has_more, "retrieved_at": time.time(),
            "coverage": "Lexical matches only within listed source prefixes; zero matches do not prove absent evidence"}
