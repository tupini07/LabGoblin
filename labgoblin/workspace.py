"""Bounded source snapshots, declared input revisions and retained observations."""

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import stat
import time

from labgoblin.config import environment
from labgoblin.evidence import (
    Capture, SizeLimitError, contained, copy_bounded, hash_file, publish_bytes, require_space, verify_input_pins,
)
from labgoblin.protocol import (
    LaunchEnvelope, LaunchKey, Resources, TERMINAL, argv, boolean, canonical,
    fingerprint, identifier, number, strings, table, text,
)


REQUEST_FIELDS = {
    "key", "runner", "argv", "cwd", "source_files", "cpus", "memory_mb", "gpus",
    "seconds", "environment", "experiment_id", "hypothesis_id", "hypothesis_description",
    "artifacts", "validators", "input_validators", "hard_memory_limit",
}


def _input_revisions(config, root: Path, runner_kind: str) -> dict:
    result = {}
    for name, item in config.inputs.items():
        source = (config.root / item.path).resolve(strict=True)
        if config.root.is_relative_to(source) or source.is_relative_to(config.state_dir.resolve()):
            raise ValueError(f"Input {name} overlaps writable campaign state")
        value = {**asdict(item), "original_path": str(source), "access_path": str(source),
                 "identity": item.identity or item.path, "observed": time.time()}
        if item.assurance == "stable-consumption":
            if runner_kind == "wsl" or (runner_kind == "native" and os.name != "nt"):
                raise ValueError("Stable input consumption requires native Windows read leases or read-only Docker inputs")
            if not source.is_file():
                raise ValueError("Stable input preparation requires an explicit small file, not a dataset directory")
            capture = Capture.read(source, config.storage.capture_bytes)
            if item.sha256 and capture.digest != item.sha256:
                raise ValueError(f"Input hash mismatch: {name}")
            target = contained(root, Path("inputs") / name / ("revision" + source.suffix))
            publish_bytes(target, capture.body)
            target.chmod(stat.S_IREAD)
            value.update(access_path=str(target), sha256=capture.digest, wsl_path="",
                         verification_bytes=config.storage.capture_bytes, prepared=True,
                         bytes=len(capture.body))
        elif item.sha256:
            if not source.is_file() or hash_file(source, item.verification_bytes) != item.sha256:
                raise ValueError(f"Input hash mismatch: {name}")
        result[name] = value
    return result


