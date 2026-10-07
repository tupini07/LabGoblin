"""Controller-owned flat research scopes, never provider-native child agents."""

import json
import os
from pathlib import Path
import sqlite3
import time

from labgoblin import credits
from labgoblin.evidence import Capture, contained, hash_file, publish_bytes, require_space
from labgoblin.protocol import BudgetExhausted, canonical, fingerprint, identifier, integer, number, strings, table, text
from labgoblin.research_memory import actor, validate_refs
from labgoblin.state import _campaign, _check_configuration_fence, _event, _json, _settings, _source


ACTIVE = ("queued", "research", "waiting")


def create(state, request, *, turn_id=None):
    request = dict(table(request, "task charter", {
        "request_id", "objective", "deliverables", "source_files", "research_refs",
        "max_invocations", "max_credits", "max_seconds", "dependencies"}))
    for field in ("request_id", "objective", "deliverables"):
        text(request.get(field), field)
    if len(canonical(request)) > 16384:
        raise ValueError("Task charter exceeds 16 KiB")
    config, fence = state.configuration()
    with state.db.read() as conn:
        owner = actor(conn, turn_id, write=True)
        if owner["scope_id"] != "root" or owner["kind"] not in ("research", "operator"):
            raise ValueError("Only the root researcher can delegate; recursive managers are not enabled")
        previous = conn.execute("SELECT id,request_digest FROM research_tasks WHERE request_id=?",
                                (request["request_id"],)).fetchone()
        if previous:
            if previous["request_digest"] != fingerprint(request):
                raise ValueError("Task request already has a different charter")
            return get(state, previous["id"])
        validate_refs(conn, request.get("research_refs", []), "support")
    settings = config.research
    if not settings.max_tasks:
        raise ValueError("Delegation is disabled; configure finite research task limits first")
    calls = integer(request.get("max_invocations", settings.task_max_invocations), "task max_invocations")
    allowance = credits.nano_limit(credits.parse_limit(
        request.get("max_credits", settings.task_max_credits), "task max_credits"))
    if allowance is None or calls > settings.task_max_invocations or allowance > credits.nano_limit(settings.task_max_credits):
        raise ValueError("Task allowance exceeds its configured finite ceiling")
    seconds = number(request.get("max_seconds", settings.task_max_seconds), "task max_seconds")
    if seconds > settings.task_max_seconds:
        raise ValueError("Task time allowance exceeds configured ceiling")
    task_id = identifier()
    root = config.root / "research" / "tasks" / task_id
    require_space(config.storage)
    publish_bytes(root / "preparation.json", canonical({"task_id": task_id, "request_id": request["request_id"],
                                                        "state": "Unpublished draft until its DB charter is committed"}))
    files = strings(request.get("source_files", []), "source_files", empty=True)
    pins, total = {}, 0
    for name in files:
        path = contained(config.root, name)
        if path.is_relative_to(config.state_dir) or any(
                part.casefold() in (".git", ".venv", "__pycache__", ".env", ".ssh") for part in path.relative_to(config.root).parts):
            raise ValueError("Task source cannot include operational state or Git metadata")
        if any(path == (config.root / value.path).resolve() or path.is_relative_to((config.root / value.path).resolve())
               for value in config.inputs.values()):
            raise ValueError("Task source snapshots cannot include declared datasets")
        capture = Capture.read(path, config.storage.snapshot_bytes)
        total += len(capture.body)
        if total > config.storage.snapshot_bytes:
            raise ValueError("Task source snapshot exceeds configured size")
        relative = str(path.relative_to(config.root))
        publish_bytes(root / "work" / relative, capture.body)
        pins[relative] = capture.digest
    (root / "work").mkdir(parents=True, exist_ok=True)
    charter = {**request, "source_hashes": pins, "max_invocations": calls,
               "max_credits": allowance / credits.NANO, "max_seconds": seconds, "parent": "root"}
    publish_bytes(root / "charter.json", canonical(charter))
    try:
        return _commit_task(state, request, charter, root, task_id, fence, settings, calls, allowance, turn_id)
    except (ValueError, OSError, sqlite3.Error) as error:
        raise ValueError(f"Task preparation was not admitted; retained draft at {root}: {error}") from error


