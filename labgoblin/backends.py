"""Native, WSL and local Docker execution of frozen, owned launches."""

from contextlib import ExitStack
from dataclasses import asdict, replace
import codecs
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace

from labgoblin import payload
from labgoblin.processes import temporary_directory
from labgoblin.evidence import (
    atomic_json, contained, copy_bounded, hash_file, parse_json, publish_bytes, read_bytes,
    read_json, require_space, tail,
)
from labgoblin.protocol import (
    AdmissionWait, LaunchEnvelope, LaunchKey, LaunchReceipt, PreExecutionError, Resources,
    UncertainExecution, canonical, identifier, number, strings,
)


INTERPRETER_PROBE = (
    "import json,os,platform,sys;"
    "print(json.dumps(dict(executable=sys.executable,version=platform.python_version(),"
    "implementation=platform.python_implementation(),platform=sys.platform,"
    "pidfd=hasattr(os,'pidfd_open'))))"
)
def command(arguments, *, timeout=30, limit=65536) -> str:
    """Bounded, model-free probes use headroom, not an inference allocation."""
    with temporary_directory(prefix="labgoblin-probe-") as name:
        root = Path(name)
        result = payload.run_process({
            "argv": list(arguments), "cwd": str(root), "root": str(root / "logs"),
            "token": identifier(), "cpus": 1, "memory_mb": 256, "seconds": timeout,
            "log_bytes": limit, "cancel_path": str(root / "cancel"),
        })
        if result["status"] != "completed":
            path = root / "logs" / "stderr.log"
            error = tail(path, limit=min(limit, 8192))["text"] if path.exists() else ""
            raise RuntimeError(f"{arguments[0]}: {result['reason']}; {error.strip()}")
        if any(item["truncated"] for item in result["logs"].values()):
            raise ValueError(f"Probe output exceeded its {limit}-byte bound")
        return tail(root / "logs" / "stdout.log", limit=limit)["text"].strip()


