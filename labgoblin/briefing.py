"""Exact, bounded research packets and revision-qualified event retrieval."""

import base64
import hashlib
import json
from pathlib import Path
import sys
import time

from labgoblin.evidence import SizeLimitError, contained, hash_file, parse_json, publish_bytes
from labgoblin.protocol import AdmissionWait, HANDOFF_BYTES, PACKET_BYTES, PACKET_EVENTS, canonical, fingerprint, identifier, integer
from labgoblin.state import _active_directives, _campaign, _queue_order, _settings, _source_revision


INSTRUCTIONS = """Conduct one autonomous research turn, not a human-agent collaboration.
Follow the versioned goal, evaluation protocol and active operator constraints. Preserve
evidence and history, but reconsider earlier researcher assumptions and strategy freely.
The latest researcher focus is a revisable interpretation, not an operator directive.
Derived summaries are unverified hints, never authority over these sources or constraints.
Do useful support work or replication without inventing a hypothesis or discovery.
Keep an iterative loop: establish useful comparisons, run a bounded test, assess the
actual evidence, retain or retire the idea, and choose what to learn next. Negative
results and actual errors are useful evidence. Revisit near misses, earlier ideas and
assumptions when stuck. Prefer a simpler approach when evidence makes it competitive.
One next step need not solve the entire goal; incremental information gain can justify it.
Use the research workspace to keep general knowledge, questions, hypotheses and reflections.
Edit draft files freely, then explicitly publish through the research tools or CLI.
Publish scientific assessments in research_updates in this owned handoff. Jobs state their
purpose (evaluation|exploration|support) and exact research_refs; evaluation pins hypotheses.
Each research update needs id, a stable unique request_id, title and body; new records also need kind.
Revisions need base set to the exact current revision ID. Each assessed update needs its own nonempty links array with exact target IDs
and relations. Prose citations and top-level research_refs do not replace these links.
Top-level research_refs names published research revisions, not observation or event IDs.
Use rationale for concise current direction, why, unresolved decisions and conditions for
revisiting alternatives; next_step states the next useful action. Put selected research_refs
in priority order for the next fresh turn. No extra document or fixed prose template is needed.
Before following the local next step, inspect goal_progress and the original deliverables.
In root research/reflection turns, at the outset and when your assessment changes, include goal_progress:
original deliverables, supported/partial/unresolved assessments, exact evidence and coverage limits.
Omit it when unchanged; the harness retains its accepted source. These are revisable judgments,
not certified truth. Do not replace the original goal with a convenient local score.
In root research/reflection turns, at a branch, refinement batch or milestone, include commitment: stable branch name,
boundary=branch|refinement|milestone, goal_connection, decision_value and revisit_when.
Explain which decision different outcomes could change or which concrete prerequisite this enables.
A useful negative result or necessary engineering is legitimate; every step need not finish the goal.
Omit commitment during routine continuation of the same batch. No credit forecast or value/credit
score is requested. Keep scientific standards unchanged; exact budgets remain operational facts.
Workers must not publish goal_progress or commitment. Final analysis must not add commitment.
Occasional strategy advice is nonbinding: consider it in your normal rationale, then choose your
own action. A request to reconsider does not require a pivot or a separate reflection turn.
Accepted inline research_updates also carry forward by exact revision; do not forward-reference
unpublished revisions in research_refs.
Selected revisions are context, not endorsements. Recent publications are discovery only.
Historical focus is not current strategy. Root background does not amend a worker charter.
The work and budget sections are timestamped operational facts, not scientific outcomes.
Use research_read operation=work (or labgoblin research work) for fresh paged work status.
Direct work is appropriate for a short continuous task. Delegate only separable work through
the harness task API when enabled, never native provider subagents. Task workers own details.

Submit heavy experiments with labgoblin submit (an argv-form JSON manifest), never as
unmanaged shell jobs. Use labgoblin schema work --json and schema research-update --json
for exact fields/examples. Resources are top-level cpus, memory_mb and gpus (an array;
[] for CPU-only). For an existing hypothesis_id, omit hypothesis_description to use its
stored statement; do not invent statement/hypothesis_statement fields or paraphrase it.
All experiments and inference share finite resources. Do not start
controllers, providers, subagents or workflows, install into shared environments, upload
data, push code, or expand the goal. Trusted execution is not filesystem isolation.
Do not wait synchronously for experiments while holding the reasoning grant.
A null task max_seconds means unlimited task lifetime, not unlimited job or reasoning deadlines.

Use labgoblin evidence and journal retrieval for exact revisions. Event and metric previews
may be incomplete: inspect their coverage and retrieve more before drawing conclusions.
Artifact retention limits are in artifact_storage; do not confuse them with small model
read limits or impose an old 1 MiB export cap. New jobs capture large files by streaming.
Use labgoblin evidence observation --id ID --export PATH for a complete verified local
file, or --offset/--bytes for explicit byte pages. SDK research_read operation=artifact
returns small base64 byte pages. Neither a page nor metadata alone means all bytes were
read. Previously prepared jobs retain their original frozen capture limits.
Zero search matches are not proof of absent evidence. Never replace historical evidence
with a current mutable file. Process success is not artifact validity or scientific success.

Prefer research_complete to validate and write one owned handoff; omit owned turn/packet IDs.
Alternatively use labgoblin research complete --file draft.json or write the supplied result path
after preflight. labgoblin schema handoff --json describes the full shape.
After successful research_complete, end this turn; do not also write the handoff manually.
Do not independently overwrite
the journal: it is projected from accepted handoffs. Explain what changed, why, and the
governing next step. Acknowledge only exact delivered event IDs; subsets may leave holes.
An assessed/excluded disposition requires a reason. Deferred evidence additionally needs
a concrete wake_condition and remains unacknowledged. Cite only delivered/retrieved IDs.
Request fixed report/compact maintenance in the handoff, never a child provider.
Fresh packets already bound working context. Do not request compaction merely because
the archive grew; use exact retrieval, and request a summary only when genuinely useful.
Use research_preflight (or labgoblin research preflight --file) for a manually written handoff.
research_complete includes this validation. Both leave final acceptance to the controller.

Choose continue, wait, blocked, or finalize with a reason. A report or empty queue alone
does not complete research. Retire an unsuccessful branch and continue or reflect; do not
equate it with campaign closure. For root blocked/finalize proposals, set stop_basis to
goal_satisfied, operator_needed, or discretionary. Goal satisfaction requires finalize,
the original stopping criterion, exact evidence/research references and honest limitations.
Name a specific missing permission/input for operator_needed, including what remains
possible without it. Discretionary partial closure needs operator approval, not a coach's
permission; uncertain value or no single-step solution is not enough. Earlier researcher
stopping prose is revisable strategy, including after an operator reopens a campaign.
Hard budgets, safety and operator controls always prevail; never spend merely to persist.
Workers may finalize their assigned task without claiming campaign completion.
Final analysis must assess the sealed inventory; it cannot submit experiments
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


WORKING_CONTEXT_BYTES = 12 * 1024
CONTEXT_RECORDS = 12


def _context(conn, campaign, kind, view_id=None) -> tuple[dict, list[dict], dict]:
    heads = {r["name"]: r["source_id"] for r in conn.execute(
        "SELECT name,source_id FROM source_heads WHERE name IN ('goal','protocol','rationale','summary','goal_progress')")}
    directives = [dict(row) for row in _active_directives(conn, campaign["generation"])]
    for item in directives:
        item["currently_active"] = True
        item["sealed_scope"] = False
    if kind == "final_analysis" or kind == "report" and view_id:
        frozen = json.loads(conn.execute("SELECT source_ids FROM views WHERE id=?", (view_id,)).fetchone()[0]
                            if kind == "report" else conn.execute("SELECT source_heads FROM generations WHERE id=?",
                                                                  (campaign["generation"],)).fetchone()[0])
        heads = {key: value for key, value in frozen.items() if key in ("goal", "protocol", "rationale", "summary", "goal_progress")}
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
        if name == "goal_progress":
            continue
        if kind == "compact" and name == "rationale":
            continue
        if name in ("rationale", "summary") and kind in ("research", "reflection", "worker", "coach", "final_analysis"):
            continue
        value = _source(conn, source_id, remaining)
        remaining -= len(canonical(value))
        sources[name] = value
    for row in directives:
        value = {**dict(row), "source": _source(conn, row["source_id"], remaining)}
        remaining -= len(canonical(value))
        constraints.append(value)
    return sources, constraints, heads


def _preview(value, limit):
    body = value.encode("utf-8")
    text = body[:limit].decode("utf-8", errors="ignore")
    return {"text": text, "bytes": len(body), "returned_bytes": len(text.encode("utf-8")),
            "truncated": len(text.encode("utf-8")) < len(body)}


def _focus(conn, source_id, campaign, scope_id, *, authority_revision, sealed=False, charter=None):
    row = conn.execute("""SELECT t.id,t.generation,t.scope_id,p.content,s.metadata
        FROM handoffs h JOIN turns t ON t.id=h.turn_id JOIN packets p ON p.id=t.packet_id
        JOIN sources s ON s.id=h.source_id
        WHERE h.source_id=? AND t.state='accepted' AND t.scope_id=?""", (source_id, scope_id)).fetchone()
    if row is None:
        raise ValueError("Research focus must name an accepted handoff in its exact scope")
    context, metadata = json.loads(row["content"]), json.loads(row["metadata"])
    reasons = []
    if metadata.get("stale"):
        reasons.append("Handoff was stale at acceptance")
    if row["generation"] != campaign["generation"]:
        reasons.append("Earlier generation")
    if context.get("authority_revision") != authority_revision:
        reasons.append("Different governing authority")
    if charter is not None and fingerprint(context.get("task", {}).get("charter")) != fingerprint(charter):
        reasons.append("Different task charter")
    value = _source(conn, source_id, 1024 * 1024)
    header = {"source_id": source_id, "digest": value["digest"], "turn_id": row["id"], "scope_id": scope_id,
              "generation": row["generation"], "authority_revision": context.get("authority_revision"),
              "applicability": "historical" if reasons else "sealed" if sealed else "current",
              "qualifications": reasons, "preview_included": False}
    return header, value, json.loads(value["text"])


def work(state, *, scope_id="root", offset=0, limit=12, connection=None, now=None):
    """Bounded operational samples, not scientific assessments or process probes."""
    from contextlib import nullcontext
    integer(offset, "work offset", zero=True)
    integer(limit, "work page size")
    if limit > 20:
        raise ValueError("Work pages cannot exceed 20 records per section")
    with state.db.read() if connection is None else nullcontext(connection) as conn:
        generation = _campaign(conn)["generation"]
        parameters = (generation, scope_id, scope_id)
        attempt_where = "a.generation=? AND (?='root' OR a.scope_id=?)"
        task_where = "generation=? AND (?='root' OR id=?)"
        attempt_counts = dict(conn.execute(f"""SELECT a.status,COUNT(*) FROM attempts a
            WHERE {attempt_where} GROUP BY a.status""", parameters))
        task_counts = dict(conn.execute(f"""SELECT state,COUNT(*) FROM research_tasks
            WHERE {task_where} GROUP BY state""", parameters))
        attempts = []
        for row in conn.execute(f"""SELECT a.id,a.scope_id,a.experiment_id,a.status,a.exit_code,
                a.collection,a.validation,a.collection_event,e.acknowledged_by,
                d.disposition AS evidence_disposition
            FROM attempts a LEFT JOIN events e ON e.id=a.collection_event
            LEFT JOIN dispositions d ON d.event_id=e.id AND d.turn_id=e.acknowledged_by
            WHERE {attempt_where}
            ORDER BY a.status NOT IN ('queued','starting','running','recovery_required'),
                (a.collection='pending' OR e.id IS NOT NULL AND e.acknowledged_by IS NULL) DESC,
                a.created DESC,a.id LIMIT ? OFFSET ?""", (*parameters, limit, offset)):
            item = dict(row)
            item["experiment_id"] = _preview(item["experiment_id"], 256)
            if item["evidence_disposition"] is None:
                item["evidence_disposition"] = "pending" if item["collection_event"] else "not_collected"
            attempts.append(item)
        tasks = [dict(row) for row in conn.execute(f"""SELECT id,state,result_source,
                json_extract(integrated,'$.state') AS integration_state
            FROM research_tasks WHERE {task_where}
            ORDER BY state NOT IN ('queued','research','waiting'),created DESC,id LIMIT ? OFFSET ?""",
                                                   (*parameters, limit, offset))]
    sections = {}
    for name, rows, counts in (("attempts", attempts, attempt_counts), ("tasks", tasks, task_counts)):
        total = sum(counts.values())
        sections[name] = {"records": rows, "counts": counts, "total": total, "offset": offset,
                          "next_offset": offset + len(rows), "has_more": offset + len(rows) < total}
    return {"recorded_at": time.time() if now is None else now, "generation": generation, "scope_id": scope_id,
            **sections,
            "coverage": "Current-generation DB sample, not process liveness. Root sees all scopes; workers see their own. "
                        "Pending includes deferred evidence. A scope's event disposition is not scientific support or root "
                        "adoption; worker completion and integration are separate. Pages are fresh samples, not a frozen view.",
            "lookup": "research_read operation=work, offset/limit; task details: operation=task, id; evidence: operation=event, id"}


def _offer(packet, changes, limit, references=()):
    candidate = {**packet, **changes, "references": sorted(set(packet["references"]) | set(references))}
    if len(canonical(candidate)) > limit:
        return False
    packet.update(candidate)
    return True


def _working_context(conn, packet, heads, focuses, selected, selected_records, sampled_work):
    # Events and sealed inventory have already taken their space.
    limit = min(PACKET_BYTES, len(canonical(packet)) + WORKING_CONTEXT_BYTES)
    for name, (header, source, content) in focuses.items():
        if name == "goal_progress":
            _offer(packet, {"goal_progress": {
                **header, "preview_included": True, "assessment": content["goal_progress"],
                "authority": "Researcher assessment of original deliverables, not certification or operator authority. "
                             "Historical assessments require reconsideration; this is not a worker charter.",
            }}, limit, [source["id"]])
            continue
        for scale in (1, 2, 4, 8):
            fields = {key: _preview(content[key], size // scale)
                      for key, size in (("summary", 512), ("rationale", 2560), ("next_step", 768))}
            value = {**source, "text": canonical({key: item["text"] for key, item in fields.items()}).decode(),
                     "preview": True, "field_coverage": {key: {k: v for k, v in item.items() if k != "text"}
                                                        for key, item in fields.items()},
                     "authority": "Revisable interpretation, not operator authority; applicability is in research_workspace.focus"}
            workspace = packet["research_workspace"]
            focus_headers = {**workspace["focus"], name: {**header, "preview_included": True}}
            if _offer(packet, {"sources": {**packet["sources"], name: value},
                               "research_workspace": {**workspace, "focus": focus_headers}}, limit, [source["id"]]):
                break
    for ref, (selection_source, selection_kind) in list(selected.items())[:CONTEXT_RECORDS]:
        value = _source(conn, ref, 1024 * 1024)
        content = json.loads(value["text"])
        head = (None if packet["kind"] == "final_analysis" else conn.execute(
            "SELECT head FROM research_records WHERE id=?", (content["id"],)).fetchone())
        item = {key: content[key] for key in ("id", "version", "scope_id", "kind", "title", "activity", "outcome")}
        item.update(source_id=ref, digest=value["digest"], selected_by=selection_source, selection=selection_kind,
                    is_current_head=head["head"] == ref if head else None, body=_preview(content["body"], 512))
        workspace = packet["research_workspace"]
        records = [*workspace["selected"], item]
        if not _offer(packet, {"research_workspace": {**workspace, "selected": records,
                  "selected_has_more": len(records) < len(selected)}}, limit, [ref]):
            break
    for name in ("attempts", "tasks"):
        for item in sampled_work[name]["records"]:
            current = packet["work"][name]
            section = {**current, "records": [*current["records"], item],
                       "next_offset": current["next_offset"] + 1,
                       "has_more": current["next_offset"] + 1 < current["total"]}
            if not _offer(packet, {"work": {**packet["work"], name: section}}, limit, [item["id"]]):
                break
    if packet["kind"] != "final_analysis":
        exclusion = f"AND id NOT IN ({','.join('?' for _ in selected_records)})" if selected_records else ""
        rows = conn.execute(f"""SELECT id,head,kind,title,activity,scope_id FROM research_records
            WHERE scope_id=? AND activity='active' {exclusion} ORDER BY updated DESC,id LIMIT ?""",
                            (packet["scope_id"], *sorted(selected_records), CONTEXT_RECORDS))
        for row in rows:
            workspace = packet["research_workspace"]
            records = [*workspace["records"], dict(row)]
            if not _offer(packet, {"research_workspace": {**workspace, "records": records,
                      "recent_has_more": len(records) < workspace["recent_total"]}}, limit, [row["head"]]):
                break
            if len(records) == CONTEXT_RECORDS:
                break
    if "summary" in heads:
        value = _source(conn, heads["summary"], 1024 * 1024)
        preview = _preview(value["text"], 1200)
        value.update(text=preview.pop("text"), preview=True, coverage=preview,
                     authority="Unverified derived hint, not accepted strategy; retrieve original by ID")
        _offer(packet, {"sources": {**packet["sources"], "summary": value},
                        "research_workspace": {**packet["research_workspace"], "summary_preview_included": True}},
               limit, [value["id"]])


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


def _content(state, conn, campaign, turn_id, packet_id, kind, view_id, scope_id="root"):
    settings = _settings(conn, campaign)
    sources, directives, heads = _context(conn, campaign, kind, view_id)
    watermark = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
    pending = conn.execute("""SELECT COUNT(*) FROM events WHERE generation=? AND acknowledged_by IS NULL
        AND scope_id=?""", (campaign["generation"], scope_id)).fetchone()[0]
    recorded_at = time.time()
    budgets = state.budget(recorded_at, connection=conn)
    resource_budget = budgets["resources"]
    budgets.update(resources=resource_budget["configured"],
                   resource_commitments={key: value for key, value in resource_budget.items() if key != "configured"},
                   final_analysis_reserve=(2 if settings["agent"]["sandbox"] else 1)
                   if settings["campaign"]["max_invocations"]["value"] and kind != "final_analysis" else 0)
    references = {source["id"] for source in sources.values()}
    references.update(item["source_id"] for item in directives)
    if view_id:
        references.add(view_id)
    packet = {
        "protocol": 3, "turn_id": turn_id, "packet_id": packet_id, "kind": kind, "scope_id": scope_id,
        "campaign_id": state.id, "generation": campaign["generation"], "revision": campaign["revision"],
        "authority_revision": campaign["authority_revision"],
        "config_revision": campaign["config_revision"],
        "watermark": watermark, "recorded_at": recorded_at, "sources": sources, "directives": directives,
        "instructions": INSTRUCTIONS, "cli_argv": [sys.executable, "-m", "labgoblin.cli"],
        "project": str(state.root.parent), "view_id": view_id,
        "budgets": budgets,
        "artifact_storage": {
            "per_file_bytes": settings["storage"]["capture_bytes"],
            "per_attempt_bytes": 4 * settings["storage"]["capture_bytes"],
            "metrics_bytes": settings["storage"]["metrics_bytes"],
            "scope": "New experiment preparations; immutable retention, not model context. "
                     "Earlier preparations keep their exact frozen allowances.",
        },
        "result_protocol": {
            "maximum_bytes": HANDOFF_BYTES,
            "required": ["turn_id", "packet_id", "summary", "rationale", "next_step", "disposition", "reason"],
            "optional": ["evidence", "stopping_criterion", "stop_basis", "maintenance", "research_updates", "research_refs",
                         "reflection", "coaching", "goal_progress", "commitment"],
            "completion": "research_complete validates and writes the owned result; schema handoff --json exposes exact fields",
            "stop_basis": "goal_satisfied|operator_needed|discretionary (root blocked/finalize only; missing is discretionary)",
            "research_update_links": [{"target": "exact delivered/retrieved evidence ID", "relation": "supports|refutes|context|derived_from|supersedes|informs"}],
            "research_update_required": ["id", "request_id", "title", "body"],
            "research_update_revisions": "New records require kind; revisions require base=exact current revision ID.",
            "evidence_shape": {"event_id": "delivered ID", "disposition": "assessed|excluded|deferred",
                               "reason": "explanation", "references": ["revision IDs"], "wake_condition": "required if deferred"},
        },
        "goal_progress": {"applicability": "unrecorded", "preview_included": False,
                          "coverage": "No accepted root goal-gap assessment yet. The original goal remains authoritative."},
        "events": [], "references": sorted(references),
        "coverage": {"pending_at_cutoff": pending, "delivered": 0, "has_more": pending > 0,
                     "maximum_events": PACKET_EVENTS, "maximum_bytes": PACKET_BYTES,
                     "ordering": "oldest evidence first, then oldest other pending events"},
    }
    count = conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0]
    packet["research_workspace"] = {
        "draft_root": str(state.root.parent / "research"), "records": [], "total": count,
        "focus": {}, "selected": [], "selected_total": 0, "selected_has_more": False,
        "source_cutoff": conn.execute("SELECT COALESCE(MAX(seq),0) FROM sources").fetchone()[0],
        "coverage": "Optional bounded context. Focus applicability is explicit; exact selected revisions precede "
                    "at most 12 same-scope recent active discovery headers (not accepted strategy). Selection is not support. "
                    "Read the exact focus source for omitted references, and use research read/list/search/history "
                    "for all revisions and parked/retired ideas. No recent discovery in sealed final analysis.",
        "limits": settings["research"], "tools": "research_read, research_publish, research_task",
        "cli": "labgoblin research --help"}
    if scope_id == "root" and kind in ("research", "reflection", "final_analysis"):
        from labgoblin.supervisor_memos import packet_context
        packet["supervisor_memo"] = packet_context(conn, state.root / "turns" / turn_id / "supervisor-memo.json")
        for item in packet["supervisor_memo"]["requests"]:
            if not item["truncated"]:
                references.add(item["id"])
            if item["reference"]:
                references.add(item["reference"])
        packet["references"] = sorted(references)
        packet["instructions"] += (
            "\nWrite a separate supervisor-memo.json at supervisor_memo.path when an update is due, "
            "or when this turn brings a meaningful result, assessment, retirement, pivot or operator decision. "
            "Do not add memo fields to the scientific handoff. Use the supplied memo format, about 150-250 words "
            "(maximum 300), plain language: bottom line, changes since the previous memo, uncertainty, next step, "
            "and a specific decision needed (or 'None; research continues'). Cite underlying exact evidence. "
            "Retiring a hypothesis does not necessarily refute it. Distinguish development results from goal "
            "completion; do not invent progress or reproduce low-level logs. Retrieve truncated/more pending requests with "
            "research_read operation=memos (or labgoblin research memos); retrieve the previous memo by its source ID. "
            "This is asynchronous communication, not permission to pause for review, expand scope or spend. "
            "A missing memo does not invalidate science; do not request a separate paid reporting turn. "
            "The dashboard supplies live health and spending separately; do not claim an old snapshot is live.")
    if kind == "worker":
        from labgoblin.research_tasks import assert_owned
        task = assert_owned(conn, scope_id)
        packet["task"] = {**dict(task), "charter": json.loads(task["charter"])}
        references.update(packet["task"]["charter"].get("research_refs", []))
        packet["references"] = sorted(references)
        packet["working_directory"] = str(Path(task["root"]) / "work")
        packet["instructions"] += ("\nYou own only this task workspace and charter. Do not edit root files, delegate "
                                   "children or close the campaign. A final local handoff completes this task, not the campaign. "
                                   "Return task_artifacts as [{path: workspace-relative path, digest: SHA-256}] for changed code. "
                                   "Run relevant tests as owned jobs using exactly those source bytes; root integration requires "
                                   "their successful validation_attempt. Use research_task message for a bounded clarification.")
        packet["result_protocol"]["optional"].append("task_artifacts")
    if kind == "reflection":
        packet["instructions"] += ("\nReflect on evidence and strategy, including useful unlinked knowledge and "
                                   "retired ideas. Return a concise decision; do not run heavy jobs or delegate in this turn.")
    if kind == "final_analysis":
        packet["result_protocol"]["automatic_structure_corrections"] = 1
        packet["instructions"] += ("\nThe SDK preflights the final draft before owned result capture. At most one "
                                   "structure/reference correction can use this same session's remaining allowance "
                                   "and deadline. It cannot change scientific conclusions, discard assessments, "
                                   "start experiments or reopen the sealed inventory.")
    if kind == "coach":
        from labgoblin.coaching import INSTRUCTIONS as COACH_INSTRUCTIONS, STRATEGY_INSTRUCTIONS, packet as coach_packet
        packet["coaching"] = coach_packet(conn)
        strategy = packet["coaching"]["purpose"] == "strategy"
        packet["instructions"] = STRATEGY_INSTRUCTIONS if strategy else COACH_INSTRUCTIONS
        if strategy:
            references.update(row["source_id"] for row in packet["coaching"]["proposal"]["trajectory"])
            packet["references"] = sorted(references)
        packet["result_protocol"] = {"required": ["turn_id", "packet_id", "episode_id", "decision",
                                                "reason", "questions", "references", "limitations"]}
    focuses, selected, selected_records, sampled_work = {}, {}, set(), None
    if kind in ("research", "reflection", "worker", "coach", "final_analysis"):
        sealed = kind == "final_analysis"
        authority = (conn.execute("SELECT authority_revision FROM generations WHERE id=?",
                                 (campaign["generation"],)).fetchone()[0] if sealed else campaign["authority_revision"])
        if heads.get("goal_progress"):
            focuses["goal_progress"] = _focus(conn, heads["goal_progress"], campaign, "root",
                                             authority_revision=authority, sealed=sealed)
            packet["goal_progress"] = {**focuses["goal_progress"][0],
                                      "coverage": "Retrieve this exact handoff source if its goal assessment is omitted."}
        if scope_id == "root":
            focus_id = heads.get("rationale")
            charter = None
        else:
            checkpoint = conn.execute("SELECT source_id FROM source_heads WHERE name=?",
                                      (f"checkpoint:{campaign['generation']}:{scope_id}",)).fetchone()
            focus_id = checkpoint[0] if checkpoint else None
            charter = packet["task"]["charter"]
        if focus_id:
            focuses["rationale"] = _focus(conn, focus_id, campaign, scope_id,
                                         authority_revision=authority, sealed=sealed, charter=charter)
            header, _, content = focuses["rationale"]
            if header["applicability"] != "historical":
                selected.update((ref, (focus_id, "research_refs")) for ref in content.get("research_refs", []))
                for update in content.get("research_updates", []):
                    revision = conn.execute("SELECT source_id,request_digest FROM research_revisions WHERE request_id=?",
                                            (update["request_id"],)).fetchone()
                    if not revision or revision["request_digest"] != fingerprint(update):
                        raise ValueError("Accepted research update is missing its exact published revision")
                    selected.setdefault(revision["source_id"], (focus_id, "research_updates"))
        if scope_id != "root":
            for ref in charter.get("research_refs", []):
                selected.setdefault(ref, (charter["source_id"], "task_charter"))
            if heads.get("rationale"):
                focuses["root_rationale"] = _focus(conn, heads["rationale"], campaign, "root",
                                                   authority_revision=authority)
        workspace = packet["research_workspace"]
        workspace.update(focus={name: value[0] for name, value in focuses.items()},
                         selected_total=len(selected), selected_has_more=bool(selected),
                         summary_source_id=heads.get("summary"), summary_preview_included=False)
        selected_records = {row[0] for ref in selected for row in conn.execute(
            "SELECT record_id FROM research_revisions WHERE source_id=?", (ref,))}
        exclusion = f"AND id NOT IN ({','.join('?' for _ in selected_records)})" if selected_records else ""
        recent_total = (0 if sealed else conn.execute(f"""SELECT COUNT(*) FROM research_records
            WHERE scope_id=? AND activity='active' {exclusion}""", (scope_id, *sorted(selected_records))).fetchone()[0])
        workspace.update(recent_total=recent_total, recent_has_more=recent_total > 0)
        references.update(value[0]["source_id"] for value in focuses.values())
        sampled_work = work(state, scope_id=scope_id, limit=6, connection=conn, now=recorded_at)
        packet["work"] = {**sampled_work, **{
            name: {**sampled_work[name], "records": [], "next_offset": 0, "has_more": sampled_work[name]["total"] > 0}
            for name in ("attempts", "tasks")}}
        packet["references"] = sorted(references)
    if len(canonical(packet)) > PACKET_BYTES:
        raise SizeLimitError("Mandatory authority and packet metadata exceed the packet byte limit; none were clipped")
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
        AND scope_id=? AND acknowledged_by IS NULL AND kind='evidence' ORDER BY seq LIMIT ?""",
                                 (campaign["generation"], scope_id, PACKET_EVENTS // 2)))
    other = list(conn.execute(f"""SELECT {columns} FROM events WHERE generation=?
        AND scope_id=? AND acknowledged_by IS NULL ORDER BY seq LIMIT ?""", (campaign["generation"], scope_id, PACKET_EVENTS)))
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
    _working_context(conn, packet, heads, focuses, selected, selected_records, sampled_work)
    return packet


def _compaction(conn, packet):
    previous = packet["sources"].get("summary")
    metadata = json.loads(conn.execute("SELECT metadata FROM sources WHERE id=?", (previous["id"],)).fetchone()[0]) if previous else {}
    scope = {"source_ids": [previous["id"]] if previous else [], "sources": [],
             "input_bytes": len(previous["text"].encode("utf-8")) if previous else 0,
             "maximum_summary_bytes": 4096,
             "archive_after": metadata.get("archive_after", 0),
             "archive_index_bytes": metadata.get("archive_index_bytes", 0),
             "coverage": "Only listed prefixes and the prior derived summary; originals remain exactly retrievable"}
    packet.update(instructions="""Produce one concise derived summary of the supplied archive prefixes and prior summary.
Preserve useful negative results, reasons and decisions. The goal, active constraints and governing
rationale are separate authority and MUST NOT be replaced. Do not perform research or experiments.
Use exact journal entry retrieval when a preview is insufficient. Semantic equivalence is not certified.
Write only the specified owned JSON result, with source_ids exactly matching compaction.source_ids.
The summary must be smaller than compaction.input_bytes and at most compaction.maximum_summary_bytes
UTF-8 bytes. Keep retrieval pointers instead of reproducing every detail. Do not launch any child provider.""",
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


def prepare(state, kind="research", *, turn_id=None, view_id=None, maintenance_id=None, scope_id="root") -> dict:
    if kind not in ("research", "reflection", "coach", "worker", "final_analysis", "report", "compact"):
        raise ValueError("Unsupported packet operation")
    turn_id = turn_id or identifier()
    with state.db.write() as conn:
        old = conn.execute("SELECT * FROM packets WHERE turn_id=?", (turn_id,)).fetchone()
        if old:
            content = json.loads(old["content"])
            if content["kind"] != kind or content.get("view_id") != view_id or content.get("scope_id", "root") != scope_id:
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
            if (kind == "worker") != (scope_id != "root"):
                raise ValueError("Worker scope and operation kind must agree")
            content = _content(state, conn, campaign, turn_id, packet_id, kind, view_id, scope_id)
            body = canonical(content)
            path = contained(state.root, Path("packets") / f"{packet_id}.json")
            conn.execute("""INSERT INTO packets(id,turn_id,generation,watermark,content,digest,path,created)
                VALUES(?,?,?,?,?,?,?,?)""", (packet_id, turn_id, campaign["generation"], content["watermark"],
                                            body.decode(), hashlib.sha256(body).hexdigest(), str(path), time.time()))
            conn.executemany("INSERT INTO packet_events(packet_id,event_id) VALUES(?,?)",
                             [(packet_id, event["id"]) for event in content["events"]])
            conn.execute("""INSERT INTO turns(id,generation,kind,packet_id,state,created,revision,source_revision,admission_order,scope_id)
                VALUES(?,?,?,?,'prepared',?,?,?,?,?)""",
                         (turn_id, campaign["generation"], kind, packet_id, time.time(), campaign["revision"],
                          hashlib.sha256(canonical(sorted(content["references"]))).hexdigest(), _queue_order(conn), scope_id))
            if kind == "coach":
                conn.execute("UPDATE stop_episodes SET state='reviewing',rounds=rounds+1 WHERE id=?",
                             (content["coaching"]["id"],))
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
