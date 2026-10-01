"""Exact, bounded research packets and revision-qualified event retrieval."""

import base64
import hashlib
import json
from pathlib import Path
import sys
import time

from labgoblin.evidence import SizeLimitError, contained, hash_file, parse_json, publish_bytes
from labgoblin.protocol import AdmissionWait, HANDOFF_BYTES, PACKET_BYTES, PACKET_EVENTS, Limit, canonical, identifier, integer
from labgoblin.state import _active_directives, _campaign, _queue_order, _settings, _source_revision


INSTRUCTIONS = """Conduct one autonomous research turn, not a human-agent collaboration.
Follow the versioned goal, evaluation protocol and active operator constraints. Preserve
the governing rationale; use archive retrieval for earlier negative findings and decisions.
Derived summaries are unverified hints, never authority over these sources or constraints.
Do useful support work or replication without inventing a hypothesis or discovery.

Submit heavy experiments with labgoblin submit (an argv-form JSON manifest), never as
unmanaged shell jobs. All experiments and inference share finite resources. Do not start
controllers, providers, subagents or workflows, install into shared environments, upload
data, push code, or expand the goal. Trusted execution is not filesystem isolation.
Do not wait synchronously for experiments while holding the reasoning grant.

Use labgoblin evidence and journal retrieval for exact revisions. Event and metric previews
may be incomplete: inspect their coverage and retrieve more before drawing conclusions.
Zero search matches are not proof of absent evidence. Never replace historical evidence
with a current mutable file. Process success is not artifact validity or scientific success.

Write one owned JSON handoff to the supplied result path. Do not independently overwrite
the journal: it is projected from accepted handoffs. Explain what changed, why, and the
governing next step. Acknowledge only exact delivered event IDs; subsets may leave holes.
An assessed/excluded disposition requires a reason. Deferred evidence additionally needs
a concrete wake_condition and remains unacknowledged. Cite only delivered/retrieved IDs.
Request fixed report/compact maintenance in the handoff, never a child provider.

Choose continue, wait, blocked, or finalize with a reason. A report or empty queue alone
does not complete research. Finalize names the goal's stopping criterion and outstanding
limitations. Final analysis must assess the sealed inventory; it cannot submit experiments
or reopen research. Be honest about failed, unperformed, invalid and unassessed attempts.
"""


def _source(conn, source_id: str, budget: int) -> dict:
    row = conn.execute("""SELECT id,kind,origin,digest,created,length(body) AS bytes
        FROM sources WHERE id=?""", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"Required source revision is unavailable: {source_id}")
    if row["bytes"] > budget:
        raise SizeLimitError(f"Mandatory {row['kind']} source exceeds packet allowance; shorten its active revision")
    body = bytes(conn.execute("SELECT body FROM sources WHERE id=?", (source_id,)).fetchone()[0])
    if hashlib.sha256(body).hexdigest() != row["digest"]:
        raise ValueError("Required source revision was modified")
    return {**dict(row), "text": body.decode("utf-8")}


def _context(conn, campaign, kind, view_id=None) -> tuple[dict, list[dict]]:
    heads = {r["name"]: r["source_id"] for r in conn.execute(
        "SELECT name,source_id FROM source_heads WHERE name IN ('goal','protocol','rationale','summary')")}
    directives = [dict(row) for row in _active_directives(conn, campaign["generation"])]
    for item in directives:
        item["currently_active"] = True
        item["sealed_scope"] = False
    if kind == "final_analysis" or kind == "report" and view_id:
        frozen = json.loads(conn.execute("SELECT source_ids FROM views WHERE id=?", (view_id,)).fetchone()[0]
                            if kind == "report" else conn.execute("SELECT source_heads FROM generations WHERE id=?",
                                                                  (campaign["generation"],)).fetchone()[0])
        heads = {key: value for key, value in frozen.items() if key in ("goal", "protocol", "rationale", "summary")}
        by_id = {row["id"]: row for row in directives}
        for key, value in frozen.items():
            if key.startswith("directive:"):
                row = conn.execute("SELECT id,source_id,origin,scope FROM directives WHERE id=?", (key[10:],)).fetchone()
                if row is None or row["source_id"] != value:
                    raise ValueError("Sealed directive revision is unavailable")
                if row["id"] in by_id:
                    by_id[row["id"]]["sealed_scope"] = True
                else:
                    by_id[row["id"]] = {**dict(row), "currently_active": False, "sealed_scope": True}
        directives = list(by_id.values())
    if "goal" not in heads:
        raise ValueError("Import a versioned research goal before preparing a provider packet")
    sources, constraints = {}, []
    remaining = PACKET_BYTES
    for name, source_id in heads.items():
        value = _source(conn, source_id, remaining)
        remaining -= len(canonical(value))
        sources[name] = value
    for row in directives:
        value = {**dict(row), "source": _source(conn, row["source_id"], remaining)}
        remaining -= len(canonical(value))
        constraints.append(value)
    return sources, constraints


