"""Explicit project-local research authority, separate from the harness runtime."""

import json
import os
from pathlib import Path
import re
import sys
import venv

from labgoblin import payload
from labgoblin.protocol import require_version, table, text
from labgoblin.storage import hash_file, read_text


AUTHORIZATION = """You have broad autonomy for ordinary local scientific work toward
the research goal, within the admitted resources and data/disclosure boundaries.
This includes managing scientific packages in the recorded research environment,
retrieving public license-compatible code, data and pretrained model weights,
local model inference, training and fine-tuning, and other appropriate research
techniques. These are examples, not an exhaustive whitelist: an unlisted local
technique does not need separate permission merely because it is unlisted.
Explicit operator restrictions in the goal or project instructions take precedence.

Use the recorded absolute research Python for scientific commands and package
installation, and sys.executable for children of that interpreter. Use an already
installed uv with an explicit --python target, or this Python's pip. If pip is
absent, you may bootstrap it with this Python's -m ensurepip. Do not install uv,
change the harness runtime, shared environments, system software or drivers.
Keep download/build/model caches project-local; retain dependency locks/versions,
model source/license/revision, relevant settings and useful checkpoints in ordinary
research artifacts. Caches and the environment are not captured as research output.

Local scientific models are not additional research agents. This authorization
does not permit unmanaged agent/provider sessions, hosted model APIs, paid services,
remote compute, unadmitted GPUs, private-data uploads or access to secrets.
Keep computation in the owned process tree. Do not replace the research Python or
redirect its environment; changing packages there does not change the goal,
evaluation criteria, input permissions, resource grant or execution deadline.
"""


def paths(project):
    directory = Path(project) / ".venv"
    python = directory / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")
    return directory, python


def validate(project, value):
    table(value, "research environment", {"version", "directory", "python", "authorization", "identity"})
    require_version(value.get("version"), 1, "research environment")
    directory, python = paths(project)
    if value.get("directory") != str(directory) or value.get("python") != str(python):
        raise ValueError("Research environment must name this project's separate .venv")
    if len(text(value.get("authorization"), "research authorization").encode()) > 8192:
        raise ValueError("Research authorization exceeds 8192 UTF-8 bytes")
    if "identity" in value:
        fields = {"prefix", "base_prefix", "python", "python_version", "interpreter_sha256", "config_sha256"}
        identity = table(value["identity"], "research interpreter identity", fields)
        if set(identity) != fields:
            raise ValueError("Incomplete research interpreter identity")
        for field in fields:
            text(identity[field], field)
        for field in ("interpreter_sha256", "config_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", identity[field]):
                raise ValueError("Invalid research interpreter identity digest")
    return value


def check(project, value):
    validate(project, value)
    directory, python = paths(project)
    if (directory.resolve() == Path(sys.prefix).resolve()
            or Path(sys.executable).absolute().is_relative_to(directory)):
        raise ValueError("The research .venv is the harness runtime; use a separate harness environment")
    for path in (directory, python.parent, directory / "pyvenv.cfg"):
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise ValueError("Research environment directories/configuration cannot be aliases")
    config = dict(line.split("=", 1) for line in read_text(directory / "pyvenv.cfg", 8192).splitlines()
                  if "=" in line)
    config = {key.strip().lower(): item.strip().lower() for key, item in config.items()}
    if config.get("include-system-site-packages") != "false" or not python.is_file():
        raise ValueError("Research requires an isolated Python venv without shared site-packages")
    identity = value.get("identity")
    if identity is not None:
        if (hash_file(python) != identity["interpreter_sha256"]
                or hash_file(directory / "pyvenv.cfg") != identity["config_sha256"]):
            raise ValueError("Research interpreter or venv configuration changed after admission")


def inspect(project, value):
    check(project, value)
    output = payload.probe([
        value["python"], "-I", "-B", "-c",
        "import json,sys; print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'python':sys.executable,'python_version':sys.version}))",
    ], timeout=15, limit=4096)
    identity = json.loads(output)
    if (Path(identity["prefix"]).resolve() != Path(value["directory"])
            or Path(identity["base_prefix"]).resolve() == Path(identity["prefix"]).resolve()
            or Path(identity["python"]).absolute() != Path(value["python"])):
        raise ValueError("Research Python does not run in the recorded separate venv")
    return {**value, "identity": {
        **identity, "interpreter_sha256": hash_file(value["python"]),
        "config_sha256": hash_file(Path(value["directory"]) / "pyvenv.cfg")}}


def prepare(project):
    directory, python = paths(project)
    value = {"version": 1, "directory": str(directory), "python": str(python),
             "authorization": AUTHORIZATION}
    if not directory.exists() and not directory.is_symlink():
        directory.mkdir()
        # No pip, package downloads, base-Python upgrades or shared-environment writes.
        venv.EnvBuilder(with_pip=False, symlinks=False).create(directory)
    inspect(project, value)
    return value


def instructions(value):
    return ("\n\nRecorded local research authorization:\n" + value["authorization"]
            + "\nResearch environment: " + value["directory"]
            + "\nScientific Python (absolute path): " + value["python"]
            + "\nThe harness interpreter is only for harness machinery, not scientific package installation.\n")


def variables(value):
    cache = Path(value["directory"]) / "cache"
    return {
        "LABGOBLIN_RESEARCH_PYTHON": value["python"], "VIRTUAL_ENV": value["directory"],
        "PYTHONNOUSERSITE": "1",
        "PATH": str(Path(value["python"]).parent) + os.pathsep + os.environ.get("PATH", ""),
        "UV_CACHE_DIR": str(cache / "uv"), "PIP_CACHE_DIR": str(cache / "pip"),
        "HF_HOME": str(cache / "huggingface"), "TORCH_HOME": str(cache / "torch"),
        "XDG_CACHE_HOME": str(cache),
    }
