"""Standalone attempt payload supervisor (also copied into WSL/containers)."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback


def write(path, value):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def identity(pid=None):
    pid = os.getpid() if pid is None else pid
    if os.name == "nt":
        import psutil
        return {"pid": pid, "created": psutil.Process(pid).create_time()}
    stat = Path(f"/proc/{pid}/stat").read_text()
    return {"pid": pid, "start_ticks": stat.rsplit(")", 1)[1].split()[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def inspect_linux(root, token, proc_root=Path("/proc")):
    """A dead supervisor is not proof that its separate process session is dead."""
    boot_id = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    for filename in ("payload-handle.json", "process-handle.json"):
        path = root / filename
        if not path.exists():
            return "unknown"
        handle = json.loads(path.read_text(encoding="utf-8"))
        if handle.get("token") != token:
            raise ValueError("Payload process ownership mismatch")
        if "boot_id" not in handle:
            return "unknown"
        if handle["boot_id"] != boot_id:
            return "dead"
        try:
            parts = (proc_root / str(handle["pid"]) / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            parts = None
        if parts and parts[19] == handle["start_ticks"] and parts[0] != "Z":
            return "alive"
        if filename == "process-handle.json":
            if parts and parts[19] != handle["start_ticks"]:
                return "unknown"
            for entry in proc_root.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    continue
                if fields[0] != "Z" and str(handle["pid"]) in (fields[2], fields[3]):
                    return "alive"
    for pattern in ("input-validator-*", "validator-*"):
        for directory in root.glob(pattern):
            if directory.is_dir():
                state = inspect_linux(directory, token, proc_root)
                if state != "dead":
                    return state
    return "dead"


def group_memory_mb(group):
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(parts[2]) == group:
                total += int(parts[21]) * os.sysconf("SC_PAGE_SIZE")
        except (FileNotFoundError, ProcessLookupError):
            continue
    return total / (1024 * 1024)


class WindowsPayload:
    def __init__(self, argv, cwd, env, out, err, spec):
        import msvcrt
        import win32api
        import win32con
        import win32job
        import win32process
        self.job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
            win32job.JOB_OBJECT_LIMIT_JOB_MEMORY)
        info["JobMemoryLimit"] = spec["memory_mb"] * 1024 * 1024
        win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, info)
        startup = win32process.STARTUPINFO()
        startup.dwFlags |= win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
        startup.wShowWindow = win32con.SW_HIDE
        null = open(os.devnull, "rb")
        handles = [msvcrt.get_osfhandle(f.fileno()) for f in (null, out, err)]
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        try:
            self.process, thread, self.pid, _ = win32process.CreateProcess(
                None, subprocess.list2cmdline(argv), None, None, True,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NEW_PROCESS_GROUP | win32con.CREATE_NO_WINDOW,
                env, cwd, startup)
            try:
                win32job.AssignProcessToJobObject(self.job, self.process)
                mask, _ = win32process.GetProcessAffinityMask(self.process)
                selected = [i for i in range(64) if mask & (1 << i)][:spec["cpus"]]
                win32process.SetProcessAffinityMask(self.process, sum(1 << i for i in selected))
                win32process.ResumeThread(thread)
            except BaseException:
                win32process.TerminateProcess(self.process, 1)
                raise
            finally:
                win32api.CloseHandle(thread)
        finally:
            for handle in handles:
                os.set_handle_inheritable(handle, False)
            null.close()

    def poll(self):
        import win32event
        import win32process
        if win32event.WaitForSingleObject(self.process, 0) == 258:
            return None
        return win32process.GetExitCodeProcess(self.process)

    def terminate(self):
        import win32job
        win32job.TerminateJobObject(self.job, 1)

    def peak_memory_mb(self):
        import win32job
        return win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation)["PeakJobMemoryUsed"] / (1024 * 1024)

    def close(self):
        self.job.Close()
        self.process.Close()


def main(manifest):
    spec = json.loads(Path(manifest).read_text(encoding="utf-8"))
    root = Path(spec["root"])
    started = time.time()
    write(root / "payload-handle.json", {**identity(), "token": spec["id"]})
    env = {}
    for name in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                 "HOME", "USERPROFILE", "LOCALAPPDATA", "LANG"):
        if name in os.environ:
            env[name] = os.environ[name]
    env.update(spec["environment"])
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": str(spec["cpus"]), "MKL_NUM_THREADS": str(spec["cpus"]),
        "XGENIUS_OUTPUT_DIR": spec["output"], "XGENIUS_ATTEMPT_ID": spec["id"],
        "CUDA_VISIBLE_DEVICES": ",".join(spec["gpus"]),
    })
    for name, value in spec["inputs"].items():
        env[f"XGENIUS_INPUT_{name.upper()}"] = value["access_path"]
    process = None
    reason = ""
    status = "failed"
    code = None
    validation_errors = []
    peak_memory_mb = 0
    try:
        for index, validator in enumerate(spec.get("input_validators", [])):
            validation_root = root / f"input-validator-{index}"
            validation_root.mkdir(exist_ok=True)
            remaining = spec["seconds"] - (time.time() - started)
            if remaining <= 0:
                raise TimeoutError("No walltime remaining for input validation")
            child = {**spec, "argv": validator, "validators": [], "input_validators": [],
                     "seconds": remaining, "root": str(validation_root),
                     "cancel_path": spec.get("cancel_path", str(root / "cancel"))}
            write(validation_root / "spec.json", child)
            main(str(validation_root / "spec.json"))
            outcome = json.loads((validation_root / "completion.json").read_text(encoding="utf-8"))
            if outcome["status"] != "completed":
                raise ValueError(f"Input validator {index} refused execution: {outcome['reason']}")
        with (root / "stdout.log").open("wb") as out, (root / "stderr.log").open("wb") as err:
            if os.name == "nt":
                process = WindowsPayload(spec["argv"], spec["cwd"], env, out, err, spec)
            else:
                process = subprocess.Popen(
                    spec["argv"], cwd=spec["cwd"], env=env, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err, start_new_session=True)
                if hasattr(os, "sched_setaffinity"):
                    cpus = sorted(os.sched_getaffinity(process.pid))[:spec["cpus"]]
                    os.sched_setaffinity(process.pid, cpus)
            try:
                write(root / "process-handle.json", {**identity(process.pid), "token": spec["id"]})
            except (FileNotFoundError, ProcessLookupError):
                if process.poll() is None:
                    raise
            while process.poll() is None:
                memory_mb = process.peak_memory_mb() if os.name == "nt" else group_memory_mb(process.pid)
                peak_memory_mb = max(peak_memory_mb, memory_mb)
                write(root / "heartbeat.json", {"token": spec["id"], "time": time.time(),
                                                "memory_mb": memory_mb, "elapsed": time.time() - started})
                if Path(spec.get("cancel_path", str(root / "cancel"))).exists():
                    status, reason = "cancelled", "Cancellation requested"
                    break
                if time.time() - started >= spec["seconds"]:
                    status, reason = "timed_out", "Attempt walltime limit exceeded"
                    break
                if os.name != "nt" and memory_mb > spec["memory_mb"]:
                    status, reason = "failed", "Monitored process-group RAM limit exceeded"
                    break
                time.sleep(0.2)
            if reason:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                    deadline = time.time() + 3
                    while process.poll() is None and time.time() < deadline:
                        time.sleep(0.1)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            code = process.poll()
            if not reason:
                status = "completed" if code == 0 else "failed"
                reason = "" if code == 0 else f"Process exited with code {code}"
            if os.name == "nt":
                process.close()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            process = None
            if status == "completed":
                for index, validator in enumerate(spec.get("validators", [])):
                    remaining = spec["seconds"] - (time.time() - started)
                    if remaining <= 0:
                        validation_errors.append("No walltime remaining for validators")
                        break
                    # Validators are child payloads with the same containment/deadline controls.
                    validation_spec = {**spec, "argv": validator, "validators": [],
                                       "input_validators": [],
                                       "seconds": remaining,
                                       "cancel_path": spec.get("cancel_path", str(root / "cancel"))}
                    validation_root = root / f"validator-{index}"
                    validation_root.mkdir(exist_ok=True)
                    validation_spec["root"] = str(validation_root)
                    write(validation_root / "spec.json", validation_spec)
                    main(str(validation_root / "spec.json"))
                    outcome = json.loads((validation_root / "completion.json").read_text())
                    if outcome["status"] != "completed":
                        validation_errors.append(f"Validator {index}: {outcome['reason']}")
                        if outcome["status"] in ("cancelled", "timed_out"):
                            status, reason = outcome["status"], outcome["reason"]
                            break
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        status = "failed"
    finally:
        if process is not None:
            if os.name == "nt":
                process.close()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        elapsed = time.time() - started
        write(root / "completion.json", {
            "token": spec["id"], "status": status, "returncode": code,
            "elapsed": elapsed, "gpu_hours": elapsed * len(spec["gpus"]) / 3600,
            "reason": reason,
            "validation_errors": validation_errors,
            "peak_observed_memory_mb": peak_memory_mb,
        })


if __name__ == "__main__":
    if sys.argv[1] == "--inspect":
        spec = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        print(inspect_linux(Path(spec["root"]), spec["id"]))
    else:
        main(sys.argv[1])
