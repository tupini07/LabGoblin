"""Recovery-first, nonblocking orchestration for the local research runtime."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time

from labgoblin import agent, agent_worker, backends, briefing, journal, reporting, worker, workspace
from labgoblin.config import load_config
from labgoblin.evidence import atomic_json, require_space
from labgoblin.processes import own_handle, process_state
from labgoblin.protocol import (
    AdmissionClosed, AdmissionWait, BudgetExhausted, LaunchEnvelope,
    Resources, UncertainExecution, canonical, identifier,
)
from labgoblin.scheduler import ResourceLedger
from labgoblin.state import State


ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)


class Campaign:
    def __init__(self, config=None, *, state=None, ledger=None):
        if state is None and config is None:
            raise ValueError("Open existing state or supply a project configuration")
        self.state = state or State.open(config.state_dir)
        self.config = config
        self.fixed_config = config is not None
        self.ledger = ledger
        self.handle = None
        self.adapter = None
        self.adapter_revision = None
        self.clock_anchor = None
        self.configuration_loaded = False
        self.configuration_error = None

    def acquire(self):
        if self.handle is not None:
            return self
        existing = self.state.campaign()["controller"]
        if existing:
            liveness = process_state(json.loads(existing))
            if liveness != "dead":
                raise UncertainExecution(f"Existing controller is {liveness}; ownership was not replaced")
        handle = own_handle(identifier())
        self.state.controller(handle, expected=existing)
        self.handle = handle
        self.configuration_loaded = False
        self.configuration_error = None
        self.clock_anchor = None
        self.adapter = None
        self.adapter_revision = None
        if not self.fixed_config:
            self.config = None
        return self

    def close(self):
        if self.handle is not None:
            self.state.controller(None, expected=canonical(self.handle).decode())
            self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()

    def _error(self, errors, key, category, error, work_id=None):
        detail = f"{type(error).__name__}: {str(error)[:4000]}"
        self.state.blocker(key, category, detail, work_id)
        errors.append({"category": category, "work_id": work_id, "error": detail})

    def _turn(self):
        with self.state.db.read() as conn:
            row = conn.execute("SELECT * FROM turns WHERE state IN ('prepared','running') LIMIT 1").fetchone()
            return dict(row) if row else None

    def _invocations(self, turn_id):
        with self.state.db.read() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM invocations WHERE turn_id=? ORDER BY bundle_position", (turn_id,))]

    def _cancel_requests(self, errors):
        with self.state.db.read() as conn:
            rows = list(conn.execute("""SELECT id,nonce,cancel_requested FROM attempts
                WHERE status IN ('starting','running','recovery_required') AND cancel_requested IS NOT NULL"""))
        for row in rows:
            try:
                launch = self.state.launch(row["nonce"])
                envelope = LaunchEnvelope.parse(json.loads(launch["envelope"]))
                atomic_json(worker.launch_directory(envelope) / "cancel",
                            {"nonce": row["nonce"], "requested_at": row["cancel_requested"]})
                self.state.resolve_blocker(f"cancel-{row['id']}")
            except ERRORS as error:
                self._error(errors, f"cancel-{row['id']}", "cancel", error, row["id"])

    def reconcile(self) -> dict:
        errors = []
        self._cancel_requests(errors)
        recovered = {"recovered": [], "unresolved": []}
        try:
            recovered = worker.reconcile(self.state, backends.inspect_payload)
            self.state.resolve_blocker("resource-recovery")
        except ERRORS as error:
            self._error(errors, "resource-recovery", "recovery", error)
        self._finish_turn(errors)
        self._retire_pending_turn(no_agent=False)
        self._flush(errors)
        self.state.converge_stop()
        with self.state.db.read() as conn:
            rows = list(conn.execute("""SELECT id FROM attempts WHERE collection='pending'
                AND status IN ('completed','failed','cancelled','timed_out','interrupted','not_started')
                ORDER BY created,id LIMIT 100"""))
        collected = []
        for row in rows:
            try:
                collected.append(workspace.collect_artifacts(self.state, row["id"]))
                self.state.resolve_blocker(f"collection-{row['id']}")
            except ERRORS as error:
                self._error(errors, f"collection-{row['id']}", "collection", error, row["id"])
        if self.state.campaign()["operator_mode"] in ("stopping", "stopped"):
            self._advance_closure(errors, no_agent=True, admit=False)
        return {**recovered, "collected": collected, "errors": errors}

    def _finish_turn(self, errors):
        turn = self._turn()
        if not turn:
            return
        invocations = self._invocations(turn["id"])
        if not invocations or any(i["state"] in ("armed", "running", "uncertain", "reserved") for i in invocations):
            return
        completed = next((i for i in invocations if i["kind"] == turn["kind"] and i["state"] == "completed"), None)
        if completed:
            try:
                agent_worker.accept_result(self.state, turn["id"])
                return
            except (OSError, sqlite3.Error) as error:
                if turn["kind"] == "report":
                    self._error(errors, f"report-publication-{turn['id']}", "report_publication", error, turn["id"])
                    return
                reason = f"Owned handoff storage failed: {type(error).__name__}: {str(error)[:4000]}"
            except ERRORS as error:
                reason = f"Owned handoff rejected: {type(error).__name__}: {str(error)[:4000]}"
        else:
            reason = "; ".join(i["reason"] for i in invocations if i["reason"]) or "Provider operation did not complete"
        self.state.finish_turn(turn["id"], reason)
        errors.append({"category": "provider_result", "work_id": turn["id"], "error": reason})

    def _retire_pending_turn(self, *, no_agent):
        turn = self._turn()
        if not turn:
            return
        if any(i["kind"] == turn["kind"] and i["state"] == "completed" for i in self._invocations(turn["id"])):
            return
        current = self.state.campaign()
        with self.state.db.read() as conn:
            explicit = conn.execute("SELECT 1 FROM maintenance WHERE turn_id=? AND origin='operator' AND state='running'",
                                    (turn["id"],)).fetchone()
        closed = not explicit and (current["generation_state"] != ("sealed" if turn["kind"] == "final_analysis" else "open"))
        with self.state.db.read() as conn:
            packet = json.loads(conn.execute("SELECT content FROM packets WHERE id=?", (turn["packet_id"],)).fetchone()[0])
        if (no_agent or current["operator_mode"] not in (("ready", "running", "stopped") if explicit else ("ready", "running"))
                or turn["revision"] != current["revision"] or closed
                or packet.get("config_revision") != current["config_revision"]):
            if not any(i["state"] in ("armed", "running", "uncertain") for i in self._invocations(turn["id"])):
                self.state.finish_turn(turn["id"], "Pending inference fenced by current control/no-agent policy", cancelled=True)

    def _ledger(self):
        path, expected = self.state.ledger_identity()
        if self.ledger is None:
            self.ledger = ResourceLedger(path, expected_id=expected or None)
        if self.ledger.path != path or (expected and self.ledger.id != expected):
            raise ValueError("Controller ledger differs from the recorded campaign identity")
        if not expected:
            self.state.bind_ledger(path, self.ledger.id)
        return self.ledger

    def _grant(self, work_id, kind, resources, *, native):
        current = self.state.campaign()
        allocation = self.state.allocation(work_id, kind, {"resources": asdict(resources)}, current["revision"])
        ledger = self._ledger()
        try:
            ledger.request(allocation["token"], self.state.id, work_id, kind, resources,
                           {"kind": "campaign", "state_dir": str(self.state.root),
                            "generation": current["generation"], "revision": current["revision"]},
                           native=native)
            grant = ledger.reserve(allocation["token"])
            if grant["state"] == "rejected":
                raise ValueError(grant["reason"])
            if grant["state"] == "released":
                raise AdmissionClosed("Admission token was revoked")
            if grant["state"] != "granted":
                raise AdmissionWait(grant["reason"] or "Waiting for machine capacity")
            if not self.state.granted(grant["token"]):
                raise AdmissionWait("Concurrent admission filled the campaign envelope")
            return grant
        except AdmissionWait:
            raise
        except ERRORS:
            self.state.release_pending(allocation["token"])
            raise

    def _launch_attempt(self, attempt):
        resources = Resources(attempt["cpus"], attempt["memory_mb"], tuple(json.loads(attempt["gpus"])))
        spec = json.loads(self.state.attempt(attempt["id"])["spec"])
        runner = workspace.queued_runner(self.config, spec)
        grant = self._grant(attempt["id"], "attempt", resources, native=runner.kind == "native")
        envelope = workspace.prepare_envelope(self.state, self.config, attempt["id"], grant)
        return worker.start(self.state, envelope)

    def _launch_turn(self, turn):
        briefing.publish(self.state, turn["packet_id"])
        kinds = ("canary", turn["kind"]) if self.config.agent.sandbox else (turn["kind"],)
        self.state.reserve_invocations(turn["id"], turn["kind"], kinds)
        invocations = self._invocations(turn["id"])
        if any(i["state"] in ("armed", "running", "uncertain") for i in invocations):
            return None
        invocation = next((i for i in invocations if i["state"] == "reserved"), None)
        if invocation is None:
            return None
        grant = self._grant(turn["id"], turn["kind"], self.config.agent.resources, native=True)
        if self.adapter_revision != self.config.revision:
            self.adapter = agent.inspect_provider(self.config.agent)
            self.adapter_revision = self.config.revision
        envelope = agent.prepare(self.state, self.config, turn["id"], invocation["id"], grant, adapter=self.adapter)
        return worker.start(self.state, envelope)

    def _compensate(self, work_id):
        with self.state.db.read() as conn:
            rows = list(conn.execute("""SELECT token FROM allocations WHERE work_id=?
                AND state IN ('requested','granted')""", (work_id,)))
        for row in rows:
            self.state.release_pending(row["token"])

    def _flush(self, errors):
        try:
            worker.flush_releases(self.state)
        except ERRORS as error:
            self._error(errors, "resource-recovery", "recovery", error)

    def _owned_launches(self, work_id, kind):
        with self.state.db.read() as conn:
            if kind == "attempt":
                return list(conn.execute("SELECT nonce FROM launches WHERE work_id=? AND phase IN ('armed','executing')",
                                         (work_id,)))
            return list(conn.execute("""SELECT l.nonce FROM launches l JOIN invocations i ON i.nonce=l.nonce
                WHERE i.turn_id=? AND l.phase IN ('armed','executing')""", (work_id,)))

    def _advance_closure(self, errors, *, no_agent, admit):
        current = self.state.campaign()
        if current["generation_state"] == "open" or current["operator_mode"] == "paused" or self._turn():
            return
        if current["generation_state"] == "closed" and current["closure"]:
            return
        try:
            decision = current["closure"]
            if decision and decision["outcome"] in ("assessed", "unassessed", "needs_more_work"):
                self.state.close_generation(decision["view_id"], decision["outcome"], decision["reason"],
                                            assessed_turn=decision["assessed_turn"])
                return
            view = reporting.seal_view(self.state, kind="closure")
            with self.state.db.read() as conn:
                generation = conn.execute("SELECT * FROM generations WHERE id=?", (current["generation"],)).fetchone()
                final = conn.execute("SELECT * FROM turns WHERE id=?", (generation["final_turn"],)).fetchone()
                quiescent = self.state._quiescent(conn)
                covered = reporting.covered_finalize(conn, view["id"]) if view["metadata"]["operationally_ready"] else None
                settings = json.loads(conn.execute("SELECT content FROM configs WHERE id=?",
                                                   (current["config_revision"],)).fetchone()[0])
            if not quiescent or not view["metadata"]["operationally_ready"]:
                if current["blockers"]:
                    self.state.close_generation(view["id"], "incomplete", "Owned work or recovery remains unresolved")
                return
            if current["operator_mode"] in ("stopping", "stopped"):
                self.state.close_generation(view["id"], "incomplete", "Operator stop; no automatic final analysis")
            elif final:
                result = json.loads(final["result"]) if final["result"] else None
                if result and result["disposition"] != "finalize":
                    outcome, reason = "needs_more_work", result["reason"]
                elif result and result.get("assessment"):
                    outcome, reason = "assessed", result["reason"]
                else:
                    outcome, reason = "unassessed", final["reason"] or "Final invocation did not return complete owned inventory coverage"
                self.state.close_generation(view["id"], outcome, reason,
                                            assessed_turn=final["id"] if outcome == "assessed" else None)
            elif covered:
                self.state.close_generation(view["id"], "assessed", "Finalize turn already assessed the complete sealed inventory",
                                            assessed_turn=covered)
            else:
                budget = self.state.budget()
                elapsed, calls = budget["elapsed_admission_seconds"], budget["managed_invocations"]
                no_time = not elapsed["unlimited"] and elapsed["remaining"] == 0
                no_calls = not calls["unlimited"] and calls["remaining"] < (2 if settings["agent"]["sandbox"] else 1)
                if no_agent or no_time or no_calls:
                    reason = "Final analysis disabled by no-agent policy" if no_agent else "No final-analysis admission allowance remains"
                    self.state.close_generation(view["id"], "unassessed", reason)
                elif admit:
                    try:
                        briefing.prepare(self.state, "final_analysis", view_id=view["id"])
                    except ERRORS as error:
                        turn = self._turn()
                        if turn:
                            self.state.finish_turn(turn["id"], str(error), cancelled=True)
                        self.state.close_generation(view["id"], "unassessed", f"Final packet preparation failed: {str(error)[:2000]}")
                        errors.append({"category": "closure", "error": str(error)[:4000]})
        except ERRORS as error:
            self._error(errors, "closure", "closure", error)

    def step(self, *, no_agent=False, admit=True) -> dict:
        if self.handle is None or self.state.campaign()["controller"] != canonical(self.handle).decode():
            raise ValueError("Controller step requires its exact acquired ownership")
        recovery = self.reconcile()
        errors = list(recovery["errors"])
        if not self.configuration_loaded:
            self.configuration_loaded = True
            try:
                config = self.config if self.fixed_config else load_config(self.state.root.parent)
                self.state.configure(config, controller=self.handle)
                self.config = config
                self.state.resolve_blocker("configuration")
            except ERRORS as error:
                self.configuration_error = error
        if self.configuration_error is not None:
            self._error(errors, "configuration", "configuration", self.configuration_error)
            return {"campaign": self.state.campaign(), "recovery": recovery, "errors": errors, "started": [], "waiting": []}
        config = self.config
        clock = time.monotonic()
        minimum = self.clock_anchor[1] + clock - self.clock_anchor[0] if self.clock_anchor else None
        elapsed = self.state.tick(minimum_elapsed=minimum)
        self.clock_anchor = (clock, elapsed) if self.state.campaign()["started"] is not None else None
        self._retire_pending_turn(no_agent=no_agent)
        self._flush(errors)
        self.state.converge_stop()
        current = self.state.campaign()
        if current["operator_mode"] in ("stopping", "stopped"):
            self._advance_closure(errors, no_agent=True, admit=False)
        current = self.state.campaign()
        maintenance = self.state.pending_maintenance()
        scoped = maintenance is not None or (self._turn() is not None and self._turn()["kind"] in ("compact", "report"))
        if (current["operator_mode"] not in ("ready", "running", "stopped")
                or (current["operator_mode"] == "stopped" or current["generation_state"] == "closed") and not scoped):
            return {"campaign": current, "recovery": recovery, "errors": errors, "started": [], "waiting": []}
        try:
            require_space(config.storage)
            if current["generation_state"] == "open" and (not no_agent or (config.root / config.project.research_goal).exists()):
                journal.ingest_goal(self.state, config)
            if self._turn() is None:
                journal.ingest_notes(self.state)
            self.state.resolve_blocker("admission-storage")
            self.state.resolve_blocker("packet")
        except ERRORS as error:
            self._error(errors, "admission-storage", "storage_or_source", error)
        self._retire_pending_turn(no_agent=no_agent)
        self.state.converge_stop()
        budget = self.state.budget()
        allowance = budget["managed_invocations"]
        if allowance["unlimited"] or allowance["remaining"] >= 2 * config.agent.invocation_bundle:
            self.state.resolve_blocker("invocation-budget")
        if (not budget["elapsed_admission_seconds"]["unlimited"]
                and budget["elapsed_admission_seconds"]["remaining"] == 0):
            self.state.close_admission("Elapsed-admission budget exhausted")
            self._retire_pending_turn(no_agent=no_agent)
            self.state.converge_stop()
        current = self.state.campaign()
        started, waiting = [], []
        self._advance_closure(errors, no_agent=no_agent, admit=admit)
        current = self.state.campaign()
        if admit and not current["blockers"]:
            turn = self._turn()
            if not no_agent and turn is None and current["generation_state"] != "sealed":
                journal.automatic_compaction(self.state)
                maintenance = self.state.pending_maintenance()
                if maintenance:
                    try:
                        view_id = None
                        if maintenance["kind"] == "report":
                            view_id = reporting.seal_view(self.state, maintenance_id=maintenance["id"],
                                                          **json.loads(maintenance["options"]))["id"]
                        briefing.prepare(self.state, maintenance["kind"], maintenance_id=maintenance["id"], view_id=view_id)
                        turn = self._turn()
                    except AdmissionWait as error:
                        waiting.append({"work_id": maintenance["id"], "reason": str(error)})
                    except ERRORS as error:
                        self.state.fail_maintenance(maintenance["id"], str(error))
                        errors.append({"category": "maintenance", "work_id": maintenance["id"], "error": str(error)})
            if not no_agent and turn is None and current["generation_state"] == "open" and self.state.reasoning_due():
                try:
                    briefing.prepare(self.state)
                    turn = self._turn()
                except ERRORS as error:
                    self._error(errors, "packet", "packet", error)
            with self.state.db.read() as conn:
                attempt = conn.execute("""SELECT id,admission_order,cpus,memory_mb,gpus FROM attempts
                    WHERE status='queued' ORDER BY admission_order,id LIMIT 1""").fetchone()
            choices = []
            if attempt and current["generation_state"] == "open":
                choices.append((attempt["admission_order"], "attempt", dict(attempt)))
            if turn and not no_agent:
                choices.append((turn["admission_order"], "turn", turn))
            for _, kind, work in sorted(choices, key=lambda item: item[0]):
                try:
                    launched = self._launch_attempt(work) if kind == "attempt" else self._launch_turn(work)
                    if launched:
                        started.append(launched.key.work_id)
                except AdmissionWait as error:
                    waiting.append({"work_id": work["id"], "reason": str(error)})
                    break
                except BudgetExhausted as error:
                    self._compensate(work["id"])
                    if kind == "turn":
                        self.state.finish_turn(work["id"], str(error), cancelled=True)
                        if work["kind"] == "final_analysis":
                            with self.state.db.read() as conn:
                                view_id = conn.execute("SELECT view_id FROM generations WHERE id=?", (current["generation"],)).fetchone()[0]
                            self.state.close_generation(view_id, "unassessed", str(error))
                    if current["generation_state"] == "open" and (error.dimension == "elapsed"
                            or (error.dimension == "invocations" and self.state.campaign()["invocations"])):
                        self.state.close_admission(str(error))
                    elif error.dimension == "gpu_hours":
                        self.state.fail_unlaunched_attempt(work["id"], str(error))
                    else:
                        self._error(errors, "invocation-budget", "budget", error, work["id"])
                    waiting.append({"work_id": work["id"], "reason": str(error)})
                    break
                except AdmissionClosed as error:
                    self._compensate(work["id"])
                    waiting.append({"work_id": work["id"], "reason": str(error)})
                    if kind == "turn":
                        self.state.finish_turn(work["id"], str(error), cancelled=True)
                    break
                except UncertainExecution as error:
                    errors.append({"category": "uncertain_launch", "work_id": work["id"], "error": str(error)})
                    break
                except ERRORS as error:
                    active = self._owned_launches(work["id"], kind)
                    if active:
                        reason = f"Launch has no established durable outcome: {type(error).__name__}: {str(error)[:4000]}"
                        for row in active:
                            self.state.launch_problem(row["nonce"], reason)
                        errors.append({"category": "uncertain_launch", "work_id": work["id"], "error": reason})
                        break
                    self._compensate(work["id"])
                    reason = f"Pre-execution preparation failed: {type(error).__name__}: {str(error)[:4000]}"
                    if kind == "attempt":
                        if self.state.attempt(work["id"])["status"] == "queued":
                            self.state.fail_unlaunched_attempt(work["id"], reason)
                    else:
                        self.state.finish_turn(work["id"], reason)
                        if work["kind"] == "final_analysis":
                            with self.state.db.read() as conn:
                                view_id = conn.execute("SELECT view_id FROM generations WHERE id=?", (current["generation"],)).fetchone()[0]
                            self.state.close_generation(view_id, "unassessed", reason)
                    errors.append({"category": "preparation", "work_id": work["id"], "error": reason})
        self._retire_pending_turn(no_agent=no_agent)
        self._flush(errors)
        self.state.converge_stop()
        self._advance_closure(errors, no_agent=no_agent, admit=False)
        return {"campaign": self.state.campaign(), "recovery": recovery, "errors": errors,
                "started": started, "waiting": waiting}

    def run(self, *, no_agent=False, once=False, poll_seconds=0.25):
        with self.state.db.read() as conn:
            cutoff = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        result = self._run_owned(no_agent=no_agent, once=once, poll_seconds=poll_seconds)
        result["campaign"] = self.state.campaign()
        if no_agent:
            with self.state.db.read() as conn:
                condition = """FROM events e JOIN attempts a ON a.collection_event=e.id
                    WHERE e.seq>? AND (a.status IN ('failed','timed_out','interrupted','not_started')
                    OR a.validation='invalid' OR a.collection='failed')"""
                count = conn.execute("SELECT COUNT(*) " + condition, (cutoff,)).fetchone()[0]
                failures = [dict(row) for row in conn.execute(
                    "SELECT a.id,a.status,a.validation,a.reason,a.collection_reason " + condition + " LIMIT 100", (cutoff,))]
            result.update(failed=count, failed_work=failures)
        return result

    def _run_owned(self, *, no_agent, once, poll_seconds):
        from labgoblin.protocol import number
        number(poll_seconds, "poll interval")
        with self:
            initial = self.step(no_agent=no_agent, admit=False)
            current = self.state.campaign()
            if current["blockers"]:
                return initial
            if current["operator_mode"] == "ready" and current["generation_state"] != "closed":
                self.state.control("run", identifier(), current["revision"])
            try:
                while True:
                    result = self.step(no_agent=no_agent)
                    if once:
                        return result
                    current = self.state.campaign()
                    with self.state.db.read() as conn:
                        live = conn.execute("SELECT 1 FROM launches WHERE phase IN ('armed','executing') LIMIT 1").fetchone()
                        queued = conn.execute("SELECT 1 FROM attempts WHERE status='queued' LIMIT 1").fetchone()
                        maintenance = conn.execute("SELECT 1 FROM maintenance WHERE state='pending' AND revision=? LIMIT 1",
                                                   (current["revision"],)).fetchone()
                        settling = conn.execute("""SELECT 1 FROM attempts WHERE collection='pending'
                            AND status IN ('completed','failed','cancelled','timed_out','interrupted','not_started') LIMIT 1""").fetchone()
                        settling = settling or conn.execute("SELECT 1 FROM allocations WHERE state='release_pending' LIMIT 1").fetchone()
                    if current["blockers"] or (not live and not self._turn() and not settling and (current["operator_mode"] != "running"
                                     or current["generation_state"] == "closed"
                                     or (not queued and (no_agent or (not self.state.reasoning_due() and not maintenance))))):
                        return result
                    time.sleep(poll_seconds)
            except KeyboardInterrupt:
                current = self.state.campaign()
                if current["operator_mode"] in ("ready", "running"):
                    self.state.control("pause", identifier(), current["revision"])
                return {"campaign": self.state.campaign(), "reason": "Controller interrupted; armed work retains its deadline"}
