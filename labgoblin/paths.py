"""The explicit shared machine ledger location; no old-name aliases."""

import os
from pathlib import Path


def machine_ledger_path():
    override = os.environ.get("LABGOBLIN_RESOURCE_DB")
    if override is not None:
        if not override.strip():
            raise ValueError("LABGOBLIN_RESOURCE_DB must not be empty")
        return Path(override).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "state"))
    return base / "labgoblin" / "resources.db"