@contextmanager
def checked_inputs(inputs: dict, runner_kind: str):
    leases = []
    try:
        for name, value in inputs.items():
            path = Path(value["access_path"])
            if value["assurance"] == "stable-consumption":
                if not value.get("prepared"):
                    raise ValueError(f"Input {name} has no prepared stable revision")
                if os.name == "nt":
                    import pywintypes
                    import win32con
                    import win32file
                    try:
                        lease = win32file.CreateFile(
                            str(path), win32con.GENERIC_READ, win32con.FILE_SHARE_READ, None,
                            win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
                    except pywintypes.error as error:
                        raise OSError(f"Stable input lease failed for {name}: {error}") from error
                    leases.append(lease)
                elif runner_kind != "docker":
                    raise ValueError("Stable native input leases are unsupported on this platform")
        yield verify_input_pins(inputs)
    finally:
        for lease in reversed(leases):
            lease.Close()


def prepare_spec(config, request: dict, attempt_id: str | None = None, *, validate_only=False, owner_id=None) -> dict:
    request = table(request, "work request", REQUEST_FIELDS)
    if len(canonical(request)) > 65536:
        raise ValueError("Work request exceeds its 64 KiB encoded limit")
    key = text(request.get("key"), "idempotency key")
    arguments = argv(request.get("argv"))
    name = text(request.get("runner", config.execution.default_runner), "runner")
    if name not in config.runners:
        raise ValueError(f"Unknown runner: {name}")
    runner = config.runners[name]
    resources = Resources.parse({"cpus": request.get("cpus", 1), "memory_mb": request.get("memory_mb", 1024),
                                 "gpus": request.get("gpus", [])})
    allowed = config.campaign.resources
    if (resources.cpus > allowed.cpus or resources.memory_mb > allowed.memory_mb
            or not set(resources.gpus).issubset(allowed.gpus)):
        raise ValueError("Work request exceeds the campaign resource envelope")
    seconds = number(request.get("seconds", 300), "seconds")
    hard_memory = boolean(request.get("hard_memory_limit", False), "hard_memory_limit")
    if hard_memory and (runner.kind == "wsl" or (runner.kind == "native" and os.name != "nt")):
        raise ValueError("This runner monitors memory; use native Windows or Docker for a kernel-hard limit")
    cwd = contained(config.root, text(request.get("cwd", "."), "cwd"))
    if not cwd.is_dir():
        raise ValueError("Work cwd must be an existing project directory")
    paths = strings(request.get("source_files", config.execution.source_files), "source_files", empty=True)
    sources = {}
    for item in paths:
        source = contained(config.root, item)
        if (not source.is_file() or source.is_relative_to(config.state_dir.resolve())
                or any(part.casefold() in (".git", ".venv", "__pycache__", ".env", ".ssh")
                       for part in source.relative_to(config.root).parts)):
            raise ValueError(f"Source snapshot requires explicit source files, not state/environments: {item}")
        sources[source.relative_to(config.root)] = source
    validators = {}
    for field in ("validators", "input_validators"):
        values = request.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"{field} must be an array of argument arrays")
        validators[field] = [list(argv(value, field)) for value in values]
    artifacts = strings(request.get("artifacts", []), "artifacts", empty=True)
    hypothesis = text(request.get("hypothesis_id", ""), "hypothesis_id", empty=True)
    statement = text(request.get("hypothesis_description", ""), "hypothesis_description", empty=not hypothesis)
    if statement and not hypothesis:
        raise ValueError("A hypothesis statement requires its explicit ID")
    if hypothesis and hypothesis == statement.strip():
        raise ValueError("Hypothesis statement must describe the claim, not repeat its ID")
    require_space(config.storage)
    if validate_only:
        for item in artifacts:
            contained(config.root / "validation-output", item)
        return {"valid": True, "runner": name, "resources": asdict(resources), "seconds": seconds,
                "source_files": [str(path) for path in sources],
                "scope": "Manifest/configuration validation; no copying, backend probing or execution"}
    root = contained(config.state_dir, Path("attempts") / (attempt_id or identifier()))
    if not root.is_relative_to(config.root):
        raise ValueError("Campaign state escapes the project through a symlink/junction")
    root.mkdir(parents=True, exist_ok=False)
    from labgoblin.processes import own_handle
    publish_bytes(root / "preparation.json", canonical({
        "protocol": 3, "owner_id": owner_id, "work_id": root.name,
        "handle": own_handle(root.name), "request_digest": fingerprint(request),
    }))
    snapshot, output = root / "source", root / "output"
    snapshot.mkdir()
    output.mkdir()
    artifacts = list(dict.fromkeys(str(contained(output, item).relative_to(output)) for item in artifacts))
    inputs = _input_revisions(config, root, runner.kind)
    copied = {}
    remaining = config.storage.snapshot_bytes
    for relative, source in sorted(sources.items()):
        if any(source == Path(value["original_path"]) or source.is_relative_to(Path(value["original_path"]))
               for value in inputs.values()):
            raise ValueError("A source snapshot cannot include a declared data input")
        if remaining <= 0:
            raise SizeLimitError("Source snapshot byte allowance exhausted")
        record = copy_bounded(source, contained(snapshot, relative), remaining)
        remaining -= record["bytes"]
        copied[str(relative)] = record
    workdir = contained(snapshot, cwd.relative_to(config.root))
    workdir.mkdir(parents=True, exist_ok=True)
    spec = {
        "version": 3, "id": root.name, "key": key, "request": request, "runner_name": name,
        "argv": list(arguments), "cwd": str(workdir), "root": str(root), "output": str(output),
        "source_root": str(snapshot), "source_hashes": copied, "inputs": inputs,
        **asdict(resources), "seconds": seconds, **validators, "artifacts": list(artifacts),
        "environment": {**config.execution.environment, **environment(request.get("environment", {}), "environment")},
        "hypothesis_id": hypothesis, "hypothesis_description": statement,
        "experiment_id": text(request.get("experiment_id", key), "experiment_id"),
        "storage": asdict(config.storage),
    }
    if len(canonical(spec)) > 1024 * 1024:
        raise ValueError("Frozen work spec exceeds 1 MiB")
    return spec


