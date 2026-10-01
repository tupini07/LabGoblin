"""Provider argv adapters and resource-authorized invocation envelopes."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

from labgoblin.backends import command
from labgoblin.evidence import contained, hash_file, require_space
from labgoblin.payload import command_units, validate_command
from labgoblin.protocol import LaunchEnvelope, LaunchKey, PACKET_BYTES, Resources, identifier


CONTROLLED_FLAGS = {
    "-p", "--prompt", "-i", "--interactive", "-r", "--resume", "--continue", "--session-id",
    "--connect", "--fleet", "--share", "--share-gist", "--autopilot", "--plan", "--mode",
    "--remote", "--remote-export", "--acp", "--model", "--reasoning-effort", "--effort",
    "--usage-output-file", "--output-format", "--sandbox", "--log-dir",
}
COPILOT_FLAGS = ("--no-auto-update", "--no-remote", "--no-remote-export", "--no-ask-user")


def inspect_provider(agent, *, probe=None) -> dict:
    probe = probe or command
    for value in agent.command[1:]:
        flag = value.split("=", 1)[0]
        if flag in CONTROLLED_FLAGS or (value.startswith(("-p", "-r", "-i")) and not value.startswith("--")):
            raise ValueError(f"agent.command cannot override managed invocation flag {flag}")
    executable = shutil.which(agent.command[0])
    if executable is None:
        raise ValueError(f"Provider executable does not exist: {agent.command[0]}")
    if os.name == "nt" and Path(executable).suffix.casefold() in (".cmd", ".bat"):
        raise ValueError("Configure a direct executable argv (for example node plus its CLI script), not a shell shim")
    arguments = [str(Path(executable).absolute()), *agent.command[1:]]
    prefix = ["--no-auto-update"] if agent.provider == "copilot" else []
    help_text = probe([*arguments, *prefix, "--help"])
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", help_text))
    if "--prompt" not in flags and not re.search(r"(?:^|\s)-p(?:\s|,)", help_text):
        raise ValueError("Provider does not advertise non-interactive prompt support")
    if agent.provider == "copilot":
        missing = set(COPILOT_FLAGS) - flags
        if missing:
            raise ValueError(f"Installed Copilot lacks required managed-session flags: {sorted(missing)}")
    if agent.model and "--model" not in flags:
        raise ValueError("Installed provider does not advertise explicit model selection")
    effort_flag = "--reasoning-effort" if agent.provider == "copilot" else "--effort"
    if agent.reasoning_effort:
        if effort_flag not in flags:
            raise ValueError(f"Installed provider does not advertise {effort_flag}")
        position = help_text.index(effort_flag)
        section = "\n".join(help_text[position:].splitlines()[:3])
        choices = re.search(r"(?:possible values|choices):\s*([^\]\n]+)", section, re.I)
        if choices and agent.reasoning_effort not in {item.strip().strip("'\"") for item in choices[1].split(",")}:
            raise ValueError(f"Unsupported explicit reasoning effort: {agent.reasoning_effort}")
    if agent.sandbox and "--sandbox" not in flags:
        raise ValueError("Installed Copilot does not advertise --sandbox; refusing an unsandboxed fallback")
    return {"provider": agent.provider, "command": arguments, "flags": sorted(flags),
            "help_digest": hashlib.sha256(help_text.encode("utf-8")).hexdigest(),
            "configured_model": agent.model or None, "configured_effort": agent.reasoning_effort or None,
            "effective_model": None, "effective_effort": None,
            "launcher_assurance": "declared argv; provider defaults remain unknown"}


def invocation_command(adapter: dict, prompt: str, root: Path, *, sandbox=False) -> tuple[str, ...]:
    arguments = list(adapter["command"])
    flags = set(adapter["flags"])
    if adapter["provider"] == "copilot":
        arguments.extend(COPILOT_FLAGS)
        if "--stream" in flags:
            arguments.extend(["--stream", "off"])
        if "--log-level" in flags:
            arguments.extend(["--log-level", "error"])
        if "--usage-output-file" in flags:
            arguments.extend(["--usage-output-file", str(root / "usage.json")])
    if adapter["configured_model"]:
        arguments.extend(["--model", adapter["configured_model"]])
    if adapter["configured_effort"]:
        arguments.extend(["--reasoning-effort" if adapter["provider"] == "copilot" else "--effort",
                          adapter["configured_effort"]])
    if sandbox:
        arguments.extend(["--experimental", "--sandbox"])
    arguments.extend(["-p", prompt])
    return validate_command(arguments)


def prepare(state, config, turn_id: str, invocation_id: str, grant: dict, *, adapter=None) -> LaunchEnvelope:
    require_space(config.storage)
    with state.db.read() as conn:
        turn = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        invocation = conn.execute("SELECT * FROM invocations WHERE id=? AND turn_id=?",
                                  (invocation_id, turn_id)).fetchone()
        packet = conn.execute("SELECT * FROM packets WHERE turn_id=?", (turn_id,)).fetchone()
        bundle = list(conn.execute("SELECT id,kind FROM invocations WHERE turn_id=? ORDER BY bundle_position",
                                   (turn_id,)))
    if (not turn or not invocation or invocation["state"] != "reserved"
            or not packet or not packet["ready"] or not packet["path"]):
        raise ValueError("Provider invocation requires its reserved slot and ready owned packet")
    if grant["state"] != "granted" or grant["work_id"] != turn_id or grant["owner_id"] != state.id:
        raise ValueError("Provider invocation requires the exact turn's machine grant")
    cpu_ids = json.loads(grant["native_cpus"])
    resources = Resources(grant["cpus"], grant["memory_mb"])
    if len(cpu_ids) != resources.cpus or resources != config.agent.resources:
        raise ValueError("Provider grant does not match configured resources and native placement")
    packet_path = contained(state.root, packet["path"])
    if hash_file(packet_path, PACKET_BYTES) != packet["digest"]:
        raise ValueError("Published provider packet changed")
    adapter = adapter or inspect_provider(config.agent)
    root = contained(state.root, Path("turns") / turn_id)
    nonce = identifier()
    launch_root = root / "launches" / nonce
    result = root / "handoff.json"
    prompt = (
        f"Execute this one LabGoblin {turn['kind']} operation. Read the UTF-8 JSON packet at "
        f"{json.dumps(str(packet_path), ensure_ascii=False)} and follow its owned result protocol. "
        f"Write the required UTF-8 JSON result to {json.dumps(str(result), ensure_ascii=False)}. "
        "Do not start another provider, controller, workflow, or interactive session."
    )
    metadata = {
        "provider": adapter, "turn_id": turn_id, "packet_id": packet["id"],
        "packet_path": str(packet_path), "packet_digest": packet["digest"],
        "result_path": str(result), "log_bytes": config.storage.log_bytes, "cpu_ids": cpu_ids,
        "last_in_bundle": invocation_id == bundle[-1]["id"],
        "kind": invocation["kind"],
    }
    ledger_path, ledger_id = state.ledger_identity()
    executable_dir = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    environment = {
        "PATH": str(executable_dir) + os.pathsep + os.environ.get("PATH", ""),
        "LABGOBLIN_PROJECT": str(config.root), "LABGOBLIN_TURN_ID": turn_id,
        "LABGOBLIN_PACKET_ID": packet["id"], "LABGOBLIN_INVOCATION_ID": invocation_id,
        "LABGOBLIN_RESOURCE_DB": str(ledger_path),
    }
    if config.agent.copilot_home:
        environment["COPILOT_HOME"] = str(contained(state.root, config.root / config.agent.copilot_home))
    if config.agent.sandbox:
        from labgoblin.agent_policy import prepare_canary, sandbox_policy
        policy = sandbox_policy(config)
        metadata["sandbox_policy"] = policy
        if invocation["kind"] == "canary":
            prompt, canary = prepare_canary(policy, launch_root, nonce)
            metadata["canary"] = canary
        else:
            metadata["canary_invocation_id"] = next(row["id"] for row in bundle if row["kind"] == "canary")
    arguments = invocation_command(adapter, prompt, launch_root, sandbox=config.agent.sandbox)
    metadata["argv_utf16_units"] = command_units(arguments)
    return LaunchEnvelope(
        LaunchKey(state.id, turn["generation"], invocation_id, grant["token"], nonce),
        invocation["kind"], arguments, str(config.root), str(root), str(state.path),
        str(ledger_path), ledger_id,
        config.agent.timeout_seconds, resources, config.revision, environment, metadata)
