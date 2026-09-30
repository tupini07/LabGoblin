"""Owned payload execution; the guest entry point uses files, never SQLite."""

from dataclasses import asdict
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from xgenius.evidence import Capture, atomic_json, parse_json, publish_bytes, read_json, verify_input_pins
from xgenius.processes import BoundedSpool
from xgenius.protocol import (
    LaunchEnvelope, LaunchReceipt, PreExecutionError, UncertainExecution,
    Resources, argv, canonical, fingerprint, integer, number,
)


def identity(pid=None, proc_root=Path("/proc")):
    pid = os.getpid() if pid is None else pid
    if os.name == "nt":
        import psutil
        return {"pid": pid, "created": psutil.Process(pid).create_time()}
    fields = (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "start_ticks": fields[19],
            "boot_id": (proc_root / "sys/kernel/random/boot_id").read_text().strip()}


def _members(group, proc_root=Path("/proc")):
    members = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if fields[0] != "Z" and str(group) in (fields[2], fields[3]):
            members.append((int(entry.name), fields))
    return members


def inspect_linux(root, token, proc_root=Path("/proc")):
    boot_id = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    for filename in ("payload-handle.json", "process-handle.json"):
        path = Path(root) / filename
        if not path.exists():
            return "unknown"
        handle = read_json(path, 16384)
        if handle.get("token") != token:
            raise ValueError("Payload process ownership mismatch")
        if "boot_id" not in handle:
            return "unknown"
        if handle["boot_id"] != boot_id:
            return "dead"
        try:
            fields = (proc_root / str(handle["pid"]) / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            fields = None
        if fields and fields[19] == handle["start_ticks"] and fields[0] != "Z":
            return "alive"
        if filename == "process-handle.json":
            if fields and fields[19] != handle["start_ticks"]:
                return "unknown"
            if _members(handle["pid"], proc_root):
                return "alive"
    return "dead"


def _signal_members(group, signum):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise UncertainExecution("Owned Linux shutdown requires pidfd support")
    for pid, fields in _members(group):
        descriptor = None
        try:
            descriptor = os.pidfd_open(pid)
            current = identity(pid)
            if current["start_ticks"] == fields[19]:
                signal.pidfd_send_signal(descriptor, signum)
        except (ProcessLookupError, FileNotFoundError):
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _close_linux(process):
    _signal_members(process.pid, signal.SIGTERM)
    until = time.monotonic() + 2
    while _members(process.pid) and time.monotonic() < until:
        time.sleep(0.02)
    _signal_members(process.pid, signal.SIGKILL)
    until = time.monotonic() + 3
    while _members(process.pid) and time.monotonic() < until:
        time.sleep(0.02)
    if _members(process.pid):
        raise UncertainExecution("Owned Linux session is not quiescent after shutdown")
    process.wait(timeout=1)


def command_units(command):
    return len(subprocess.list2cmdline(list(command)).encode("utf-16-le")) // 2 + 1


def validate_command(command):
    command = argv(command)
    if os.name == "nt" and command_units(command) > 24000:
        raise ValueError("Complete Windows command exceeds 24000 UTF-16 units; use a file for large input")
    return command


class WindowsPayload:
    def __init__(self, command, cwd, env, out, err, spec):
        import msvcrt
        import pywintypes
        import win32api
        import win32con
        import win32event
        import win32job
        import win32process
        command = validate_command(command)
        cpu_ids = spec.get("cpu_ids")
        if cpu_ids is not None:
            if ((os.cpu_count() or 1) > 64 or len(cpu_ids) != spec["cpus"]
                    or len(set(cpu_ids)) != len(cpu_ids)
                    or any(type(cpu) is not int or not 0 <= cpu < 64 for cpu in cpu_ids)):
                raise PreExecutionError("Native CPU placement requires a valid single-group assigned CPU set")
        self.process = None
        try:
            self.job = win32job.CreateJobObject(None, spec.get("job_name", ""))
        except pywintypes.error as error:
            raise PreExecutionError(f"Could not create owned Job Object: {error}") from error
        if win32api.GetLastError() == 183:
            self.job.Close()
            raise PreExecutionError("Owned Windows Job Object name already exists")
        null = None
        handles = []
        try:
            info = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY)
            info["JobMemoryLimit"] = spec["memory_mb"] * 1024 * 1024
            if cpu_ids is not None:
                info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_AFFINITY
                info["BasicLimitInformation"]["Affinity"] = sum(1 << cpu for cpu in cpu_ids)
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
            startup = win32process.STARTUPINFO()
            startup.dwFlags |= win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
            startup.wShowWindow = win32con.SW_HIDE
            null = open(os.devnull, "rb")
            handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in (null, out, err)]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
            self.process, thread, self.pid, _ = win32process.CreateProcess(
                None, subprocess.list2cmdline(command), None, None, True,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NEW_PROCESS_GROUP | win32con.CREATE_NO_WINDOW,
                env, cwd, startup)
            try:
                win32job.AssignProcessToJobObject(self.job, self.process)
                win32process.ResumeThread(thread)
            except (OSError, pywintypes.error) as error:
                win32process.TerminateProcess(self.process, 1)
                if win32event.WaitForSingleObject(self.process, 5000) != 0:
                    raise UncertainExecution("Suspended payload could not be retired") from error
                raise PreExecutionError(f"Payload was not resumed: {error}") from error
            finally:
                thread.Close()
        except pywintypes.error as error:
            created = self.process is not None
            self.job.Close()
            if created:
                self.process.Close()
                raise UncertainExecution(f"Windows ownership failed after process creation: {error}") from error
            raise PreExecutionError(f"Windows payload creation failed: {error}") from error
        except BaseException:
            self.job.Close()
            if self.process is not None:
                self.process.Close()
            raise
        finally:
            for handle in handles:
                os.set_handle_inheritable(handle, False)
            if null is not None:
                null.close()

    def poll(self):
        import win32event
        import win32process
        if win32event.WaitForSingleObject(self.process, 0) == 258:
            return None
        return win32process.GetExitCodeProcess(self.process)

    def active(self):
        import win32job
        return win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]

    def peak_memory_mb(self):
        import win32job
        return win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation)["PeakJobMemoryUsed"] / 1048576

    def close(self):
        import win32job
        try:
            if self.active():
                win32job.TerminateJobObject(self.job, 1)
            until = time.monotonic() + 5
            while self.active() and time.monotonic() < until:
                time.sleep(0.02)
            if self.active():
                raise UncertainExecution("Owned Windows Job Object is not quiescent after shutdown")
        finally:
            self.job.Close()
            self.process.Close()


