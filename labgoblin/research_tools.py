"""Thin SDK adapters over the same validated research services as the CLI."""

import base64
import json
import sqlite3
from pathlib import Path

from labgoblin.evidence import contained, observation_page, publish_bytes, read_json
from labgoblin.protocol import Handoff, LaunchEnvelope, canonical, handoff_schema, integer, table
from labgoblin import journal, research_memory, research_tasks


def capabilities():
    import inspect
    from copilot import CopilotClient, ToolSet
    required = {"tools", "available_tools", "excluded_tools", "hooks", "custom_agents", "session_limits",
                "enable_config_discovery", "enable_file_hooks", "skip_custom_instructions", "request_extensions"}
    missing = required - set(inspect.signature(CopilotClient.create_session).parameters)
    if missing or "mode" not in inspect.signature(CopilotClient).parameters:
        raise ValueError(f"Installed Copilot SDK cannot enforce research role tools: {sorted(missing)}")
    ToolSet().add_builtin("task").add_custom("research_read")
    return {"sdk_role_controls": "available", "probe": "Local SDK API inspection; no session or model call",
            "managed_scopes": "flat only", "native_execution": "trusted, not filesystem-isolated"}


def read(state, request, *, turn_id=None):
    request = table(request, "research read", {"operation", "id", "query", "offset", "limit", "cutoff"})
    with state.db.read() as conn:
        owner = research_memory.actor(conn, turn_id)
    operation = request.get("operation")
    if operation in ("record", "history", "source", "event", "observation", "artifact") and not request.get("id"):
        raise ValueError("This read operation requires an exact ID")
    if operation == "status":
        with state.db.read() as conn:
            episodes = [dict(row) for row in conn.execute(
                "SELECT id,state,rounds,reason,result_source FROM stop_episodes ORDER BY created DESC LIMIT 10")]
        return {"campaign": state.campaign(), "budget": state.budget(), "tasks": research_tasks.listing(state),
                "coaching": episodes}
    if operation == "work":
        from labgoblin.briefing import work
        if request.get("id") or request.get("query") or request.get("cutoff") is not None:
            raise ValueError("Work is a fresh offset/limit sample; use task/event reads for exact details, not a historical cutoff")
        value = work(state, scope_id=owner["scope_id"], offset=request.get("offset", 0), limit=request.get("limit", 12))
        if turn_id:
            for section in ("attempts", "tasks"):
                for item in value[section]["records"]:
                    state.retrieved(turn_id, item["id"])
        return value
    if operation == "memos":
        from labgoblin.supervisor_memos import get, status
        with state.db.read() as conn:
            value = (get(conn, request["id"]) if request.get("id") else
                     status(conn, offset=request.get("offset", 0), limit=request.get("limit", 8)))
        if turn_id:
            for ref in ([value["id"]] if request.get("id") else
                        [ref for row in value["requests"] for ref in (row["id"], row["content"]["reference"]) if ref]):
                state.retrieved(turn_id, ref)
        return value
    if operation in ("list", "search"):
        return research_memory.index(state, **{k: v for k, v in request.items()
                                              if k in ("query", "offset", "limit", "cutoff")})
    if operation == "record":
        value = research_memory.read(state, request["id"])
        if turn_id:
            state.retrieved(turn_id, value["source_id"])
        return value
    if operation == "history":
        return research_memory.history(state, request["id"], offset=request.get("offset", 0), limit=request.get("limit", 20))
    if operation == "source":
        value = journal.entry_page(state.db, request["id"], offset=request.get("offset", 0),
                                   limit=request.get("limit", 8192))
        if turn_id:
            state.retrieved(turn_id, request["id"])
        return value
    if operation == "task":
        if not request.get("id"):
            return research_tasks.listing(state, offset=request.get("offset", 0), limit=request.get("limit", 20))
        value = research_tasks.get(state, request["id"])
        if turn_id:
            state.retrieved(turn_id, request["id"])
        return value
    if operation == "artifact":
        limit = integer(request.get("limit", 8192), "artifact byte page size")
        if limit > 16384:
            raise ValueError("SDK artifact byte pages cannot exceed 16 KiB; use file export for full artifacts")
        value = observation_page(state.db, request["id"], offset=request.get("offset", 0), limit=limit)
        body = value.pop("body")
        if body is not None:
            value["base64"] = base64.b64encode(body).decode("ascii")
    elif operation == "observation":
        from labgoblin.dashboard_data import observation_context
        with state.db.read() as conn:
            value = observation_context(conn, request["id"])
    elif operation == "event":
        from labgoblin.briefing import event
        value = event(state.db, request["id"], offset=request.get("offset", 0), limit=16384)
    else:
        raise ValueError("Read operation must be status, work, memos, list, search, record, history, source, task, event, observation or artifact")
    if turn_id:
        state.retrieved(turn_id, request["id"])
    return value


