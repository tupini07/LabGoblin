"""Opt-in paired checks of captured candidates, not a scientific success oracle."""

import asyncio
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time
import uuid

from labgoblin import payload
from labgoblin.protocol import UncertainExecution, integer, number, table, text
from labgoblin.storage import (
    atomic_json, canonical, digest, hash_file, publish_bytes, publish_json, read_json,
    run_lock, snapshot,
)


VERSION = 1
MAX_CASES = 256
MAX_CHECKS = 256
PAGE = 4
RECORD_BYTES = 1024 * 1024
PAGE_BYTES = 16 * 1024
NOTICE = ("An operator-supplied check, not independent scientific certification. "
          "A rejected candidate does not establish exhaustion of the research goal. "
          "Regression replays and exposed confirmation cases are not fresh evidence.")
ID = re.compile(r"[a-f0-9]{32}")


def bounded(value, label):
    if len(canonical(value)) > RECORD_BYTES:
        raise ValueError(f"{label} exceeds the one-MiB record bound")
    return value


def reply(value, rows=None, offset=0):
    if rows is not None:
        while len(json.dumps(value).encode()) > PAGE_BYTES and len(value[rows]) > 1:
            value[rows].pop()
            value["next_offset"] = offset + len(value[rows])
    if len(json.dumps(value).encode()) > PAGE_BYTES:
        raise ValueError("Evaluation evidence page exceeds 16 KiB")
    return value


def exact_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("An exact evaluation or research run ID is required")
    return value


def regular(path):
    path = Path(path)
    if path.resolve(strict=True) != path or path.is_symlink() or not path.is_file():
        raise ValueError(f"Evaluation files must be regular files without aliases: {path}")
    return path


def relative(root, value):
    text(value, "relative evaluation path")
    part = Path(value)
    if part.is_absolute() or part.drive or any(item == ".." or ":" in item for item in part.parts):
        raise ValueError("Use a relative evaluation path without parent traversal")
    target = root / part
    if target.resolve(strict=True) != target or not target.is_relative_to(root):
        raise ValueError("Evaluation paths cannot follow aliases or escape their root")
    return target


