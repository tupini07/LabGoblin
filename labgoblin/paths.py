"""Canonical project paths and explicit environment overrides."""

from dataclasses import dataclass
import os
from pathlib import Path


DATABASE_NAME = "labgoblin.db"


def present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


@dataclass(frozen=True)
class ProjectPaths:
    config: Path
    state: Path


def project_paths(root: str | Path) -> ProjectPaths:
    root = Path(root).resolve()
    return ProjectPaths(root / "labgoblin.toml", root / ".labgoblin")


def configuration_path(path: str | Path = ".") -> Path:
    path = Path(path).absolute()
    if path.is_dir():
        return project_paths(path).config
    return path.resolve()


def state_directory(config_path: str | Path) -> Path:
    return project_paths(Path(config_path).resolve().parent).state


def database_path(state: str | Path) -> Path:
    return Path(state).absolute() / DATABASE_NAME


def environment_value(name: str, default=None):
    key = f"LABGOBLIN_{name}"
    value = os.environ.get(key)
    if value is not None and not value.strip():
        raise ValueError(f"{key} must not be empty")
    return value if value is not None else default


def machine_ledger_path() -> Path:
    override = environment_value("RESOURCE_DB")
    if override is not None:
        return Path(override).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "state"))
    return base / "labgoblin" / "resources.db"