def docker_prefix(runner: dict) -> list[str]:
    context = runner["context"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", context):
        raise ValueError("Docker context must be an explicit local context name")
    return ["docker", "--context", context]


def validate_docker_endpoint(context: str) -> str:
    docker_prefix({"context": context})
    endpoint = command(["docker", "context", "inspect", context,
                        "--format", "{{.Endpoints.docker.Host}}"])
    if not (endpoint.startswith("npipe:////./pipe/") or endpoint.startswith("unix:///")):
        raise ValueError("Local execution refuses remote Docker endpoints")
    return endpoint


def validate_runner(runner: dict, gpu_ids=()) -> dict:
    result = dict(runner)
    kind = runner["kind"]
    if kind == "native":
        executable = shutil.which(runner["python"])
        if executable is None:
            raise ValueError(f"Native interpreter does not exist: {runner['python']}")
        executable = str(Path(executable).resolve(strict=True))
        info = parse_json(command([executable, "-c", INTERPRETER_PROBE]).encode("utf-8"))
        result.update(resolved_python=executable, interpreter=info,
                      interpreter_sha256=hash_file(Path(executable), 64 * 1024 * 1024))
    elif kind == "wsl":
        if os.name != "nt":
            raise ValueError("WSL requires the Windows control plane")
        if runner["distro"].casefold() in ("docker-desktop", "docker-desktop-data"):
            raise ValueError("Use a development WSL distro, not Docker Desktop's internal distro")
        info = parse_json(command(["wsl", "-d", runner["distro"], "--exec",
                                  runner["python"], "-c", INTERPRETER_PROBE]).encode("utf-8"))
        if info["platform"] != "linux" or not info["pidfd"] or tuple(map(int, info["version"].split(".")[:2])) < (3, 11):
            raise ValueError("WSL payload helpers require Linux pidfd support and Python 3.11+")
        result.update(resolved_python=info["executable"], interpreter=info)
    elif kind == "docker":
        prefix = docker_prefix(runner)
        result["endpoint"] = validate_docker_endpoint(runner["context"])
        if command([*prefix, "version", "--format", "{{.Server.Os}}"]) != "linux":
            raise ValueError("Docker runner requires a Linux engine")
        image = command([*prefix, "image", "inspect", runner["image"], "--format", "{{.Id}}"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Docker returned an invalid local image identity")
        result.update(image_id=image, resolved_python=runner["python"],
                      interpreter={"executable": runner["python"], "assurance": "declared in frozen image"})
    else:
        raise ValueError(f"Unsupported runner: {kind}")
    if gpu_ids:
        if kind == "wsl":
            listing = command(["wsl", "-d", runner["distro"], "--exec", "/usr/lib/wsl/lib/nvidia-smi",
                               "--query-gpu=uuid", "--format=csv,noheader"])
        else:
            listing = command(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"])
        if not set(gpu_ids).issubset(set(listing.splitlines())):
            raise ValueError("Requested physical GPU UUID is not visible to the runner")
    return result


def wsl_path(runner: dict, path: str) -> str:
    return command(["wsl", "-d", runner["distro"], "--exec", "wslpath", "-a", "-u", path])


def _root(envelope):
    return contained(Path(envelope.root), Path("launches") / envelope.key.nonce)


def native_spec(envelope: LaunchEnvelope) -> dict:
    execution = envelope.metadata.get("execution", {})
    return {
        "envelope": asdict(envelope), "envelope_digest": envelope.digest,
        "runner_kind": envelope.metadata.get("runner", {"kind": "native"})["kind"],
        "token": envelope.key.nonce, "work_id": envelope.key.work_id,
        "root": str(_root(envelope)), "cwd": envelope.cwd, "argv": list(envelope.argv),
        "output": str(contained(Path(envelope.root), execution.get("output", "output"))),
        "cancel_path": str(_root(envelope) / "cancel"),
        "seconds": envelope.timeout_seconds, **asdict(envelope.resources),
        "cpu_ids": envelope.metadata.get("cpu_ids"),
        "log_bytes": envelope.metadata["log_bytes"], "environment": dict(envelope.environment),
        "inputs": {name: dict(value) for name, value in execution.get("inputs", {}).items()},
        "input_validators": execution.get("input_validators", []),
        "validators": execution.get("validators", []),
    }


def _mount(source: Path, target: str, *, readonly=False) -> str:
    source = source.resolve(strict=True)
    forbidden = {".ssh", ".aws", ".azure", ".kube", ".copilot", ".git-credentials",
                 ".netrc", "id_rsa", "id_ed25519", "docker.sock", "docker_engine"}
    if (source == Path(source.anchor) or source == Path.home().resolve()
            or any(part.casefold() in forbidden for part in source.parts)
            or source.is_socket()):
        raise ValueError(f"Refusing broad, credential or socket mount: {source}")
    fields = ["type=bind", f"source={source}", f"target={target}"]
    if readonly:
        fields.append("readonly")
    value = io.StringIO()
    csv.writer(value, lineterminator="\n").writerow(fields)
    return value.getvalue()[:-1]


def prepare_guest(envelope: LaunchEnvelope) -> tuple[dict, list[str]]:
    runner = envelope.metadata["runner"]
    spec = native_spec(envelope)
    spec["cpu_ids"] = None
    root = _root(envelope)
    root.mkdir(parents=True, exist_ok=True)
    runtime = Path(envelope.metadata["runtime"]["root"])
    if runner["kind"] == "wsl":
        for key in ("root", "cwd", "output", "cancel_path"):
            spec[key] = wsl_path(runner, spec[key])
        for name, value in spec["inputs"].items():
            spec["inputs"][name] = {**value, "access_path":
                                   value.get("wsl_path") or wsl_path(runner, value["access_path"])}
        guest_runtime = wsl_path(runner, str(runtime))
        invoke = ["wsl", "-d", runner["distro"], "--exec", runner["resolved_python"],
                  "-I", "-B", f"{guest_runtime}/bootstrap.py", "--payload", f"{spec['root']}/payload.json"]
    else:
        source = contained(Path(envelope.root), envelope.metadata["execution"]["source_root"])
        cwd = Path(envelope.cwd).resolve().relative_to(source)
        output = Path(spec["output"])
        output.mkdir(parents=True, exist_ok=True)
        spec.update(root="/run", cwd="/source/" + cwd.as_posix(), output="/output", cancel_path="/run/cancel")
        name = f"labgoblin-{envelope.key.nonce}"
        invoke = [
            *docker_prefix(runner), "create", "--pull=never", "--name", name,
            "--label", f"labgoblin.nonce={envelope.key.nonce}",
            "--label", f"labgoblin.envelope={envelope.digest}",
            "--label", f"labgoblin.campaign={envelope.key.campaign_id}",
            "--cpus", str(envelope.resources.cpus), "--memory", f"{envelope.resources.memory_mb}m",
            "--restart", "no", "--network", "bridge" if runner["network"] else "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        ]
        for host, guest, readonly in ((root, "/run", False), (source, "/source", True),
                                      (output, "/output", False), (runtime, "/runtime", True)):
            invoke.extend(["--mount", _mount(host, guest, readonly=readonly)])
        project = Path(envelope.state_path).parent.parent.resolve()
        for name, value in spec["inputs"].items():
            host = Path(value["access_path"]).resolve(strict=True)
            if project.is_relative_to(host):
                raise ValueError("Input mount cannot expose the whole project or a parent directory")
            guest = f"/inputs/{name}"
            invoke.extend(["--mount", _mount(host, guest, readonly=True)])
            spec["inputs"][name] = {**value, "access_path": guest}
        if envelope.resources.gpus:
            invoke.extend(["--gpus", "device=" + ",".join(envelope.resources.gpus)])
        invoke.extend(["--entrypoint", runner["resolved_python"], runner["image_id"],
                       "-I", "-B", "/runtime/bootstrap.py", "--payload", "/run/payload.json"])
    spec_digest = publish_bytes(root / "payload.json", canonical(spec))
    invoke[-1:-1] = ["--digest", spec_digest]
    return spec, invoke


def docker_state(envelope):
    runner = envelope.metadata["runner"]
    if validate_docker_endpoint(runner["context"]) != runner["endpoint"]:
        raise ValueError("Frozen Docker endpoint identity changed")
    fields = command([
        *docker_prefix(runner), "inspect", f"labgoblin-{envelope.key.nonce}", "--format",
        '{"id":{{json .Id}},"image":{{json .Image}},"labels":{{json .Config.Labels}},'
        '"state":{{json .State}},"restart":{{json .HostConfig.RestartPolicy.Name}}}',
    ])
    value = parse_json(fields.encode("utf-8"))
    expected = {"labgoblin.nonce": envelope.key.nonce, "labgoblin.envelope": envelope.digest,
                "labgoblin.campaign": envelope.key.campaign_id}
    if any(value["labels"].get(key) != item for key, item in expected.items()):
        raise ValueError("Container incarnation labels differ from the frozen launch")
    if value["image"] != runner["image_id"] or value["restart"] not in ("no", ""):
        raise ValueError("Container image/restart policy differs from its authorization")
    return value


def read_receipt(envelope):
    path = _root(envelope) / "backend-receipt.json"
    if not path.exists():
        return None
    receipt = LaunchReceipt.parse(read_json(path))
    if receipt.key != envelope.key or receipt.envelope_digest != envelope.digest:
        raise ValueError("Backend receipt belongs to a different incarnation")
    if envelope.metadata.get("runner", {}).get("kind") in ("wsl", "docker"):
        if receipt.metadata.get("payload_spec_digest") != hash_file(_root(envelope) / "payload.json", 1024 * 1024):
            raise ValueError("Guest receipt does not identify the exact derived execution mapping")
    return receipt


def _supervise_docker(envelope, invoke):
    runner = envelope.metadata["runner"]
    prefix = docker_prefix(runner)
    if validate_docker_endpoint(runner["context"]) != runner["endpoint"]:
        raise PreExecutionError("Frozen Docker endpoint changed before container creation")
    started = time.monotonic()
    container_id = command(invoke)
    state = docker_state(envelope)
    if state["id"] != container_id or state["state"]["Status"] != "created":
        raise UncertainExecution("Docker creation does not identify a fresh, unstarted owned container")
    publish_bytes(_root(envelope) / "backend.json", canonical({"container_id": container_id}))
    command([*prefix, "start", container_id])
    while True:
        state = docker_state(envelope)
        if state["state"]["Status"] in ("exited", "dead") and not state["state"]["Running"]:
            break
        if (_root(envelope) / "cancel").exists() or time.monotonic() - started > envelope.timeout_seconds + 15:
            command([*prefix, "stop", "--time", "2", container_id], timeout=15)
        time.sleep(0.2)
    receipt = read_receipt(envelope)
    if receipt is not None:
        return receipt
    return LaunchReceipt(
        envelope.key, envelope.digest, "interrupted", True, time.monotonic() - started,
        returncode=state["state"]["ExitCode"],
        reason="Owned container exited without a payload receipt; execution/validation result is unavailable",
        metadata={"container_id": container_id, "state": state["state"]})


def supervise(envelope: LaunchEnvelope) -> LaunchReceipt:
    from labgoblin.workspace import checked_inputs, verify_sources
    with ExitStack() as stack:
        try:
            execution = envelope.metadata.get("execution", {})
            verify_sources(execution)
            stack.enter_context(checked_inputs(execution.get("inputs", {}), envelope.metadata["runner"]["kind"]))
        except (OSError, ValueError) as error:
            raise PreExecutionError(f"Frozen execution inputs failed validation: {error}") from error
        return _supervise(envelope)


def _supervise(envelope: LaunchEnvelope) -> LaunchReceipt:
    runner = envelope.metadata["runner"]
    if runner["kind"] == "native":
        try:
            if hash_file(Path(runner["resolved_python"]), 64 * 1024 * 1024) != runner["interpreter_sha256"]:
                raise PreExecutionError("Frozen native interpreter changed before execution")
            if envelope.metadata.get("cpu_ids") is None:
                raise PreExecutionError("Native execution requires its assigned CPU set")
            spec = native_spec(envelope)
            publish_bytes(_root(envelope) / "payload.json", canonical(spec))
        except (OSError, ValueError) as error:
            raise PreExecutionError(f"Native preparation failed before payload start: {error}") from error
        return payload.execute_spec(spec)
    try:
        spec, invoke = prepare_guest(envelope)
    except (OSError, ValueError, RuntimeError) as error:
        raise PreExecutionError(f"Guest launch preparation failed before payload start: {error}") from error
    if runner["kind"] == "docker":
        return _supervise_docker(envelope, invoke)
    # The transport tree is owned too, but its exit alone never proves guest exit.
    result = payload.run_process({
        "argv": invoke, "cwd": str(_root(envelope)), "root": str(_root(envelope) / "transport"),
        "token": envelope.key.nonce, "cpus": 1, "memory_mb": 256,
        "seconds": envelope.timeout_seconds + 15, "log_bytes": envelope.metadata["log_bytes"],
        "cancel_path": str(_root(envelope) / "transport-cancel"),
    })
    receipt = read_receipt(envelope)
    if receipt is not None:
        return receipt
    if not result["executed"]:
        raise PreExecutionError(result["reason"])
    raise UncertainExecution(f"WSL transport ended without a qualified guest receipt: {result['reason']}")


def inspect_payload(envelope: LaunchEnvelope) -> str:
    runner = envelope.metadata.get("runner", {"kind": "native"})
    if runner["kind"] == "docker":
        state = docker_state(envelope)["state"]
        if state["Running"]:
            return "alive"
        return "dead" if state["Status"] in ("exited", "dead") else "unknown"
    if read_receipt(envelope) is not None:
        return "dead"
    if runner["kind"] == "wsl":
        path = _root(envelope) / "payload.json"
        if not path.exists():
            return "unknown"
        runtime = wsl_path(runner, envelope.metadata["runtime"]["root"])
        manifest = wsl_path(runner, str(path))
        value = command(["wsl", "-d", runner["distro"], "--exec", runner["resolved_python"],
                         "-I", "-B", f"{runtime}/bootstrap.py", "--payload", "--inspect", manifest])
        if value not in ("alive", "dead", "unknown"):
            raise ValueError("WSL returned an invalid ownership observation")
        return value
    from labgoblin.processes import process_state
    from labgoblin.state import State
    state = State.open(Path(envelope.state_path).parent)
    launch = state.launch(envelope.key.nonce)
    if launch["phase"] != "executing" or not launch["supervisor"]:
        return "unknown"
    if process_state(json.loads(launch["supervisor"])) != "dead":
        return "unknown"
    if os.name != "nt":
        path = _root(envelope) / "payload.json"
        return payload.inspect_spec(read_json(path)) if path.exists() else "unknown"
    import pywintypes
    import win32job
    execution = envelope.metadata.get("execution", {})
    names = ["main", *[f"input-validator-{i}" for i in range(len(execution.get("input_validators", [])))],
             *[f"validator-{i}" for i in range(len(execution.get("validators", [])))]]
    for name in names:
        try:
            job = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False,
                                        f"labgoblin-{envelope.key.nonce}-{name}")
        except pywintypes.error as error:
            if error.winerror == 2:
                continue
            raise RuntimeError(f"Owned Windows Job Object cannot be inspected: {error}") from error
        try:
            if win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
                return "alive"
        finally:
            job.Close()
    return "dead"


BUILD_SDK_VERSION = "7.1.0"
BUILD_CLIENT_MEMORY_MB = 256


def build_dockerfile(body: bytes, resolve_image, token: str) -> tuple[bytes, dict]:
    """Pin literal local bases; refuse features that can fetch outside RUN networking."""
    stages, bases, output = [], {}, []
    pending = ""
    for line in body.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if re.match(r"#\s*(syntax|escape)\s*=", line, re.I):
                raise ValueError("Local builds require the classic Dockerfile syntax and backslash escapes")
            continue
        pending += line[:-1] + " " if line.endswith("\\") else line
        if line.endswith("\\"):
            continue
        instruction, _, rest = pending.partition(" ")
        instruction = instruction.upper()
        if instruction in ("ADD", "ONBUILD") or "<<" in pending:
            raise ValueError("Local builds refuse ADD, ONBUILD and heredocs; use explicit COPY and RUN")
        if instruction == "FROM":
            fields = rest.split()
            if (len(fields) not in (1, 3) or (len(fields) == 3 and fields[1].upper() != "AS")
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", fields[0])):
                raise ValueError("FROM requires a literal local image or earlier stage; no variables/platform overrides")
            base = fields[0]
            if base.lower() != "scratch" and base.lower() not in stages:
                image = resolve_image(base)
                if (not re.fullmatch(r"sha256:[a-f0-9]{64}", image["id"])
                        or image.get("onbuild") or image.get("os") != "linux"):
                    raise ValueError("Base must be an existing Linux image without ONBUILD instructions")
                bases[base] = image["id"]
                # A bare full image ID cannot be interpreted as a registry repository if removed.
                fields[0] = image["id"].removeprefix("sha256:")
            name = fields[2].lower() if len(fields) == 3 else str(len(stages))
            if name in stages or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name):
                raise ValueError("Build stages require distinct literal names")
            stages.append(name)
            pending = "FROM " + " ".join(fields)
            output.extend((pending, f'LABEL labgoblin.build="{token}"'))
        else:
            if not stages and instruction != "ARG":
                raise ValueError("Dockerfile must start with FROM (optionally preceded by ARG)")
            if instruction == "COPY":
                for item in shlex.split(rest):
                    if item.startswith("--from="):
                        source = item.removeprefix("--from=").lower()
                        previous = stages[:-1]
                        if source not in previous and not (source.isdecimal() and int(source) < len(previous)):
                            raise ValueError("COPY --from may only reference an earlier stage, never an external image")
            if instruction == "LABEL" and ("labgoblin." in rest.lower() or "$" in rest):
                raise ValueError("Reserved labgoblin labels and dynamic label keys are not permitted")
            if instruction not in ("ARG", "RUN", "COPY", "ENV", "LABEL", "EXPOSE", "VOLUME", "USER",
                                   "WORKDIR", "CMD", "ENTRYPOINT", "STOPSIGNAL", "HEALTHCHECK", "SHELL"):
                raise ValueError(f"Unsupported classic Dockerfile instruction: {instruction}")
            output.append(pending)
        pending = ""
    if pending or not stages:
        raise ValueError("Dockerfile is incomplete or has no FROM instruction")
    return ("\n".join(output) + "\n").encode("utf-8"), bases


def prepare_build_context(context: Path, files, directory: Path, runner: dict, token: str, limit: int) -> dict:
    context = context.resolve(strict=True)
    names = strings(files, "explicit build source files")
    if len(names) > 10000 or not context.is_dir():
        raise ValueError("Build context requires a directory and at most 10000 explicit files")
    sources, total = {}, 0
    for name in dict.fromkeys(("Dockerfile", *names)):
        source = contained(context, name)
        relative = source.relative_to(context)
        if (not source.is_file() or any(part.casefold() in
                (".git", ".venv", ".labgoblin", ".xgenius", ".env", ".ssh", ".copilot") for part in relative.parts)):
            raise ValueError(f"Build context requires explicit source files, not state or credentials: {name}")
        record = copy_bounded(source, contained(directory / "context", relative), max(1, limit - total))
        total += record["bytes"]
        if total > limit:
            raise ValueError("Build source snapshot allowance exceeded")
        sources[relative.as_posix()] = record

    def resolve_image(name):
        return parse_json(command([*docker_prefix(runner), "image", "inspect", name, "--format",
            '{"id":{{json .Id}},"os":{{json .Os}},"onbuild":{{json (index .Config "OnBuild")}}}']).encode("utf-8"))

    dockerfile, bases = build_dockerfile(
        read_bytes(directory / "context" / "Dockerfile", 65536), resolve_image, token)
    path = directory / "context.tar"
    with tarfile.open(path, "x", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(sources):
            source = directory / "context" / name
            info = archive.gettarinfo(str(source), arcname=name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            if name == "Dockerfile":
                info.size = len(dockerfile)
                archive.addfile(info, io.BytesIO(dockerfile))
            else:
                with source.open("rb") as stream:
                    archive.addfile(info, stream)
    bound = limit + 16 * 1024 * 1024
    return {"files": sources, "source_bytes": total, "bases": bases,
            "tar": str(path), "tar_bytes": path.stat().st_size, "tar_sha256": hash_file(path, bound),
            "tar_limit": bound, "context_policy": "Explicit file allowlist; .dockerignore is not an additional filter"}


def _build_response(response) -> dict:
    if not (response.headers.get("Content-Length") or
            "chunked" in response.headers.get("Transfer-Encoding", "").lower()):
        raise UncertainExecution("Build response has no verifiable HTTP message boundary")
    pending, error, invalid = b"", "", False
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    for block in response.iter_content(chunk_size=16384):
        sys.stdout.write(decoder.decode(block))
        pending += block
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            if not raw.strip():
                continue
            try:
                value = parse_json(raw)
                if not isinstance(value, dict):
                    raise ValueError("Build frame is not an object")
                if value.get("errorDetail") or value.get("error"):
                    error = str(value.get("errorDetail") or value["error"])[:4000]
            except ValueError:
                invalid = True
        if len(pending) > 256 * 1024:
            invalid, pending = True, b""
    sys.stdout.write(decoder.decode(b"", final=True))
    if pending.strip():
        try:
            value = parse_json(pending)
            if isinstance(value, dict):
                error = str(value.get("message") or value.get("error") or error)[:4000]
            else:
                invalid = True
        except ValueError:
            invalid = True
    if invalid or response.status_code != 200:
        error = error or f"Engine build response was invalid (HTTP {response.status_code})"
    return {"status": "failed" if error else "completed", "reason": error}


def build_api(envelope: LaunchEnvelope):
    import docker
    import requests
    directory = Path(envelope.root)
    from labgoblin.scheduler import ResourceLedger
    ledger = ResourceLedger(envelope.ledger_path, expected_id=envelope.ledger_id)
    run = ledger.consumer_run(envelope.key.grant_id)
    if not run or run["phase"] != "executing" or run["digest"] != envelope.digest:
        raise ValueError("Build API worker lacks its matching executing authorization")
    with (directory / "api-start.claim").open("xb") as claim:
        claim.write(canonical({"nonce": envelope.key.nonce, "envelope_digest": envelope.digest}))
    entered = False
    try:
        settings = envelope.metadata["build"]
        with docker.APIClient(base_url=settings["runner"]["endpoint"], version="auto", timeout=15) as client:
            if docker.__version__ != BUILD_SDK_VERSION:
                raise ValueError("Docker build SDK differs from the frozen adapter")
            params = {"version": "1", "t": settings["temporary_tag"], "pull": "false",
                      "rm": "true", "forcerm": "true", "networkmode": settings["network"],
                      "memory": settings["memory_mb"] * 1048576, "memswap": settings["memory_mb"] * 1048576,
                      "cpusetcpus": ",".join(str(i) for i in range(settings["cpus"]))}
            with Path(settings["snapshot"]["tar"]).open("rb") as context:
                entered = True
                # Public Session transport: explicit classic API, no auth/proxy injection or builder plugins.
                with client.post(
                    f"{client.base_url}/v{client.api_version}/build", params=params, data=context,
                    headers={"Content-Type": "application/tar"}, stream=True,
                    timeout=envelope.timeout_seconds,
                ) as response:
                    result = _build_response(response)
            result.update(envelope_digest=envelope.digest, executed=True)
            publish_bytes(directory / "build-result.json", canonical(result))
            return 0 if result["status"] == "completed" else 1
    except (docker.errors.DockerException, requests.exceptions.RequestException, OSError, ValueError,
            RuntimeError) as error:
        detail = f"{type(error).__name__}: {str(error)[:4000]}"
        if entered:
            atomic_json(directory / "diagnostic.json", {"error": detail, "daemon_quiescence": "unknown"})
        else:
            publish_bytes(directory / "build-result.json", canonical({
                "envelope_digest": envelope.digest, "status": "not_started", "executed": False, "reason": detail}))
        return 1


def build_main(mode, path):
    from labgoblin.processes import own_handle
    from labgoblin.scheduler import ResourceLedger
    from labgoblin.worker import verify_runtime
    envelope = LaunchEnvelope.parse(read_json(Path(path)))
    if envelope.kind != "build":
        raise ValueError("Build helper requires its exact build authorization")
    if mode == "--build-api":
        return build_api(envelope)
    ledger = ResourceLedger(envelope.ledger_path, expected_id=envelope.ledger_id)
    if not ledger.claim_consumer(envelope, own_handle(envelope.key.nonce)):
        return 0
    directory = Path(envelope.root)
    entered, started = False, time.monotonic()
    try:
        verify_runtime(SimpleNamespace(root=directory.parent), envelope)
        settings = envelope.metadata["build"]
        runner, snapshot = settings["runner"], settings["snapshot"]
        if validate_docker_endpoint(runner["context"]) != runner["endpoint"]:
            raise PreExecutionError("Frozen Docker build endpoint changed")
        if hash_file(Path(snapshot["tar"]), snapshot["tar_limit"]) != snapshot["tar_sha256"]:
            raise PreExecutionError("Frozen Docker context bytes changed")
        entered = True
        outcome = payload.run_process({
            "argv": list(envelope.argv), "cwd": envelope.cwd, "root": str(directory / "client"),
            "token": envelope.key.nonce, "cancel_path": str(directory / "cancel"),
            "cpus": 1, "memory_mb": BUILD_CLIENT_MEMORY_MB, "seconds": envelope.timeout_seconds,
            "log_bytes": envelope.metadata["log_bytes"],
        })
        result_path = directory / "build-result.json"
        if result_path.exists():
            result = read_json(result_path, 16384)
            if result.get("envelope_digest") != envelope.digest:
                raise UncertainExecution("Build result does not identify its owned request")
        elif not outcome["executed"]:
            result = {"status": "not_started", "executed": False, "reason": outcome["reason"]}
        else:
            raise UncertainExecution("Build client ended without a complete engine response; daemon work may remain")
        if result["status"] not in ("completed", "failed", "not_started"):
            raise UncertainExecution("Invalid build operation result")
        prefix = docker_prefix(runner)
        remaining = command([*prefix, "ps", "--all", "--filter",
                             f"label=labgoblin.build={envelope.key.nonce}", "--format", "{{.ID}}"])
        for container_id in remaining.splitlines():
            active = command([*prefix, "inspect", container_id, "--format", "{{.State.Running}}"])
            if active != "false":
                raise UncertainExecution("An owned build container is not quiescent")
        if result["status"] == "completed":
            image = parse_json(command([*prefix, "image", "inspect", settings["temporary_tag"], "--format",
                '{"id":{{json .Id}},"owner":{{json (index .Config.Labels "labgoblin.build")}}}']).encode("utf-8"))
            if image["owner"] != envelope.key.nonce or not re.fullmatch(r"sha256:[a-f0-9]{64}", image["id"]):
                raise UncertainExecution("Built image does not identify this build incarnation")
            command([*prefix, "tag", image["id"], runner["image"]])
            result["image_id"] = image["id"]
        receipt = LaunchReceipt(
            envelope.key, envelope.digest, result["status"], True, time.monotonic() - started,
            returncode=0 if result["status"] == "completed" else 1,
            reason=result["reason"], executed=result["executed"], metadata={
                **result, "client": outcome, "temporary_tag": settings["temporary_tag"]})
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        if entered:
            atomic_json(directory / "diagnostic.json", {"error": f"{type(error).__name__}: {str(error)[:4000]}",
                                                       "grant": envelope.key.grant_id})
            return 1
        receipt = LaunchReceipt(envelope.key, envelope.digest, "not_started", True, time.monotonic() - started,
                                executed=False, reason=f"{type(error).__name__}: {str(error)[:4000]}")
    publish_bytes(directory / "backend-receipt.json", canonical(asdict(receipt)))
    ledger.finish_consumer(receipt)
    return 0 if receipt.status == "completed" else 1


def build(state, config, runner_name: str, context: Path, files, *, cpus=1, memory_mb=1024, timeout=600) -> dict:
    import importlib.metadata
    from labgoblin.processes import CampaignLease, background_options, own_handle
    from labgoblin.scheduler import ResourceLedger
    from labgoblin.worker import prepare_runtime
    try:
        version = importlib.metadata.version("docker")
    except importlib.metadata.PackageNotFoundError:
        version = None
    if version != BUILD_SDK_VERSION:
        raise ValueError(f"Explicit image builds require docker=={BUILD_SDK_VERSION}; "
                         'run python -m pip install -e ".[docker-build]" from the LabGoblin checkout '
                         "with this environment's interpreter")
    if runner_name not in config.runners or config.runners[runner_name].kind != "docker":
        raise ValueError("Local image build requires an explicitly selected Docker runner")
    number(timeout, "build deadline")
    Resources(cpus, memory_mb)
    runner = asdict(config.runners[runner_name])
    runner["endpoint"] = validate_docker_endpoint(runner["context"])
    probe = (
        "import docker,json,sys;"
        "c=docker.APIClient(base_url=sys.argv[1],version='auto',timeout=10);"
        "v=c.info();c.close();"
        "print(json.dumps(dict(os=v['OSType'],cpus=v['NCPU'],memory=v['MemTotal'])))")
    info = parse_json(command([sys.executable, "-c", probe, runner["endpoint"]]).encode("utf-8"))
    if info["os"] != "linux" or cpus > info["cpus"] or memory_mb * 1048576 > info["memory"]:
        raise ValueError("Build resources cannot fit the prepared local Linux Docker engine")
    resources = Resources(cpus + 1, memory_mb + BUILD_CLIENT_MEMORY_MB)
    location, identity = state.ledger_identity()
    ledger = ResourceLedger(location, expected_id=identity or None)
    owner_id, token = identifier(), identifier()
    directory = ledger.path.parent / "builds" / owner_id / token
    owner = {"kind": "build", "handle": own_handle(owner_id), "campaign_id": state.id,
             "state_dir": str(state.root)}
    envelope = None
    with CampaignLease(state.root):
        try:
            require_space(config.storage)
            ledger.request(token, owner_id, token, "build", resources, owner, native=False)
            wait_until = time.monotonic() + timeout
            while (grant := ledger.reserve(token))["state"] != "granted":
                if grant["state"] != "pending" or time.monotonic() >= wait_until:
                    raise AdmissionWait("Build not admitted: " + grant["reason"])
                time.sleep(0.25)
            directory.mkdir(parents=True, exist_ok=False)
            snapshot = prepare_build_context(context, files, directory, runner, token, config.storage.snapshot_bytes)
            envelope = LaunchEnvelope(
                LaunchKey(owner_id, 1, token, token, token), "build", (sys.executable,),
                str(directory), str(directory), str(state.path), str(ledger.path), ledger.id, timeout,
                resources, config.revision, metadata={"cpu_ids": [], "log_bytes": config.storage.log_bytes,
                    "build": {"runner": runner, "snapshot": snapshot, "cpus": cpus, "memory_mb": memory_mb,
                              "network": "default" if runner["network"] else "none",
                              "temporary_tag": "labgoblin-build:" + token}})
            envelope = prepare_runtime(state, envelope, root=directory.parent)
            bootstrap = Path(envelope.metadata["runtime"]["root"]) / "bootstrap.py"
            path = directory / "envelope.json"
            envelope = replace(envelope, argv=(sys.executable, "-I", "-B", str(bootstrap), "--build-api", str(path)))
            publish_bytes(path, canonical(asdict(envelope)))
            ledger.arm_consumer(envelope)
            try:
                with (directory / "supervisor.stdout.log").open("xb") as out, (
                        directory / "supervisor.stderr.log").open("xb") as err:
                    subprocess.Popen([sys.executable, "-I", "-B", str(bootstrap), "--build-supervisor", str(path)],
                        cwd=directory, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                        **background_options(independent=True))
            except (OSError, ValueError) as error:
                ledger.finish_consumer(LaunchReceipt(envelope.key, envelope.digest, "not_started", True, 0,
                    executed=False, reason=f"{type(error).__name__}: {str(error)[:4000]}"))
                raise
            until = time.monotonic() + timeout + 60
            while True:
                run = ledger.consumer_run(token)
                if run["phase"] == "quiescent":
                    receipt = LaunchReceipt.parse(json.loads(run["receipt"]))
                    return {"grant": token, "status": receipt.status, "failed": int(receipt.status != "completed"),
                            "reason": receipt.reason, "image": runner["image"],
                            "image_id": receipt.metadata.get("image_id"), "root": str(directory),
                            "resources": asdict(resources), "snapshot": snapshot}
                if (directory / "diagnostic.json").exists() or time.monotonic() >= until:
                    diagnostic = read_json(directory / "diagnostic.json") if (directory / "diagnostic.json").exists() else {}
                    raise UncertainExecution(f"Build grant {token} retained pending verified daemon quiescence: "
                                             f"{diagnostic.get('error', 'No terminal receipt')}; records: {directory}")
                time.sleep(0.1)
        finally:
            if ledger.consumer_run(token) is None:
                ledger.release(token, owner_id=owner_id)