def cases(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CASES:
        raise ValueError(f"An evaluation set must contain 1 to {MAX_CASES} cases")
    ids, fingerprints = set(), set()
    for item in value:
        table(item, "case", {"id", "data"})
        name = text(item.get("id"), "case id")
        if len(name.encode()) > 128 or name in ids or "data" not in item:
            raise ValueError("Case IDs must be unique, bounded, and accompanied by data")
        fingerprint = digest(item["data"])
        if fingerprint in fingerprints:
            raise ValueError("Duplicate case data is not independent evidence")
        ids.add(name)
        fingerprints.add(fingerprint)
    return value


def capture(source, root, limit):
    root.mkdir()
    result = snapshot(source, root / "files", limit, max_entries=2048)
    if result["excluded"] or not result["files"]:
        raise ValueError("Evaluation captures must be nonempty and self-contained; links, inputs, state and excluded files are not copied")
    return result["manifest_sha256"]


def verify_capture(root, expected):
    manifest = read_json(regular(root / "snapshot.json"))
    if digest(manifest) != expected:
        raise ValueError("Evaluation capture manifest changed")
    directory = root / "files"
    if directory.resolve(strict=True) != directory:
        raise ValueError("Evaluation capture directory is an alias")
    observed = set()
    visited = 0
    for parent, directories, names in os.walk(directory, followlinks=False):
        visited += len(directories) + len(names)
        if visited > 2048:
            raise ValueError("Evaluation capture inventory exceeded its bound")
        for name in directories:
            path = Path(parent) / name
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError("Evaluation capture gained a directory alias")
        for name in names:
            path = regular(Path(parent) / name)
            observed.add(str(path.relative_to(directory)))
    if observed != {item["path"] for item in manifest["files"]}:
        raise ValueError("Evaluation capture file inventory changed")
    for item in manifest["files"]:
        path = regular(relative(directory, item["path"]))
        if path.stat().st_size != item["bytes"] or hash_file(path) != item["sha256"]:
            raise ValueError(f"Evaluation capture bytes changed: {item['path']}")
    return directory


def prepare(project, directory, spec_path, limit, *, research_environment=None):
    """Capture a reviewed external kit once, before the execution is armed."""
    project, spec_path = Path(project).resolve(), Path(spec_path)
    regular(spec_path)
    if spec_path.is_relative_to(project):
        raise ValueError("Keep the evaluation specification and kit outside research")
    value = read_json(spec_path)
    table(value, "evaluation specification",
          {"version", "name", "purpose", "evaluator", "entrypoint", "baseline",
           "development", "confirmation", "metrics", "seconds", "max_capture_bytes"})
    if type(value.get("version")) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported evaluation specification version")
    for field, size in (("name", 128), ("purpose", 2000)):
        text(value.get(field), field)
        if len(value[field].encode()) > size:
            raise ValueError(f"Evaluation {field} exceeds {size} UTF-8 bytes")
    metrics = table(value.get("metrics"), "evaluation metrics")
    if not 1 <= len(metrics) <= 8 or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key) or direction not in ("minimize", "maximize")
            for key, direction in metrics.items()):
        raise ValueError("Declare 1 to 8 named metrics with minimize/maximize directions")
    seconds = number(value.get("seconds", 300), "evaluation seconds")
    if seconds < 10:
        raise ValueError("Evaluation seconds must include at least ten seconds for owned shutdown")
    bound = integer(value.get("max_capture_bytes", 64 * 1024 * 1024), "evaluation capture bytes")
    if bound > limit:
        raise ValueError("Evaluation capture allowance exceeds the campaign snapshot allowance")
    confirmations = value.get("confirmation", [])
    if not isinstance(confirmations, list) or len(confirmations) > 32:
        raise ValueError("Confirmation must be an array of at most 32 case-set paths")
    sources = {}
    for field in ("evaluator", "baseline"):
        source = relative(spec_path.parent, value.get(field))
        if not source.is_dir() or source.is_relative_to(project) or project.is_relative_to(source):
            raise ValueError("Evaluator and baseline must be separate external directories")
        sources[field] = source
    regular(relative(sources["evaluator"], value.get("entrypoint")))
    batches, seen = [], set()
    for stage, name in [("development", value.get("development")),
                        *(("confirmation", name) for name in confirmations)]:
        source = regular(relative(spec_path.parent, name))
        if source.is_relative_to(project):
            raise ValueError("Evaluation cases must be outside research")
        values = cases(read_json(source))
        fingerprints = {digest(item["data"]) for item in values}
        if seen & fingerprints:
            raise ValueError("Development and confirmation batches must contain distinct case data")
        seen.update(fingerprints)
        batches.append({"stage": stage, "cases": values})
        bounded(batches, "Evaluation case batches")
    root = Path(directory) / "evaluation-definition"
    root.mkdir()
    captures = {field: capture(source, root / field, bound) for field, source in sources.items()}
    executable = (research_environment["python"] if research_environment is not None
                  else str(Path(sys.executable).absolute()))
    definition = {
        "version": VERSION, "name": value["name"], "purpose": value["purpose"],
        "entrypoint": value["entrypoint"], "metrics": metrics, "seconds": seconds,
        "max_capture_bytes": bound, "captures": captures, "batches": batches,
        "interpreter": executable, "interpreter_sha256": hash_file(Path(executable)),
        "python_version": (research_environment["identity"]["python_version"]
                           if research_environment is not None else sys.version),
        "environment_coverage": "Captured evaluator/baseline files and interpreter; installed dependencies and OS are not frozen",
    }
    if research_environment is not None:
        definition["research_environment"] = research_environment
    publish_json(root / "definition.json", bounded(definition, "Evaluation definition"))
    state = project / ".labgoblin"
    if not (state / "evaluation.lock").exists():
        publish_bytes(state / "evaluation.lock", b"\0")
    return {"version": VERSION, "sha256": digest(definition)}


