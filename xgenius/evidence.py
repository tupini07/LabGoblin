"""Bounded byte capture and durable publication, shared by research views."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from xgenius.protocol import canonical, identifier, integer, number, text
from xgenius.processes import replace_file, unlink_file


class SizeLimitError(ValueError):
    pass


def contained(root: Path, name: str | Path) -> Path:
    root = root.resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Path escapes its declared root: {name}")
    return target


def read_bytes(path: Path, limit: int) -> bytes:
    integer(limit, "read byte limit")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise SizeLimitError(f"{path.name} exceeds the {limit}-byte read limit")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Nonfinite JSON value: {value}")


def parse_json(value: bytes):
    return json.loads(value.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def read_json(path: Path, limit: int = 1024 * 1024):
    return parse_json(read_bytes(path, limit))


def atomic_bytes(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{identifier()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        unlink_file(temporary, missing_ok=True)


def atomic_json(path: Path, value):
    atomic_bytes(path, canonical(value))


def publish_bytes(path: Path, value: bytes) -> str:
    """Publish once without overwriting a concurrent or corrupt revision."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{identifier()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.stat().st_size != len(value) or read_bytes(path, max(1, len(value))) != value:
                raise ValueError(f"Published revision already contains different bytes: {path}")
    finally:
        unlink_file(temporary, missing_ok=True)
    return hashlib.sha256(value).hexdigest()


def publish_stream(path: Path, chunks, *, limit: int) -> dict:
    """Bounded immutable streaming publication, with exact idempotent replay."""
    integer(limit, "publication byte limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{identifier()}.tmp")
    digest, size = hashlib.sha256(), 0
    try:
        with temporary.open("xb") as stream:
            for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise SizeLimitError(f"Publication exceeds its explicit {limit}-byte allowance")
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.stat().st_size != size or hash_file(path, max(1, size)) != digest.hexdigest():
                raise ValueError("Published revision already contains different bytes")
    finally:
        unlink_file(temporary, missing_ok=True)
    return {"path": str(path), "bytes": size, "sha256": digest.hexdigest()}


def copy_bounded(source: Path, target: Path, limit: int) -> dict:
    integer(limit, "copy byte limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{identifier()}.tmp")
    digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            while True:
                block = src.read(min(64 * 1024, limit - total + 1))
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise SizeLimitError(f"Source copy exceeds remaining {limit}-byte snapshot allowance")
                dst.write(block)
                digest.update(block)
            dst.flush()
            os.fsync(dst.fileno())
            if os.name != "nt":
                import stat
                os.chmod(temporary, stat.S_IMODE(os.fstat(src.fileno()).st_mode))
        os.link(temporary, target)
    finally:
        unlink_file(temporary, missing_ok=True)
    return {"bytes": total, "sha256": digest.hexdigest()}


def hash_file(path: Path, limit: int) -> str:
    integer(limit, "hash byte limit")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(min(64 * 1024, limit - total + 1))
            if not block:
                break
            total += len(block)
            if total > limit:
                raise SizeLimitError(f"{path.name} exceeds its explicit {limit}-byte verification limit")
            digest.update(block)
    return digest.hexdigest()


def verify_input_pins(inputs: dict) -> list[dict]:
    checks = []
    for name, value in inputs.items():
        path = Path(value["access_path"])
        if not path.exists():
            raise ValueError(f"Declared input disappeared: {name}")
        pin = value.get("sha256")
        if pin and (not path.is_file() or hash_file(path, value["verification_bytes"]) != pin):
            raise ValueError(f"Input revision drift before execution: {name}")
        checks.append({"name": name, "path": str(path), "sha256": pin or None,
                       "assurance": value["assurance"], "checked_at": time.time()})
    return checks


@dataclass(frozen=True)
class Capture:
    body: bytes
    digest: str

    @classmethod
    def read(cls, path: Path, limit: int) -> "Capture":
        body = read_bytes(path, limit)
        return cls(body, hashlib.sha256(body).hexdigest())

    def metrics(self) -> dict[str, int | float]:
        values = parse_json(self.body)
        if not isinstance(values, dict):
            raise ValueError("Metrics must contain a numeric JSON object")
        for name, value in values.items():
            text(name, "metric name")
            if type(value) not in (int, float):
                raise ValueError(f"Metric {name} must be a finite number")
            number(abs(value), f"metric {name}", zero=True)
        return values


def tail(path: Path, *, limit: int = 64 * 1024, lines: int | None = None) -> dict:
    integer(limit, "tail byte limit")
    if lines is not None:
        integer(lines, "tail lines")
    pieces = []
    remaining = limit
    read_count = 0
    total = 0
    for part in (path, path.with_name(path.name + ".previous")):
        try:
            with part.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                total += size
                if remaining:
                    stream.seek(max(0, size - remaining))
                    value = stream.read(remaining)
                    pieces.insert(0, value)
                    remaining -= len(value)
                    read_count += len(value)
        except FileNotFoundError:
            if part == path:
                raise
    content = b"".join(pieces).decode("utf-8", errors="replace")
    if lines is not None:
        content = "".join(content.splitlines(keepends=True)[-lines:])
    return {"text": content, "bytes_read": read_count, "retained_bytes": total,
            "truncated": total > read_count or path.with_name(path.name + ".previous").exists()}


def watermarks(storage) -> list[dict]:
    result = []
    for name, item in storage.volumes.items():
        path = Path(item.path).resolve(strict=True)
        free = shutil.disk_usage(path).free
        threshold = item.min_free_mb * 1024 * 1024
        result.append({"name": name, "path": str(path), "free_bytes": free,
                       "minimum_bytes": threshold, "ready": free >= threshold,
                       "enforcement": "soft admission watermark"})
    return result


