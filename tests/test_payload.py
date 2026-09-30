from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import threading

import psutil
import pytest

from xgenius.evidence import read_json, tail
from xgenius.payload import command_units, execute_spec, run_process
from xgenius.protocol import LaunchEnvelope, LaunchKey, Resources


@pytest.fixture
def spec(tmp_path):
    cpu = psutil.Process().cpu_affinity()[0]
    return {"argv": [sys.executable, "-c", "print('complete')"], "cwd": str(tmp_path),
            "root": str(tmp_path / "process"), "token": "fixture", "cpus": 1,
            "cpu_ids": [cpu], "memory_mb": 256, "gpus": [], "seconds": 10,
            "log_bytes": 1024, "environment": {}, "cancel_path": str(tmp_path / "cancel")}


def test_real_payload_retains_bounded_output_and_reaps(spec):
    spec["argv"] = [sys.executable, "-c",
                    "import sys;sys.stdout.write('x'*200000);sys.stderr.write('y'*150000)"]
    result = run_process(spec)
    assert result["status"] == "completed", result
    assert result["executed"]
    assert result["logs"]["stdout"]["received_bytes"] == 200000
    assert result["logs"]["stderr"]["received_bytes"] == 150000
    for stream in result["logs"].values():
        assert stream["truncated"]
        assert stream["retained_bytes"] <= spec["log_bytes"]
    for name in ("stdout", "stderr"):
        path = Path(spec["root"]) / f"{name}.log"
        assert path.stat().st_size + path.with_name(path.name + ".previous").stat().st_size <= 1024


def test_cancellation_before_start_does_not_execute(spec):
    Path(spec["cancel_path"]).touch()
    result = run_process(spec)
    assert result["status"] == "cancelled"
    assert result["executed"] is False
    assert not (Path(spec["root"]) / "process-handle.json").exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_flooding_payload_still_obeys_deadline_and_cancellation(spec, cancel):
    spec["argv"] = [sys.executable, "-c", "import os\nwhile True: os.write(1,b'x'*10000)"]
    timer = None
    if cancel:
        timer = threading.Timer(0.4, lambda: Path(spec["cancel_path"]).touch())
        timer.start()
    else:
        spec["seconds"] = 0.4
    try:
        result = run_process(spec)
    finally:
        if timer:
            timer.join()
    assert result["status"] == ("cancelled" if cancel else "timed_out"), result
    assert result["elapsed"] < 8
    assert result["logs"]["stdout"]["retained_bytes"] <= spec["log_bytes"]
    handle = read_json(Path(spec["root"]) / "process-handle.json")
    assert not psutil.pid_exists(handle["pid"])


def test_missing_executable_is_proven_not_started(spec):
    spec["argv"] = [str(Path(spec["root"]) / "missing-program")]
    result = run_process(spec)
    assert result["status"] == "not_started", result
    assert not result["executed"]
    assert result["reason"]


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited Job Object and CPU placement")
def test_descendants_inherit_exact_assigned_cpu_set_and_are_reaped(spec):
    selected = psutil.Process().cpu_affinity()[-1]
    spec["cpu_ids"] = [selected]
    child = "import os,time;print(os.getpid(),flush=True);time.sleep(60)"
    parent = ("import ctypes,subprocess,sys;from ctypes import wintypes;"
              "mask=ctypes.c_size_t();system=ctypes.c_size_t();"
              "kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=wintypes.HANDLE;"
              "kernel.GetProcessAffinityMask.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p];"
              "kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(),ctypes.byref(mask),ctypes.byref(system));"
              "print('mask='+str(mask.value),flush=True);"
              f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
              "import time;time.sleep(0.25)")
    spec["argv"] = [sys.executable, "-c", parent]
    result = run_process(spec)
    assert result["status"] == "completed", result
    output = tail(Path(spec["root"]) / "stdout.log", limit=1024)["text"].splitlines()
    assert output[0] == f"mask={1 << selected}"
    assert len(output) == 2
    assert not psutil.pid_exists(int(output[1]))


def envelope_spec(spec):
    envelope = LaunchEnvelope(
        LaunchKey("campaign", 1, "work", "grant", "fixture"), "attempt",
        tuple(spec["argv"]), spec["cwd"], spec["root"], "state.db", "ledger.db", "ledger",
        spec["seconds"], Resources(1, 256), "config")
    return {**spec, "envelope": asdict(envelope), "envelope_digest": envelope.digest,
            "input_validators": [], "validators": []}


def test_guest_file_claim_is_one_time_and_receipt_is_fully_qualified(spec):
    spec = envelope_spec(spec)
    first = execute_spec(spec)
    assert first.status == "completed", first
    assert first.envelope_digest == spec["envelope_digest"]
    stored = read_json(Path(spec["root"]) / "backend-receipt.json")
    assert stored["key"] == spec["envelope"]["key"]
    with pytest.raises(FileExistsError):
        execute_spec(spec)