def definition(envelope):
    prompt_path = regular(Path(envelope.root) / "prompt.json")
    prompt = read_json(prompt_path)
    if hash_file(prompt_path) != envelope.prompt_sha256:
        raise ValueError("Evaluation execution prompt identity changed")
    binding = prompt.get("evaluation")
    if not binding or binding.get("version") != VERSION:
        raise ValueError("This execution did not enable a supported evaluation kit")
    root = Path(envelope.root) / "evaluation-definition"
    value = read_json(regular(root / "definition.json"))
    if digest(value) != binding["sha256"]:
        raise ValueError("Frozen evaluation definition changed")
    return value


def manifest(directory):
    root = Path(directory) / "evaluations"
    result = {}
    if not root.exists():
        return result
    if root.resolve(strict=True) != root:
        raise ValueError("Evaluation history cannot be an alias")
    entries = list(root.iterdir())
    if len(entries) > MAX_CHECKS:
        raise ValueError("Evaluation history exceeded its execution bound")
    for entry in sorted(entries):
        exact_id(entry.name)
        if not entry.is_dir() or entry.resolve(strict=True) != entry:
            raise ValueError("Invalid evaluation history directory")
        result[entry.name] = {name: hash_file(regular(entry / name))
                              for name in ("attempt.json", "preparation-error.json", "request.json", "result.json")
                              if (entry / name).exists()}
    return result


def checked_request(envelope, check):
    root = Path(envelope.root) / "evaluations" / exact_id(check)
    value = read_json(regular(root / "request.json"))
    if value.get("run") != envelope.id or value.get("envelope_sha256") != envelope.digest:
        raise ValueError("Evaluation request belongs to another execution")
    return root, value


def page(envelope, *, check=None, offset=0, expected=None):
    offset = integer(offset, "evaluation offset", zero=True)
    inventory = manifest(envelope.root)
    if expected is not None and inventory != expected:
        raise ValueError("Evaluation history differs from its owned terminal receipt")
    prompt = read_json(regular(Path(envelope.root) / "prompt.json"))
    config = definition(envelope) if "evaluation" in prompt else None
    provenance = ({"name": config["name"], "purpose": config["purpose"], "metrics": config["metrics"],
                   "captures": config["captures"], "definition_sha256": digest(config),
                   "environment_coverage": config["environment_coverage"]} if config else None)
    if check is None:
        ids = list(inventory)
        rows = []
        for item in ids[offset:offset + PAGE]:
            if "request.json" not in inventory[item]:
                rows.append(incomplete(envelope, item))
                continue
            root, request = checked_request(envelope, item)
            result = read_json(regular(root / "result.json")) if "result.json" in inventory[item] else None
            if result and result["request_sha256"] != digest(request):
                raise ValueError("Evaluation result is not bound to its retained request")
            rows.append({"check": item, "stage": request["stage"], "created": request["created"],
                         "status": result["status"] if result else "incomplete",
                         "reason": result["reason"] if result else "No settled evaluation result",
                         "summary": result.get("summary") if result else None})
        return reply({"run": envelope.id, "evaluation": provenance, "checks": rows, "total": len(ids),
                      "next_offset": offset + PAGE if offset + PAGE < len(ids) else None,
                      "coverage": NOTICE}, "checks", offset)
    exact_id(check)
    if check not in inventory:
        raise ValueError("No retained evaluation with that exact ID")
    if "request.json" not in inventory[check]:
        return reply(incomplete(envelope, check))
    root, request = checked_request(envelope, check)
    if "result.json" not in inventory[check]:
        return {"run": envelope.id, "check": check, "stage": request["stage"], "status": "incomplete",
                "reason": "No settled evaluation result; do not retry an uncertain call as fresh confirmation",
                "coverage": NOTICE}
    result = read_json(regular(root / "result.json"))
    if result["request_sha256"] != digest(request):
        raise ValueError("Evaluation result is not bound to its retained request")
    rows = result.get("comparisons", [])
    return reply({**{key: item for key, item in result.items() if key != "comparisons"},
            "run": envelope.id, "check": check, "stage": request["stage"],
            "evaluation": provenance,
            "candidate_sha256": request["candidate_sha256"], "definition_sha256": request["definition_sha256"],
            "source": request.get("source"), "comparisons": rows[offset:offset + PAGE],
            "case_count": len(request["cases"]),
            "next_offset": offset + PAGE if offset + PAGE < len(rows) else None, "coverage": NOTICE},
            "comparisons", offset)


