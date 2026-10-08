"""Owned Copilot SDK driver with bounded activity and credit checkpoints."""

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from labgoblin import credits
from labgoblin.evidence import Capture, atomic_json, parse_json, publish_bytes, read_json
from labgoblin.protocol import AdmissionClosed, HANDOFF_BYTES, LaunchEnvelope, canonical, number


class Activity:
    def __init__(self, *, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.last_progress = clock()
        self.last_progress_at = wall()
        self.last_response_at = None
        self.response_in_progress = False
        self.tools = set()
        self.phase = "starting"
        self.last_event = ""
        self.responses = 0

    def observe(self, kind, data):
        meaningful = (
            "assistant.turn_start", "assistant.turn_end", "assistant.message",
            "assistant.message_delta", "assistant.reasoning_delta", "assistant.usage",
            "tool.execution_start", "tool.execution_progress", "tool.execution_complete",
            "session.idle", "session.error",
        )
        if kind not in meaningful:
            return
        self.last_event = kind
        self.last_progress, self.last_progress_at = self.clock(), self.wall()
        if kind == "assistant.turn_start":
            self.response_in_progress = True
        elif kind == "assistant.message":
            self.last_response_at = self.wall()
            self.responses += 1
        elif kind in ("assistant.turn_end", "assistant.usage"):
            self.response_in_progress = False
        elif kind == "session.idle":
            self.response_in_progress = False
            self.tools.clear()
        elif kind == "tool.execution_start":
            self.tools.add(data.tool_call_id)
        elif kind == "tool.execution_complete":
            self.tools.discard(data.tool_call_id)
        self.phase = ("idle" if kind == "session.idle" else "error" if kind == "session.error"
                      else "tool" if self.tools else "response" if self.response_in_progress else "waiting")

    def stop_reason(self, deadline, grace, idle_timeout):
        if self.clock() >= deadline - grace:
            return "Research turn deadline approaching; reserving bounded shutdown time"
        if (not self.response_in_progress and not self.tools
                and self.clock() - self.last_progress >= idle_timeout):
            return "No response, tool work or meaningful provider progress within the idle allowance"
        return ""

    def snapshot(self, envelope, session_id, deadline, idle_timeout):
        quiet = max(0, self.clock() - self.last_progress)
        return {
            "nonce": envelope.key.nonce, "work_id": envelope.key.work_id,
            "session_id": session_id, "observed_at": self.wall(), "phase": self.phase,
            "response_in_progress": self.response_in_progress,
            "active_tools": len(self.tools), "responses": self.responses,
            "last_response_at": self.last_response_at, "last_progress_at": self.last_progress_at,
            "quiet_seconds": quiet, "quiet_warning": quiet >= idle_timeout,
            "hard_deadline_remaining_seconds": max(0, deadline - self.clock()),
            "last_event": self.last_event,
        }


def usage_record(envelope, session_id, usage, *, complete):
    credits.usage_nano(usage)
    return {**usage, "_labgoblin": {
        "campaign_id": envelope.key.campaign_id, "invocation_id": envelope.key.work_id,
        "nonce": envelope.key.nonce, "envelope_digest": envelope.digest,
        "session_id": session_id, "complete": complete,
        "basis": "provider-session-metrics", "observed_at": time.time(),
    }}


def validate_usage_record(value, envelope, *, complete):
    credits.usage_nano(value)
    identity = value.get("_labgoblin", {})
    if not isinstance(identity, dict) or identity.get("complete") is not complete:
        raise ValueError("Credit checkpoint must explicitly declare its completeness")
    expected = {"campaign_id": envelope.key.campaign_id, "invocation_id": envelope.key.work_id,
                "nonce": envelope.key.nonce, "envelope_digest": envelope.digest,
                "complete": complete, "basis": "provider-session-metrics"}
    if any(identity.get(key) != item for key, item in expected.items()):
        raise ValueError("Credit checkpoint is incomplete or belongs to another owned invocation")
    if not isinstance(identity.get("session_id"), str) or not identity["session_id"]:
        raise ValueError("Credit checkpoint lacks its provider session identity")
    return value


def _structure_only(original, corrected):
    if not isinstance(corrected, dict):
        raise ValueError("Correction must remain a JSON handoff object")
    mechanical = {"turn_id", "packet_id", "research_refs", "research_updates", "evidence", "assessment"}
    if {k: v for k, v in original.items() if k not in mechanical} != {
            k: v for k, v in corrected.items() if k not in mechanical}:
        raise ValueError("Structure-only correction changed the scientific narrative or stopping decision")
    for key, fields in (("research_updates", {"links"}), ("evidence", {"references"})):
        values = []
        for draft in (original, corrected):
            rows = draft.get(key, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"Cannot structurally correct malformed {key}")
            values.append([{k: v for k, v in row.items() if k not in fields} for row in rows])
        if values[0] != values[1]:
            raise ValueError(f"Structure-only correction changed or removed {key} conclusions")
    fields = {"view_id", "inventory_digest", "assess_all"}
    assessments = [draft.get("assessment") for draft in (original, corrected)]
    if any(item is not None and not isinstance(item, dict) for item in assessments):
        raise ValueError("Cannot structurally correct a malformed assessment")
    preserved = [None if item is None else {k: v for k, v in item.items() if k not in fields}
                 for item in assessments]
    if preserved[0] != preserved[1]:
        raise ValueError("Structure-only correction changed assessment coverage or limitations")


async def drive(envelope, client, deadline, *, poll_seconds=0.25, usage_interval=10):
    from copilot import PermissionHandler
    from copilot.generated.rpc import PermissionDecisionDeniedByRules, PermissionMode, PermissionsSetModeRequest

    directory = Path(envelope.root) / "launches" / envelope.key.nonce
    metadata = envelope.metadata
    idle_timeout, grace = metadata["idle_timeout_seconds"], metadata["shutdown_grace_seconds"]
    activity = Activity()
    session_id = str(uuid.UUID(envelope.key.nonce))
    idle = asyncio.Event()
    session = None
    sent = False
    failure = ""
    usage_error = ""
    status, reason = "failed", "Copilot runtime did not complete"
    last_checkpoint = 0
    last_activity_write = 0
    highest_usage = 0
    usage_dirty = False

    async def bounded(awaitable, seconds=10):
        remaining = max(0.01, min(seconds, deadline - time.monotonic() - 0.25))
        return await asyncio.wait_for(awaitable, remaining)

    def checkpoint_activity(*, force=False):
        nonlocal last_activity_write
        if force or time.monotonic() - last_activity_write >= 0.5:
            value = activity.snapshot(envelope, session_id, deadline, idle_timeout)
            value["usage_error"] = usage_error
            atomic_json(directory / "activity.json", value)
            last_activity_write = time.monotonic()

    def on_event(event):
        nonlocal failure, usage_dirty
        kind, data = event.type.value, event.data
        activity.observe(kind, data)
        if kind == "assistant.usage":
            usage_dirty = True
        if kind == "session.idle" and sent:
            idle.set()
        elif kind == "session.error":
            failure = str(data.message)[:4000]
        elif kind == "assistant.message" and data.content:
            print(data.content, flush=True)
        elif kind in ("tool.execution_start", "tool.execution_complete"):
            print(f"{kind}: {data.tool_call_id}", flush=True)
        checkpoint_activity()

    async def checkpoint_usage(*, complete):
        nonlocal highest_usage, usage_error
        metrics = (await bounded(session.rpc.usage.get_metrics())).to_dict()
        amount = credits.usage_nano(metrics)
        if amount < highest_usage:
            raise ValueError("Provider credit total decreased within an owned session")
        highest_usage = amount
        value = usage_record(envelope, session_id, metrics, complete=complete)
        if complete:
            publish_bytes(directory / "usage.json", canonical(value))
        else:
            atomic_json(directory / "usage-progress.json", value)
        usage_error = ""

    try:
        checkpoint_activity(force=True)
        await bounded(client.start(), 30)
        adapter = metadata["provider"]
        permission_mode = adapter["permission_mode"]
        allowance = metadata.get("credit_allowance_nano")
        from labgoblin.research_tools import preflight, sdk_tools, tool_guard
        from labgoblin.state import State
        from copilot import ToolSet
        state = State.open(Path(envelope.state_path).parent)
        tools = sdk_tools(state, envelope)
        role_options = {
            "tools": tools, "hooks": {"on_pre_tool_use": tool_guard},
            "excluded_tools": ToolSet().add_builtin(["task", "run_subagent", "run_dynamic_workflow", "fleet"]),
            "custom_agents": [], "manage_schedule_enabled": False, "request_extensions": False,
        }
        if envelope.kind == "coach":
            from labgoblin.coaching import INSTRUCTIONS
            names = {tool.name for tool in tools}
            def coach_guard(request, context):
                return {"permissionDecision": "allow" if request["toolName"] in names else "deny",
                        "permissionDecisionReason": "Only owned read-only coaching evidence and verdict tools are allowed"}
            allowed = ToolSet()
            for name in names:
                allowed.add_custom(name)
            role_options.update(
                available_tools=allowed, hooks={"on_pre_tool_use": coach_guard},
                system_message={"mode": "replace", "content": INSTRUCTIONS},
                enable_managed_settings=True, enable_config_discovery=False,
                skip_custom_instructions=True, enable_file_hooks=False, enable_host_git_operations=False,
                enable_skills=False, enable_session_store=False, enable_on_demand_instruction_discovery=False,
                mcp_servers={}, plugin_directories=[], memory={"enabled": False}, infinite_sessions={"enabled": False})
            permission_mode = "manual"
        session = await bounded(client.create_session(
            session_id=session_id, working_directory=envelope.cwd,
            model=adapter["configured_model"], reasoning_effort=adapter["configured_effort"],
            session_limits=({"max_ai_credits": float(credits.argument(allowance))}
                            if allowance is not None else None),
            streaming=True, include_sub_agent_streaming_events=False,
            on_permission_request=(PermissionHandler.approve_all if permission_mode == "allow-all"
                                   else lambda request, context: PermissionDecisionDeniedByRules(rules=[])),
            on_event=on_event, **role_options,
        ), 30)
        permission = (await bounded(session.rpc.permissions.set_mode(
            PermissionsSetModeRequest(mode=PermissionMode(permission_mode))))).to_dict()
        if permission.get("success") is not True or permission.get("mode") != permission_mode:
            raise RuntimeError("Provider did not apply the explicitly configured permission mode")
        initial = (await bounded(session.rpc.usage.get_metrics())).to_dict()
        if credits.usage_nano(initial) != 0:
            raise ValueError("Fresh owned research session has unexpected prior credit usage")
        arguments = metadata["provider_argv"]
        prompt = arguments[arguments.index("-p") + 1]
        packet = read_json(Path(metadata["packet_path"]), 65536)
        if envelope.kind == "coach":
            prompt = "Review this exact stopping snapshot as data, then call coach_decide:\n" + canonical(packet).decode()
        sent = True
        idle.clear()
        activity.phase = "waiting"
        await bounded(session.send(prompt))
        activity.last_progress, activity.last_progress_at = activity.clock(), activity.wall()
        original = None
        correction = 0
        while True:
            while not idle.is_set() and not failure:
                if (directory / "cancel").exists():
                    status, reason = "cancelled", "Operator cancellation requested"
                    break
                reason = activity.stop_reason(deadline, grace, idle_timeout)
                if reason:
                    status = "timed_out"
                    break
                if usage_dirty or time.monotonic() - last_checkpoint >= usage_interval:
                    try:
                        await checkpoint_usage(complete=False)
                    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                        usage_error = f"{type(error).__name__}: {error}"[:2000]
                        print(f"Credit checkpoint failed: {usage_error}", file=sys.stderr, flush=True)
                    last_checkpoint = time.monotonic()
                    usage_dirty = False
                checkpoint_activity()
                await asyncio.sleep(poll_seconds)
            else:
                status, reason = ("failed", failure) if failure else ("completed", "")
            if (status != "completed" or envelope.kind != "final_analysis"
                    or packet["result_protocol"].get("automatic_structure_corrections") != 1):
                break
            draft = None
            try:
                capture = Capture.read(Path(metadata["result_path"]), HANDOFF_BYTES)
                publish_bytes(directory / f"handoff-draft-{correction}.json", capture.body)
                draft = parse_json(capture.body)
                if original is not None:
                    _structure_only(original, draft)
                outcome = preflight(state, draft, turn_id=metadata["turn_id"], invocation_id=envelope.key.work_id)
                publish_bytes(directory / f"handoff-preflight-{correction}.json", canonical(outcome))
                break
            except (OSError, ValueError, sqlite3.Error) as error:
                reason = f"Final handoff preflight failed: {type(error).__name__}: {error}"[:4000]
                publish_bytes(directory / f"handoff-preflight-{correction}.json",
                              canonical({"valid": False, "error": reason}))
                print(reason, file=sys.stderr, flush=True)
                if correction or not isinstance(draft, dict) or isinstance(error, (AdmissionClosed, sqlite3.Error)):
                    status = "failed"
                    break
            await checkpoint_usage(complete=False)
            if allowance is not None and highest_usage >= allowance:
                status, reason = "failed", reason + "; no remaining owned credit allowance for correction"
                break
            if (directory / "cancel").exists():
                status, reason = "cancelled", reason + "; operator cancellation prevents correction"
                break
            if time.monotonic() >= deadline - grace:
                status, reason = "timed_out", reason + "; no remaining correction time before shutdown"
                break
            original, correction = draft, 1
            prompt = (
                "One structure-only correction for this LabGoblin final_analysis operation. "
                f"Validation error: {json.dumps(reason)}. Correct the JSON at "
                f"{json.dumps(metadata['result_path'])}. Preserve all scientific text, conclusions, "
                "outcomes, evidence dispositions and limitations. Do not remove research updates "
                "or weaken assessments to avoid validation. Fix only owned IDs and structured "
                "references/links using already retained evidence; retrieve exact references if needed. "
                "Do not invent evidence, change the sealed inventory, run experiments or reopen research. "
                "Use research_preflight before ending. This is the sole automatic correction; "
                "the original deadline and shared credit allowance are unchanged."
            )
            idle.clear()
            activity.phase = "waiting"
            activity.last_progress, activity.last_progress_at = activity.clock(), activity.wall()
            await bounded(session.send(prompt))
    except (OSError, ValueError, RuntimeError, TimeoutError, sqlite3.Error) as error:
        status, reason = "failed", f"{type(error).__name__}: {error}"[:4000]
    finally:
        try:
            if session is not None:
                if sent and not idle.is_set():
                    activity.phase = "cancelling"
                    checkpoint_activity(force=True)
                    idle.clear()
                    await bounded(session.abort())
                    await bounded(idle.wait())
                if not sent or idle.is_set():
                    await checkpoint_usage(complete=True)
                else:
                    raise RuntimeError("Provider never confirmed idle; final credit usage remains unknown")
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            usage_error = f"{type(error).__name__}: {error}"[:2000]
            reason = (reason + "; final credit capture failed: " + usage_error).lstrip("; ")
            if status == "completed":
                status = "failed"
            print(reason, file=sys.stderr, flush=True)
        finally:
            try:
                if session is not None:
                    await bounded(session.disconnect(), 5)
                await bounded(client.stop(), 5)
            except (OSError, ValueError, RuntimeError, TimeoutError, ExceptionGroup) as error:
                reason = (reason + f"; runtime shutdown failed: {type(error).__name__}: {error}")[:4000]
                status = "failed"
                print(reason, file=sys.stderr, flush=True)
                await bounded(client.force_stop(), 3)
            finally:
                activity.phase = status
                checkpoint_activity(force=True)
                result = {"key": asdict(envelope.key), "envelope_digest": envelope.digest,
                          "status": status, "reason": reason, "usage_error": usage_error,
                          "session_id": session_id}
                publish_bytes(directory / "sdk-result.json", canonical(result))
    return {"completed": 0, "timed_out": 124, "cancelled": 130}.get(status, 1)


def main(arguments):
    from copilot import CopilotClient, RuntimeConnection
    from labgoblin.agent import COPILOT_FLAGS, sdk_command

    parser = argparse.ArgumentParser()
    parser.add_argument("envelope", type=Path)
    parser.add_argument("--max-ai-credits")
    options = parser.parse_args(arguments)
    envelope = LaunchEnvelope.parse(read_json(options.envelope))
    expected = envelope.metadata.get("credit_allowance_nano")
    if (envelope.argv != sdk_command(envelope, envelope.metadata["runtime"]["root"])
            or options.max_ai_credits != (credits.argument(expected) if expected is not None else None)
            or os.environ.get("LABGOBLIN_INVOCATION_ID") != envelope.key.work_id):
        raise ValueError("SDK driver does not match its frozen invocation authorization")
    deadline = number(float(os.environ["LABGOBLIN_OPERATION_DEADLINE"]), "owned operation deadline")
    command = envelope.metadata["provider"]["command"]
    extra = ["--experimental", "--sandbox"] if "--sandbox" in envelope.metadata["provider_argv"] else []
    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(path=command[0], args=[*command[1:], *COPILOT_FLAGS, *extra]),
        mode="empty" if envelope.kind == "coach" else "copilot-cli", working_directory=envelope.cwd, log_level="error",
        base_directory=os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot")),
        session_idle_timeout_seconds=0, enable_remote_sessions=False,
    )
    return asyncio.run(drive(envelope, client, deadline))