def task(state, request, *, turn_id=None):
    request = table(request, "research task operation", {"operation", "id", "charter", "reason", "decision", "message", "amendment"})
    action = request.get("operation")
    required = {"create": ("charter",), "cancel": ("id", "reason"), "integrate": ("id", "decision"),
                "message": ("id", "message"), "amend": ("id", "amendment")}
    if action not in required or any(key not in request for key in required[action]):
        raise ValueError("Task operation is unknown or missing its required fields")
    if action == "create":
        return research_tasks.create(state, request["charter"], turn_id=turn_id)
    if action == "cancel":
        return research_tasks.cancel(state, request["id"], request["reason"], turn_id=turn_id)
    if action == "integrate":
        return research_tasks.integrate(state, request["id"], request["decision"], turn_id=turn_id)
    if action == "message":
        return research_tasks.message(state, request["id"], request["message"], turn_id=turn_id)
    if action == "amend":
        return research_tasks.amend(state, request["id"], request["amendment"], turn_id=turn_id)
    raise ValueError("Task operation must be create, cancel, integrate, message or amend")


def preflight(state, value, *, turn_id, invocation_id):
    handoff = Handoff.parse(value)
    if not turn_id or handoff.turn_id != turn_id or not invocation_id:
        raise ValueError("Preflight requires the calling turn and invocation's exact owned handoff")
    return state.preflight_handoff(handoff, invocation_id=invocation_id)


def complete(state, value, *, turn_id, invocation_id):
    from dataclasses import asdict
    with state.db.read() as conn:
        row = conn.execute("""SELECT t.packet_id,l.envelope FROM turns t
            JOIN invocations i ON i.turn_id=t.id JOIN launches l ON l.nonce=i.nonce
            WHERE t.id=? AND i.id=? AND i.kind=t.kind""", (turn_id, invocation_id)).fetchone()
    if not row:
        raise ValueError("Completion requires this exact owned turn and invocation")
    value = dict(table(value, "owned completion"))
    for name, expected in (("turn_id", turn_id), ("packet_id", row["packet_id"])):
        if name in value and value[name] != expected:
            raise ValueError(f"Completion {name} differs from the calling owned operation")
        value[name] = expected
    handoff = Handoff.parse(value)
    state.preflight_handoff(handoff, invocation_id=invocation_id)
    envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
    path = contained(state.root, envelope.metadata["result_path"])
    body = canonical(asdict(handoff))
    try:
        existing = read_json(path, 16384)
    except FileNotFoundError:
        publish_bytes(path, body)
    else:
        if canonical(asdict(Handoff.parse(existing))) != body:
            raise ValueError("Owned result already contains a different handoff; it cannot be replaced by completion")
    return {"recorded": True, "accepted": False, "turn_id": turn_id, "packet_id": row["packet_id"],
            "note": "Validated and wrote the owned handoff. End this turn; receipt capture and controller acceptance follow."}


