import errno
import json
import os
from pathlib import Path
import sys
import threading
import time

import psutil
import pytest

from labgoblin import payload
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


@pytest.mark.skipif(os.name == "nt", reason="Linux nested process-group/session ownership")
def test_nested_evaluator_remains_in_outer_owned_session(spec):
    nested = {**spec, "root": str(Path(spec["cwd"]) / "nested"), "token": identifier(),
              "seconds": 60, "parent_session": True}
    nested["argv"] = [sys.executable, "-c",
                      "import os,json,time;print(json.dumps({'session':os.getsid(0),'group':os.getpgrp()}),flush=True);time.sleep(60)"]
    code = ("import sys;sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[1])) + ");"
            "from labgoblin.payload import run_process;run_process(" + repr(nested) + ")")
    spec.update(argv=[sys.executable, "-c", code], seconds=3)
    outcome = run_process(spec)
    assert outcome["status"] == "timed_out"
    outer = read_json(Path(spec["root"]) / "process-handle.json")
    child = read_json(Path(nested["root"]) / "process-handle.json")
    identity = read_json(Path(nested["root"]) / "stdout.log")
    assert identity == {"session": outer["pid"], "group": child["pid"]}
    assert tree_state(spec["root"], spec["token"]) == "dead"
    assert tree_state(nested["root"], nested["token"]) == "dead"


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


@pytest.mark.parametrize("mode,winerror", [("complete", 5), ("cancel", 32), ("deadline", 33)])
def test_heartbeat_access_failures_preserve_supervision(spec, monkeypatch, capsys, mode, winerror):
    original = payload.atomic_json
    attempts = []
    signal = Path(spec["cancel_path"] if mode == "cancel" else Path(spec["cwd"]) / "finish")
    spec.update(seconds=5, provider="copilot", shutdown_grace_seconds=2)
    if mode == "deadline":
        spec.update(seconds=1.2, argv=[sys.executable, "-c", "import time;time.sleep(60)"])
    else:
        code = ("import pathlib,sys,time\n"
                f"signal=pathlib.Path({str(signal)!r})\n"
                "while not signal.exists():time.sleep(.01)\n"
                "time.sleep(.2)\nprint('settled',flush=True)\n"
                f"sys.exit({130 if mode == 'cancel' else 0})")
        spec["argv"] = [sys.executable, "-c", code]

    def blocked(path, value):
        if Path(path).name == "heartbeat.json":
            attempts.append(time.monotonic())
            if len(attempts) == 2 and mode != "deadline":
                signal.touch()
            error = PermissionError("blocked heartbeat")
            error.winerror = winerror
            raise error
        return original(path, value)

    monkeypatch.setattr(payload, "atomic_json", blocked)
    outcome = run_process(spec)
    if mode == "deadline":
        assert outcome["status"] == "timed_out", outcome
    else:
        assert outcome["returncode"] == (130 if mode == "cancel" else 0), outcome
        assert "settled" in (Path(spec["root"]) / "stdout.log").read_text()
        if mode == "complete":
            assert outcome["status"] == "completed", outcome
    assert outcome["elapsed"] < 5
    assert attempts
    assert all(later - earlier >= .95 for earlier, later in zip(attempts, attempts[1:]))
    assert outcome["heartbeat"]["write_failures"] == len(attempts)
    assert outcome["heartbeat"]["last_success_elapsed"] is None
    assert "blocked heartbeat" in outcome["heartbeat"]["last_error"]
    assert capsys.readouterr().err.count("Heartbeat publication unavailable") == 1
    assert_dead(read_json(Path(spec["root"]) / "process-handle.json"))
    assert tree_state(spec["root"], spec["token"]) == "dead"


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows file replacement sharing violation")
def test_heartbeat_recovers_after_windows_reader_releases_file(spec, monkeypatch, capsys):
    import win32con
    import win32file

    original = payload.atomic_json
    heartbeat = Path(spec["root"]) / "heartbeat.json"
    original(heartbeat, {"token": "previous"})
    held = win32file.CreateFile(str(heartbeat), win32con.GENERIC_READ,
                               win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                               None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
    errors = []

    def publish(path, value):
        nonlocal held
        try:
            return original(path, value)
        except PermissionError as error:
            if Path(path) != heartbeat:
                raise
            errors.append(error.winerror)
            held.Close()
            held = None
            raise

    code = ("import json,pathlib,time\n"
            f"path=pathlib.Path({str(heartbeat)!r})\n"
            f"while json.loads(path.read_text())['token']!={spec['token']!r}:time.sleep(.01)\n"
            "print('complete',flush=True)")
    spec["argv"] = [sys.executable, "-c", code]
    monkeypatch.setattr(payload, "atomic_json", publish)
    try:
        outcome = run_process(spec)
    finally:
        if held is not None:
            held.Close()
    assert outcome["status"] == "completed", outcome
    assert len(errors) == 1 and errors[0] in (5, 32, 33)
    assert outcome["heartbeat"]["write_failures"] == 1
    assert outcome["heartbeat"]["last_success_elapsed"] > outcome["heartbeat"]["last_failure_elapsed"]
    assert read_json(heartbeat)["token"] == spec["token"]
    assert not list(heartbeat.parent.glob("heartbeat.json.*"))
    assert "Heartbeat publication unavailable" in capsys.readouterr().err
    assert tree_state(spec["root"], spec["token"]) == "dead"


@pytest.mark.parametrize("target,kind", [
    ("process-handle.json", "sharing"),
    ("heartbeat.json", "permission"),
    ("heartbeat.json", "disk"),
])
def test_heartbeat_recovery_does_not_hide_other_publication_failures(spec, monkeypatch, target, kind):
    original = payload.atomic_json
    handles = []
    spec["argv"] = [sys.executable, "-c", "import time;time.sleep(60)"]

    def denied(path, value):
        if Path(path).name == "process-handle.json":
            handles.append(value)
        if Path(path).name == target:
            if kind == "disk":
                raise OSError(errno.ENOSPC, "publication denied")
            error = PermissionError("publication denied")
            if kind == "sharing":
                error.winerror = 5
            raise error
        return original(path, value)

    monkeypatch.setattr(payload, "atomic_json", denied)
    outcome = run_process(spec)
    assert outcome["status"] == "failed"
    assert "publication denied" in outcome["reason"]
    assert len(handles) == 1
    assert_dead(handles[0])
    expected = "unknown" if os.name != "nt" and target == "process-handle.json" else "dead"
    assert tree_state(spec["root"], spec["token"]) == expected
