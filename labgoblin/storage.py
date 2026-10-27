"""Bounded control records and streaming, immutable research snapshots."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from labgoblin.processes import replace_file


CONTROL_BYTES = 1024 * 1024
EXCLUDED = {".git", ".labgoblin", ".labgoblin.lock", ".venv", "venv",
            "__pycache__", ".pytest_cache", "node_modules", "inputs",
            ".ssh", ".aws", ".azure", ".copilot", ".docker",
            ".npmrc", ".pypirc", ".netrc", ".git-credentials"}


def excluded_name(name):
    value = name.casefold()
    return value in EXCLUDED or value.startswith(".env")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path, limit=CONTROL_BYTES):
    with Path(path).open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"Control record exceeds {limit} bytes: {path}")
    return json.loads(body, parse_constant=lambda value: invalid_constant(value))


def invalid_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def read_text(path, limit=65536):
    with Path(path).open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"Text exceeds {limit} bytes: {path}")
    return body.decode("utf-8")


def hash_file(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def publish_bytes(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def publish_json(path, value):
    publish_bytes(path, canonical(value))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LockBusy(ValueError):
    pass


@contextmanager
def run_lock(path, *, owner="controller or accounting editor"):
    """One controller or accounting editor; status readers never take ownership."""
    path = Path(path)
    stream = path.open("r+b")
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise LockBusy(f"Another {owner} owns this project") from error
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise LockBusy(f"Another {owner} owns this project") from error
        yield
    finally:
        stream.close()


def inside(root, relative):
    root = Path(root).resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Path escapes the research workspace")
    return path


def snapshot(project, destination, limit, *, max_entries=100000):
    """Copy workspace files after owned execution ends; never follow directory links."""
    project, destination = Path(project).resolve(), Path(destination)
    destination.mkdir()
    files, excluded, total, visited = [], [], 0, 0
    for directory, subdirs, names in os.walk(project, followlinks=False):
        visited += len(subdirs) + len(names)
        if visited > max_entries:
            raise ValueError(f"Research snapshot exceeds {max_entries} filesystem entries")
        parent = Path(directory)
        for name in sorted(subdirs.copy()):
            path = parent / name
            if excluded_name(name) or path.is_symlink() or path.resolve() != path:
                subdirs.remove(name)
                excluded.append(str(path.relative_to(project)))
        for name in sorted(names):
            source = parent / name
            relative = source.relative_to(project)
            if excluded_name(name) or source.is_symlink() or source.resolve() != source:
                excluded.append(str(relative))
                continue
            before = source.stat()
            if not stat.S_ISREG(before.st_mode):
                excluded.append(str(relative))
                continue
            if total + before.st_size > limit:
                raise ValueError(f"Research snapshot exceeds {limit} bytes at {relative}")
            if shutil.disk_usage(destination).free < before.st_size + 512 * 1024 * 1024:
                raise OSError(f"Insufficient free space to retain {relative} with 512 MiB headroom")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            hashed, size = hashlib.sha256(), 0
            with source.open("rb") as reader, target.open("xb") as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    size += len(block)
                    if total + size > limit:
                        raise ValueError(f"Research snapshot grew beyond its byte allowance at {relative}")
                    writer.write(block)
                    hashed.update(block)
                writer.flush()
                os.fsync(writer.fileno())
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino) or size != before.st_size:
                raise ValueError(f"Research file changed during snapshot: {relative}")
            total += size
            files.append({"path": str(relative), "bytes": size, "sha256": hashed.hexdigest()})
    result = {"files": files, "bytes": total, "excluded": sorted(excluded),
              "coverage": "Regular workspace files except inputs, environments, common credential paths, caches, state, links and special files"}
    publish_json(destination.parent / "snapshot.json", result)
    return {"manifest_sha256": digest(result), "bytes": total, "files": len(files),
            "excluded": result["excluded"]}