def sdk_tools(state, envelope):
    from copilot import Tool, ToolResult
    turn_id = envelope.metadata["turn_id"]
    coach = envelope.kind == "coach"
    strategy = (coach and read_json(Path(envelope.metadata["packet_path"]), 65536)
                .get("coaching", {}).get("purpose") == "strategy")

    async def handler(invocation):
        try:
            if invocation.tool_name == "research_read":
                value = read(state, invocation.arguments, turn_id=turn_id)
            elif invocation.tool_name == "research_publish" and not coach:
                value = research_memory.publish(state, invocation.arguments, turn_id=turn_id)
            elif invocation.tool_name == "research_task" and not coach:
                value = task(state, invocation.arguments, turn_id=turn_id)
            elif invocation.tool_name == "research_preflight" and not coach:
                value = preflight(state, invocation.arguments["handoff"], turn_id=turn_id,
                                  invocation_id=envelope.key.work_id)
            elif invocation.tool_name == "research_complete" and not coach:
                value = complete(state, invocation.arguments, turn_id=turn_id, invocation_id=envelope.key.work_id)
            elif invocation.tool_name == "coach_decide" and coach:
                from labgoblin.coaching import preflight as coach_preflight
                packet = read_json(Path(envelope.metadata["packet_path"]), 65536)
                value = {**invocation.arguments, "turn_id": turn_id,
                         "packet_id": envelope.metadata["packet_id"], "episode_id": packet["coaching"]["id"]}
                coach_preflight(state, value, invocation_id=envelope.key.work_id)
                publish_bytes(Path(envelope.metadata["result_path"]), canonical(value))
                value = {"recorded": True, "note": "Owned result still requires controller acceptance"}
            else:
                raise ValueError("Tool is not available to this role")
            encoded = json.dumps(value, ensure_ascii=False)
            if len(encoded.encode()) > 48000:
                raise ValueError("Result exceeds tool bound; narrow or page the request")
            return ToolResult(text_result_for_llm=encoded, result_type="success")
        except (ValueError, OSError, sqlite3.Error, KeyError) as error:
            return ToolResult(result_type="failure", error=f"{type(error).__name__}: {str(error)[:2000]}",
                              text_result_for_llm=f"{type(error).__name__}: {str(error)[:2000]}")

    definitions = {
        "research_read": ("Read published research knowledge, revisions, task results or status. Search includes retired ideas. "
                          "Work returns fresh scoped job/task status pages, not scientific outcomes. "
                          "Observation reads metadata; artifact returns a SHA-256-verified base64 byte page (offset/limit, at most 16 KiB).",
                          {"operation": {"type": "string", "enum": ["status", "work", "memos", "list", "search", "record", "history", "source", "task", "event", "observation", "artifact"]},
                           "id": {"type": "string"}, "query": {"type": "string"}, "offset": {"type": "integer"},
                           "limit": {"type": "integer"}, "cutoff": {"type": "integer"}}, ["operation"]),
        "research_publish": ("Publish a draft knowledge/question/hypothesis/reflection revision using expected base. "
                             "Assessments go in the owned handoff, not this tool.",
                             research_memory.PUBLICATION_PROPERTIES, research_memory.PUBLICATION_REQUIRED),
        "research_task": ("Root-only bounded worker commissioning, cancellation or integration; never native subagents. "
                          "Tasks share campaign credits; charter has no max_credits field. Omit charter max_seconds "
                          "to inherit the task lifetime, or use positive seconds or 'unlimited' within its configured ceiling.",
                          {"operation": {"type": "string", "enum": ["create", "cancel", "integrate", "message", "amend"]},
                           "id": {"type": "string"}, "charter": {"type": "object"},
                           "reason": {"type": "string"}, "decision": {"type": "object"},
                           "message": {"type": "string"}, "amendment": {"type": "object"}}, ["operation"]),
        "research_preflight": ("Validate the complete proposed owned handoff without publishing, acknowledging or "
                               "accepting it. Fix reported structure/reference errors before writing the result file.",
                               {"handoff": {"type": "object"}}, ["handoff"]),
        "research_complete": ("Finish this owned turn: validate and write its handoff in one action. Omit turn_id and "
                              "packet_id to use the owned identities. Correct reported errors in this same session. "
                              "After success, end the turn; do not also write a handoff file. Acceptance remains controller-owned.",
                              handoff_schema(owned=True)["properties"], handoff_schema(owned=True)["required"]),
        "coach_decide": ("Validate and record this coach's process-only verdict. Correct structure/reference errors "
                         "in this session; mentioned IDs must be retrieved before direct citation. Changed authority "
                         "cannot be repaired here. Final controller acceptance is still required.",
                         {"decision": {"type": "string", "enum": ["advise"] if strategy else
                                                               ["request_reflection", "accept_stop", "needs_operator"]},
                          "reason": {"type": "string"}, "questions": {"type": "array", "items": {"type": "string"}},
                          "references": {"type": "array", "items": {"type": "string"}}, "limitations": {"type": "string"}},
                         ["decision", "reason", "questions", "references", "limitations"]),
    }
    names = (("research_read", "coach_decide") if coach else
             ("research_read", "research_publish", "research_task", "research_preflight"))
    if envelope.kind in ("research", "reflection", "worker", "final_analysis"):
        names = (*names, "research_complete")
    return [Tool(name=name, description=definitions[name][0], handler=handler, skip_permission=True, defer="never",
                 parameters={"type": "object", "properties": definitions[name][1], "required": definitions[name][2],
                             "additionalProperties": False}) for name in names]


def tool_guard(request, context):
    name = request["toolName"].lower().replace("-", "_")
    if any(word in name for word in ("subagent", "sub_agent", "workflow", "fleet")) or name in ("task", "delegate", "spawn_agent"):
        return {"permissionDecision": "deny",
                "permissionDecisionReason": "Use the owned research_task API; native delegation is not accounted"}
    return {}
