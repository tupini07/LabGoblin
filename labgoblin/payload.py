"""Owned native process trees with finite deadlines and bounded output."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from labgoblin.storage import atomic_json, read_json
from labgoblin.processes import BoundedSpool, temporary_directory
from labgoblin.protocol import PreExecutionError, UncertainExecution, argv, identifier, integer, number


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
    path = Path(root) / "process-handle.json"
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
    if fields and fields[19] != handle["start_ticks"]:
        return "unknown"
    if fields and fields[0] != "Z":
        return "alive"
    return "alive" if _members(handle["pid"], proc_root) else "dead"


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
    def __init__(self, command, cwd, env, out, err, spec, *, stdin=None):
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
            info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if "memory_mb" in spec:
                info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
                info["JobMemoryLimit"] = spec["memory_mb"] * 1024 * 1024
            if cpu_ids is not None:
                info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_AFFINITY
                info["BasicLimitInformation"]["Affinity"] = sum(1 << cpu for cpu in cpu_ids)
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
            startup = win32process.STARTUPINFO()
            startup.dwFlags |= win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
            startup.wShowWindow = win32con.SW_HIDE
            if stdin is None:
                null = open(os.devnull, "rb")
            handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin or null, out, err)]
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
             "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "LANG", "PATHEXT",
             "ProgramFiles", "ProgramW6432")
    env = {name: os.environ[name] for name in names if name in os.environ}
    if spec.get("provider"):
        env = os.environ.copy()
        for name in list(env):
            if name.startswith("LABGOBLIN_"):
                del env[name]
    env.update(spec.get("environment", {}))
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
        "OMP_NUM_THREADS": str(spec["cpus"]), "MKL_NUM_THREADS": str(spec["cpus"]),
        "CUDA_VISIBLE_DEVICES": ",".join(spec.get("gpus", [])),
    })
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
    deadline = started + spec["seconds"]
    process = None
    streams = []
    code = None
    executed = False
    peak_memory = 0
    status, reason = "not_started", "Payload was not started"
    stream_stats = {}
    previous_affinity = None
    cooperative = bool(spec.get("provider"))
    cancel_started = None
    try:
        if cancel.exists():
            status, reason = "cancelled", "Cancellation requested before payload start"
        elif deadline <= time.monotonic():
            status, reason = "timed_out", "Task deadline expired before payload start"
        else:
            if os.name != "nt":
                if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                    raise PreExecutionError("Owned Linux execution requires pidfd support")
                if hasattr(os, "sched_setaffinity"):
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
            if cooperative:
                env["LABGOBLIN_OPERATION_DEADLINE"] = str(deadline)
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
                    if cancel_started is None:
                        cancel_started = time.monotonic()
                    grace = spec.get("shutdown_grace_seconds", 0) if cooperative else 0
                    if time.monotonic() - cancel_started >= grace:
                        status, reason = "cancelled", "Cancellation requested; shutdown grace expired"
                        break
                if time.monotonic() >= deadline:
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


def tree_state(root, token):
    if os.name != "nt":
        return inspect_linux(root, token)
    import pywintypes
    import win32job
    try:
        job = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, f"labgoblin-{token}")
    except pywintypes.error as error:
        if error.winerror == 2:
            return "dead"
        raise UncertainExecution(f"Cannot inspect owned Job Object: {error}") from error
    try:
        count = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]
        return "dead" if count == 0 else "alive"
    finally:
        job.Close()


def probe(arguments, *, timeout=30, limit=65536, memory_mb=256):
    """Explicit model-free local capability probes, not research execution."""
    with temporary_directory(prefix="labgoblin-probe-") as name:
        root = Path(name)
        result = run_process({
            "argv": arguments, "cwd": str(root), "root": str(root / "logs"),
            "token": identifier(), "cpus": 1, "memory_mb": memory_mb, "seconds": timeout,
            "log_bytes": limit, "cancel_path": str(root / "cancel"),
        })
        output = {}
        for stream in ("stdout", "stderr"):
            paths = [root / "logs" / f"{stream}.log.previous", root / "logs" / f"{stream}.log"]
            output[stream] = b"".join(path.read_bytes() for path in paths if path.exists()).decode("utf-8", errors="replace")
        if result["status"] != "completed":
            raise RuntimeError(f"{arguments[0]}: {result['reason']}; "
                               f"stderr: {output['stderr'][-4096:]}; stdout: {output['stdout'][-4096:]}")
        if any(item["truncated"] for item in result["logs"].values()):
            raise ValueError(f"Probe output exceeded {limit} bytes")
        return output["stdout"].strip()
