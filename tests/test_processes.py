"""Windowless Windows launches without weakening independent supervision."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from xgenius import processes
from xgenius.backends import launch_independent
from xgenius.payload import WindowsPayload


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
        process = launch_independent(argv, tmp_path)
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
                                     {"cpus": 1, "memory_mb": 256})
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


def test_legacy_agent_keeps_inherited_output(tmp_path):
    code = (
        "import os,sys;from xgenius.agent import run_agent;"
        "from xgenius.config import WatcherConfig,XGeniusConfig;"
        "command='\"'+sys.executable+'\" -c \"import sys;print(123);print(456,file=sys.stderr)\"';"
        "config=XGeniusConfig(config_path=os.path.abspath('xgenius.toml'),"
        "watcher=WatcherConfig(trigger_command=command));"
        "sys.exit(run_agent(config,'fixture').returncode)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15,
                            **processes.background_options())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "123"
    assert result.stderr.strip() == "456"
