"""Thin SDK adapters over the same validated research services as the CLI."""

import json
import sqlite3
from pathlib import Path

from labgoblin.evidence import publish_bytes, read_json
from labgoblin.protocol import canonical, table
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
        research_memory.actor(conn, turn_id)
    operation = request.get("operation")
    if operation in ("record", "history", "source", "event", "observation") and not request.get("id"):
        raise ValueError("This read operation requires an exact ID")
    if operation == "status":
        with state.db.read() as conn:
            episodes = [dict(row) for row in conn.execute(
                "SELECT id,state,rounds,reason,result_source FROM stop_episodes ORDER BY created DESC LIMIT 10")]
        return {"campaign": state.campaign(), "budget": state.budget(), "tasks": research_tasks.listing(state),
                "coaching": episodes}
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
    if operation == "observation":
        from labgoblin.dashboard_data import observation_context
        with state.db.read() as conn:
            value = observation_context(conn, request["id"])
    elif operation == "event":
        from labgoblin.briefing import event
        value = event(state.db, request["id"], offset=request.get("offset", 0), limit=16384)
    else:
        raise ValueError("Read operation must be status, list, search, record, history, source, task, event or observation")
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


def sdk_tools(state, envelope):
    from copilot import Tool, ToolResult
    turn_id = envelope.metadata["turn_id"]
    coach = envelope.kind == "coach"

    async def handler(invocation):
        try:
            if invocation.tool_name == "research_read":
                value = read(state, invocation.arguments, turn_id=turn_id)
            elif invocation.tool_name == "research_publish" and not coach:
                value = research_memory.publish(state, invocation.arguments, turn_id=turn_id)
            elif invocation.tool_name == "research_task" and not coach:
                value = task(state, invocation.arguments, turn_id=turn_id)
            elif invocation.tool_name == "coach_decide" and coach:
                packet = read_json(Path(envelope.metadata["packet_path"]), 65536)
                value = {**invocation.arguments, "turn_id": turn_id,
                         "packet_id": envelope.metadata["packet_id"], "episode_id": packet["coaching"]["id"]}
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
        "research_read": ("Read published research knowledge, revisions, task results or status. Search includes retired ideas.",
                          {"operation": {"type": "string", "enum": ["status", "list", "search", "record", "history", "source", "task", "event", "observation"]},
                           "id": {"type": "string"}, "query": {"type": "string"}, "offset": {"type": "integer"},
                           "limit": {"type": "integer"}, "cutoff": {"type": "integer"}}, ["operation"]),
        "research_publish": ("Publish a draft knowledge/question/hypothesis/reflection revision using expected base. "
                             "Assessments go in the owned handoff, not this tool.",
                             {"id": {"type": "string"}, "request_id": {"type": "string"},
                              "base": {"type": ["string", "null"]}, "kind": {"type": "string"},
                              "title": {"type": "string"}, "body": {"type": "string"},
                              "activity": {"type": "string"}, "outcome": {"type": "string"},
                              "links": {"type": "array", "items": {"type": "object"}},
                              "provenance": {"type": "object"}}, ["id", "request_id", "title", "body"]),
        "research_task": ("Root-only bounded worker commissioning, cancellation or integration; never native subagents.",
                          {"operation": {"type": "string", "enum": ["create", "cancel", "integrate", "message", "amend"]},
                           "id": {"type": "string"}, "charter": {"type": "object"},
                           "reason": {"type": "string"}, "decision": {"type": "object"},
                           "message": {"type": "string"}, "amendment": {"type": "object"}}, ["operation"]),
        "coach_decide": ("Record this coach's process-only verdict; no scientific methods or experiments.",
                         {"decision": {"type": "string", "enum": ["request_reflection", "accept_stop", "needs_operator"]},
                          "reason": {"type": "string"}, "questions": {"type": "array", "items": {"type": "string"}},
                          "references": {"type": "array", "items": {"type": "string"}}, "limitations": {"type": "string"}},
                         ["decision", "reason", "questions", "references", "limitations"]),
    }
    names = ("research_read", "coach_decide") if coach else ("research_read", "research_publish", "research_task")
    return [Tool(name=name, description=definitions[name][0], handler=handler, skip_permission=True, defer="never",
                 parameters={"type": "object", "properties": definitions[name][1], "required": definitions[name][2],
                             "additionalProperties": False}) for name in names]


def tool_guard(request, context):
    name = request["toolName"].lower().replace("-", "_")
    if any(word in name for word in ("subagent", "sub_agent", "workflow", "fleet")) or name in ("task", "delegate", "spawn_agent"):
        return {"permissionDecision": "deny",
                "permissionDecisionReason": "Use the owned research_task API; native delegation is not accounted"}
    return {}