def process_environment(spec):
    names = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
             "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "LANG", "PATHEXT")
    env = {name: os.environ[name] for name in names if name in os.environ}
    if spec.get("provider"):
        env = os.environ.copy()
        if spec["provider"] == "claude":
            env.pop("ANTHROPIC_API_KEY", None)
    env.update(spec.get("environment", {}))
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
        "OMP_NUM_THREADS": str(spec["cpus"]), "MKL_NUM_THREADS": str(spec["cpus"]),
        "CUDA_VISIBLE_DEVICES": ",".join(spec.get("gpus", [])),
    })
    if "output" in spec:
        env["XGENIUS_OUTPUT_DIR"] = spec["output"]
    if "work_id" in spec:
        env["XGENIUS_ATTEMPT_ID"] = spec["work_id"]
    for name, value in spec.get("inputs", {}).items():
        env[f"XGENIUS_INPUT_{name.upper()}"] = value["access_path"]
    return env


def run_process(spec):
    command = validate_command(spec["argv"])
    integer(spec["cpus"], "cpus")
    integer(spec["memory_mb"], "memory_mb")
    number(spec["seconds"], "seconds")
    integer(spec["log_bytes"], "log_bytes")
    root = Path(spec["root"])
    root.mkdir(parents=True, exist_ok=True)
    token = spec["token"]
    cancel = Path(spec["cancel_path"])
    atomic_json(root / "payload-handle.json", {**identity(), "token": token})
    started = time.monotonic()
    process = None
    streams = []
    code = None
    executed = False
    peak_memory = 0
    status, reason = "not_started", "Payload was not started"
    stream_stats = {}
    previous_affinity = None
    try:
        if cancel.exists():
            status, reason = "cancelled", "Cancellation requested before payload start"
        else:
            if os.name != "nt":
                if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                    raise PreExecutionError("Owned Linux execution requires pidfd support")
                if hasattr(os, "sched_setaffinity") and spec.get("runner_kind") != "docker":
                    previous_affinity = os.sched_getaffinity(0)
                    selected = spec.get("cpu_ids")
                    if selected is None:
                        selected = sorted(previous_affinity)[:spec["cpus"]]
                    if len(selected) != spec["cpus"]:
                        raise PreExecutionError("Requested guest CPU count exceeds available CPUs")
                    os.sched_setaffinity(0, selected)
            for name in ("stdout", "stderr"):
                streams.append(BoundedSpool(root / f"{name}.log", spec["log_bytes"]))
            env = process_environment(spec)
            if os.name == "nt":
                process = WindowsPayload(command, spec["cwd"], env,
                                         streams[0].writer, streams[1].writer, spec)
            else:
                process = subprocess.Popen(command, cwd=spec["cwd"], env=env,
                                           stdin=subprocess.DEVNULL, stdout=streams[0].writer,
                                           stderr=streams[1].writer, start_new_session=True)
            executed = True
            for stream in streams:
                stream.close_writer()
            try:
                atomic_json(root / "process-handle.json",
                            {**identity(process.pid), "token": token, "job_name": spec.get("job_name")})
            except (FileNotFoundError, ProcessLookupError):
                if process.poll() is None:
                    raise
            while process.poll() is None:
                memory = (process.peak_memory_mb() if os.name == "nt" else
                          sum(int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
                              for _, fields in _members(process.pid)) / 1048576)
                peak_memory = max(peak_memory, memory)
                atomic_json(root / "heartbeat.json", {
                    "token": token, "time": time.time(), "elapsed": time.monotonic() - started,
                    "observed_memory_mb": memory,
                })
                if cancel.exists():
                    status, reason = "cancelled", "Cancellation requested"
                    break
                if time.monotonic() - started >= spec["seconds"]:
                    status, reason = "timed_out", "Operation deadline exceeded"
                    break
                if os.name != "nt" and memory > spec["memory_mb"]:
                    status, reason = "failed", "Monitored process-session RAM limit exceeded"
                    break
                if any(stream.error for stream in streams):
                    raise OSError("Owned output retention failed")
                time.sleep(0.05)
            code = process.poll()
            if code is not None and status == "not_started":
                status = "completed" if code == 0 else "failed"
                reason = "" if code == 0 else f"Process exited with code {code}"
        # Every returned outcome below follows tree shutdown and pipe EOF.
    except UncertainExecution:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        status = "failed" if executed else "not_started"
        reason = f"{type(error).__name__}: {str(error)[:4000]}"
    finally:
        try:
            if process is not None:
                if os.name == "nt":
                    peak_memory = max(peak_memory, process.peak_memory_mb())
                    process.close()
                else:
                    _close_linux(process)
        finally:
            if previous_affinity is not None:
                os.sched_setaffinity(0, previous_affinity)
            for name, stream in zip(("stdout", "stderr"), streams):
                try:
                    stream_stats[name] = stream.finish()
                except OSError as error:
                    stream_stats[name] = stream.snapshot()
                    status = "failed" if executed else "not_started"
                    reason = f"Output retention failed: {str(error)[:4000]}"
                except RuntimeError as error:
                    raise UncertainExecution(str(error)) from error
    return {"status": status, "reason": reason, "returncode": code, "executed": executed,
            "elapsed": time.monotonic() - started, "logs": stream_stats,
            "peak_observed_memory_mb": peak_memory}


