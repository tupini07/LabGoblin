"""Platform-specific launch options for non-interactive subprocesses."""

import os
import subprocess


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
