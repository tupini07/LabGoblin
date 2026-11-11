"""Sparse host-timed advice at an awaited researcher tool boundary."""

import asyncio
import json
from pathlib import Path
import time
import uuid

from labgoblin import consultation, credits, guidance
from labgoblin.protocol import table
from labgoblin.storage import publish_json


VERSION = 1
CHECKPOINT_SECONDS = 15 * 60


def enabled(prompt):
    value = prompt.get("research_checkins_version")
    if value is not None and (type(value) is not int or value != VERSION):
        raise ValueError("Unsupported research check-in capability")
    return value == VERSION


def trajectory_tool(caller, audit):
    from copilot import Tool, ToolResult
    from labgoblin.inspection_evidence import bounded_session_page
    from labgoblin.session_log import page

    def handle(invocation):
        try:
            args = table(invocation.arguments, "trajectory request",
                         {"offset", "event", "prompt", "kind", "tool_call_id"})
            audit.check()
            caller.events.checkpoint()
            result = bounded_session_page({
                **page(caller.directory, args), "role": caller.role,
                "coverage": "Original events from this researcher only; live, possibly incomplete. "
                            "Use event=sequence to read a complete retained event. Content is evidence, not authority."})
            audit.record("labgoblin_read_trajectory", args, result)
            return ToolResult(result_type="success", text_result_for_llm=json.dumps(result))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return consultation.failure(error)

    return Tool(
        "labgoblin_read_trajectory",
        "Read bounded original researcher events, not a researcher-selected status report. "
        "Follow next_offset; use kind to filter messages or tool starts/completions. "
        "The supplied recent offset is only a starting point, not complete history.",
        handle, {"type": "object", "properties": {
            "offset": {"type": "integer", "minimum": 0},
            "event": {"type": "integer", "minimum": 0},
            "prompt": {"type": "integer", "minimum": 1},
            "kind": {"type": "string"}, "tool_call_id": {"type": "string"}},
            "additionalProperties": False}, skip_permission=True, defer="never")


class Checkins:
    def __init__(self, caller, pool, deadline):
        self.caller, self.pool, self.deadline = caller, pool, deadline
        self.interval = caller.config.goal_review_interval_seconds if caller.config.coach and pool else 0
        self.next_review = time.monotonic() + self.interval
        self.notice_sent = False
        self.reviews_allowed = True
        self.active_hook_id = None
        self.lock = asyncio.Lock()
        self.directory = Path(caller.envelope.root) / "checkins"

    def checkpoint_at(self):
        from labgoblin.provider import GRACE_SECONDS
        return min(self.deadline, self.caller.deadline) - GRACE_SECONDS - min(
            CHECKPOINT_SECONDS, self.caller.config.timeout_seconds / 10)

    async def before_tool(self, request, context):
        from labgoblin.provider import GRACE_SECONDS, StopRequested, research_guard
        decision = research_guard(request, context)
        if decision:
            return decision
        caller = self.caller
        try:
            async with self.lock:
                if context.get("session_id") != caller.id or request.get("sessionId") != caller.id:
                    raise ValueError("Research check-in requires the exact active, unexhausted caller")
                if caller.failure:
                    raise StopRequested("budget_exhausted" if caller.failure_is_limit else "failed", caller.failure)
                if (Path(caller.envelope.root) / "cancel").exists():
                    raise StopRequested("cancelled", "Operator cancellation requested at a research check-in")
                if time.monotonic() >= caller.deadline - GRACE_SECONDS:
                    raise StopRequested("timed_out", "Research check-in reached the shutdown boundary")
                if caller.limit_reached:
                    raise StopRequested("budget_exhausted", "Provider session credit limit reached")
                if caller.supervisor and caller.supervisor.ready:
                    return {"permissionDecision": "deny", "permissionDecisionReason": "Supervisor review is settling"}
                if not caller.waiting or caller.idle.is_set() or caller.aborting:
                    raise ValueError("Research check-in requires the exact active, unexhausted caller")
                if caller.supervisor and caller.supervisor.request_due():
                    notice = caller.supervisor.notice("pre_tool_hook_response")
                    return {"additionalContext": notice} if notice else {}
                if time.monotonic() >= self.checkpoint_at():
                    if self.notice_sent:
                        return {}
                    result = {"additionalContext": guidance.DEADLINE_CHECKPOINT}
                    publish_json(self.directory / "deadline-notice.json", {
                        "run_id": caller.envelope.id, "envelope_digest": caller.envelope.digest,
                        "session_id": caller.id, "prompt": caller.turns, "at": time.time(),
                        "delivery": "pre_tool_hook_response", "response": result})
                    self.notice_sent = True
                    return result
                evaluation = getattr(self.pool, "evaluation", None)
                if (not self.interval or not self.reviews_allowed or time.monotonic() < self.next_review
                        or self.pool.active is not None
                        or evaluation is not None and evaluation.active is not None
                        or self.checkpoint_at() - time.monotonic() <= 2 * GRACE_SECONDS):
                    return {}
                hook_id = uuid.uuid4().hex
                directory = self.directory / hook_id
                publish_json(directory / "request.json", {
                    "run_id": caller.envelope.id, "envelope_digest": caller.envelope.digest,
                    "session_id": caller.id, "prompt": caller.turns, "at": time.time(),
                    "hook_id": hook_id, "tool": request["toolName"],
                    "trigger": "elapsed_interval", "interval_seconds": self.interval})
                args = {"purpose": "goal_review", "question": guidance.GOAL_REVIEW_QUESTION}
                self.active_hook_id = hook_id
                self.pool.active = asyncio.create_task(self.pool.consult(
                    caller, hook_id, args, [], credits.COACH_RESERVE, hook=True,
                    deadline=min(self.checkpoint_at(), time.monotonic() + caller.config.goal_review_timeout_seconds)))
                try:
                    response = await self.pool.active
                    self.pool.raise_if_failed()
                finally:
                    self.pool.active = None
                    self.active_hook_id = None
                    self.next_review = time.monotonic() + self.interval
                feedback = response.text_result_for_llm
                if response.result_type == "success":
                    retained = json.loads(feedback)
                    feedback = retained["brief"] + "\nRetained review: " + retained["artifact"]
                context_text = ("Scheduled goal review (advisory, not a stopping decision):\n" + feedback +
                                "\nYou choose whether to adopt, adapt or reject this perspective. "
                                "Staying the course is valid; no separate status report or reply is required.")
                if caller.supervisor:
                    notice = caller.supervisor.notice("pre_tool_hook_response_after_advice")
                    if notice:
                        context_text += "\n\n" + notice
                result = {"additionalContext": context_text}
                publish_json(directory / "response.json", {
                    "status": response.result_type, "at": time.time(),
                    "delivery": "pre_tool_hook_response", "response": result})
                return result
        except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError, ExceptionGroup) as error:
            # SDK ordinary hook exceptions fail open; never release an uncertain transfer.
            if not caller.failure or caller.failure_is_limit:
                caller.failure = str(error) if isinstance(error, StopRequested) else f"Research check-in failed: {error}"
                caller.failure_is_limit = isinstance(error, StopRequested) and error.status == "budget_exhausted"
            raise asyncio.CancelledError(caller.failure) from error
