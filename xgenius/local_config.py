"""Versioned local execution contracts, independent of cluster configuration."""

from dataclasses import dataclass, field
import math
import re


def positive(value, name: str, *, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'} and finite")
    return value


def strings(value, name: str, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and "\0" not in x for x in value):
        raise ValueError(f"{name} must be an array of strings")
    if not empty and (not value or not value[0]):
        raise ValueError(f"{name} must not be empty")
    return value


@dataclass
class Runner:
    kind: str = "local"
    python: str = ""
    distro: str = ""
    image: str = ""
    context: str = ""
    network: bool = False


@dataclass
class LocalConfig:
    default_runner: str = "native"
    runners: dict[str, Runner] = field(default_factory=dict)
    cpus: int = 1
    memory_mb: int = 1024
    gpus: list[str] = field(default_factory=list)
    max_jobs: int = 1
    max_gpu_hours: float = 1
    max_turns: int = 10
    turn_timeout: float = 600
    max_seconds: float = 3600
    retries: int = 1
    command: list[str] = field(default_factory=list)
    sandbox: bool = False
    copilot_home: str = ""
    inputs: dict = field(default_factory=dict)
    source_files: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)


def parse_local(raw: dict) -> LocalConfig | None:
    version = raw.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Unsupported schema_version (expected 1 or 2)")
    if version == 1:
        if "execution" in raw or "runners" in raw:
            raise ValueError("Local execution settings require schema_version = 2")
        return None
    execution = raw.get("execution", {})
    campaign = raw.get("campaign", {})
    agent = raw.get("agent", {})
    for name, value in (("execution", execution), ("campaign", campaign), ("agent", agent),
                        ("runners", raw.get("runners", {})), ("inputs", raw.get("inputs", {}))):
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be a table")
    for name, value, allowed in [
        ("execution", execution, {"default_runner", "source_files", "environment"}),
        ("campaign", campaign, {"cpus", "memory_mb", "gpus", "max_jobs", "max_gpu_hours", "max_seconds"}),
        ("agent", agent, {"command", "max_turns", "timeout_seconds", "retries", "sandbox", "copilot_home"}),
    ]:
        if set(value) - allowed:
            raise ValueError(f"Unknown {name} settings: {sorted(set(value) - allowed)}")
    if type(agent.get("sandbox", False)) is not bool or not isinstance(agent.get("copilot_home", ""), str):
        raise ValueError("agent.sandbox must be boolean and copilot_home a path string")
    if "command" in agent and "trigger_command" in raw.get("watcher", {}):
        raise ValueError("Use agent.command or watcher.trigger_command, not both")
    runners = {}
    for name, value in raw.get("runners", {}).items():
        if not isinstance(value, dict):
            raise ValueError(f"runners.{name} must be a table")
        unknown = set(value) - set(Runner.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown runner settings: {sorted(unknown)}")
        runner = Runner(**value)
        if not all(isinstance(getattr(runner, k), str) for k in ("kind", "python", "distro", "image", "context")):
            raise ValueError("Runner identities and paths must be strings")
        if runner.kind not in ("local", "wsl", "docker"):
            raise ValueError(f"Unsupported local runner kind: {runner.kind}")
        if runner.kind == "wsl" and (not runner.distro or not runner.python):
            raise ValueError("WSL runners require explicit distro and python")
        if runner.kind == "docker" and (not runner.image or not runner.context):
            raise ValueError("Docker runners require explicit image and local context")
        if type(runner.network) is not bool:
            raise ValueError("runner.network must be boolean")
        runners[name] = runner
    default = execution.get("default_runner", "native")
    if default not in runners:
        raise ValueError(f"Default runner {default!r} is not defined")
    required = ("cpus", "memory_mb", "max_jobs", "max_gpu_hours", "max_seconds")
    for key in required:
        if key not in campaign:
            raise ValueError(f"campaign.{key} must be explicitly configured")
        positive(campaign[key], f"campaign.{key}", zero=key == "max_gpu_hours")
    for key in ("cpus", "memory_mb", "max_jobs"):
        if type(campaign[key]) is not int:
            raise ValueError(f"campaign.{key} must be an integer")
    max_turns = agent.get("max_turns", 10)
    timeout = agent.get("timeout_seconds", 600)
    retries = agent.get("retries", 1)
    positive(max_turns, "agent.max_turns")
    positive(timeout, "agent.timeout_seconds")
    positive(retries, "agent.retries", zero=True)
    if type(max_turns) is not int or type(retries) is not int:
        raise ValueError("Agent turn and retry limits must be integers")
    command = strings(agent["command"], "agent.command") if "command" in agent else []
    env = execution.get("environment", {})
    if not isinstance(env, dict) or not all(
            re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", k) and isinstance(v, str) and "\0" not in v
            for k, v in env.items()):
        raise ValueError("execution.environment must contain string values")
    inputs = raw.get("inputs", {})
    for name, value in inputs.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
            raise ValueError("Input names must be valid environment variable identifiers")
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            raise ValueError(f"inputs.{name} requires a path")
        if type(value.get("prompt_access", False)) is not bool:
            raise ValueError(f"inputs.{name}.prompt_access must be boolean")
    return LocalConfig(
        default_runner=default, runners=runners, cpus=campaign["cpus"],
        memory_mb=campaign["memory_mb"],
        gpus=strings(campaign.get("gpus", []), "campaign.gpus", empty=True),
        max_jobs=campaign["max_jobs"], max_gpu_hours=campaign["max_gpu_hours"],
        max_seconds=campaign["max_seconds"], max_turns=max_turns,
        turn_timeout=timeout, retries=retries, command=command,
        sandbox=agent.get("sandbox", False), copilot_home=agent.get("copilot_home", ""),
        inputs=inputs,
        source_files=strings(execution.get("source_files", []), "execution.source_files", empty=True),
        environment=env,
    )
