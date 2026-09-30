"""Windowless Windows launches without weakening independent supervision."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from xgenius import processes
from xgenius.payload import WindowsPayload
from xgenius.protocol import identifier


WINDOW_PROBE = (
    "import ctypes,json,os;from ctypes import wintypes;"
    "kernel=ctypes.WinDLL('kernel32');user=ctypes.WinDLL('user32');"
    "kernel.GetConsoleWindow.restype=wintypes.HWND;"
    "user.IsWindowVisible.argtypes=[wintypes.HWND];"
    "window=kernel.GetConsoleWindow();"
    "print(json.dumps(dict(pid=os.getpid(),window=window,"
    "visible=bool(window and user.IsWindowVisible(window)),"
    "console_codepage=kernel.GetConsoleCP())),flush=True)"
)


def test_posix_options_preserve_session_behavior(monkeypatch):
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="posix"))
    assert processes.background_options() == {}
    assert processes.background_options(independent=True) == {"start_new_session": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows console creation flags")
@pytest.mark.parametrize("independent", [False, True])
def test_windows_background_options(independent):
    options = processes.background_options(independent=independent)
    flags = options["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE)
    assert bool(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB) == independent
    assert bool(flags & subprocess.CREATE_NEW_PROCESS_GROUP) == independent
    assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert options["startupinfo"].wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows console and child inheritance")
@pytest.mark.parametrize("launcher", ["supervisor", "payload"])
def test_background_process_and_unmodified_child_have_no_console_window(tmp_path, launcher):
    code = (
        WINDOW_PROBE + ";import subprocess,sys;"
        f"subprocess.run([sys.executable,'-c',{WINDOW_PROBE!r}],check=True)"
    )
    argv = [sys.executable, "-c", code]
    if launcher == "supervisor":
        with (tmp_path / "supervisor.stdout.log").open("wb") as out, (
                tmp_path / "supervisor.stderr.log").open("wb") as err:
            process = subprocess.Popen(argv, cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       **processes.background_options(independent=True))
        try:
            assert process.wait(timeout=15) == 0, (tmp_path / "supervisor.stderr.log").read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        output = (tmp_path / "supervisor.stdout.log").read_text()
    else:
        with (tmp_path / "stdout.log").open("wb") as out, (tmp_path / "stderr.log").open("wb") as err:
            process = WindowsPayload(argv, str(tmp_path), os.environ.copy(), out, err,
                                     {"cpus": 1, "memory_mb": 256, "token": identifier()})
            try:
                deadline = time.monotonic() + 15
                while process.poll() is None:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                assert process.poll() == 0, (tmp_path / "stderr.log").read_text()
            finally:
                process.close()
        output = (tmp_path / "stdout.log").read_text()
    rows = [json.loads(line) for line in output.splitlines()]
    assert len(rows) == 2
    assert rows[0]["pid"] != rows[1]["pid"]
    assert all(not row["window"] and not row["visible"] and row["console_codepage"] for row in rows)

def test_temporary_cleanup_retries_only_transient_windows_sharing(monkeypatch):
    from xgenius import processes
    calls = []
    class Directory:
        name = "owned"
        def cleanup(self):
            calls.append("cleanup")
            if len(calls) == 1:
                error = PermissionError("working directory handle not released yet")
                error.winerror = 32
                raise error
    monkeypatch.setattr(processes.tempfile, "TemporaryDirectory", lambda **kwargs: Directory())
    monkeypatch.setattr(processes.time, "sleep", lambda delay: None)
    with processes.temporary_directory(prefix="test-") as name:
        assert name == "owned"
    assert calls == ["cleanup", "cleanup"]
    def denied(self):
        raise PermissionError("Not a sharing violation")
    monkeypatch.setattr(Directory, "cleanup", denied)
    with pytest.raises(PermissionError, match="Not a sharing violation"):
        with processes.temporary_directory(prefix="test-"):
            pass