def incomplete(envelope, check):
    root = Path(envelope.root) / "evaluations" / check
    attempt = read_json(regular(root / "attempt.json"))
    if attempt["run"] != envelope.id or attempt["envelope_sha256"] != envelope.digest:
        raise ValueError("Evaluation preparation belongs to another execution")
    error = (read_json(regular(root / "preparation-error.json"))
             if (root / "preparation-error.json").exists() else None)
    return {"run": envelope.id, "check": check, "stage": attempt["stage"], "created": attempt["created"],
            "status": "not_started" if error else "incomplete",
            "reason": error["reason"] if error else "Evaluation preparation has no settled result",
            "coverage": NOTICE}


def measurements(value, expected, metrics):
    table(value, "evaluator output", {"version", "cases"})
    if type(value.get("version")) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported evaluator result version")
    rows = value.get("cases")
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("Evaluator must return every requested case, including failures")
    found = {}
    for row in rows:
        table(row, "case result", {"id", "passed", "metrics", "diagnostic"})
        name = text(row.get("id"), "returned case ID")
        if name not in {case["id"] for case in expected} or name in found:
            raise ValueError("Evaluator returned missing, duplicate or unrequested case IDs")
        if type(row.get("passed")) is not bool:
            raise ValueError("Every case requires an explicit passed boolean")
        actual = table(row.get("metrics"), "case metrics")
        if set(actual) != set(metrics) or any(
                type(item) not in (int, float) or not math.isfinite(item) for item in actual.values()):
            raise ValueError("Every case requires exactly the declared finite numeric metrics")
        detail = text(row.get("diagnostic"), "case diagnostic", empty=True)
        if len(detail.encode()) > 1000:
            raise ValueError("Case diagnostics exceed 1000 UTF-8 bytes; retain concise discrepancies")
        found[name] = row
    return found


