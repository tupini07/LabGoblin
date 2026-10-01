"""Owned stdio relay for setup; no research admission or independent lifetime."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from labgoblin.payload import WindowsPayload, _close_linux
from labgoblin.processes import process_state


def relay(arguments, parent):
    if process_state(parent) != "alive":
        raise RuntimeError("Setup parent is no longer alive")
    if os.name == "nt":
        if arguments[0].lower().endswith((".cmd", ".bat")):
            arguments = [os.environ["COMSPEC"], "/d", "/s", "/c", subprocess.list2cmdline(arguments)]
        process = WindowsPayload(arguments, os.getcwd(), dict(os.environ),
                                 sys.stdout.buffer, sys.stderr.buffer, {}, stdin=sys.stdin.buffer)
    else:
        process = subprocess.Popen(arguments, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                                   start_new_session=True)
    try:
        while process.poll() is None:
            if process_state(parent) != "alive":
                raise RuntimeError("Setup parent exited; retiring its runtime")
            time.sleep(0.1)
        return process.poll()
    finally:
        if os.name == "nt":
            process.close()
        else:
            _close_linux(process)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A local runtime executable is required")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    try:
        return relay(command, json.loads(args.parent))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Setup runtime: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
