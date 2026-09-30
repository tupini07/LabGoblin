"""Platform-specific launch options for non-interactive subprocesses."""

from contextlib import contextmanager
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time


@contextmanager
def temporary_directory(*, prefix):
    directory = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield directory.name
    finally:
        deadline = time.monotonic() + 2
        while True:
            try:
                directory.cleanup()
                break
            except PermissionError as error:
                if getattr(error, "winerror", None) not in (32, 33) or time.monotonic() >= deadline:
                    raise
                # WSL can release its host working-directory handle after exit.
                time.sleep(0.05)


class CampaignLease:
    """Shared reader lifetime versus an exclusive, fail-fast archive/reset."""

    def __init__(self, root: Path, *, exclusive=False):
        root = Path(root)
        self.path = root.with_name(root.name + ".lock")
        self.exclusive = exclusive
        self.handle = None

    def __enter__(self):
        if os.name == "nt":
            import pywintypes
            import win32con
            import win32file
            self.handle = win32file.CreateFile(
                str(self.path), win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                None, win32con.OPEN_EXISTING, 0, None)
            self.overlapped = pywintypes.OVERLAPPED()
            flags = win32con.LOCKFILE_FAIL_IMMEDIATELY | (win32con.LOCKFILE_EXCLUSIVE_LOCK if self.exclusive else 0)
            try:
                win32file.LockFileEx(self.handle, flags, 0, 1, self.overlapped)
            except pywintypes.error as error:
                self.handle.Close()
                self.handle = None
                raise ValueError("Campaign has live readers or an archive/reset in progress") from error
        else:
            import fcntl
            self.handle = self.path.open("rb")
            try:
                fcntl.flock(self.handle, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except OSError as error:
                self.handle.close()
                self.handle = None
                raise ValueError("Campaign has live readers or an archive/reset in progress") from error
        return self

    def close(self):
        if self.handle is not None:
            if os.name == "nt":
                import win32file
                win32file.UnlockFileEx(self.handle, 0, 1, self.overlapped)
                self.handle.Close()
            else:
                self.handle.close()
            self.handle = None

    def __exit__(self, *args):
        self.close()


class BoundedDiagnostic(io.TextIOBase):
    def __init__(self, stream, limit=65536):
        if type(limit) is not int or limit <= 0:
            raise ValueError("Diagnostic byte limit must be a positive integer")
        self.stream = stream
        self.limit = limit
        self.received = 0
        self.retained = 0
        self.truncated = False

    @property
    def encoding(self):
        return "utf-8"

    def write(self, value):
        encoded = value.encode("utf-8", errors="replace")
        self.received += len(encoded)
        if self.truncated:
            return len(value)
        marker = b"\n[xgenius supervisor diagnostics truncated]\n"
        room = max(0, self.limit - len(marker) - self.retained)
        body = encoded[:room].decode("utf-8", errors="ignore").encode("utf-8")
        self.stream.write(body)
        self.retained += len(body)
        if len(encoded) > room:
            ending = marker[:max(0, self.limit - self.retained)]
            self.stream.write(ending)
            self.retained += len(ending)
            self.truncated = True
        self.stream.flush()
        return len(value)

    def flush(self):
        if not self.stream.closed:
            self.stream.flush()

    def fileno(self):
        return self.stream.fileno()


def bound_diagnostics():
    sys.stdout = BoundedDiagnostic(sys.stdout.buffer)
    sys.stderr = BoundedDiagnostic(sys.stderr.buffer)


def own_handle(token: str) -> dict:
    import psutil
    return {"pid": os.getpid(), "created": psutil.Process().create_time(), "token": token}


def process_state(handle: dict | None) -> str:
    import psutil
    if handle is None:
        return "unknown"
    if (not isinstance(handle, dict) or type(handle.get("pid")) is not int
            or type(handle.get("created")) not in (int, float)):
        raise ValueError("A host process handle requires PID and creation time")
    try:
        process = psutil.Process(handle["pid"])
        return "alive" if process.create_time() == handle["created"] and process.is_running() else "dead"
    except psutil.NoSuchProcess:
        return "dead"
    except psutil.AccessDenied:
        return "unknown"


def alive(handle: dict | None) -> bool:
    if handle is None:
        return False
    state = process_state(handle)
    if state == "unknown":
        raise RuntimeError("Owned process liveness is unavailable")
    return state == "alive"


def replace_file(source: Path, target: Path):
    for attempt in range(10):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 9:
                raise
            time.sleep(0.02)


def unlink_file(path: Path, *, missing_ok: bool = False):
    for attempt in range(10):
        try:
            path.unlink(missing_ok=missing_ok)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 9:
                raise
            time.sleep(0.02)


class BoundedSpool:
    """Drain a child pipe while retaining at most two bounded log segments."""

    def __init__(self, path: Path, limit: int):
        if type(limit) is not int or limit <= 0:
            raise ValueError("Spool limit must be a positive integer")
        self.path = Path(path)
        self.limit = limit
        self.segment = max(1, limit // 2)
        self.previous = self.path.with_name(self.path.name + ".previous")
        self.received = 0
        self.retained = 0
        self.error: OSError | None = None
        if self.path.exists() or self.previous.exists():
            raise FileExistsError(f"Owned log stream already exists: {self.path}")
        read_fd, write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, "rb", buffering=0)
        self.writer = os.fdopen(write_fd, "wb", buffering=0)
        self.thread = threading.Thread(target=self._drain, name="xgenius-log-drain", daemon=True)
        self.thread.start()

    def _drain(self):
        output = None
        size = 0
        previous_size = 0
        try:
            try:
                output = self.path.open("xb", buffering=0)
            except OSError as error:
                self.error = error
            while True:
                try:
                    block = self.reader.read(16 * 1024)
                except OSError as error:
                    self.error = self.error or error
                    break
                if not block:
                    break
                self.received += len(block)
                if self.error:
                    continue
                try:
                    if len(block) > self.limit:
                        block = block[-self.limit:]
                        output.close()
                        unlink_file(self.previous, missing_ok=True)
                        output = self.path.open("wb", buffering=0)
                        size = previous_size = 0
                    position = 0
                    while position < len(block):
                        if size == self.segment:
                            output.close()
                            if self.limit > 1:
                                replace_file(self.path, self.previous)
                                previous_size = size
                            else:
                                unlink_file(self.path)
                            output = self.path.open("xb", buffering=0)
                            size = 0
                        count = min(self.segment - size, len(block) - position)
                        written = output.write(block[position:position + count])
                        if not written:
                            raise OSError("Owned log stream accepted no output bytes")
                        position += written
                        size += written
                        self.retained = previous_size + size
                except OSError as error:
                    self.error = error
        finally:
            if output is not None:
                try:
                    output.close()
                except OSError as error:
                    self.error = self.error or error
            self.reader.close()

    def close_writer(self):
        self.writer.close()

    def finish(self, timeout: float = 10) -> dict:
        self.close_writer()
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise RuntimeError("Owned stdout/stderr pipe remains open after shutdown; quiescence is unverified")
        if self.error:
            raise OSError(f"Could not retain owned output {self.path}: {self.error}") from self.error
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"received_bytes": self.received, "retained_bytes": self.retained,
                "truncated": self.received > self.retained,
                "error": str(self.error) if self.error else None}


def background_options(*, independent: bool = False) -> dict:
    """Keep explicit stdio routing: Windows can replace implicitly inherited handles."""
    if os.name != "nt":
        return {"start_new_session": True} if independent else {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    # Unlike DETACHED_PROCESS, a windowless console is inherited by ordinary children.
    flags = subprocess.CREATE_NO_WINDOW
    if independent:
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
    return {"creationflags": flags, "startupinfo": startup}