class Evaluator:
    def __init__(self, envelope, deadline):
        self.envelope, self.deadline = envelope, deadline
        self.directory = Path(envelope.root)
        self.project = Path(envelope.project)
        self.definition = definition(envelope)
        self.active = None
        self.fatal = None

    def source(self, run, check, *, allow_failed=False):
        from labgoblin.inspection_evidence import Evidence
        evidence = Evidence(self.project, self.envelope.project_id)
        row, envelope, _ = evidence.run(run)
        if run != self.envelope.id and row["phase"] != "quiescent":
            raise ValueError("Only this execution or a settled previous execution can supply evaluation evidence")
        receipt = json.loads(row["receipt"]) if row["receipt"] else None
        expected = receipt.get("metadata", {}).get("evaluations", {}) if receipt else None
        value = page(envelope, check=check, expected=expected)
        if value["status"] != "completed" and not (allow_failed and value["status"] == "failed"):
            raise ValueError("Select a settled check; confirmation requires a completely measured development check")
        root, request = checked_request(envelope, check)
        return envelope, root, request

    def evaluate(self, call_id, args):
        table(args, "evaluation request", {"stage", "candidate", "source_run", "source_check"})
        stage = args.get("stage")
        if stage not in ("development", "confirmation", "regression"):
            raise ValueError("Choose development, confirmation or regression")
        if stage == "development" and set(args) != {"stage", "candidate"}:
            raise ValueError("Development requires only stage and candidate")
        if stage == "confirmation" and set(args) != {"stage", "source_run", "source_check"}:
            raise ValueError("Confirmation requires an exact selected development run/check, not mutable candidate files")
        if stage == "regression" and set(args) != {"stage", "candidate", "source_run", "source_check"}:
            raise ValueError("Regression requires a candidate and the exact earlier run/check to replay")
        check = uuid.uuid5(uuid.UUID(self.envelope.id), "evaluation:" + text(call_id, "tool call ID")).hex
        root = self.directory / "evaluations" / check
        with run_lock(self.project / ".labgoblin" / "evaluation.lock", owner="evaluation check"):
            if root.exists():
                _, request = checked_request(self.envelope, check)
                if request["arguments"] != args:
                    raise ValueError("Evaluation tool-call identity was reused with different arguments")
                if not (root / "result.json").exists():
                    raise ValueError("Prior evaluation outcome is incomplete; no automatic retry or fresh holdout reuse")
                return page(self.envelope, check=check)
            if len(manifest(self.directory)) >= MAX_CHECKS:
                raise ValueError("This execution reached its retained evaluation-check bound")
            config = definition(self.envelope)
            if config != self.definition:
                raise ValueError("Admitted evaluation configuration changed")
            bundle = self.directory / "evaluation-definition"
            for field in ("evaluator", "baseline"):
                verify_capture(bundle / field, config["captures"][field])
            environment = config.get("research_environment")
            if environment is not None:
                from labgoblin.research_environment import check as check_environment, variables
                check_environment(self.project, environment)
            # Resolving a venv's Python symlink would discard its environment.
            interpreter = Path(config["interpreter"])
            if hash_file(interpreter) != config["interpreter_sha256"]:
                raise ValueError("The evaluation interpreter changed after admission")
            selected = None
            if stage != "development":
                source_envelope, source_root, source_request = self.source(
                    args["source_run"], args["source_check"], allow_failed=stage == "regression")
                selected = {"run": source_envelope.id, "check": args["source_check"],
                            "request_sha256": digest(source_request)}
            if stage == "confirmation":
                if source_request["stage"] != "development" or source_request["definition_sha256"] != digest(config):
                    raise ValueError("Confirmation must select development under this exact evaluator/baseline definition")
                candidate = verify_capture(source_root / "candidate", source_request["candidate_sha256"])
            else:
                from labgoblin.inspection_evidence import safe_path
                candidate = safe_path(self.project, args["candidate"])
                if not candidate.is_dir():
                    raise ValueError("Candidate must be a self-contained research directory")
            exposures_path = self.project / ".labgoblin" / "evaluation-exposures.json"
            exposures = table(read_json(regular(exposures_path)), "evaluation exposures") if exposures_path.exists() else {}
            if stage == "regression":
                values = cases(source_request["cases"])
            elif stage == "development":
                values = config["batches"][0]["cases"]
            else:
                fresh = [batch for batch in config["batches"] if batch["stage"] == "confirmation"
                         and not any(digest(case["data"]) in exposures for case in batch["cases"])]
                if not fresh:
                    raise ValueError("No unexposed confirmation batch remains; use development/regression or prepare new cases for a later execution")
                values = fresh[0]["cases"]
            if len(set(exposures) | {digest(case["data"]) for case in values}) > 8192:
                raise ValueError("Evaluation exposure history reached its retained case bound; do not reset it")
            until = min(self.deadline() - 10, time.monotonic() + config["seconds"] - 10)
            if until <= time.monotonic() or (self.directory / "cancel").exists():
                raise ValueError("Evaluation cannot start at the execution deadline or after cancellation")
            root.mkdir(parents=True)
            publish_json(root / "attempt.json", {
                "run": self.envelope.id, "envelope_sha256": self.envelope.digest,
                "stage": stage, "arguments": args, "created": time.time()})
            try:
                candidate_hash = capture(candidate, root / "candidate", config["max_capture_bytes"])
            except (OSError, ValueError, RuntimeError) as error:
                publish_json(root / "preparation-error.json", {"reason": f"{type(error).__name__}: {error}"})
                raise
            if stage == "confirmation" and candidate_hash != source_request["candidate_sha256"]:
                raise ValueError("Selected candidate changed during confirmation capture")
            request = {"version": VERSION, "run": self.envelope.id, "envelope_sha256": self.envelope.digest,
                       "check": check, "created": time.time(), "arguments": args, "stage": stage,
                       "definition_sha256": digest(config), "candidate_sha256": candidate_hash,
                       "cases": values, "source": selected}
            publish_json(root / "request.json", bounded(request, "Evaluation request"))
            # Exposure is irreversible even if the child fails or its reply is lost.
            for case in values:
                exposures.setdefault(digest(case["data"]), {"run": self.envelope.id, "check": check, "stage": stage})
            atomic_json(exposures_path, bounded(exposures, "Evaluation exposure history"))
            result = {"request_sha256": digest(request), "status": "failed", "reason": "",
                      "processes": {}, "environment_coverage": config["environment_coverage"]}
            try:
                sides = {}
                for side, artifact in (("baseline", bundle / "baseline"), ("candidate", root / "candidate")):
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Paired evaluation deadline expired; incomplete comparison")
                    work = root / side / "work"
                    work.mkdir(parents=True)
                    child_request = {
                        "version": VERSION, "artifact": str(artifact / "files"),
                        "cases": values, "metrics": config["metrics"],
                    }
                    publish_json(work / "request.json", child_request)
                    token = uuid.uuid5(uuid.UUID(check), side).hex
                    outcome = payload.run_process({
                        "argv": [config["interpreter"], "-I", "-B",
                                 str(bundle / "evaluator" / "files" / config["entrypoint"]),
                                 str(work / "request.json"), str(work / "result.json")],
                        "cwd": str(work), "root": str(work / "process"), "token": token,
                        "cpus": self.envelope.resources.cpus, "cpu_ids": list(self.envelope.cpu_ids),
                        "memory_mb": self.envelope.resources.memory_mb, "gpus": list(self.envelope.resources.gpus),
                        "seconds": remaining, "log_bytes": 1024 * 1024,
                        "cancel_path": str(self.directory / "cancel"), "job_name": f"labgoblin-{token}",
                        "parent_session": True,
                        **({"environment": variables(environment)} if environment is not None else {}),
                    })
                    result["processes"][side] = outcome
                    if outcome["status"] != "completed":
                        raise RuntimeError(f"{side} evaluator {outcome['status']}: {outcome['reason']}")
                    if hash_file(regular(work / "request.json")) != digest(child_request):
                        raise ValueError("Evaluator changed its admitted input request")
                    sides[side] = measurements(read_json(regular(work / "result.json")), values, config["metrics"])
                    for field in ("evaluator", "baseline"):
                        verify_capture(bundle / field, config["captures"][field])
                    verify_capture(root / "candidate", candidate_hash)
                    if definition(self.envelope) != config:
                        raise ValueError("Evaluation definition changed during measurement")
                comparisons = [{"id": case["id"], "baseline": sides["baseline"][case["id"]],
                                "candidate": sides["candidate"][case["id"]]} for case in values]
                metric_summary = {}
                for metric, direction in config["metrics"].items():
                    left = statistics.fmean(row["baseline"]["metrics"][metric] for row in comparisons)
                    right = statistics.fmean(row["candidate"]["metrics"][metric] for row in comparisons)
                    metric_summary[metric] = {"direction": direction, "baseline_mean": left,
                                              "candidate_mean": right, "candidate_minus_baseline": right - left}
                summary = {"cases": len(values),
                           "baseline_passed": sum(row["baseline"]["passed"] for row in comparisons),
                           "candidate_passed": sum(row["candidate"]["passed"] for row in comparisons),
                           "metrics": metric_summary,
                           "interpretation": "Descriptive paired measurements; no automatic promotion, significance claim or scientific stopping decision"}
                result = bounded({**result, "status": "completed", "summary": summary,
                                  "comparisons": comparisons}, "Paired evaluation result")
            except UncertainExecution as error:
                self.fatal = error
                self.cancel()
                raise
            except (OSError, ValueError, RuntimeError, OverflowError) as error:
                result["reason"] = f"{type(error).__name__}: {error}"[:2000]
            publish_json(root / "result.json", result)
            return page(self.envelope, check=check)

    def cancel(self):
        path = self.directory / "cancel"
        if not path.exists():
            publish_bytes(path, b"Owned evaluation interrupted; settle the execution tree\n")

    def raise_if_failed(self):
        if self.fatal:
            raise self.fatal

    async def close(self):
        if self.active is not None:
            self.cancel()
            await asyncio.wait_for(asyncio.shield(self.active), timeout=15)
        self.raise_if_failed()

    def tool(self, caller):
        from copilot import Tool, ToolResult

        async def handle(invocation):
            try:
                self.raise_if_failed()
                if (invocation.session_id != caller.id or not caller.waiting or caller.idle.is_set()
                        or caller.aborting or caller.limit_reached or caller.failure
                        or caller.consultation_call_id or self.active is not None):
                    raise ValueError("Only the exact blocked researcher may request one evaluation at a time")
                task = asyncio.create_task(asyncio.to_thread(self.evaluate, invocation.tool_call_id, invocation.arguments))
                self.active = task
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    self.cancel()
                    raise
                finally:
                    if task.done():
                        self.active = None
                failed = result["status"] != "completed"
                return ToolResult(result_type="failure" if failed else "success",
                                  error=result["reason"] if failed else None,
                                  text_result_for_llm=json.dumps(result))
            except (OSError, ValueError, RuntimeError) as error:
                if self.fatal is not None:
                    raise asyncio.CancelledError(str(self.fatal)) from error
                return ToolResult(result_type="failure", error=str(error),
                                  text_result_for_llm=f"{type(error).__name__}: {error}")

        return Tool("labgoblin_evaluate",
                    "Run the admitted model-free paired evaluator. Development captures a candidate directory. "
                    "Confirmation uses the exact candidate of source_run/source_check (a completed development check), "
                    "spending one fresh batch even on failure. Regression captures a candidate and replays that earlier "
                    "check's cases; it is not fresh confirmation. Results do not decide scientific stopping.",
                    handle, {"type": "object", "properties": {
                        "stage": {"type": "string", "enum": ["development", "confirmation", "regression"]},
                        "candidate": {"type": "string", "description": "Relative self-contained directory; development/regression only"},
                        "source_run": {"type": "string"}, "source_check": {"type": "string"},
                    }, "required": ["stage"], "additionalProperties": False}, skip_permission=True, defer="never")

    def reader(self, audit=None):
        from copilot import Tool, ToolResult

        async def handle(invocation):
            try:
                from labgoblin.inspection_evidence import Evidence
                if audit is not None:
                    audit.check()
                args = dict(invocation.arguments)
                args.setdefault("run", self.envelope.id)
                value = Evidence(self.project, self.envelope.project_id).evaluations(args)
                if audit is not None:
                    audit.record("labgoblin_read_evaluation", args, value)
                return ToolResult(text_result_for_llm=json.dumps(value))
            except (OSError, ValueError, KeyError, TypeError) as error:
                return ToolResult(result_type="failure", error=str(error),
                                  text_result_for_llm=f"{type(error).__name__}: {error}")

        return Tool("labgoblin_read_evaluation",
                    "Read host-retained paired evaluation history or page case discrepancies. "
                    "Omit run for this execution and check for the check list. Read-only; no evaluator or model calls. " + NOTICE,
                    handle, {"type": "object", "properties": {
                        "run": {"type": "string"}, "check": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0},
                    }, "additionalProperties": False}, skip_permission=True, defer="never")
