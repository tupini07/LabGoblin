"""Independent, deadline-bounded provider session with a durable exit receipt."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

from xgenius.backends import own_handle
from xgenius.config import load_config
from xgenius.payload import WindowsPayload
from xgenius.workspace import atomic_json, read_json


def main(manifest):
    request = read_json(Path(manifest))
    root = Path(manifest).parent
    config = load_config(request["config_path"])
    local = config.local
    atomic_json(root / "agent-handle.json", own_handle(request["id"]))
    command = local.command or config.watcher.command_args()
    argv = [*command, *(["--experimental", "--sandbox"] if local.sandbox else []),
            "-p", request["prompt"]]
    env = os.environ.copy()
    if Path(command[0]).stem.lower() == "claude":
        env.pop("ANTHROPIC_API_KEY", None)
    if local.copilot_home:
        env["COPILOT_HOME"] = local.copilot_home
    process = None
    code, error = 1, None
    started = time.monotonic()
    try:
        with (root / "stdout.log").open("wb") as out, (root / "stderr.log").open("wb") as err:
            if os.name == "nt":
                process = WindowsPayload(argv, str(Path(config.config_path).parent), env, out, err,
                                         {"cpus": local.cpus, "memory_mb": local.memory_mb})
            else:
                process = subprocess.Popen(argv, cwd=Path(config.config_path).parent, env=env,
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                           start_new_session=True)
            while process.poll() is None:
                if time.monotonic() - started >= local.turn_timeout:
                    raise TimeoutError("Agent walltime limit exceeded")
                time.sleep(0.1)
            code = process.poll()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        if process is not None:
            if os.name == "nt":
                process.close()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        atomic_json(root / "agent-completion.json", {
            "token": request["id"], "returncode": code, "error": error,
            "elapsed": time.monotonic() - started, "provider_usage": None,
        })


if __name__ == "__main__":
    main(sys.argv[1])