def _event_header(conn, row) -> dict:
    value = dict(row)
    preview = bytes(value.pop("preview"))
    value.update(payload_preview=preview.decode("utf-8", errors="replace"),
                 truncated=value["payload_bytes"] > len(preview))
    observations = []
    for item in conn.execute("""SELECT o.id,o.kind,o.size,o.digest,o.assurance,
            substr(CAST(json_extract(o.metadata,'$.metrics') AS BLOB),1,512) AS metrics,
            length(CAST(json_extract(o.metadata,'$.metrics') AS BLOB)) AS metric_bytes
        FROM events e,json_each(e.payload,'$.observation_ids') j
        JOIN observations o ON o.id=j.value WHERE e.id=? LIMIT 8""", (row["id"],)):
        record = dict(item)
        metrics = record.pop("metrics")
        record["metrics_preview"] = bytes(metrics).decode("utf-8", errors="replace") if metrics else None
        record["metrics_truncated"] = (record["metric_bytes"] or 0) > 512
        observations.append(record)
    value["observations"] = observations
    value["observation_preview_limit"] = 8
    return value


def _content(state, conn, campaign, turn_id, packet_id, kind, view_id):
    settings = _settings(conn, campaign)
    sources, directives = _context(conn, campaign, kind, view_id)
    watermark = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
    pending = conn.execute("""SELECT COUNT(*) FROM events WHERE generation=? AND acknowledged_by IS NULL""",
                           (campaign["generation"],)).fetchone()[0]
    reserved = conn.execute("SELECT COUNT(*) FROM invocations WHERE state='reserved'").fetchone()[0]
    references = {source["id"] for source in sources.values()}
    references.update(item["source_id"] for item in directives)
    if view_id:
        references.add(view_id)
    packet = {
        "protocol": 3, "turn_id": turn_id, "packet_id": packet_id, "kind": kind,
        "campaign_id": state.id, "generation": campaign["generation"], "revision": campaign["revision"],
        "authority_revision": campaign["authority_revision"],
        "watermark": watermark, "recorded_at": time.time(), "sources": sources, "directives": directives,
        "instructions": INSTRUCTIONS, "cli_argv": [sys.executable, "-m", "labgoblin.cli"],
        "project": str(state.root.parent), "view_id": view_id,
        "budgets": {
            "elapsed_admission_seconds": Limit(settings["campaign"]["max_seconds"]["value"]).view(campaign["elapsed"]),
            "managed_invocations": Limit(settings["campaign"]["max_invocations"]["value"]).view(campaign["invocations"], reserved),
            "final_analysis_reserve": (2 if settings["agent"]["sandbox"] else 1)
            if settings["campaign"]["max_invocations"]["value"] and kind != "final_analysis" else 0,
            "resources": settings["campaign"]["resources"],
        },
        "result_protocol": {
            "maximum_bytes": HANDOFF_BYTES,
            "required": ["turn_id", "packet_id", "summary", "rationale", "next_step", "disposition", "reason"],
            "optional": ["evidence", "stopping_criterion", "maintenance"],
            "evidence_shape": {"event_id": "delivered ID", "disposition": "assessed|excluded|deferred",
                               "reason": "explanation", "references": ["revision IDs"], "wake_condition": "required if deferred"},
        },
        "events": [], "references": sorted(references),
        "coverage": {"pending_at_cutoff": pending, "delivered": 0, "has_more": pending > 0,
                     "maximum_events": PACKET_EVENTS, "maximum_bytes": PACKET_BYTES,
                     "ordering": "oldest evidence first, then oldest other pending events"},
    }
    if len(canonical(packet)) > PACKET_BYTES:
        raise SizeLimitError("Mandatory goal, rationale and constraints exceed the packet byte limit; none were clipped")
    if view_id:
        from labgoblin.reporting import _page
        packet["inventory"] = _page(conn, view_id, byte_limit=PACKET_BYTES - len(canonical(packet)) - 2048)
        packet["result_protocol"]["optional"].append("assessment")
        packet["result_protocol"]["assessment_shape"] = {
            "view_id": view_id, "inventory_digest": packet["inventory"]["metadata"]["inventory_digest"],
            "assess_all": True, "exclusions": [{"attempt_id": "optional exact ID", "reason": "explicit rationale"}],
            "limitations": "Required limitations; assess_all explicitly considers every inventoried observation except named exclusions",
        }
        packet["instructions"] += ("\nRetrieve all remaining inventory pages with labgoblin view --id VIEW_ID --offset END --json."
                                   " Your final handoff must include the exact assessment object to establish inventory coverage.\n")
        if len(canonical(packet)) > PACKET_BYTES:
            raise SizeLimitError("Mandatory final inventory context exceeds the packet byte allowance")
    if kind == "report":
        if not view_id:
            raise ValueError("Report inference requires its committed source view")
        packet["instructions"] = """Write an interim historical research report from ONLY this immutable source view and
its exact retained source/observation references. Retrieve every inventory page with labgoblin view.
Keep the complete denominator visible even when only selected attempts support a comparison.
Do not perform research, read raw datasets, submit experiments, or start another provider.
Return concise title, summary, limitations, reference IDs and structured numeric claims.
Claims have observation_id, metric and value matching captured evidence in the selected scope.
Do not return HTML, output paths, raw commands or a research-finalization decision.
The harness renders immutable HTML/Markdown with accurate inventory tables and bounded local figures."""
        packet["result_protocol"] = {"maximum_bytes": HANDOFF_BYTES,
                                    "required": ["turn_id", "packet_id", "view_id", "title", "summary", "limitations", "claims", "references"],
                                    "claim_shape": {"observation_id": "exact ID", "metric": "exact captured metric name", "value": "finite number"}}
        packet["coverage"].update(delivered=0)
        return packet
    if kind == "compact":
        return _compaction(conn, packet)
    columns = """id,seq,kind,created,payload_digest,length(CAST(payload AS BLOB)) AS payload_bytes,
        substr(CAST(payload AS BLOB),1,512) AS preview"""
    evidence = list(conn.execute(f"""SELECT {columns} FROM events WHERE generation=?
        AND acknowledged_by IS NULL AND kind='evidence' ORDER BY seq LIMIT ?""",
                                 (campaign["generation"], PACKET_EVENTS // 2)))
    other = list(conn.execute(f"""SELECT {columns} FROM events WHERE generation=?
        AND acknowledged_by IS NULL ORDER BY seq LIMIT ?""", (campaign["generation"], PACKET_EVENTS)))
    seen = set()
    for row in evidence + other:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        header = _event_header(conn, row)
        additions = {row["id"], *(o["id"] for o in header["observations"])}
        candidate = {**packet, "events": [*packet["events"], header],
                     "references": sorted(references | additions),
                     "coverage": {**packet["coverage"], "delivered": len(packet["events"]) + 1,
                                  "has_more": pending > len(packet["events"]) + 1}}
        if len(canonical(candidate)) > PACKET_BYTES:
            break
        packet, references = candidate, references | additions
        if len(packet["events"]) == PACKET_EVENTS:
            break
    if pending and not packet["events"]:
        raise SizeLimitError("Mandatory context leaves no room for an event header; shorten active context")
    return packet


def _compaction(conn, packet):
    previous = packet["sources"].get("summary")
    metadata = json.loads(conn.execute("SELECT metadata FROM sources WHERE id=?", (previous["id"],)).fetchone()[0]) if previous else {}
    scope = {"source_ids": [previous["id"]] if previous else [], "sources": [],
             "input_bytes": len(previous["text"].encode("utf-8")) if previous else 0,
             "archive_after": metadata.get("archive_after", 0),
             "archive_index_bytes": metadata.get("archive_index_bytes", 0),
             "coverage": "Only listed prefixes and the prior derived summary; originals remain exactly retrievable"}
    packet.update(instructions="""Produce one concise derived summary of the supplied archive prefixes and prior summary.
Preserve useful negative results, reasons and decisions. The goal, active constraints and governing
rationale are separate authority and MUST NOT be replaced. Do not perform research or experiments.
Use exact journal entry retrieval when a preview is insufficient. Semantic equivalence is not certified.
Write only the specified owned JSON result, with source_ids exactly matching compaction.source_ids.
The summary must be smaller than compaction.input_bytes. Do not launch any child provider.""",
                  result_protocol={"maximum_bytes": HANDOFF_BYTES,
                                   "required": ["turn_id", "packet_id", "summary", "source_ids"]},
                  compaction=scope)
    rows = conn.execute("""SELECT id,seq,digest,length(body) AS bytes,substr(body,1,8192) AS preview
        FROM sources WHERE seq>? AND kind IN ('handoff','journal_import') ORDER BY seq LIMIT 64""",
                        (scope["archive_after"],)).fetchall()
    for row in rows:
        item = {"id": row["id"], "seq": row["seq"], "digest": row["digest"], "bytes": row["bytes"],
                "preview_bytes": len(row["preview"]), "truncated": len(row["preview"]) < row["bytes"],
                "text": bytes(row["preview"]).decode("utf-8", errors="replace")}
        candidate = {**scope, "sources": [*scope["sources"], item], "source_ids": [*scope["source_ids"], row["id"]],
                     "input_bytes": scope["input_bytes"] + len(item["text"].encode("utf-8")),
                     "archive_after": row["seq"], "archive_index_bytes": scope["archive_index_bytes"] + row["bytes"]}
        updated = {**packet, "compaction": candidate, "references": [*packet["references"], row["id"]]}
        if len(canonical(updated)) > PACKET_BYTES:
            break
        packet, scope = updated, candidate
    if not scope["sources"]:
        raise SizeLimitError("No new archive source fits the compaction packet; shorten mandatory context or inspect the archive")
    packet["coverage"].update(delivered=0, has_more=bool(packet["coverage"]["pending_at_cutoff"]))
    return packet


def prepare(state, kind="research", *, turn_id=None, view_id=None, maintenance_id=None) -> dict:
    if kind not in ("research", "final_analysis", "report", "compact"):
        raise ValueError("Unsupported packet operation")
    turn_id = turn_id or identifier()
    with state.db.write() as conn:
        old = conn.execute("SELECT * FROM packets WHERE turn_id=?", (turn_id,)).fetchone()
        if old:
            content = json.loads(old["content"])
            if content["kind"] != kind or content.get("view_id") != view_id:
                raise ValueError("Packet replay cannot change its operation or source view")
            packet_id = old["id"]
        else:
            campaign = _campaign(conn)
            state._admissible(conn, campaign, kind, maintenance_id=maintenance_id)
            campaign = _campaign(conn)
            if kind == "final_analysis":
                used = conn.execute("SELECT final_turn FROM generations WHERE id=?", (campaign["generation"],)).fetchone()[0]
                pending = conn.execute("""SELECT 1 FROM turns WHERE generation=? AND kind='final_analysis'
                    AND state IN ('prepared','running')""", (campaign["generation"],)).fetchone()
                if used or pending:
                    raise ValueError("Generation already owns its one final analysis")
            if view_id is not None and not conn.execute("SELECT 1 FROM views WHERE id=? AND generation=?",
                                                       (view_id, campaign["generation"])).fetchone():
                raise ValueError("Packet source view does not belong to this generation")
            packet_id = identifier()
            content = _content(state, conn, campaign, turn_id, packet_id, kind, view_id)
            body = canonical(content)
            path = contained(state.root, Path("packets") / f"{packet_id}.json")
            conn.execute("""INSERT INTO packets(id,turn_id,generation,watermark,content,digest,path,created)
                VALUES(?,?,?,?,?,?,?,?)""", (packet_id, turn_id, campaign["generation"], content["watermark"],
                                            body.decode(), hashlib.sha256(body).hexdigest(), str(path), time.time()))
            conn.executemany("INSERT INTO packet_events(packet_id,event_id) VALUES(?,?)",
                             [(packet_id, event["id"]) for event in content["events"]])
            conn.execute("""INSERT INTO turns(id,generation,kind,packet_id,state,created,revision,source_revision,admission_order)
                VALUES(?,?,?,?,'prepared',?,?,?,?)""",
                         (turn_id, campaign["generation"], kind, packet_id, time.time(), campaign["revision"],
                          hashlib.sha256(canonical(sorted(content["references"]))).hexdigest(), _queue_order(conn)))
            if kind in ("compact", "report"):
                request = conn.execute("SELECT source_revision FROM maintenance WHERE id=?", (maintenance_id,)).fetchone()
                if request["source_revision"] != _source_revision(conn):
                    raise AdmissionWait("Maintenance sources changed before packet commitment; coalesce to their new revision")
                conn.execute("UPDATE maintenance SET state='running',turn_id=? WHERE id=? AND state='pending'",
                             (turn_id, maintenance_id))
            if view_id:
                coverage = content["inventory"]["coverage"]
                conn.execute("INSERT INTO turn_view_pages(turn_id,view_id,start,end) VALUES(?,?,?,?)",
                             (turn_id, view_id, coverage["offset"], coverage["end"]))
    return publish(state, packet_id)


def publish(state, packet_id: str) -> dict:
    with state.db.read() as conn:
        row = conn.execute("SELECT * FROM packets WHERE id=?", (packet_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown committed packet")
    body = row["content"].encode("utf-8")
    if len(body) > PACKET_BYTES or hashlib.sha256(body).hexdigest() != row["digest"]:
        raise ValueError("Committed packet integrity failed")
    path = contained(state.root / "packets", row["path"])
    if publish_bytes(path, body) != row["digest"] or hash_file(path, PACKET_BYTES) != row["digest"]:
        raise ValueError("Published packet integrity failed")
    with state.db.write() as conn:
        conn.execute("UPDATE packets SET ready=1 WHERE id=? AND digest=?", (packet_id, row["digest"]))
    return {**dict(row), "ready": 1, "content": json.loads(row["content"])}


def event(db, event_id: str, *, offset=0, limit=PACKET_BYTES) -> dict:
    integer(offset, "event byte offset", zero=True)
    integer(limit, "event byte page size")
    if limit > PACKET_BYTES:
        raise ValueError("Event pages cannot exceed 64 KiB")
    with db.read() as conn:
        row = conn.execute("""SELECT id,seq,generation,kind,created,payload_digest,
            length(CAST(payload AS BLOB)) AS bytes,
            substr(CAST(payload AS BLOB),?,?) AS body FROM events WHERE id=?""",
                           (offset + 1, limit, event_id)).fetchone()
    if row is None:
        raise ValueError("Unknown event revision")
    value = dict(row)
    body = bytes(value.pop("body"))
    complete = offset == 0 and len(body) == value["bytes"]
    if complete and hashlib.sha256(body).hexdigest() != value["payload_digest"]:
        raise ValueError("Retained event revision was modified")
    return {**value, "offset": offset, "returned_bytes": len(body), "has_more": offset + len(body) < value["bytes"],
            "retrieved_at": time.time(), "payload": parse_json(body) if complete else None,
            "text": body.decode("utf-8", errors="replace"), "base64": base64.b64encode(body).decode("ascii")}
