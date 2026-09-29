"""Local backend launch and inspection with explicit ownership and path semantics."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import psutil

from xgenius import payload
from xgenius.workspace import atomic_json, read_json


def command(argv, *, timeout=30) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def alive(handle: dict | None) -> bool:
    if not handle:
        return False
    try:
        process = psutil.Process(handle["pid"])
        return process.create_time() == handle["created"] and process.is_running()
    except psutil.NoSuchProcess:
        return False


def own_handle(token: str) -> dict:
    return {"pid": os.getpid(), "created": psutil.Process().create_time(), "token": token}


def launch_independent(argv, root: Path):
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP |
                          subprocess.CREATE_BREAKAWAY_FROM_JOB),
    }
    with (root / "supervisor.stdout.log").open("ab") as out, (root / "supervisor.stderr.log").open("ab") as err:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **options)


def docker_prefix(runner: dict) -> list[str]:
    return ["docker", "--context", runner["context"]]


def validate_docker_endpoint(context: str):
    endpoint = command(["docker", "context", "inspect", context,
                        "--format", "{{.Endpoints.docker.Host}}"])
    if not (endpoint.startswith("npipe:////./pipe/") or endpoint.startswith("unix:///")):
        raise ValueError("Local execution refuses remote Docker endpoints")


def validate_runner(runner: dict, gpu_ids: list[str] | None = None):
    kind = runner["kind"]
    if kind == "local":
        python = runner["python"] or sys.executable
        command([python, "--version"])
    elif kind == "wsl":
        if os.name != "nt":
            raise ValueError("WSL runner requires the Windows control plane")
        if runner["distro"] == "docker-desktop":
            raise ValueError("Use a development distro, not Docker Desktop's internal distro")
        command(["wsl", "-d", runner["distro"], "--exec", runner["python"], "--version"])
    elif kind == "docker":
        prefix = docker_prefix(runner)
        validate_docker_endpoint(runner["context"])
        if command([*prefix, "info", "--format", "{{.OSType}}"]) != "linux":
            raise ValueError("Docker runner requires a Linux engine")
        command([*prefix, "image", "inspect", runner["image"], "--format", "{{.Id}}"])
    else:
        raise ValueError(f"Unknown local runner: {kind}")
    if gpu_ids:
        if kind == "wsl":
            probe = ("import shutil,subprocess,sys;"
                     "subprocess.run([shutil.which('nvidia-smi') or '/usr/lib/wsl/lib/nvidia-smi',"
                     "*sys.argv[1:]],check=True)")
            listing = command(["wsl", "-d", runner["distro"], "--exec", runner["python"], "-c", probe,
                               "--query-gpu=uuid", "--format=csv,noheader"])
        else:
            listing = command(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"])
        if not set(gpu_ids).issubset(set(listing.splitlines())):
            raise ValueError("Requested GPU UUID is not visible to this runner")


def wsl_path(runner: dict, path: str) -> str:
    return command(["wsl", "-d", runner["distro"], "--exec", "wslpath", "-a", "-u", path])


def payload_command(spec: dict) -> tuple[list[str], dict]:
    runner = spec["runner"]
    kind = runner["kind"]
    root = Path(spec["root"])
    shutil.copyfile(payload.__file__, root / "payload.py")
    resolved = json.loads(json.dumps(spec))
    interpreter = runner["python"] or ("python" if kind == "docker" else sys.executable)
    for args in [resolved["argv"], *resolved["validators"], *resolved.get("input_validators", [])]:
        if args[0] in ("python", "python3"):
            args[0] = interpreter
    if kind == "local":
        if resolved["argv"][0] in ("python", "python3"):
            resolved["argv"][0] = runner["python"] or sys.executable
        atomic_json(root / "payload.json", resolved)
        return [sys.executable, str(root / "payload.py"), str(root / "payload.json")], {}
    if kind == "wsl":
        for key in ("root", "cwd", "output"):
            resolved[key] = wsl_path(runner, spec[key])
        for name, item in resolved["inputs"].items():
            item["access_path"] = item.get("wsl_path") or wsl_path(runner, item["access_path"])
            command(["wsl", "-d", runner["distro"], "--exec", "test", "-e", item["access_path"]])
        if resolved["argv"][0] in ("python", "python3"):
            resolved["argv"][0] = runner["python"]
        atomic_json(root / "payload.json", resolved)
        guest_root = resolved["root"]
        return ["wsl", "-d", runner["distro"], "--exec", runner["python"],
                f"{guest_root}/payload.py", f"{guest_root}/payload.json"], {}
    prefix = docker_prefix(runner)
    validate_docker_endpoint(runner["context"])
    image_id = command([*prefix, "image", "inspect", runner["image"], "--format", "{{.Id}}"])
    name = f'xgenius-{spec["id"]}'
    source = root / "source"
    resolved["root"] = "/attempt"
    resolved["cwd"] = "/source/" + Path(spec["cwd"]).relative_to(source).as_posix()
    resolved["output"] = "/output"
    args = [*prefix, "create", "--name", name, "--label", f'xgenius.attempt={spec["id"]}',
            "--cpus", str(spec["cpus"]), "--memory", f'{spec["memory_mb"]}m',
            "--restart", "no", "--network", "bridge" if runner["network"] else "none",
            "--mount", f"type=bind,source={root},target=/attempt",
            "--mount", f"type=bind,source={source},target=/source,readonly",
            "--mount", f"type=bind,source={source},target=/attempt/source,readonly",
            "--mount", f'type=bind,source={spec["output"]},target=/output']
    for name_in, item in resolved["inputs"].items():
        host_path = item["access_path"]
        item["access_path"] = f"/inputs/{name_in}"
        args.extend(["--mount", f'type=bind,source={host_path},target={item["access_path"]},readonly'])
    if spec["gpus"]:
        args.extend(["--gpus", "device=" + ",".join(spec["gpus"])])
    atomic_json(root / "payload.json", resolved)
    # A deterministic label/name makes a create-before-persist crash reconcilable.
    container_id = command([*args, "--entrypoint", runner["python"] or "python",
                            image_id, "/attempt/payload.py", "/attempt/payload.json"])
    handle = {"container_id": container_id, "image_id": image_id}
    atomic_json(root / "backend.json", handle)
    return [*prefix, "start", "--attach", container_id], handle


def inspect_payload(spec: dict) -> str:
    """Return alive/dead/unknown without trusting a recycled PID."""
    root = Path(spec["root"])
    runner = spec["runner"]
    if runner["kind"] == "docker":
        validate_docker_endpoint(runner["context"])
        name = f'xgenius-{spec["id"]}'
        result = subprocess.run([*docker_prefix(runner), "inspect", name],
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            return "unknown"
        value = json.loads(result.stdout)[0]
        if value["Config"]["Labels"].get("xgenius.attempt") != spec["id"]:
            raise ValueError("Container ownership mismatch")
        return "alive" if value["State"]["Running"] else "dead"
    path = root / "payload-handle.json"
    if not path.exists():
        return "unknown"
    handle = read_json(path)
    if handle.get("token") != spec["id"]:
        raise ValueError("Payload ownership token mismatch")
    if runner["kind"] == "local":
        if os.name == "nt":
            return "alive" if alive(handle) else "dead"
        try:
            return payload.inspect_linux(root, spec["id"])
        except OSError:
            return "unknown"
    if "boot_id" not in handle:
        return "unknown"
    try:
        guest_root = wsl_path(runner, spec["root"])
        return command(["wsl", "-d", runner["distro"], "--exec", runner["python"],
                        f"{guest_root}/payload.py", "--inspect", f"{guest_root}/payload.json"])
    except (RuntimeError, subprocess.TimeoutExpired):
        return "unknown"