def _commit_task(state, request, charter, root, task_id, fence, settings, calls, allowance, turn_id):
    with state.db.write() as conn:
        _check_configuration_fence(conn, fence)
        owner = actor(conn, turn_id, write=True)
        campaign = _campaign(conn)
        state._admissible(conn, campaign, "worker")
        previous = conn.execute("SELECT id,request_digest FROM research_tasks WHERE request_id=?",
                                (request["request_id"],)).fetchone()
        if previous:
            if previous["request_digest"] != fingerprint(request):
                raise ValueError("Task request already has a different charter")
            return get(state, previous["id"])
        if conn.execute("SELECT COUNT(*) FROM research_tasks WHERE generation=?", (campaign["generation"],)).fetchone()[0] >= settings.max_tasks:
            raise ValueError("Campaign task count ceiling reached")
        dependencies = strings(request.get("dependencies", []), "dependencies", empty=True)
        for dependency in dependencies:
            if not conn.execute("SELECT 1 FROM research_tasks WHERE id=? AND generation=?",
                                (dependency, campaign["generation"])).fetchone():
                raise ValueError("Task dependencies must name existing tasks in this generation")
        charter["source_id"] = _source(conn, "task_charter", canonical(charter), owner["kind"],
                                       {"scope_id": task_id, "turn_id": turn_id})
        conn.execute("""INSERT INTO research_tasks(id,request_id,request_digest,generation,authority_revision,
            charter,state,root,created,max_invocations,max_credits_nano) VALUES(?,?,?,?,?,?,'queued',?,?,?,?)""",
                     (task_id, request["request_id"], fingerprint(request), campaign["generation"],
                      campaign["authority_revision"], _json(charter), str(root), time.time(), calls, allowance))
        _event(conn, campaign["generation"], "task_start", {"task_id": task_id}, scope_id=task_id)
    return get(state, task_id)


def get(state, task_id):
    with state.db.read() as conn:
        row = conn.execute("SELECT * FROM research_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown research task")
        return {**dict(row), "charter": json.loads(row["charter"]), "charter_revision": fingerprint(json.loads(row["charter"])),
                "budget": budget(conn, task_id)}


def listing(state, *, offset=0, limit=20):
    integer(offset, "task offset", zero=True)
    integer(limit, "task page size")
    if limit > 50:
        raise ValueError("Task pages cannot exceed 50 records")
    with state.db.read() as conn:
        total = conn.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0]
        rows = [dict(row) for row in conn.execute("""SELECT id,state,reason,created,ended,result_source,
            json_extract(integrated,'$.state') AS integration_state FROM research_tasks ORDER BY created,id LIMIT ? OFFSET ?""",
                                                (limit, offset))]
    return {"tasks": rows, "total": total, "offset": offset, "next_offset": offset + len(rows),
            "has_more": offset + len(rows) < total}


def budget(conn, task_id):
    task = conn.execute("SELECT * FROM research_tasks WHERE id=?", (task_id,)).fetchone()
    rows = conn.execute("""SELECT l.envelope,l.receipt,l.phase FROM launches l JOIN invocations i ON i.nonce=l.nonce
        JOIN turns t ON t.id=i.turn_id WHERE t.scope_id=?""", (task_id,))
    value = credits.summarize(rows, task["max_credits_nano"] / credits.NANO)
    pending = conn.execute("""SELECT COALESCE(SUM(i.credit_allowance_nano),0) FROM invocations i
        JOIN turns t ON t.id=i.turn_id WHERE t.scope_id=? AND i.state='reserved'""", (task_id,)).fetchone()[0]
    value["reserved_nano"] += pending
    value["reserved"] = value["reserved_nano"] / credits.NANO
    if value["remaining_nano"] is not None:
        value["remaining_nano"] = max(0, value["remaining_nano"] - pending)
        value["remaining"] = value["remaining_nano"] / credits.NANO
    value["invocations"] = conn.execute("""SELECT COUNT(*) FROM invocations i JOIN turns t ON t.id=i.turn_id
        WHERE t.scope_id=? AND i.state NOT IN ('cancelled','not_started')""", (task_id,)).fetchone()[0]
    value["max_invocations"] = task["max_invocations"]
    return value


