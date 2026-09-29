"""Local campaign queue, recovery, and bounded agent-driven research loop."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

from xgenius.backends import alive, inspect_payload, launch_independent, own_handle, validate_runner
from xgenius.agent_policy import AgentSession, start_session
from xgenius.db import _connect
from xgenius.scheduler import ResourceLedger
from xgenius.state import ACTIVE, TERMINAL, LocalState, identifier
from xgenius.workspace import atomic_json, collect_artifacts, digest, prepare_spec, read_json, validate_inputs


class Campaign:
    def __init__(self, config):
        if config.local is None:
            raise ValueError("This command requires schema_version = 2 local configuration")
        self.config = config
        self.local = config.local
        self.state = LocalState(config)
        self.ledger = ResourceLedger()
        self.project = Path(config.config_path).parent

    def submit(self, request: dict) -> str:
        if not isinstance(request, dict):
            raise ValueError("Job manifest must be a JSON object")
        # Replay before creating snapshots or reserving resources.
        with _connect(self.state.path) as c:
            row = c.execute("SELECT id,spec FROM attempts WHERE idempotency_key=?",
                            (request.get("key"),)).fetchone()
        if row:
            if json.loads(row["spec"])["request"] != request:
                raise ValueError("Idempotency key already used for a different request")
            return row["id"]
        if self.state.campaign()["state"] in ("stopping", "stopped", "completed", "finishing"):
            raise ValueError("Campaign is stopped/completed; resume before submitting work")
        spec = prepare_spec(self.config, request)
        validate_runner(spec["runner"], spec["gpus"])
        self.ledger.validate(spec)
        return self.state.enqueue(spec)

    def budget(self) -> dict:
        attempts = self.state.attempts()
        used = sum(a["gpu_hours"] or 0 for a in attempts if a["status"] in TERMINAL)
        reserved = sum(
            len(json.loads(a["spec"])["gpus"]) * json.loads(a["spec"])["seconds"] / 3600
            for a in attempts if a["status"] in ("starting", "running", "recovery_required"))
        return {"gpu_hours_used": used, "gpu_hours_reserved": reserved,
                "gpu_hours_limit": self.local.max_gpu_hours,
                "running_gpu_hours_estimate": sum(
                    max(0, time.time() - a["started"]) * len(json.loads(a["spec"])["gpus"]) / 3600
                    for a in attempts if a["started"] and a["status"] in ACTIVE),
                "provider_usage": None,
                "active_jobs": sum(a["status"] in ACTIVE for a in attempts)}

    def dispatch(self):
        if self.state.campaign()["state"] not in ("ready", "running", "waiting"):
            return
        with self.ledger.connect() as c:
            c.execute("UPDATE reservations SET state='paused' WHERE campaign=? AND state='queued'",
                      (self.state.id,))
        for attempt in self.state.attempts():
            if attempt["status"] != "queued":
                continue
            spec = json.loads(attempt["spec"])
            running = [json.loads(a["spec"]) for a in self.state.attempts()
                       if a["status"] in ("starting", "running", "recovery_required")]
            if (len(running) >= self.local.max_jobs
                    or sum(s["cpus"] for s in running) + spec["cpus"] > self.local.cpus
                    or sum(s["memory_mb"] for s in running) + spec["memory_mb"] > self.local.memory_mb):
                continue
            budget = self.budget()
            estimate = len(spec["gpus"]) * spec["seconds"] / 3600
            if budget["gpu_hours_used"] + budget["gpu_hours_reserved"] + estimate > self.local.max_gpu_hours:
                self.state.set_campaign("blocked", "GPU-hour admission budget exhausted")
                return
            self.ledger.register(self.state, spec)
            if not self.ledger.reserve(attempt["id"]):
                continue
            if not self.state.claim(attempt["id"]):
                if self.state.attempt(attempt["id"])["status"] in (*TERMINAL, "queued"):
                    self.ledger.finish(attempt["id"])
                continue
            root = Path(spec["root"])
            try:
                process = launch_independent(
                    [sys.executable, "-m", "xgenius.worker", str(root / "spec.json")], root)
                handle = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(),
                          "token": attempt["id"]}
                self.state.transition(attempt["id"], "starting", handle=handle)
            except Exception as e:
                self.state.transition(attempt["id"], "recovery_required",
                                      reason=f"Supervisor launch outcome uncertain: {e}")

    def reconcile(self):
        for attempt in self.state.attempts():
            if attempt["status"] in TERMINAL:
                self.ledger.finish(attempt["id"])
                continue
            if attempt["status"] == "queued":
                continue
            spec = json.loads(attempt["spec"])
            root = Path(spec["root"])
            receipt = root / "completion.json"
            handle_file = root / "supervisor.json"
            handle = (read_json(handle_file) if handle_file.exists() else
                      json.loads(attempt["handle"]) if attempt["handle"] else None)
            if handle and handle.get("token") != attempt["id"]:
                raise ValueError("Supervisor ownership mismatch")
            if alive(handle):
                continue
            if receipt.exists():
                data = read_json(receipt)
                if data.get("token") != attempt["id"]:
                    raise ValueError("Completion receipt ownership mismatch")
                reason = data["reason"]
                if data["status"] == "completed":
                    try:
                        if data.get("validation_errors"):
                            raise ValueError("; ".join(data["validation_errors"]))
                        collect_artifacts(self.state, spec)
                    except (ValueError, OSError) as e:
                        reason = f"Artifact validation failed: {e}"
                        self.state.event("validation_failed", {"attempt_id": spec["id"], "reason": reason},
                                         f'validation-{spec["id"]}')
                self.state.transition(attempt["id"], data["status"], reason=reason, receipt=data)
                self.ledger.finish(attempt["id"])
            elif inspect_payload(spec) == "dead":
                self.state.transition(attempt["id"], "interrupted",
                                      reason="Supervisor and payload exited without a completion receipt")
                self.ledger.finish(attempt["id"])
            else:
                self.state.transition(attempt["id"], "recovery_required",
                                      reason="Supervisor unavailable; payload liveness unresolved; reservation retained")

    def cancel(self, attempt_id: str):
        if self.state.transition(attempt_id, "cancelled", reason="Cancelled before dispatch", expected="queued"):
            self.ledger.finish(attempt_id)
            return
        attempt = self.state.attempt(attempt_id)
        if attempt["status"] not in TERMINAL:
            spec = json.loads(attempt["spec"])
            (Path(spec["root"]) / "cancel").touch()
            if attempt["status"] == "recovery_required":
                raise RuntimeError("Cancellation recorded; backend recovery still required before releasing capacity")

    def control(self, action: str):
        if action == "pause":
            self.state.set_campaign("paused", "Paused by user")
        elif action == "stop":
            self.state.set_campaign("stopping", "Graceful stop requested")
            for a in self.state.attempts():
                if a["status"] == "queued":
                    self.cancel(a["id"])
        elif action == "resume":
            self.state.set_campaign("running", "Resumed by user")
        else:
            raise ValueError(f"Unknown campaign control: {action}")
        if action in ("pause", "stop"):
            with self.ledger.connect() as c:
                c.execute("UPDATE reservations SET state='paused' WHERE campaign=? AND state='queued'",
                          (self.state.id,))

    def doctor(self, *, sandbox: bool = True) -> dict:
        capacity = self.ledger.capacity()
        inputs = validate_inputs(self.config)
        for runner in self.local.runners.values():
            validate_runner(asdict(runner), self.local.gpus)
        if sandbox and self.local.sandbox:
            if not self.local.copilot_home:
                raise ValueError("Sandbox mode requires an explicitly provisioned isolated agent.copilot_home")
            from xgenius.agent_policy import sandbox_preflight
            sandbox_preflight(self.config)
        return {"status": "ready", "mode": "sandbox" if self.local.sandbox else "trusted",
                "runners": list(self.local.runners),
                "inputs": list(inputs), "capacity": capacity,
                "warning": "Trusted execution is not a sandbox; shared inputs are not OS-protected"}

    def _begin_turn(self):
        events = self.state.pending_events()
        turn_id = identifier()
        directory = self.state.root / "turns" / turn_id
        directory.mkdir(parents=True)
        result_path = directory / "result.json"
        journal = self.state.root / "journal.md"
        with _connect(self.state.path) as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM turns WHERE state IN ('starting','running','maintenance')").fetchone():
                return None
            maintenance = c.execute("SELECT agent FROM campaign WHERE id=?", (self.state.id,)).fetchone()[0]
            if maintenance and alive(json.loads(maintenance)):
                return None
            c.execute("INSERT INTO turns(id,events,started,state,journal_before) VALUES(?,?,?,'starting',?)",
                      (turn_id, json.dumps([e["id"] for e in events]), time.time(),
                       digest(journal) if journal.exists() else None))
        prompt = f"""You are the research agent for this local xgenius campaign.
