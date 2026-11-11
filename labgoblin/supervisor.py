"""Calendar appointments and cooperative, execution-bound human review holds."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
import json
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psutil

from labgoblin import payload
from labgoblin.protocol import LaunchEnvelope, integer, number, table, text
from labgoblin.storage import atomic_json, digest, hash_file, publish_bytes, publish_json, read_json


VERSION = 1
TASK_WAIT_SECONDS = 600
NOTICE = (
    "The scheduled human supervisor meeting is due. Stop starting new investigations. "
    "Let existing experiments finish, or checkpoint them safely and wait for their processes to exit. "
    "Keep useful results, partial-work limitations and resume pointers in your ordinary research files; "
    "then return a brief checkpoint answer. Do not kill unfinished work merely to meet this appointment. "
    "The harness will wait for tracked tasks and owned experimental processes to settle, then stop. "
    "Only an explicit human continuation can start a fresh conversation; no automatic restart or review call."
)


@dataclass(frozen=True)
class Schedule:
    time: str
    timezone: str
    start_date: str
    every_days: int = 1

    def __post_init__(self):
        text(self.time, "supervisor_review.time")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.time):
            raise ValueError("supervisor_review.time must be HH:MM in 24-hour local time")
        text(self.start_date, "supervisor_review.start_date")
        if date.fromisoformat(self.start_date).isoformat() != self.start_date:
            raise ValueError("supervisor_review.start_date must be YYYY-MM-DD")
        integer(self.every_days, "supervisor_review.every_days")
        text(self.timezone, "supervisor_review.timezone")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown IANA timezone {self.timezone!r}; Windows requires the declared tzdata dependency") from error

    @classmethod
    def parse(cls, value):
        value = table(value, "supervisor_review", cls.__dataclass_fields__)
        if not {"time", "timezone", "start_date"} <= value.keys():
            raise ValueError("supervisor_review requires time, timezone and start_date")
        return cls(**value)

    def next_after(self, timestamp):
        zone = ZoneInfo(self.timezone)
        now = datetime.fromtimestamp(timestamp, timezone.utc)
        anchor = date.fromisoformat(self.start_date)
        elapsed = max(0, (now.astimezone(zone).date() - anchor).days)
        try:
            day = anchor + timedelta(days=(elapsed // self.every_days) * self.every_days)
            while True:
                local = datetime.combine(day, wall_time.fromisoformat(self.time))
                # Spring gaps move to the first real minute; autumn folds occur only once.
                for minute in range(1441):
                    target = local + timedelta(minutes=minute)
                    candidates = [target.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                                  for fold in (0, 1)]
                    valid = [item for item in candidates
                             if item.astimezone(zone).replace(tzinfo=None) == target]
                    if valid:
                        occurrence = min(valid)
                        break
                else:
                    raise ValueError("Timezone has no valid appointment minute within one day")
                if occurrence > now:
                    return occurrence.timestamp()
                day += timedelta(days=self.every_days)
        except OverflowError as error:
            raise ValueError("Supervisor appointment exceeds the supported calendar") from error


def binding(envelope, prompt=None):
    prompt = read_json(Path(envelope.root) / "prompt.json") if prompt is None else prompt
    value = prompt.get("supervisor_review")
    if value is None:
        if envelope.config.get("supervisor_review") is not None:
            raise ValueError("Supervisor schedule lacks its frozen appointment")
        return None
    table(value, "supervisor appointment", {"version", "due_at"})
    if type(value.get("version")) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported supervisor-review capability")
    number(value.get("due_at"), "supervisor appointment due_at")
    if envelope.config.get("supervisor_review") is None:
        raise ValueError("Supervisor appointment lacks its frozen schedule")
    return value


def observation(rows, *, now=None):
    """Project immutable admissions/receipts; observation never acknowledges a meeting."""
    now = time.time() if now is None else now
    pending = None
    latest = None
    for row in rows:
        envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
        if envelope.config.get("supervisor_review") is None and pending is None:
            latest = None
            continue
        path = Path(envelope.root) / "prompt.json"
        if hash_file(path) != envelope.prompt_sha256:
            raise ValueError("Supervisor review prompt identity changed")
        prompt = read_json(path)
        reviewed = prompt.get("supervisor_reviewed")
        if reviewed is not None:
            if pending is None or reviewed != {
                    "run_id": pending["run_id"], "receipt_sha256": pending["receipt_sha256"]}:
                raise ValueError("Supervisor continuation does not bind the pending review receipt")
            pending = None
        appointment = binding(envelope, prompt)
        if appointment is None:
            latest = None
            continue
        receipt = json.loads(row["receipt"]) if row["receipt"] else None
        terminal = receipt is not None
        cutoff = row["ended"] if terminal else now
        due = cutoff >= appointment["due_at"] and (not terminal or receipt["executed"])
        request = Path(envelope.root) / "supervisor-review" / "request.json"
        notice = request.with_name("notice.json")
        checkpoint = request.with_name("checkpoint.json")
        records = {}
        for name, source in (("request", request), ("notice", notice), ("checkpoint", checkpoint)):
            if source.exists():
                item = read_json(source)
                if (item.get("run_id") != envelope.id or item.get("envelope_digest") != envelope.digest
                        or item.get("due_at") != appointment["due_at"]):
                    raise ValueError("Supervisor review evidence belongs to another execution")
                records[name] = item
        due = due or bool(records)
        provider_result = receipt.get("metadata", {}).get("provider", {}) if terminal else {}
        checkpoint_hash = provider_result.get("supervisor_checkpoint_sha256")
        if checkpoint_hash and ("checkpoint" not in records or hash_file(checkpoint) != checkpoint_hash):
            raise ValueError("Supervisor checkpoint does not match its settled receipt")
        ready = (terminal and receipt["status"] == "checkpointed" and receipt["quiescent"]
                 and bool(checkpoint_hash) and (receipt.get("usage") or {}).get("complete") is True
                 and provider_result.get("checkpoint_reason") == "supervisor_review")
        drain = request.with_name("drain.json")
        drain_status = read_json(drain) if drain.exists() else None
        if drain_status is not None and (drain_status.get("run_id") != envelope.id
                                        or drain_status.get("envelope_digest") != envelope.digest):
            raise ValueError("Supervisor drain evidence belongs to another execution")
        latest = {
            "run_id": envelope.id, "due_at": appointment["due_at"],
            "schedule": envelope.config["supervisor_review"],
            "state": ("awaiting_review" if ready else "interrupted" if terminal else
                      "waiting_for_work" if drain_status else "wrapping_up" if "notice" in records
                      else "checkpoint_requested")
                     if due else "finished_before_meeting" if terminal else "scheduled",
            "notice_delivered": "notice" in records, "checkpoint_retained": "checkpoint" in records,
            "requires_review": due,
            "drain": drain_status,
            "receipt_sha256": digest(receipt) if receipt else None,
            "continuation": f"labgoblin run --resume --reviewed {envelope.id}",
        }
        if due:
            pending = latest
    return pending or latest


def continuation(rows, reviewed, *, resume):
    current = observation(rows)
    if current and current["requires_review"]:
        if not resume or reviewed != current["run_id"] or current["receipt_sha256"] is None:
            raise ValueError("Supervisor review requires a settled execution and explicit continuation: " +
                             current["continuation"])
        return {"run_id": reviewed, "receipt_sha256": current["receipt_sha256"]}
    if reviewed is not None:
        raise ValueError("--reviewed must name the exact pending supervisor review")
    return None


class Meeting:
    def __init__(self, caller, appointment):
        self.caller = caller
        self.due_at = appointment["due_at"]
        self.directory = Path(caller.envelope.root) / "supervisor-review"
        self.requested = False
        self.notified = False
        self.ready = False
        self.tools = set()
        self.epoch = 0
        self.controls = []
        self.last_drain = None
        self.task_wait = None
        self.task_wait_epoch = None
        self.checkpoint_sha256 = None

    def record(self, name, **values):
        publish_json(self.directory / name, {
            "run_id": self.caller.envelope.id, "envelope_digest": self.caller.envelope.digest,
            "due_at": self.due_at, "at": time.time(), **values})

    def request_due(self):
        if not self.requested and time.time() >= self.due_at:
            self.record("request.json", session_id=self.caller.id)
            self.requested = True
        return self.requested

    def notice(self, delivery):
        if not self.request_due() or self.notified:
            return ""
        self.record("notice.json", session_id=self.caller.id, prompt=self.caller.turns,
                    delivery=delivery, text=NOTICE)
        self.notified = True
        return NOTICE

    def event(self, kind, data):
        if kind in ("assistant.turn_start", "tool.execution_start"):
            self.caller.idle.clear()
        if kind in ("assistant.turn_start", "session.idle", "session.background_tasks_changed",
                    "tool.execution_start", "tool.execution_complete"):
            self.epoch += 1
        if kind in ("tool.execution_start", "tool.execution_complete"):
            tool_id = text(getattr(data, "tool_call_id", None), "supervisor tool call identity")
            if kind == "tool.execution_start":
                self.tools.add(tool_id)
            else:
                self.tools.discard(tool_id)

    async def start(self):
        self.controls = payload.tree_members(Path(self.caller.envelope.root) / "process",
                                             self.caller.envelope.id)
        if await self.tasks():
            raise ValueError("A fresh supervisor-enabled session must have no background tasks")
        self.record("controls.json", processes=self.controls)

    async def tasks(self):
        try:
            tasks = (await self.caller.bounded(self.caller.session.rpc.tasks.list())).to_dict()
        except (AssertionError, KeyError, TypeError) as error:
            raise ValueError(f"Invalid SDK background-task listing: {error}") from error
        items = table(tasks, "background task listing").get("tasks")
        if not isinstance(items, list) or len(items) > 4096:
            raise ValueError("Supervisor review needs a bounded background-task listing")
        for item in items:
            table(item, "background task")
            text(item.get("id"), "background task identity")
            text(item.get("status"), "background task status")
        return items

    async def drained(self):
        try:
            return await self._drained()
        except (OSError, ValueError, RuntimeError, TimeoutError, psutil.Error) as error:
            atomic_json(self.directory / "drain.json", {
                "run_id": self.caller.envelope.id, "envelope_digest": self.caller.envelope.digest,
                "due_at": self.due_at, "at": time.time(),
                "error": f"{type(error).__name__}: {error}", "quiescence": "unverified"})
            self.last_drain = None
            return False

    async def wait_for_tasks(self):
        started = time.monotonic()
        remaining = max(.1, self.caller.deadline - started)
        try:
            await self.caller.bounded(
                self.caller.session.rpc.tasks.wait_for_pending(timeout=remaining), remaining)
        except (AssertionError, KeyError, TypeError) as error:
            raise ValueError(f"Invalid SDK background-task wait: {error}") from error
        # The RPC's empty result also represents its internal timeout, not just drain.
        return time.monotonic() - started < TASK_WAIT_SECONDS - 1

    async def _drained(self):
        caller, epoch = self.caller, self.epoch
        if self.task_wait is None or self.task_wait.done() and self.task_wait_epoch != epoch:
            if self.task_wait is not None:
                self.task_wait.result()
            self.task_wait_epoch = epoch
            self.task_wait = asyncio.create_task(self.wait_for_tasks())
            return False
        if not self.task_wait.done():
            return False
        if not self.task_wait.result():
            self.task_wait = None
            raise ValueError("Background-task wait reached its internal timeout; drain must be checked again")
        active = [item["id"] for item in await self.tasks()
                  if item["status"] not in ("completed", "failed", "cancelled")]
        if active:
            self.task_wait = None
        members = payload.tree_members(Path(caller.envelope.root) / "process", caller.envelope.id)
        extra = [member for member in members if member not in self.controls]
        result = {"tasks": active, "tools": sorted(self.tools), "extra_processes": extra,
                  "idle": caller.idle.is_set(), "unchanged_events": epoch == self.epoch}
        if result != self.last_drain:
            atomic_json(self.directory / "drain.json", {
                "run_id": caller.envelope.id, "envelope_digest": caller.envelope.digest,
                "due_at": self.due_at, "at": time.time(), **result})
            self.last_drain = result
        return not (active or self.tools or extra) and result["idle"] and result["unchanged_events"]

    async def close(self):
        if self.task_wait is not None:
            if not self.task_wait.done():
                self.task_wait.cancel()
            try:
                await self.task_wait
            except asyncio.CancelledError:
                pass

    async def checkpoint(self):
        from labgoblin.provider import GRACE_SECONDS, StopRequested
        caller = self.caller
        if not self.request_due():
            return
        while True:
            if (Path(caller.envelope.root) / "cancel").exists():
                raise StopRequested("cancelled", "Operator cancellation during supervisor checkpoint")
            if time.monotonic() >= caller.deadline - GRACE_SECONDS:
                raise StopRequested("timed_out", "Supervisor checkpoint reached the execution shutdown boundary")
            if caller.failure:
                raise StopRequested("budget_exhausted" if caller.failure_is_limit else "failed", caller.failure)
            if caller.limit_reached:
                raise StopRequested("budget_exhausted", "Provider session credit limit reached")
            if await self.drained():
                retained = caller.directory / f"answer-{caller.turns}.txt"
                response = self.directory / "response.txt"
                publish_bytes(response, caller.answer.encode("utf-8"))
                self.record("checkpoint.json", session_id=caller.id, prompt=caller.turns,
                            answer_sha256=hash_file(retained), latest_response_sha256=hash_file(response),
                            coverage="Researcher idle; tracked tasks and non-control owned processes exited. "
                                     "answer_sha256 identifies the ordinary retained answer; response.txt retains "
                                     "the latest response, including any later automatic follow-up. "
                                     "Retained files are not independently validated scientific checkpoints.")
                self.checkpoint_sha256 = hash_file(self.directory / "checkpoint.json")
                self.ready = True
                raise StopRequested("checkpointed", "Scheduled supervisor review: research is stopped for human discussion, "
                                    "not scientifically complete. Explicit reviewed continuation is required.")
            await asyncio.sleep(.2)
