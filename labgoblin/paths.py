"""Research workspace and shared machine ledger locations."""

import os
from pathlib import Path


PROJECT_MARKERS = (".labgoblin", ".labgoblin.lock", "labgoblin.toml")


def research_root(project):
    root = Path(project).resolve(strict=True)
    research = root / "research"
    direct = any((root / name).exists() or (root / name).is_symlink() for name in PROJECT_MARKERS)
    nested = any((research / name).exists() or (research / name).is_symlink() for name in PROJECT_MARKERS)
    if direct:
        if nested:
            raise ValueError("Ambiguous campaign directory: project files exist both here and in research")
        return root
    if nested:
        if research.is_symlink() or research.resolve() != research:
            raise ValueError("The campaign's research directory cannot be an alias; use its exact project path")
        return research
    return root


def machine_ledger_path():
    override = os.environ.get("LABGOBLIN_RESOURCE_DB")
    if override is not None:
        if not override.strip():
            raise ValueError("LABGOBLIN_RESOURCE_DB must not be empty")
        return Path(override).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "state"))
    return base / "labgoblin" / "resources.db"
