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

_OS_REPLACE = os.replace
_WIN_LOCK_ERRORS = (5, 32, 33)
_WIN_UNSUPPORTED_RENAME = (1, 50, 87)
_WIN_DEVICES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _nt_path(path: Path, *, always_extended: bool = False) -> str:
    raw = os.fspath(path)
    if raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
        return raw
    resolved = os.path.abspath(raw)
    if not always_extended and len(resolved) < 248:
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def _posix_replace_nt(source: Path, target: Path) -> None:
    import ctypes
    from ctypes import wintypes
    import _winapi

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    dst_bytes = _nt_path(target, always_extended=True).encode("utf-16-le")

    class _FileRenameInfoEx(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", ctypes.c_byte * (len(dst_bytes) + 2)),
        ]

    info = _FileRenameInfoEx()
    # FILE_RENAME_FLAG_REPLACE_IF_EXISTS (0x1) | FILE_RENAME_FLAG_POSIX_SEMANTICS (0x2)
    info.Flags = 0x00000001 | 0x00000002
    info.RootDirectory = None
    info.FileNameLength = len(dst_bytes)
    ctypes.memmove(info.FileName, dst_bytes, len(dst_bytes))

    # DELETE (0x00010000) | SYNCHRONIZE (0x00100000); full Read/Write/Delete sharing (0x07)
    handle = _winapi.CreateFile(
        _nt_path(source, always_extended=True), 0x00110000, 0x07, 0, _winapi.OPEN_EXISTING, 0x80, 0,
    )
    try:
        if not kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(handle), 22, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _winapi.CloseHandle(handle)


def open_shared_read(path: Path):
    """Open a regular file for reading with FILE_SHARE_DELETE on Windows so atomic renames never get WinError 5."""
    raw = os.fspath(path)
    target = Path(raw)
    if os.name != "nt" or target.name.split(".")[0].upper() in _WIN_DEVICES:
        return target.open("rb")
    import msvcrt
    import _winapi

    try:
        handle = _winapi.CreateFile(
            _nt_path(target), _winapi.GENERIC_READ, 0x07, 0, _winapi.OPEN_EXISTING, 0x80, 0,
        )
    except OSError as error:
        winerror = getattr(error, "winerror", None)
        raise OSError(error.errno or 0, error.strerror, raw, winerror) from None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


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
        marker = b"\n[labgoblin supervisor diagnostics truncated]\n"
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
        if process.create_time() != handle["created"] or not process.is_running():
            return "dead"
        if os.name == "nt":
            return "alive"
        return "dead" if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD) else "alive"
    except psutil.NoSuchProcess:
        return "dead"
    except (psutil.AccessDenied, MemoryError):
        return "unknown"


def alive(handle: dict | None) -> bool:
    if handle is None:
        return False
    state = process_state(handle)
    if state == "unknown":
        raise RuntimeError("Owned process liveness is unavailable")
    return state == "alive"


def replace_file(source: Path, target: Path, *, timeout: float | None = None):
    source, target = Path(source), Path(target)
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    attempt = 0
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            winerror = getattr(error, "winerror", None)
            if os.name == "nt" and os.replace is _OS_REPLACE and winerror in (5, 32):
                try:
                    _posix_replace_nt(source, target)
                    return
                except OSError as nt_error:
                    nt_code = getattr(nt_error, "winerror", None)
                    if nt_code not in _WIN_UNSUPPORTED_RENAME:
                        if isinstance(nt_error, PermissionError):
                            error = nt_error
                            winerror = nt_code
                        else:
                            raise
            exhausted = attempt >= 9 if deadline is None else time.monotonic() >= deadline
            if winerror not in _WIN_LOCK_ERRORS or exhausted:
                raise
            delay = min(0.02 * 2 ** min(attempt, 8), 0.25)
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.monotonic()))
            time.sleep(delay)
            attempt += 1


def unlink_file(path: Path, *, missing_ok: bool = False):
    path = Path(path)
    for attempt in range(10):
        try:
            path.unlink(missing_ok=missing_ok)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in _WIN_LOCK_ERRORS or attempt == 9:
                raise
            time.sleep(min(0.02 * 2 ** attempt, 0.25))


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
        self.thread = threading.Thread(target=self._drain, name="labgoblin-log-drain", daemon=True)
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