def assert_owned(conn, task_id, *, admission=False):
    task = conn.execute("SELECT * FROM research_tasks WHERE id=?", (task_id,)).fetchone()
    campaign = _campaign(conn)
    if (not task or task["state"] not in ACTIVE or task["generation"] != campaign["generation"]
            or task["authority_revision"] != campaign["authority_revision"]):
        raise ValueError("Task is inactive or its governing authority changed")
    if time.time() >= task["created"] + json.loads(task["charter"])["max_seconds"]:
        raise BudgetExhausted("task_time", "Task elapsed-time allowance exhausted")
    if admission:
        value = budget(conn, task_id)
        if value["invocations"] >= value["max_invocations"]:
            raise BudgetExhausted("task_invocations", "Research task invocation allowance exhausted")
        credits.available(value)
    return task


def accept_in(conn, turn, handoff, source_id, watermark):
    task = conn.execute("SELECT * FROM research_tasks WHERE id=?", (turn["scope_id"],)).fetchone()
    if not task:
        raise ValueError("Worker result has no task")
    live = conn.execute("""SELECT 1 FROM attempts WHERE scope_id=?
        AND (status IN ('queued','starting','running','recovery_required') OR collection='pending')""",
                        (task["id"],)).fetchone()
    current = _campaign(conn)
    stale = task["state"] not in ACTIVE or task["authority_revision"] != current["authority_revision"]
    message = conn.execute("""SELECT json_extract(metadata,'$.target') FROM sources
        WHERE kind='task_message' AND json_extract(metadata,'$.task_id')=? ORDER BY seq DESC LIMIT 1""",
                           (task["id"],)).fetchone()
    clarification = message and message[0] == "root" or conn.execute("""SELECT 1 FROM events
        WHERE scope_id=? AND kind='task_message' AND seq>? AND acknowledged_by IS NULL""",
                                                                  (task["id"], watermark)).fetchone()
    status = ("cancelled" if stale else "waiting" if live else
              "research" if handoff.disposition == "continue" else
              "failed" if handoff.disposition == "blocked" else "completed")
    if not live and handoff.disposition == "wait" and not stale:
        status = "waiting" if clarification else "failed"
    reason = ("Task result retained after its authority/scope changed" if stale else
              "Worker waited without pending owned work" if status == "failed" and handoff.disposition == "wait" else handoff.reason)
    artifacts = []
    paths = set()
    remaining = _settings(conn, current)["storage"]["snapshot_bytes"]
    base = json.loads(task["charter"])["source_hashes"]
    for item in handoff.task_artifacts:
        path = contained(Path(task["root"]) / "work", item["path"])
        relative = str(path.relative_to(Path(task["root"]) / "work"))
        key = os.path.normcase(relative)
        if key in paths:
            raise ValueError("Returned task artifacts contain duplicate paths")
        paths.add(key)
        capture = Capture.read(path, max(1, remaining))
        if len(capture.body) > remaining:
            raise ValueError("Returned task artifacts exceed their capture allowance")
        remaining -= len(capture.body)
        if capture.digest != item["digest"]:
            raise ValueError("Returned task artifact differs from its owned handoff digest")
        output = Path(task["root"]) / "returned" / capture.digest
        publish_bytes(output, capture.body)
        artifacts.append({"path": relative, "digest": capture.digest, "base": base.get(relative),
                          "captured": str(output), "bytes": len(capture.body)})
    conn.execute("UPDATE research_tasks SET artifacts=? WHERE id=?", (_json(artifacts), task["id"]))
    conn.execute("UPDATE research_tasks SET state=?,reason=?,result_source=?,wake_after=?,ended=? WHERE id=?",
                 (status, reason, source_id, watermark, time.time() if status not in ACTIVE else None, task["id"]))
    if status == "research":
        _event(conn, task["generation"], "continue", {"task_id": task["id"]}, scope_id=task["id"])
    elif status not in ACTIVE:
        _event(conn, task["generation"], "task_result",
               {"task_id": task["id"], "state": status, "source_id": source_id, "reason": reason})


