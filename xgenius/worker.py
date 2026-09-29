"""Independent host supervisor; retains resource ownership after controller exit."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from xgenius.backends import own_handle, payload_command
from xgenius.config import load_config
from xgenius.processes import background_options
from xgenius.scheduler import ResourceLedger
from xgenius.state import LocalState
from xgenius.workspace import atomic_json, collect_artifacts, read_json


def main(spec_path):
    spec = read_json(Path(spec_path))
    root = Path(spec["root"])
    state = LocalState(load_config(spec["config_path"]))
    ledger = ResourceLedger()
    handle = own_handle(spec["id"])
    atomic_json(root / "supervisor.json", handle)
    state.transition(spec["id"], "starting", handle=handle)
    process = None
    started = time.time()
    try:
        argv, _ = payload_command(spec)
        with (root / "backend.stdout.log").open("wb") as out, (root / "backend.stderr.log").open("wb") as err:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       **background_options())
            state.transition(spec["id"], "running", handle=handle)
            while process.poll() is None:
                atomic_json(root / "supervisor-heartbeat.json",
                            {"token": spec["id"], "time": time.time()})
                time.sleep(0.25)
        receipt_path = root / "completion.json"
        if not receipt_path.exists():
            raise RuntimeError(
                f"Backend launcher exited {process.returncode} without a completion receipt; "
                f"inspect {root / 'backend.stderr.log'}")
        receipt = read_json(receipt_path)
        if receipt.get("token") != spec["id"]:
            raise RuntimeError("Completion token mismatch")
        if receipt["status"] == "completed":
            try:
                if receipt.get("validation_errors"):
                    raise ValueError("; ".join(receipt["validation_errors"]))
                collect_artifacts(state, spec)
            except (ValueError, OSError) as e:
                receipt["reason"] = f"Artifact validation failed: {e}"
                state.event("validation_failed", {"attempt_id": spec["id"], "reason": str(e)},
                            f'validation-{spec["id"]}')
        state.transition(spec["id"], receipt["status"], reason=receipt["reason"], receipt=receipt)
        ledger.finish(spec["id"])
    except Exception as e:
        atomic_json(root / "supervisor-error.json",
                    {"token": spec["id"], "error": f"{type(e).__name__}: {e}",
                     "elapsed": time.time() - started})
        # Backend liveness may be unknown after a failed launch/engine disconnect.
        state.transition(spec["id"], "recovery_required", reason=str(e), handle=handle)
        print(f"Worker recovery required: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