def submit(state, config, request: dict, *, turn_id: str | None = None) -> dict:
    from labgoblin.processes import CampaignLease
    with CampaignLease(state.root):
        return _submit_owned(state, config, request, turn_id=turn_id)


def _submit_owned(state, config, request: dict, *, turn_id=None) -> dict:
    request = dict(table(request, "work request", REQUEST_FIELDS))
    with state.db.read() as conn:
        campaign = conn.execute("SELECT generation,authority_revision FROM campaign").fetchone()
        generation = campaign["generation"]
        from labgoblin.state import _active_directives
        source_refs = dict(conn.execute("SELECT name,source_id FROM source_heads WHERE name IN ('goal','protocol','rationale','summary')"))
        source_refs.update({f"directive:{r['id']}": r["source_id"] for r in _active_directives(conn, generation)})
        if turn_id:
            owner = conn.execute("""SELECT t.generation,t.kind,t.state,p.content FROM turns t
                JOIN packets p ON p.id=t.packet_id WHERE t.id=?""", (turn_id,)).fetchone()
            if (not owner or owner["generation"] != generation or owner["kind"] != "research"
                    or owner["state"] not in ("prepared", "running")
                    or not conn.execute("""SELECT 1 FROM invocations WHERE turn_id=? AND kind='research'
                        AND state IN ('armed','running')""", (turn_id,)).fetchone()):
                raise ValueError("Submission does not belong to a currently owned research turn")
            packet = json.loads(owner["content"])
            if packet.get("authority_revision") != campaign["authority_revision"]:
                raise ValueError("Governing authority changed since this research packet; return a handoff before submitting")
            source_refs = {name: value["id"] for name, value in packet["sources"].items()}
            source_refs.update({f"directive:{value['id']}": value["source_id"] for value in packet["directives"]})
        previous = conn.execute("SELECT id,request_digest FROM attempts WHERE generation=? AND idempotency_key=?",
                                 (generation, request.get("key"))).fetchone()
        hypothesis = conn.execute("SELECT statement FROM hypotheses WHERE id=?",
                                   (request.get("hypothesis_id"),)).fetchone()
    if previous:
        if previous["request_digest"] != fingerprint(request):
            raise ValueError("Idempotency key already belongs to a different request")
        return state.attempt(previous["id"])
    effective = dict(request)
    if hypothesis and not effective.get("hypothesis_description"):
        effective["hypothesis_description"] = hypothesis["statement"]
    spec = prepare_spec(config, effective, owner_id=state.id)
    spec["request"] = request
    spec.update(source_refs=source_refs, generation=generation,
                authority_revision=campaign["authority_revision"], submitted_by=turn_id)
    publish_bytes(Path(spec["root"]) / "spec.json", canonical(spec))
    attempt_id = state.enqueue(spec)
    return state.attempt(attempt_id)