def execute_spec(spec, *, publish_receipt=True):
    envelope = LaunchEnvelope.parse(spec["envelope"])
    if envelope.digest != spec["envelope_digest"] or spec["token"] != envelope.key.nonce:
        raise ValueError("Payload envelope identity mismatch")
    if (argv(spec["argv"]) != envelope.argv
            or Resources.parse({key: spec[key] for key in ("cpus", "memory_mb", "gpus")}) != envelope.resources
            or spec["seconds"] > envelope.timeout_seconds):
        raise ValueError("Payload command, resources or deadline differ from the frozen authorization")
    root = Path(spec["root"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / "payload-claim.json").open("xb") as stream:
        stream.write(canonical({"key": asdict(envelope.key), "supervisor": identity()}))
        stream.flush()
        os.fsync(stream.fileno())
    started = time.monotonic()
    try:
        input_checks = verify_input_pins(spec.get("inputs", {}))
    except (OSError, ValueError) as error:
        receipt = LaunchReceipt(envelope.key, envelope.digest, "not_started", True,
                                time.monotonic() - started, executed=False,
                                reason=f"Consumption-path input verification failed: {error}",
                                metadata={"payload_spec_digest": fingerprint(spec), "payload_executed": False})
        if publish_receipt:
            publish_bytes(root / "backend-receipt.json", canonical(asdict(receipt)))
        return receipt
    outcomes = []
    main = None
    validation_errors = []
    for stage, commands in (("input-validator", spec.get("input_validators", [])),
                            ("main", [spec["argv"]]), ("validator", spec.get("validators", []))):
        if outcomes and outcomes[-1]["status"] != "completed":
            break
        for index, command in enumerate(commands):
            remaining = spec["seconds"] - (time.monotonic() - started)
            if remaining <= 0:
                outcomes.append({"status": "timed_out", "reason": "Operation deadline exceeded",
                                 "returncode": None, "executed": False, "elapsed": 0})
                if stage == "validator":
                    validation_errors.append("No deadline remaining for output validation")
                break
            name = "main" if stage == "main" else f"{stage}-{index}"
            child = {**spec, "argv": command, "seconds": remaining,
                     "root": str(root / name), "job_name": f"xgenius-{envelope.key.nonce}-{name}"}
            outcome = run_process(child)
            outcomes.append({"stage": name, **outcome})
            if stage == "main":
                main = outcome
            if outcome["status"] != "completed":
                if stage == "validator":
                    validation_errors.append(f"{name}: {outcome['reason']}")
                break
    last = outcomes[-1]
    status = last["status"]
    reason = last["reason"]
    code = last["returncode"]
    executed = any(outcome["executed"] for outcome in outcomes)
    if status == "not_started" and executed:
        status = "failed"
    if main and main["status"] == "completed" and validation_errors and status not in ("cancelled", "timed_out"):
        status, reason, code = main["status"], main["reason"], main["returncode"]
    if main is None:
        reason = f"Input validation did not admit the payload: {reason}"
    receipt = LaunchReceipt(
        envelope.key, envelope.digest, status, True, time.monotonic() - started,
        returncode=code, reason=reason, executed=executed,
        metadata={"stages": outcomes, "payload_spec_digest": fingerprint(spec),
                  "payload_executed": bool(main and main["executed"]),
                  "validation_errors": validation_errors,
                  "validation_status": ("invalid" if validation_errors else
                                        "valid" if spec.get("validators") and main and status == "completed"
                                        else "unchecked"),
                  "cpu_ids": spec.get("cpu_ids"),
                  "input_checks": input_checks,
                  "cpu_enforcement": "native placement" if spec.get("cpu_ids") is not None
                  else "container CPU quota" if spec.get("runner_kind") == "docker"
                  else "guest-local affinity; not host CPU placement"},
    )
    if publish_receipt:
        publish_bytes(root / "backend-receipt.json", canonical(asdict(receipt)))
    return receipt


def inspect_spec(spec):
    envelope = LaunchEnvelope.parse(spec["envelope"])
    if spec["envelope_digest"] != envelope.digest:
        raise ValueError("Payload envelope identity mismatch")
    root = Path(spec["root"])
    receipt = root / "backend-receipt.json"
    if receipt.exists():
        value = LaunchReceipt.parse(read_json(receipt))
        if value.key != envelope.key or value.envelope_digest != envelope.digest:
            raise ValueError("Backend receipt identity mismatch")
        return "dead"
    if not (root / "payload-claim.json").exists():
        return "unknown"
    states = []
    for directory in root.iterdir():
        if directory.is_dir() and (directory.name == "main" or
                                  directory.name.startswith(("input-validator-", "validator-"))):
            states.append(inspect_linux(directory, spec["token"]))
    return "dead" if states and all(state == "dead" for state in states) else "unknown"


def main(manifest, inspect=False, *, expected_digest=None):
    try:
        capture = Capture.read(Path(manifest), 1024 * 1024)
        spec = parse_json(capture.body)
        if inspect:
            print(inspect_spec(spec))
            return 0
        if expected_digest != capture.digest:
            raise ValueError("Guest execution mapping differs from its owned launch arguments")
        receipt = execute_spec(spec)
        return 0 if receipt.status == "completed" else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Payload outcome is unavailable: {type(error).__name__}: {str(error)[:4000]}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    digest = sys.argv[sys.argv.index("--digest") + 1] if "--digest" in sys.argv else None
    raise SystemExit(main(sys.argv[-1], inspect="--inspect" in sys.argv[1:-1], expected_digest=digest))
