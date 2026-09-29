"""Explicit input identities, immutable code snapshots, and output validation."""

from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import re

from xgenius.local_config import positive, strings
from xgenius.state import identifier


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + identifier() + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def contained(root: Path, path: str) -> Path:
    root = root.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path escapes {root}: {path}")
    return candidate


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_inputs(config) -> dict:
    project = Path(config.config_path).parent.resolve()
    result = {}
    for name, data in config.local.inputs.items():
        path = (project / data["path"]).resolve(strict=True)
        if project.is_relative_to(path) or path.is_relative_to(project / ".xgenius"):
            raise ValueError(f"Input {name} overlaps the writable campaign state")
        if data.get("sha256"):
            if not path.is_file() or digest(path) != data["sha256"]:
                raise ValueError(f"Input hash mismatch: {name}")
        result[name] = {
            **data, "identity": data.get("identity", data["path"]),
            "access_path": str(path),
        }
    return result


def prepare_spec(config, request: dict, attempt_id: str | None = None) -> dict:
    if not isinstance(request, dict):
        raise ValueError("Job manifest must be a JSON object")
    unknown = set(request) - {
        "key", "runner", "argv", "cwd", "source_files", "cpus", "memory_mb", "gpus",
        "seconds", "environment", "experiment_id", "hypothesis_id", "artifacts",
        "validators", "input_validators", "hard_memory_limit",
    }
    if unknown:
        raise ValueError(f"Unknown job settings: {sorted(unknown)}")
    local = config.local
    runner_name = request.get("runner", local.default_runner)
    if not isinstance(runner_name, str):
        raise ValueError("runner must be a string")
    if runner_name not in local.runners:
        raise ValueError(f"Unknown runner: {runner_name}")
    argv = strings(request.get("argv"), "argv")
    key = request.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Job manifest requires a stable non-empty idempotency key")
    for field, default in (("cwd", "."), ("experiment_id", key), ("hypothesis_id", "")):
        if not isinstance(request.get(field, default), str):
            raise ValueError(f"{field} must be a string")
    if type(request.get("hard_memory_limit", False)) is not bool:
        raise ValueError("hard_memory_limit must be boolean")
    cpus = request.get("cpus", 1)
    memory = request.get("memory_mb", 1024)
    seconds = request.get("seconds", 300)
    for name, value in [("cpus", cpus), ("memory_mb", memory), ("seconds", seconds)]:
        positive(value, name)
    if type(cpus) is not int or type(memory) is not int:
        raise ValueError("cpus and memory_mb must be integers")
    gpus = strings(request.get("gpus", []), "gpus", empty=True)
    if len(set(gpus)) != len(gpus) or not set(gpus).issubset(local.gpus):
        raise ValueError("Job GPUs must be unique devices allowed by the campaign")
    if cpus > local.cpus or memory > local.memory_mb:
        raise ValueError("Request exceeds the campaign CPU/RAM envelope")
    if request.get("hard_memory_limit", False) and (
            local.runners[runner_name].kind == "wsl" or
            (local.runners[runner_name].kind == "local" and os.name != "nt")):
        raise ValueError("Linux process memory limit is monitored, not kernel-hard; use Docker for a hard limit")
    inputs = validate_inputs(config)
    project = Path(config.config_path).parent.resolve()
    cwd = contained(project, request.get("cwd", "."))
    if not cwd.is_dir():
        raise ValueError("Job cwd must be an existing directory inside the research workspace")
    paths = request.get("source_files", local.source_files)
    strings(paths, "source_files", empty=True)
    for item in paths:
        source = contained(project, item)
        if not source.is_file() or source.is_relative_to(project / ".xgenius"):
            raise ValueError(f"Source snapshot requires explicit workspace files: {item}")
        if any(source.is_relative_to(Path(v["access_path"])) or
               source == Path(v["access_path"]) for v in inputs.values()):
            raise ValueError(f"Source snapshot includes a protected input: {item}")
        if any(part in (".git", ".venv", "__pycache__", ".env") for part in source.parts):
            raise ValueError(f"Source snapshot includes environment/cache state: {item}")
    env = request.get("environment", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", k)
            and isinstance(v, str) and "\0" not in v for k, v in env.items()):
        raise ValueError("Job environment values must be strings")
    outputs = strings(request.get("artifacts", []), "artifacts", empty=True)
    validators = request.get("validators", [])
    input_validators = request.get("input_validators", [])
    if not isinstance(validators, list) or not isinstance(input_validators, list):
        raise ValueError("validators must be arrays of argument arrays")
    for validator in validators + input_validators:
        strings(validator, "validator")
    attempt_id = attempt_id or identifier()
    root = (project / ".xgenius" / "attempts" / attempt_id).resolve()
    if not root.is_relative_to(project):
        raise ValueError("Attempt state cannot escape the campaign workspace through a symlink/junction")
    snapshot = root / "source"
    output = root / "output"
    for item in outputs:
        contained(output, item)
    root.mkdir(parents=True, exist_ok=False)
    snapshot.mkdir()
    output.mkdir()
    for name, value in inputs.items():
        p = Path(value["access_path"])
        if root.is_relative_to(p) or p.is_relative_to(root):
            raise ValueError(f"Output overlaps protected input {name}")
    hashes = {}
    for item in paths:
        source = contained(project, item)
        target = contained(snapshot, item)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[item] = digest(target)
    # A snapshot is always used, including for interpreter-only experiments.
    workdir = snapshot / cwd.relative_to(project)
    workdir.mkdir(parents=True, exist_ok=True)
    spec = {
        "version": 1, "id": attempt_id, "key": key, "request": request,
        "runner_name": runner_name, "runner": asdict(local.runners[runner_name]),
        "argv": argv, "cwd": str(workdir), "cpus": cpus, "memory_mb": memory,
        "gpus": gpus, "seconds": seconds, "inputs": inputs, "source_hashes": hashes,
        "environment": {**local.environment, **env},
        "experiment_id": request.get("experiment_id", key),
        "hypothesis_id": request.get("hypothesis_id", ""),
        "output": str(output), "root": str(root), "artifacts": outputs,
        "validators": validators, "input_validators": input_validators, "host_python": sys.executable,
        "config_path": config.config_path,
    }
    atomic_json(root / "spec.json", spec)
    return spec


def collect_artifacts(state, spec: dict):
    from xgenius.db import _connect
    output = Path(spec["output"])
    metadata = []
    for item in spec["artifacts"]:
        path = contained(output, item)
        if not path.is_file():
            raise ValueError(f"Required artifact is missing: {item}")
        metadata.append({
            "path": item, "bytes": path.stat().st_size, "sha256": digest(path),
        })
    manifest = contained(output, "metrics.json")
    if manifest.exists():
        values = read_json(manifest)
        if not isinstance(values, dict):
            raise ValueError("metrics.json must contain an object")
        for name, value in values.items():
            if not isinstance(name, str):
                raise ValueError("Metric names must be strings")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Metric {name} must be a finite number")
        metadata.append({"path": "metrics.json", "metrics": values, "sha256": digest(manifest)})
    with _connect(state.path) as c:
        for item in metadata:
            c.execute("""INSERT INTO artifacts VALUES(?,?,?,?)
                ON CONFLICT(attempt_id,path) DO UPDATE SET metadata=excluded.metadata""",
                      (identifier(), spec["id"], item["path"], json.dumps(item)))
    atomic_json(Path(spec["root"]) / "artifacts.json", {"artifacts": metadata})