def prepare_envelope(state, config, attempt_id: str, grant: dict) -> LaunchEnvelope:
    from labgoblin.backends import validate_runner
    from labgoblin.payload import validate_command
    attempt = state.attempt(attempt_id)
    spec = json.loads(attempt["spec"])
    if grant["state"] != "granted" or grant["work_id"] != attempt_id or grant["owner_id"] != state.id:
        raise ValueError("Work launch requires its exact machine grant")
    require_space(config.storage)
    runner = validate_runner(asdict(config.runners[spec["runner_name"]]), spec["gpus"])

    def resolve(arguments):
        result = list(arguments)
        if result[0] in ("python", "python3"):
            result[0] = runner["resolved_python"]
        elif runner["kind"] == "native":
            candidate = Path(spec["cwd"]) / result[0]
            executable = str(candidate.resolve()) if candidate.is_file() else shutil.which(result[0])
            if executable is None:
                raise ValueError(f"Work executable is unavailable: {result[0]}")
            result[0] = executable
        return list(validate_command(result))

    execution = {**spec, "validators": [resolve(value) for value in spec["validators"]],
                 "input_validators": [resolve(value) for value in spec["input_validators"]]}
    path, ledger_id = state.ledger_identity()
    return LaunchEnvelope(
        LaunchKey(state.id, attempt["generation"], attempt_id, grant["token"], identifier()), "attempt",
        tuple(resolve(spec["argv"])), spec["cwd"], spec["root"], str(state.path), str(path), ledger_id,
        spec["seconds"], Resources(spec["cpus"], spec["memory_mb"], tuple(spec["gpus"])),
        config.revision, spec["environment"],
        {"runner": runner, "cpu_ids": json.loads(grant["native_cpus"]) if runner["kind"] == "native" else None,
         "execution": execution, "spec_digest": fingerprint(spec), "log_bytes": config.storage.log_bytes})


def verify_sources(execution: dict):
    root = Path(execution["source_root"])
    for name, value in execution.get("source_hashes", {}).items():
        if hash_file(contained(root, name), max(1, value["bytes"])) != value["sha256"]:
            raise ValueError(f"Source snapshot drift before execution: {name}")


def collect_artifacts(state, attempt_id: str, *, recollect=False) -> dict:
    attempt = state.attempt(attempt_id)
    if attempt["status"] not in TERMINAL:
        raise ValueError("Evidence collection requires a quiescent terminal execution")
    if attempt["collection"] != "pending" and not recollect:
        return state.collection(attempt_id)
    spec = json.loads(attempt["spec"])
    if attempt["status"] == "not_started" or (attempt["status"] == "cancelled" and attempt["started"] is None):
        return state.record_collection(attempt_id, [], "not_performed", "not_performed", "", recollect=recollect)
    output = Path(spec["output"])
    paths = {contained(output, name): name for name in spec["artifacts"]}
    if (output / "metrics.json").exists():
        paths[output / "metrics.json"] = "metrics.json"
    rows, errors, validation_errors = [], [], []
    if attempt["nonce"]:
        receipt = json.loads(state.launch(attempt["nonce"])["receipt"])
        validation_errors.extend(receipt["metadata"].get("validation_errors", []))
    for path, name in paths.items():
        try:
            if not path.is_file():
                raise ValueError(f"Required artifact is missing: {name}")
            observed = path.stat()
            oid = identifier()
            metadata = {"name": name, "current_path": str(path), "observed": time.time(),
                        "mtime_ns": observed.st_mtime_ns, "execution_status": attempt["status"]}
            kind = "metrics" if name == "metrics.json" else "artifact"
            limit = spec["storage"]["metrics_bytes"] if kind == "metrics" else spec["storage"]["capture_bytes"]
            digest = None
            size = observed.st_size
            assurance = "unmanaged"
            if size <= limit or kind == "metrics":
                capture = Capture.read(path, limit)
                size, digest, assurance = len(capture.body), capture.digest, "captured"
                retained = contained(state.root, Path("evidence") / oid)
                publish_bytes(retained, capture.body)
                metadata["capture_path"] = str(retained)
                if kind == "metrics":
                    try:
                        metadata["metrics"] = capture.metrics()
                    except ValueError as error:
                        validation_errors.append(f"{name}: {error}")
                        metadata["validation_error"] = str(error)
            else:
                metadata["limitation"] = "Large mutable artifact: current path only; bytes were not retained or hashed"
            rows.append({"id": oid, "path": name, "kind": kind, "size": size, "digest": digest,
                         "metadata": metadata, "assurance": assurance})
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {error}")
    reason = "; ".join([*errors, *validation_errors])[:8000]
    validated = any(row["kind"] == "metrics" for row in rows) or bool(spec["validators"])
    return state.record_collection(
        attempt_id, rows, "failed" if errors else "complete",
        "invalid" if errors or validation_errors else "valid" if validated else "unchecked",
        reason, recollect=recollect)
