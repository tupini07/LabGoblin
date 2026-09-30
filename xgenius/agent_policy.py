"""Frozen provider supervision and explicitly budgeted sandbox checks."""

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from xgenius.evidence import Capture, contained, hash_file, parse_json, publish_bytes, read_bytes, read_json
from xgenius.payload import execute_spec
from xgenius.protocol import HANDOFF_BYTES, Handoff, LaunchReceipt, PACKET_BYTES, PreExecutionError, canonical, fingerprint


def sandbox_policy(config) -> dict:
    home = contained(config.state_dir, config.root / config.agent.copilot_home)
    if not home.is_dir():
        raise ValueError("Sandbox requires an already provisioned campaign-local Copilot profile")
    path = contained(home, "settings.json")
    body = read_bytes(path, 65536)
    settings = parse_json(body)
    sandbox = settings.get("sandbox", {})
    if sandbox.get("enabled") is not True or sandbox.get("allowBypass") is not False:
        raise ValueError("Sandbox profile must enable sandboxing and set allowBypass=false")
    denied = contained(home, Path("preflight") / "denied")
    declared = sandbox.get("userPolicy", {}).get("filesystem", {}).get("deniedPaths", [])
    if str(denied) not in declared:
        raise ValueError(f"Provision the sandbox policy to deny this exact canary directory: {denied}")
    return {"home": str(home), "settings_path": str(path),
            "settings_digest": hashlib.sha256(body).hexdigest(), "denied_root": str(denied)}


def prepare_canary(policy: dict, root: Path, nonce: str) -> tuple[str, dict]:
    denied = Path(policy["denied_root"]) / f"{nonce}.txt"
    marker = b"xgenius sandbox marker\n"
    publish_bytes(denied, marker)
    result = root / "canary-result.json"
    script = root / "canary.py"
    body = (
        "import json,pathlib\n"
        f"denied=pathlib.Path({str(denied)!r})\n"
        "blocked=False\n"
        "try: denied.write_text('changed',encoding='utf-8')\n"
        "except PermissionError: blocked=True\n"
        f"pathlib.Path({str(result)!r}).write_text(json.dumps(dict("
        f"challenge={nonce!r},allowed_write=True,denied_write_blocked=blocked)),encoding='utf-8')\n"
    ).encode("utf-8")
    publish_bytes(script, body)
    prompt = (
        "This is one sandbox canary, not a research turn. Do not bypass sandbox policy. "
        f"Run exactly this argv with your shell tool: {json.dumps([sys.executable, str(script)])}. "
        "Do not edit any files or author the receipt yourself. If it fails, report the error and stop."
    )
    return prompt, {"challenge": nonce, "script": str(script), "script_digest": hashlib.sha256(body).hexdigest(),
                    "result": str(result), "denied": str(denied), "marker_digest": hashlib.sha256(marker).hexdigest()}


def _verify_policy(envelope):
    policy = envelope.metadata.get("sandbox_policy")
    if policy is None:
        return
    if hash_file(Path(policy["settings_path"]), 65536) != policy["settings_digest"]:
        raise PreExecutionError("Sandbox settings changed after authorization")
    if envelope.kind == "canary":
        canary = envelope.metadata["canary"]
        if hash_file(Path(canary["script"]), 65536) != canary["script_digest"]:
            raise PreExecutionError("Owned sandbox canary changed before invocation")
        return
    from xgenius.state import State
    state = State.open(Path(envelope.state_path).parent)
    with state.db.read() as conn:
        invocation = conn.execute("SELECT * FROM invocations WHERE id=? AND turn_id=? AND kind='canary'",
                                  (envelope.metadata["canary_invocation_id"], envelope.metadata["turn_id"])).fetchone()
        launch = conn.execute("SELECT receipt,envelope FROM launches WHERE nonce=?",
                              (invocation["nonce"],)).fetchone() if invocation else None
    if not invocation or invocation["state"] != "completed" or not launch or not launch["receipt"]:
        raise PreExecutionError("Required budgeted sandbox canary has not completed")
    receipt = LaunchReceipt.parse(json.loads(launch["receipt"]))
    previous = json.loads(launch["envelope"])["metadata"]["sandbox_policy"]
    if not receipt.metadata.get("sandbox_probe", {}).get("passed") or previous != policy:
        raise PreExecutionError("Sandbox canary does not establish this operation's exact policy")


def supervise(envelope) -> LaunchReceipt:
    from xgenius.backends import native_spec
    from xgenius.worker import launch_directory
    try:
        if hash_file(Path(envelope.metadata["packet_path"]), PACKET_BYTES) != envelope.metadata["packet_digest"]:
            raise PreExecutionError("Owned inference packet changed before provider start")
        if not envelope.metadata.get("cpu_ids"):
            raise PreExecutionError("Managed provider invocation lacks native CPU placement")
        _verify_policy(envelope)
    except (OSError, ValueError, sqlite3.Error) as error:
        raise PreExecutionError(f"Provider pre-execution validation failed: {error}") from error
    spec = native_spec(envelope)
    spec["provider"] = envelope.metadata["provider"]["provider"]
    receipt = execute_spec(spec, publish_receipt=False)
    metadata = {**receipt.metadata, "provider": envelope.metadata["provider"], "usage_status": "unavailable"}
    usage = None
    usage_path = launch_directory(envelope) / "usage.json"
    if usage_path.exists():
        try:
            usage = read_json(usage_path, 256 * 1024)
            if not isinstance(usage, dict):
                raise ValueError("Provider usage must be a JSON object")
            metadata["usage_status"] = "recorded"
        except (OSError, ValueError) as error:
            usage = None
            metadata["usage_status"] = "invalid"
            metadata["usage_error"] = str(error)[:2000]
    if envelope.kind == "canary" and receipt.status == "completed":
        canary = envelope.metadata["canary"]
        try:
            result = read_json(Path(canary["result"]), 16384)
            if (result != {"challenge": canary["challenge"], "allowed_write": True, "denied_write_blocked": True}
                    or hash_file(Path(canary["denied"]), 65536) != canary["marker_digest"]):
                raise ValueError("Sandbox denied-write canary failed")
            metadata["sandbox_probe"] = {"passed": True, "scope": "one shell denied-write check; not a security proof"}
        except (OSError, ValueError) as error:
            receipt = replace(receipt, status="failed", reason=f"Sandbox canary failed: {error}")
            metadata["sandbox_probe"] = {"passed": False}
    elif receipt.status == "completed":
        result_capture = {}
        try:
            captured = Capture.read(Path(envelope.metadata["result_path"]), HANDOFF_BYTES)
            result_capture.update(digest=captured.digest, bytes=len(captured.body))
            result = parse_json(captured.body)
            if (not isinstance(result, dict) or result.get("turn_id") != envelope.metadata["turn_id"]
                    or result.get("packet_id") != envelope.metadata["packet_id"]):
                raise ValueError("Result does not belong to this turn and packet")
            if envelope.kind in ("research", "final_analysis"):
                result_capture["handoff_digest"] = fingerprint(asdict(Handoff.parse(result)))
            result_capture["result_digest"] = fingerprint(result)
        except (OSError, ValueError) as error:
            result_capture["error"] = str(error)[:2000]
        metadata["result_capture"] = result_capture
    receipt = replace(receipt, usage=usage, metadata=metadata)
    publish_bytes(launch_directory(envelope) / "backend-receipt.json", canonical(asdict(receipt)))
    return receipt