def test_input_validator_refusal_prevents_main_payload(spec):
    spec = envelope_spec(spec)
    spec["input_validators"] = [[sys.executable, "-c", "raise SystemExit(3)"]]
    result = execute_spec(spec)
    assert result.status == "failed"
    assert result.executed
    assert not result.metadata["payload_executed"]
    assert not (Path(spec["root"]) / "main").exists()
    assert "Input validation" in result.reason


def test_consumption_pin_refusal_still_identifies_exact_guest_mapping(spec, monkeypatch):
    from xgenius import payload
    from xgenius.protocol import fingerprint
    spec = envelope_spec(spec)
    def refused(inputs):
        raise ValueError("strict input changed before consumption")
    monkeypatch.setattr(payload, "verify_input_pins", refused)
    result = execute_spec(spec)
    assert result.status == "not_started" and not result.executed
    assert result.metadata["payload_spec_digest"] == fingerprint(spec)
    assert not result.metadata["payload_executed"]
    assert "strict input changed" in result.reason


def test_output_validation_does_not_rewrite_successful_execution(spec):
    spec = envelope_spec(spec)
    spec["validators"] = [[sys.executable, "-c", "raise SystemExit(4)"]]
    result = execute_spec(spec)
    assert result.status == "completed"
    assert result.returncode == 0
    assert result.metadata["validation_errors"]
    assert result.metadata["validation_status"] == "invalid"
    assert result.metadata["stages"][-1]["returncode"] == 4


def test_full_quoted_command_is_measured_in_utf16_units():
    command = ["C:\\path with space\\python.exe", "\U0001f680" * 20, 'trailing space\\ \\"']
    assert command_units(command) == len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2 + 1


def test_real_payload_preserves_empty_quoted_unicode_and_windows_arguments(spec):
    import json
    arguments = ["", 'a "quoted" word', r"C:\path with spaces\file", "\u03bb"]
    spec["argv"] = [sys.executable, "-c", "import sys,json;print(json.dumps(sys.argv[1:]))", *arguments]
    assert run_process(spec)["status"] == "completed"
    assert json.loads(tail(Path(spec["root"]) / "stdout.log")["text"]) == arguments


def test_guest_mapping_and_validator_mutation_are_fenced_before_start(spec):
    from xgenius import payload
    from xgenius.evidence import publish_bytes
    from xgenius.protocol import canonical
    spec = envelope_spec(spec)
    path = Path(spec["cwd"]) / "manifest.json"
    digest = publish_bytes(path, canonical(spec))
    spec["validators"] = [[sys.executable, "-c", "raise RuntimeError('changed validator')"]]
    path.write_bytes(canonical(spec))
    assert payload.main(path, expected_digest=digest) == 1
    assert not (Path(spec["root"]) / "payload-claim.json").exists()


@pytest.mark.parametrize("case,expected", [
    ("supervisor-alive", "alive"), ("child-alive", "alive"), ("descendant-alive", "alive"),
    ("gone", "dead"), ("reboot", "dead"), ("missing-handle", "unknown"), ("recycled-pid", "unknown"),
])
def test_linux_session_liveness_after_supervisor_loss(tmp_path, case, expected):
    import json
    from xgenius.payload import inspect_linux
    root, proc = tmp_path / "attempt", tmp_path / "proc"
    root.mkdir()
    boot = proc / "sys" / "kernel" / "random" / "boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("new-boot" if case == "reboot" else "original-boot")
    for filename, pid in (("payload-handle.json", 10), ("process-handle.json", 20)):
        if case == "missing-handle" and pid == 20:
            continue
        (root / filename).write_text(json.dumps({
            "pid": pid, "start_ticks": "100", "boot_id": "original-boot", "token": "owned"}))

    def process(pid, group, ticks="100"):
        directory = proc / str(pid)
        directory.mkdir()
        fields = ["S", "1", str(group), str(group)] + ["0"] * 15 + [ticks]
        (directory / "stat").write_text(f"{pid} (fixture) " + " ".join(fields))

    if case == "supervisor-alive":
        process(10, 10)
    elif case == "child-alive":
        process(20, 20)
    elif case == "descendant-alive":
        process(30, 20)
    elif case == "recycled-pid":
        process(20, 20, "200")
    assert inspect_linux(root, "owned", proc) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows command size")
def test_oversized_command_fails_before_process_creation(spec):
    spec["argv"] = [sys.executable, "-c", "\U0001f680" * 12000]
    with pytest.raises(ValueError, match="UTF-16"):
        run_process(spec)
    assert not Path(spec["root"]).exists()
