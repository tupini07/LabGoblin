import json
import os
from pathlib import Path
import sys
import threading
import time

import psutil
import pytest

from labgoblin.payload import run_process, tree_state
from labgoblin.protocol import identifier
from labgoblin.storage import read_json


@pytest.fixture
def spec(tmp_path):
    token = identifier()
    return {"argv": [sys.executable, "-c", "print('complete')"], "cwd": str(tmp_path),
            "root": str(tmp_path / "process"), "token": token, "cpus": 1,
            "cpu_ids": psutil.Process().cpu_affinity()[:1], "memory_mb": 256, "gpus": [],
            "seconds": 10, "log_bytes": 1024, "job_name": f"labgoblin-{token}",
            "cancel_path": str(tmp_path / "cancel")}


def assert_dead(handle):
    try:
        process = psutil.Process(handle["pid"])
        if "created" in handle and process.create_time() != handle["created"]:
            return
        process.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass


@pytest.mark.parametrize("cancel", [False, True])
def test_flooding_cannot_disable_deadline_or_cancel(spec, cancel):
    spec["argv"] = [sys.executable, "-c", "import os\nwhile True: os.write(1,b'x'*10000)"]
    timer = None
    if cancel:
        timer = threading.Timer(0.5, lambda: Path(spec["cancel_path"]).touch())
        timer.start()
    else:
        spec["seconds"] = 0.5
    try:
        outcome = run_process(spec)
    finally:
        if timer:
            timer.join()
    assert outcome["status"] == ("cancelled" if cancel else "timed_out")
    assert outcome["elapsed"] < 8
    assert outcome["logs"]["stdout"]["retained_bytes"] <= spec["log_bytes"]
    assert_dead(read_json(Path(spec["root"]) / "process-handle.json"))
    assert tree_state(spec["root"], spec["token"]) == "dead"


def test_cancel_before_start_never_executes(spec):
    Path(spec["cancel_path"]).touch()
    outcome = run_process(spec)
    assert outcome["status"] == "cancelled" and not outcome["executed"]
    assert not (Path(spec["root"]) / "process-handle.json").exists()


def test_missing_executable_is_not_started(spec):
    spec["argv"] = [str(Path(spec["cwd"]) / "missing-program")]
    outcome = run_process(spec)
    assert outcome["status"] == "not_started" and not outcome["executed"]
    assert outcome["reason"]


def test_payload_retires_descendants_and_preserves_assigned_cpus(spec):
    child = ("import json,psutil,time;p=psutil.Process();"
             "print(json.dumps({'pid':p.pid,'created':p.create_time(),'cpus':p.cpu_affinity()}),flush=True);"
             "time.sleep(60)")
    code = ("import subprocess,sys;"
            f"child=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.PIPE,text=True);"
            "print(child.stdout.readline(),end='',flush=True)")
    spec["argv"] = [sys.executable, "-c", code]
    outcome = run_process(spec)
    assert outcome["status"] == "completed", outcome
    handle = json.loads((Path(spec["root"]) / "stdout.log").read_text())
    assert handle["cpus"] == spec["cpu_ids"]
    assert_dead(handle)
    assert tree_state(spec["root"], spec["token"]) == "dead"


def test_cooperative_cancel_has_time_to_flush_but_not_extend_deadline(spec):
    spec.update(provider="copilot", shutdown_grace_seconds=2)
    code = ("import pathlib,time,sys,os\n"
            "assert float(os.environ['LABGOBLIN_OPERATION_DEADLINE'])>time.monotonic()\n"
            "assert 'LABGOBLIN_OUTPUT_DIR' not in os.environ\n"
            f"cancel=pathlib.Path({spec['cancel_path']!r})\n"
            "while not cancel.exists():time.sleep(.01)\n"
            "time.sleep(.2)\nprint('settled',flush=True)\nsys.exit(130)")
    spec["argv"] = [sys.executable, "-c", code]
    timer = threading.Timer(0.5, lambda: Path(spec["cancel_path"]).touch())
    timer.start()
    try:
        outcome = run_process(spec)
    finally:
        timer.join()
    assert outcome["returncode"] == 130
    assert "settled" in (Path(spec["root"]) / "stdout.log").read_text()
    assert outcome["elapsed"] < 5