def require_space(storage):
    failures = [item for item in watermarks(storage) if not item["ready"]]
    if failures:
        raise OSError("Storage admission blocked: " + "; ".join(
            f"{item['name']} has {item['free_bytes']} bytes free; requires {item['minimum_bytes']}"
            for item in failures))


def observation(db, observation_id: str, *, limit: int = 1024 * 1024) -> dict:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown evidence revision")
    metadata = json.loads(row["metadata"])
    result = {**dict(row), "metadata": metadata}
    if row["assurance"] != "captured":
        result["body"] = None
        result["download_status"] = "Current mutable artifact only; no exact retained download"
        return result
    path = contained(db.path.parent, metadata["capture_path"])
    capture = Capture.read(path, min(limit, max(1, row["size"])))
    if capture.digest != row["digest"] or len(capture.body) != row["size"]:
        raise ValueError("Retained evidence revision is unavailable or has been modified")
    result.update(body=capture.body, download_status="Exact captured bytes")
    return result


def _owned_size(path: Path, *, max_entries=10000):
    import stat
    count = size = skipped = 0
    pending = [path]
    while pending and count < max_entries:
        current = pending.pop()
        info = current.lstat()
        count += 1
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            skipped += 1
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                for entry in entries:
                    if count + len(pending) >= max_entries:
                        return {"logical_bytes": size, "entries": count, "complete": False, "links_skipped": skipped}
                    pending.append(Path(entry.path))
    return {"logical_bytes": size, "entries": count, "complete": not pending, "links_skipped": skipped}


def storage_inventory(state, *, limit=25, offset=0):
    integer(limit, "inventory page size")
    integer(offset, "inventory offset", zero=True)
    if limit > 100:
        raise ValueError("Inventory pages cannot exceed 100 owned objects")
    query = """SELECT 'attempt' kind,id,json_extract(spec,'$.root') path FROM attempts
        UNION ALL SELECT 'packet',id,path FROM packets WHERE path IS NOT NULL
        UNION ALL SELECT 'capture',id,json_extract(metadata,'$.capture_path') FROM observations WHERE assurance='captured'
        UNION ALL SELECT 'report',r.id || ':' || j.key,json_extract(j.value,'$.path') FROM reports r,json_each(r.outputs) j
        UNION SELECT 'runtime',json_extract(envelope,'$.metadata.runtime.id'),
            json_extract(envelope,'$.metadata.runtime.root') FROM launches
        UNION ALL SELECT 'database','campaign',?"""
    with state.db.read() as conn:
        total = conn.execute("SELECT COUNT(*) FROM (" + query + ")", (str(state.path),)).fetchone()[0]
        rows = conn.execute("SELECT * FROM (" + query + ") ORDER BY kind,id LIMIT ? OFFSET ?",
                            (str(state.path), limit, offset)).fetchall()
    objects = []
    for row in rows:
        item = {**dict(row), "referenced": True, "retention": "Retain; authoritative or audit-referenced"}
        try:
            path = contained(state.root, row["path"])
            item.update(_owned_size(path))
        except (OSError, ValueError, TypeError) as error:
            item["error"] = f"{type(error).__name__}: {error}"
        objects.append(item)
    return {"objects": objects, "total": total, "offset": offset, "has_more": offset + len(rows) < total,
            "scope": "Referenced owned objects, not archives or arbitrary native writes; live sizes may change and hardlinks count logically",
            "failed": sum("error" in row for row in objects)}


def retention_candidates(state, *, limit=25, offset=0):
    import itertools
    from xgenius.processes import process_state
    integer(limit, "retention page size")
    integer(offset, "retention offset", zero=True)
    if limit > 100 or offset > 10000:
        raise ValueError("Retention inspections page at most 100 items within a 10000-directory scope")
    root = state.root / "attempts"
    if not root.exists():
        return {"dry_run": True, "candidates": [], "has_more": False, "scanned": 0}
    with os.scandir(root) as entries:
        selected = sorted(itertools.islice(entries, 10001), key=lambda entry: entry.name)
    scope_truncated = len(selected) > 10000
    selected = selected[:10000]
    candidates = []
    for entry in selected[offset:offset + limit]:
        item = {"path": entry.path, "candidate": False, "reason": "Ownership unverified; retain"}
        marker = Path(entry.path) / "preparation.json"
        try:
            if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                item["reason"] = "Link/reparse point is not followed"
            elif marker.is_file():
                owner = read_json(marker, 16384)
                with state.db.read() as conn:
                    reference = conn.execute("SELECT 1 FROM attempts WHERE id=?", (entry.name,)).fetchone()
                if reference:
                    item["reason"] = "Referenced attempt; retain every original"
                elif owner.get("protocol") == 3 and owner.get("owner_id") == state.id and owner.get("work_id") == entry.name:
                    liveness = process_state(owner.get("handle"))
                    item.update(candidate=liveness == "dead",
                                reason="Uncommitted owned preparation; owner " + liveness + ". Review before any manual cleanup.")
            if item["candidate"]:
                item.update(_owned_size(Path(entry.path)))
        except (OSError, ValueError, RuntimeError) as error:
            item.update(candidate=False, reason=f"Inspection failed; retain: {error}")
        candidates.append(item)
    return {"dry_run": True, "candidates": candidates, "scanned": len(candidates), "offset": offset,
            "has_more": offset + len(candidates) < len(selected), "scope_truncated": scope_truncated,
            "scope": "Only explicitly marked uncommitted preparations; unmarked paths are not guessed safe. No delete/apply mode."}