def cancel(state, task_id, reason, *, turn_id=None):
    text(reason, "cancellation reason")
    with state.db.write() as conn:
        if actor(conn, turn_id, write=True)["scope_id"] != "root":
            raise ValueError("Only root can cancel a research task")
        task = conn.execute("SELECT * FROM research_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("Unknown task")
        conn.execute("UPDATE research_tasks SET state='cancelled',reason=?,ended=? WHERE id=?",
                     (reason, time.time(), task_id))
        attempts = [row[0] for row in conn.execute("SELECT id FROM attempts WHERE scope_id=?", (task_id,))]
        _event(conn, task["generation"], "task_result", {"task_id": task_id, "state": "cancelled", "reason": reason})
    for attempt_id in attempts:
        state.cancel_attempt(attempt_id, reason)
    signal_cancel(state, task_id)
    return get(state, task_id)


def signal_cancel(state, task_id):
    from labgoblin.evidence import atomic_json
    from labgoblin.protocol import LaunchEnvelope
    from labgoblin.worker import launch_directory
    with state.db.read() as conn:
        attempts = [row[0] for row in conn.execute("""SELECT id FROM attempts WHERE scope_id=?
            AND status IN ('queued','starting','running','recovery_required')""", (task_id,))]
        rows = list(conn.execute("""SELECT l.envelope FROM launches l JOIN invocations i ON i.nonce=l.nonce
            JOIN turns t ON t.id=i.turn_id WHERE t.scope_id=? AND l.phase IN ('armed','executing')""", (task_id,)))
    for attempt_id in attempts:
        state.cancel_attempt(attempt_id, "Owning research task retired")
    for row in rows:
        envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
        atomic_json(launch_directory(envelope) / "cancel", {"nonce": envelope.key.nonce, "requested_at": time.time()})


def reconcile(state):
    with state.db.read() as conn:
        tasks = [dict(row) for row in conn.execute("""SELECT * FROM research_tasks t
            WHERE state IN ('queued','research','waiting') OR state='cancelled' AND (
                EXISTS(SELECT 1 FROM attempts a WHERE a.scope_id=t.id
                       AND a.status IN ('queued','starting','running','recovery_required'))
                OR EXISTS(SELECT 1 FROM turns r WHERE r.scope_id=t.id AND r.state IN ('prepared','running')))""")]
        current = _campaign(conn)
    for task in tasks:
        if task["state"] == "cancelled":
            signal_cancel(state, task["id"])
            continue
        if task["state"] not in ACTIVE:
            continue
        charter = json.loads(task["charter"])
        failed = [ref for ref in charter.get("dependencies", [])
                  if get(state, ref)["state"] in ("failed", "cancelled")]
        reason = ("Task prerequisite failed or was cancelled: " + ", ".join(failed) if failed else
                  "Task governing authority changed" if task["authority_revision"] != current["authority_revision"] else
                  "Task elapsed-time allowance exhausted" if time.time() >= task["created"] + charter["max_seconds"] else "")
        if reason:
            cancel(state, task["id"], reason)


def ready(state):
    with state.db.read() as conn:
        tasks = list(conn.execute("""SELECT * FROM research_tasks WHERE state IN ('queued','research','waiting')
            AND NOT EXISTS(SELECT 1 FROM turns WHERE scope_id=research_tasks.id
            AND state IN ('prepared','running')) ORDER BY created,id"""))
        result = []
        for task in tasks:
            dependencies = json.loads(task["charter"]).get("dependencies", [])
            if any(not conn.execute("SELECT 1 FROM research_tasks WHERE id=? AND state='completed'", (ref,)).fetchone()
                   for ref in dependencies):
                continue
            if conn.execute("""SELECT 1 FROM events WHERE scope_id=? AND acknowledged_by IS NULL
                AND seq>? LIMIT 1""", (task["id"], task["wake_after"])).fetchone():
                result.append(task["id"])
        return result


def integrate(state, task_id, decision, *, turn_id=None):
    from labgoblin.processes import CampaignLease
    with CampaignLease(state.root):
        return _integrate(state, task_id, decision, turn_id=turn_id)


def _integrate(state, task_id, decision, *, turn_id=None):
    from labgoblin.evidence import atomic_bytes
    decision = dict(table(decision, "task integration", {"reason", "records", "artifacts", "validation_attempt"}))
    text(decision.get("reason"), "integration reason")
    names = strings(decision.get("artifacts", []), "integration artifacts", empty=True)
    if len(canonical(decision)) > 32768:
        raise ValueError("Integration decision exceeds 32 KiB")
    config, fence = state.configuration()
    from labgoblin.research_memory import publish_in
    with state.db.write() as conn:
        owner = actor(conn, turn_id, write=True)
        if owner["scope_id"] != "root" or owner["kind"] not in ("research", "operator"):
            raise ValueError("Only root can integrate worker findings")
        task = conn.execute("SELECT * FROM research_tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["state"] in ACTIVE:
            raise ValueError("Task is not ready for integration")
        if task["generation"] != _campaign(conn)["generation"] or task["authority_revision"] != _campaign(conn)["authority_revision"]:
            raise ValueError("Task result predates current authority; explicitly reassess it rather than integrate")
        prior = json.loads(task["integrated"]) if task["integrated"] else None
        if task["integrated"]:
            if prior["decision"] != decision:
                raise ValueError("Task already has a different integration decision")
            if prior["state"] == "complete":
                return {"id": task_id, "replayed": True, "state": "complete"}
        artifacts = {item["path"]: item for item in json.loads(task["artifacts"])}
        records = decision.get("records", [])
        if not isinstance(records, list):
            raise ValueError("Integration records must be an array")
        conn.execute("SAVEPOINT integration_records")
        for record in records:
            publish_in(conn, record, owner)
        conn.execute("ROLLBACK TO integration_records")
        conn.execute("RELEASE integration_records")
        if set(names) - set(artifacts):
            raise ValueError("Integration names an artifact not captured from this worker")
        if names:
            validation = conn.execute("SELECT status,exit_code,spec FROM attempts WHERE id=?",
                                      (decision.get("validation_attempt"),)).fetchone()
            if not validation or validation["status"] != "completed" or validation["exit_code"] != 0:
                raise ValueError("Artifact integration requires a successful owned validation attempt")
            hashes = json.loads(validation["spec"]).get("source_hashes", {})
            for name in names:
                if not any(item["sha256"] == artifacts[name]["digest"] and
                           Path(path).parts[-len(Path(name).parts):] == Path(name).parts for path, item in hashes.items()):
                    raise ValueError("Validation did not execute the exact returned artifact revision")
        for name in names:
            target = _target(config, name)
            actual = hash_file(target, config.storage.snapshot_bytes) if target.exists() else None
            allowed = (artifacts[name]["base"], artifacts[name]["digest"]) if prior else (artifacts[name]["base"],)
            if actual not in allowed:
                raise ValueError(f"Artifact integration conflict; preserve root edits: {name}")
        conn.execute("UPDATE research_tasks SET integrated=? WHERE id=?",
                     (_json({"state": "applying", "decision": decision}), task_id))
    for name in names:
        item = artifacts[name]
        target = _target(config, name)
        capture = Capture.read(contained(Path(task["root"]) / "returned", item["captured"]), config.storage.snapshot_bytes)
        if capture.digest != item["digest"]:
            raise ValueError("Returned artifact capture integrity failed; integration remains incomplete")
        actual = hash_file(target, config.storage.snapshot_bytes) if target.exists() else None
        if actual == item["digest"]:
            continue
        if actual != item["base"]:
            raise ValueError(f"Artifact changed during integration; retained partial publication intent: {name}")
        with state.db.write() as conn:
            _check_configuration_fence(conn, fence)
            actor(conn, turn_id, write=True)
            target = _target(config, name)
            actual = hash_file(target, config.storage.snapshot_bytes) if target.exists() else None
            if actual not in (item["base"], item["digest"]):
                raise ValueError(f"Artifact changed during integration; retained partial publication intent: {name}")
            if actual != item["digest"]:
                atomic_bytes(target, capture.body)
    with state.db.write() as conn:
        _check_configuration_fence(conn, fence)
        owner = actor(conn, turn_id, write=True)
        for name in names:
            if hash_file(_target(config, name), config.storage.snapshot_bytes) != artifacts[name]["digest"]:
                raise ValueError(f"Artifact changed before integration completion: {name}")
        for record in decision.get("records", []):
            publish_in(conn, record, owner)
        conn.execute("UPDATE research_tasks SET integrated=? WHERE id=?",
                     (_json({"state": "complete", "decision": decision}), task_id))
        _source(conn, "task_integration", canonical({"task_id": task_id, **decision}), owner["kind"],
                {"turn_id": turn_id, "worker_source": task["result_source"]})
    return {"id": task_id, "reason": decision["reason"], "state": "complete"}


def _target(config, name):
    target = contained(config.root, name)
    if (target.is_relative_to(config.state_dir) or
            any(part.casefold() in (".git", ".venv", ".github", ".ssh", ".env", "research") for part in target.relative_to(config.root).parts)
            or target.name.casefold() in ("claude.md", "labgoblin.toml")
            or target == config.root / config.project.research_goal
            or any(target == (config.root / value.path).resolve() or target.is_relative_to((config.root / value.path).resolve())
                   for value in config.inputs.values())):
        raise ValueError("Integration target is protected configuration, authority, input or scope data")
    return target


def message(state, task_id, body, *, turn_id=None):
    text(body, "task message")
    if len(body.encode()) > 4000:
        raise ValueError("Task message exceeds 4000 bytes")
    with state.db.write() as conn:
        owner = actor(conn, turn_id, write=True)
        task = assert_owned(conn, task_id)
        if owner["scope_id"] not in ("root", task_id) or owner["kind"] == "reflection":
            raise ValueError("Messages may address only the owned task or its root")
        target = task_id if owner["scope_id"] == "root" else "root"
        source = _source(conn, "task_message", body.encode(), owner["kind"],
                         {"task_id": task_id, "turn_id": turn_id, "target": target})
        _event(conn, task["generation"], "task_message", {"task_id": task_id, "source_id": source}, scope_id=target)
    return {"source_id": source, "target": target}


def amend(state, task_id, change, *, turn_id=None):
    table(change, "task amendment", {"base", "objective", "deliverables", "reason"})
    text(change.get("reason"), "amendment reason")
    with state.db.write() as conn:
        owner = actor(conn, turn_id, write=True)
        if owner["scope_id"] != "root" or owner["kind"] not in ("operator", "research"):
            raise ValueError("Only the root researcher may amend a task")
        task = assert_owned(conn, task_id)
        charter = json.loads(task["charter"])
        if change.get("base") != fingerprint(charter):
            raise ValueError("Task charter revision conflict")
        if (conn.execute("SELECT 1 FROM turns WHERE scope_id=? AND state IN ('prepared','running')", (task_id,)).fetchone()
                or conn.execute("""SELECT 1 FROM attempts WHERE scope_id=? AND
                    (status IN ('queued','starting','running','recovery_required') OR collection='pending')""", (task_id,)).fetchone()):
            raise ValueError("Amendments require the task's owned work to settle first; cancel to interrupt it")
        for key in ("objective", "deliverables"):
            if key in change:
                charter[key] = text(change[key], key)
        source = _source(conn, "task_amendment", canonical({"task_id": task_id, "previous": json.loads(task["charter"]), **change}),
                         owner["kind"], {"turn_id": turn_id})
        charter["amendment"] = source
        if len(canonical(charter)) > 16384:
            raise ValueError("Task charter exceeds 16 KiB")
        conn.execute("UPDATE research_tasks SET charter=? WHERE id=?", (_json(charter), task_id))
        _event(conn, task["generation"], "task_amendment", {"task_id": task_id, "source_id": source}, scope_id=task_id)
    return get(state, task_id)