Read CLAUDE.md, {self.config.project.research_goal}, and `xgenius journal read` first.
Use `xgenius status --json` and `xgenius budget --json` for operational state.
Submit experiments ONLY through `xgenius submit --spec FILE --json`.
Job manifests need a stable unique key, argv array, source_files, runner, resource request,
and declared output artifacts. Reuse the same key when retrying the same submission.
Do not execute heavyweight experiments directly or start another campaign controller.
Do not modify shared inputs/environments, push commits, create issues/PRs, upload data,
or use remote compute. This is trusted local mode, not an OS sandbox.
Events for this turn:
{json.dumps(events, indent=2)}
If events you need to handle arrive after this batch, return "continue" to receive
them in the next turn. Never acknowledge an event ID outside the batch above.
When finished, append your findings and next steps with `xgenius journal write`.
Write a JSON object to {result_path} with exactly these fields:
turn_id: "{turn_id}"
acknowledged_events: array of event IDs from the batch you actually handled
disposition: "continue", "wait" (only with pending jobs), "blocked", or "complete"
reason: a non-empty explanation.
journal: ".xgenius/journal.md" (must have been updated during this turn).
Do not claim scientific success merely because a process exited zero.
"""
        atomic_json(directory / "prompt.json", {"prompt": prompt})
        process = start_session(self.config, prompt, turn_id, directory)
        with _connect(self.state.path) as c:
            c.execute("UPDATE turns SET state='running' WHERE id=?", (turn_id,))
        return turn_id, process, directory

    def _end_turn(self, turn_id, process, directory):
        try:
            if process.returncode != 0:
                raise ValueError(f"Agent exited {process.returncode}; see {directory}")
            result = read_json(directory / "result.json")
            self.state.accept_turn(turn_id, result)
            if self.state.campaign()["state"] not in ("paused", "stopping"):
                disposition = result["disposition"]
                has_work = any(a["status"] in ACTIVE for a in self.state.attempts())
                if disposition == "complete":
                    for a in self.state.attempts():
                        if a["status"] == "queued":
                            self.cancel(a["id"])
                    has_work = any(a["status"] in ACTIVE for a in self.state.attempts())
                    target = "finishing" if has_work else "completed"
                elif disposition == "wait":
                    target = ("waiting" if has_work else
                              "running" if self.state.pending_events() else "blocked")
                elif disposition == "blocked":
                    target = "blocked"
                else:
                    target = "running"
                    self.state.event("continue", {"reason": result["reason"]})
                self.state.set_campaign(target, result["reason"])
        except (ValueError, OSError) as e:
            with _connect(self.state.path) as c:
                c.execute("UPDATE turns SET state='failed',ended=?,result=? WHERE id=?",
                          (time.time(), json.dumps({"error": str(e)}), turn_id))
                c.execute("UPDATE campaign SET failures=failures+1 WHERE id=?", (self.state.id,))
            if self.state.campaign()["failures"] > self.local.retries:
                self.state.set_campaign("blocked", str(e))

    def run(self, *, no_agent: bool = False, once: bool = False):
        self.doctor(sandbox=False)
        with _connect(self.state.path) as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT controller FROM campaign WHERE id=?", (self.state.id,)).fetchone()[0]
            if existing and alive(json.loads(existing)):
                raise ValueError("A controller is already running for this campaign")
            c.execute("UPDATE campaign SET controller=?,started=COALESCE(started,?) WHERE id=?",
                      (json.dumps(own_handle(self.state.id)), time.time(), self.state.id))
        turn = None
        try:
            self.reconcile()
            with _connect(self.state.path) as c:
                old_turns = [dict(r) for r in c.execute(
                    "SELECT * FROM turns WHERE state IN ('starting','running','maintenance')")]
            if old_turns:
                if len(old_turns) == 1 and not old_turns[0]["handle"]:
                    path = self.state.root / "turns" / old_turns[0]["id"] / "agent-handle.json"
                    if path.exists():
                        handle = read_json(path)
                        if handle.get("token") != old_turns[0]["id"]:
                            raise ValueError("Agent start receipt ownership mismatch")
                        old_turns[0]["handle"] = json.dumps(handle)
                if len(old_turns) != 1 or not old_turns[0]["handle"]:
                    self.state.set_campaign("blocked", "Previous agent launch has no verified handle; recovery required")
                    return
                old = old_turns[0]
                directory = self.state.root / "turns" / old["id"]
                process = AgentSession(json.loads(old["handle"]), directory)
                if old["kind"] == "maintenance":
                    if process.poll() is None:
                        raise ValueError("A maintenance session is still active")
                    with _connect(self.state.path) as c:
                        c.execute("UPDATE turns SET state='failed',ended=? WHERE id=?",
                                  (time.time(), old["id"]))
                    self.state.event("maintenance_interrupted", {"turn_id": old["id"]})
                else:
                    turn = (old["id"], process, directory)
            if self.local.sandbox and not turn:
                from xgenius.agent_policy import sandbox_preflight
                sandbox_preflight(self.config)
            if self.state.campaign()["state"] in ("ready", "stopped"):
                self.state.set_campaign("running")
            while True:
                self.reconcile()
                campaign = self.state.campaign()
                if any(a["status"] == "recovery_required" for a in self.state.attempts()):
                    self.state.set_campaign("blocked", "Attempt recovery required; reservations retained")
                    campaign = self.state.campaign()
                active = any(a["status"] in ACTIVE for a in self.state.attempts())
                if time.time() - campaign["started"] >= self.local.max_seconds and campaign["state"] not in (
                        "stopping", "stopped", "completed"):
                    self.control("stop")
                    self.state.set_campaign("stopping", "Campaign elapsed budget exhausted")
                if turn:
                    tid, process, directory = turn
                    if process.poll() is not None:
                        self._end_turn(tid, process, directory)
                        turn = None
                campaign = self.state.campaign()
                if campaign["state"] in ("stopping", "finishing") and not active and not turn:
                    self.state.set_campaign("stopped" if campaign["state"] == "stopping" else "completed")
                    break
                if campaign["state"] in ("paused", "blocked", "completed", "stopped") and not turn:
                    break
                self.dispatch()
                if not no_agent and not turn and campaign["state"] in ("running", "waiting"):
                    with _connect(self.state.path) as c:
                        count = c.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
                    if count >= self.local.max_turns:
                        self.control("stop")
                    elif self.state.pending_events():
                        turn = self._begin_turn()
                if once or (no_agent and not any(a["status"] in ACTIVE for a in self.state.attempts())):
                    break
                time.sleep(0.25)
        except KeyboardInterrupt:
            self.control("pause")
        finally:
            with _connect(self.state.path) as c:
                c.execute("UPDATE campaign SET controller=NULL WHERE id=?", (self.state.id,))
