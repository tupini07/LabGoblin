"""Project-qualified, bounded inspection without executing research artifacts."""

import base64
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from labgoblin import runtime
from labgoblin.protocol import LaunchEnvelope, integer, table, text
from labgoblin.storage import canonical, digest, excluded_name, publish_json, read_json


PAGE_BYTES = 16384
FILE_BYTES = 8 * 1024 * 1024
IMAGE_BYTES = 4 * 1024 * 1024
ROLE = re.compile(r"researcher|coach-(0|[1-9][0-9]{0,5})|consult-(0|[1-9][0-9]{0,5})|adjudicator-([1-9][0-9]{0,5})")


def safe_path(root, relative):
    text(relative, "relative evidence path")
    path = Path(relative)
    if path.is_absolute() or path.drive or any(part in ("..", "") or ":" in part for part in path.parts):
        raise ValueError("Use a relative path inside the selected evidence scope")
    current = root
    for part in path.parts:
        if excluded_name(part):
            raise ValueError("Inputs, runtime state, credentials and environments are not ordinary inspection evidence")
        current = current / part
        if current.is_symlink() or current.resolve(strict=True) != current:
            raise ValueError("Inspection does not follow file links or directory aliases")
    if not current.resolve(strict=True).is_relative_to(root):
        raise ValueError("Evidence path escaped its selected root")
    return current


def bounded_bytes(path, limit):
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError("Inspection does not follow file aliases")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError(f"Evidence must be a regular file of at most {limit} bytes")
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("Evidence changed during this read; refresh it")
    if len(body) != before.st_size:
        raise ValueError("Evidence changed or exceeded its read bound")
    return body


def text_page(path, offset, limit, maximum):
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ValueError(f"Evidence must be a regular file of at most {maximum} bytes")
    hashed, size, page = hashlib.sha256(), 0, bytearray()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if size + len(block) > maximum:
                raise ValueError("Evidence exceeded its read bound")
            start, end = max(0, offset - size), min(len(block), offset + limit - size)
            if start < end:
                page.extend(block[start:end])
            hashed.update(block)
            size += len(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino) or size != before.st_size:
        raise ValueError("Evidence changed during this read; refresh it")
    return bytes(page), hashed.hexdigest(), size


def bounded_session_page(value):
    # Bound serialized replies, not raw events: the CLI spills large replies
    # into files that the read-only inspector is deliberately unable to open.
    while len(json.dumps(value).encode("utf-8")) > PAGE_BYTES:
        if "text" in value and len(value["text"]) > 1:
            value["text"] = value["text"][:len(value["text"]) // 2]
            value["next_offset"] = value["offset"] + len(value["text"].encode("utf-8"))
        elif len(value["events"]) > 1:
            removed = value["events"].pop()
            key = "sequence" if value.get("offset_unit") == "event_sequence" else "offset"
            value["next_offset"] = removed[key]
        elif value["events"] and value["events"][0].get("data"):
            entry = value["events"][0]
            entry["data"] = entry["data"][:len(entry["data"]) // 2]
            entry["truncated"] = True
        else:
            raise ValueError("Session event metadata exceeds its response bound")
    return value


def record(path, limit=1024 * 1024):
    return json.loads(bounded_bytes(path, limit))


def image_type(body):
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 24 and body[12:16] == b"IHDR":
        width, height = int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
        if not 0 < width * height <= 16_000_000 or max(width, height) > 16384:
            raise ValueError("PNG dimensions exceed the inspection bound")
        return "image/png"
    if body.startswith(b"\xff\xd8"):
        position = 2
        while position + 4 <= len(body):
            if body[position] != 255:
                break
            marker = body[position + 1]
            if marker == 255:
                position += 1
                continue
            if marker in (0xD9, 0xDA):
                break
            size = int.from_bytes(body[position + 2:position + 4], "big")
            if size < 2 or position + size + 2 > len(body):
                break
            if marker in (0xC0, 0xC1, 0xC2) and size >= 8:
                height = int.from_bytes(body[position + 5:position + 7], "big")
                width = int.from_bytes(body[position + 7:position + 9], "big")
                if 0 < width * height <= 16_000_000 and max(width, height) <= 16384:
                    return "image/jpeg"
                break
            position += size + 2
    raise ValueError("Only bounded PNG and JPEG images are supported; active SVG/HTML is never rendered")


class Evidence:
    def __init__(self, project, project_id, *, terminal=None):
        self.root, self.state, record = runtime.project_record(project)
        if record["id"] != project_id:
            raise ValueError("Inspection target identity changed")
        self.project_id = project_id
        self.terminal = terminal

    def status(self):
        if self.terminal is not None:
            return self.execution({"run": self.terminal["research_run"]})
        value = runtime.status(self.root)
        if value["project_id"] != self.project_id:
            raise ValueError("Inspection target identity changed")
        return {**value, "observed_at": time.time(), "runs": value["runs"][-100:],
                "omitted_runs": max(0, len(value["runs"]) - 100),
                "coverage": "Read-only operational facts, not scientific progress or independent validation"}

    def ideas(self, args):
        if self.terminal is not None:
            raise ValueError("A terminal handoff cannot read the mutable operator inbox")
        from labgoblin.ideas import Inbox
        table(args, "idea listing", set())
        inbox = Inbox(self.root)
        return {**inbox.status(), "entries": inbox.entries(), "observed_at": time.time(), "live": True,
                "coverage": "Operator publications and retained replies, not verified research findings"}

    def run(self, run_id):
        if self.terminal is not None and run_id != self.terminal["research_run"]:
            raise ValueError("This handoff can inspect only its exact terminal execution")
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("An exact research execution ID is required")
        ledger = runtime.bound_ledger(self.state)
        row = ledger.consumer_run(run_id) if ledger else None
        if row is None or row["inspection"]:
            raise ValueError("No research execution with that ID belongs to this project")
        envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
        directory = self.state / "runs" / run_id
        if (envelope.project_id != self.project_id or envelope.project != str(self.root)
                or envelope.root != str(directory) or directory.resolve(strict=True) != directory):
            raise ValueError("Execution is not qualified for this exact project")
        if record(directory / "envelope.json") != json.loads(row["envelope"]):
            raise ValueError("Execution envelope differs from its ledger identity")
        if self.terminal is not None and (
                row["phase"] != "quiescent" or not row["receipt"]
                or digest(json.loads(row["receipt"])) != self.terminal["receipt_sha256"]):
            raise ValueError("Handoff terminal receipt identity changed")
        return row, envelope, directory

    def scope(self, selected):
        if self.terminal is not None:
            if selected not in ("latest", self.terminal["research_run"]):
                raise ValueError("A handoff cannot read mutable workspace files or another execution")
            selected = self.terminal["research_run"]
        if selected == "latest":
            status = self.status()
            latest = status["runs"][-1] if status["runs"] else None
            selected = latest["id"] if latest and latest["phase"] == "quiescent" else "workspace"
        if selected == "workspace":
            return self.root, None, {"scope": "workspace", "live": True,
                                      "coverage": "Mutable working files; not an atomic project snapshot"}
        row, envelope, directory = self.run(selected)
        if row["phase"] != "quiescent" or not row["receipt"]:
            raise ValueError("This execution has no terminal snapshot; select workspace for a live read")
        receipt = json.loads(row["receipt"])
        expected = receipt.get("metadata", {}).get("snapshot")
        if expected is None:
            raise ValueError("This execution did not retain a workspace snapshot")
        manifest = record(directory / "snapshot.json", 8 * 1024 * 1024)
        if digest(manifest) != expected["manifest_sha256"]:
            raise ValueError("Snapshot manifest identity differs from its owned receipt")
        root = directory / "snapshot"
        if root.resolve(strict=True) != root:
            raise ValueError("Snapshot directory cannot be an alias")
        inventory = {item["path"]: item for item in manifest["files"]}
        return root, inventory, {"scope": envelope.id, "live": False,
                                 "coverage": manifest["coverage"], "manifest_sha256": digest(manifest)}

    def read(self, args):
        table(args, "evidence request", {"scope", "path", "offset", "limit", "image", "sha256"})
        root, inventory, context = self.scope(args.get("scope", "latest"))
        path = safe_path(root, args.get("path", "report.md"))
        relative = str(path.relative_to(root))
        image = args.get("image", False)
        if type(image) is not bool:
            raise ValueError("image must be boolean")
        if image:
            body = bounded_bytes(path, IMAGE_BYTES)
            sha, size = hashlib.sha256(body).hexdigest(), len(body)
        else:
            from labgoblin.ideas import MAX_BYTES
            offset = integer(args.get("offset", 0), "byte offset", zero=True)
            limit = integer(args.get("limit", PAGE_BYTES), "page bytes")
            if limit > PAGE_BYTES:
                raise ValueError("Invalid evidence page; read at most 16 KiB")
            published = tuple(part.casefold() for part in path.relative_to(root).parts[:2]) == ("ideas_inbox", "published")
            page, sha, size = text_page(path, offset, limit, MAX_BYTES if published else FILE_BYTES)
        if "sha256" in args and args["sha256"] != sha:
            raise ValueError("Evidence changed between pages; start a fresh read")
        if not image and offset > size:
            raise ValueError("Invalid evidence page offset")
        if inventory is not None:
            item = inventory.get(relative)
            if item is None or item["sha256"] != sha or item["bytes"] != size:
                raise ValueError("Evidence bytes differ from their captured snapshot")
        result = {**context, "path": relative, "sha256": sha, "bytes": size, "observed_at": time.time()}
        if image:
            return {**result, "mime": image_type(body), "image": base64.b64encode(body).decode("ascii")}
        return {**result, "offset": offset, "text": page.decode("utf-8", errors="replace"),
                "next_offset": offset + limit if offset + limit < size else None,
                "truncated": offset != 0 or offset + limit < size}

    def listing(self, args):
        table(args, "listing request", {"scope", "path", "offset"})
        root, inventory, context = self.scope(args.get("scope", "latest"))
        path = safe_path(root, args.get("path", "."))
        offset = integer(args.get("offset", 0), "entry offset", zero=True)
        entries = list(itertools.islice(path.iterdir(), 10001))
        if len(entries) > 10000:
            raise ValueError("Directory exceeds 10000 entries; select a narrower path")
        result = []
        excluded = 0
        for item in sorted(entries, key=lambda entry: entry.name):
            if excluded_name(item.name) or item.is_symlink() or item.resolve() != item:
                excluded += 1
                continue
            relative = str(item.relative_to(root))
            if item.is_file() and inventory is not None and relative not in inventory:
                excluded += 1
                continue
            result.append({"path": relative, "directory": item.is_dir()})
        return {**context, "entries": result[offset:offset + 128], "excluded": excluded,
                "total": len(result), "next_offset": offset + 128 if offset + 128 < len(result) else None,
                "observed_at": time.time()}

    def search(self, args):
        table(args, "search request", {"scope", "path", "query", "offset"})
        query = text(args.get("query"), "literal search query")
        if len(query) > 256:
            raise ValueError("Search query exceeds 256 characters")
        root, inventory, context = self.scope(args.get("scope", "latest"))
        directory = safe_path(root, args.get("path", "."))
        offset = integer(args.get("offset", 0), "file offset", zero=True)
        candidates, visited = [], 0
        for current, dirs, files in os.walk(directory, followlinks=False):
            visited += len(dirs) + len(files)
            if visited > 10000:
                raise ValueError("Search exceeds 10000 entries; choose a narrower directory")
            parent = Path(current)
            dirs[:] = sorted(name for name in dirs if not excluded_name(name)
                             and (parent / name).resolve() == parent / name and not (parent / name).is_symlink())
            for name in sorted(files):
                item = parent / name
                if (not excluded_name(name) and not item.is_symlink() and item.resolve() == item
                        and item.suffix.lower() in {".md", ".txt", ".py", ".json", ".csv", ".toml", ".log"}
                        and (inventory is None or str(item.relative_to(root)) in inventory)):
                    candidates.append(item)
        hits, skipped, scanned, size = [], [], 0, 0
        for item in candidates[offset:offset + 64]:
            relative = str(item.relative_to(root))
            if item.stat().st_size > FILE_BYTES:
                skipped.append(relative)
                scanned += 1
                continue
            body = bounded_bytes(item, FILE_BYTES)
            if size + len(body) > 2 * 1024 * 1024:
                skipped.append(relative)
                scanned += 1
                continue
            if inventory is not None and hashlib.sha256(body).hexdigest() != inventory[relative]["sha256"]:
                raise ValueError("Search input differs from its snapshot")
            size += len(body)
            byte_offset = 0
            for index, raw_line in enumerate(body.splitlines(keepends=True)):
                line = raw_line.decode("utf-8", errors="replace")
                if query.casefold() in line.casefold():
                    hits.append({"path": relative, "line": index + 1, "offset": byte_offset, "text": line[:500]})
                    if len(hits) == 32:
                        break
                byte_offset += len(raw_line)
            scanned += 1
            if len(hits) == 32:
                break
        end = offset + scanned
        return {**context, "hits": hits, "scanned_files": scanned, "scanned_bytes": size,
                "skipped_files": skipped, "next_offset": end if end < len(candidates) else None,
                "truncated": bool(skipped) or end < len(candidates) or len(hits) == 32,
                "coverage": "Bounded literal text search; no match does not establish absence",
                "observed_at": time.time()}

    def execution(self, args):
        table(args, "execution request", {"run", "offset"})
        row, envelope, directory = self.run(args.get("run"))
        prompt_body = bounded_bytes(directory / "prompt.json", 1024 * 1024)
        if hashlib.sha256(prompt_body).hexdigest() != envelope.prompt_sha256:
            raise ValueError("Frozen research prompt identity changed")
        prompt = json.loads(prompt_body)
        receipt = json.loads(row["receipt"]) if row["receipt"] else None
        provider = (receipt or {}).get("metadata", {}).get("provider", {})
        reviews = provider.get("reviews", [])
        offset = integer(args.get("offset", 0), "review offset", zero=True)
        return {"run": envelope.id, "goal": prompt["goal"], "config": envelope.config,
                "phase": row["phase"], "created": row["created"], "ended": row["ended"],
                "status": receipt["status"] if receipt else "running",
                "reason": receipt["reason"] if receipt else "",
                "elapsed": receipt["elapsed"] if receipt else None,
                "checkpoint_reason": provider.get("checkpoint_reason"),
                "reviews": reviews[offset:offset + 10], "review_count": len(reviews),
                "next_offset": offset + 10 if offset + 10 < len(reviews) else None,
                "sessions": (receipt.get("usage") or {}).get("sessions", []) if receipt else [],
                "live": row["phase"] != "quiescent", "observed_at": time.time(),
                "coverage": "Frozen goal and owned execution/review records; coach judgments are not scientific certification"}

    def session(self, args):
        table(args, "session request", {"run", "role", "offset", "prompt", "kind", "tool_call_id", "event"})
        row, envelope, directory = self.run(args.get("run"))
        role = text(args.get("role", "researcher"), "session role")
        if not ROLE.fullmatch(role):
            raise ValueError("Unknown research session role")
        session = directory / "sessions" / role
        if session.resolve(strict=True) != session:
            raise ValueError("Session directory cannot be an alias")
        identity = record(session / "identity.json")
        expected = str(uuid.UUID(envelope.id) if role == "researcher" else uuid.uuid5(uuid.UUID(envelope.id), role))
        if (identity.get("run_id") != envelope.id or identity.get("envelope_digest") != envelope.digest
                or identity.get("session_id") != expected or identity.get("role") != role):
            raise ValueError("Session is not bound to this owned research execution")
        receipt = json.loads(row["receipt"]) if row["receipt"] else {}
        usage = next((item for item in (receipt.get("usage") or {}).get("sessions", [])
                      if item.get("role") == role and item.get("session_id") == expected), None)
        terminal_log = usage.get("event_log") if usage else None
        if terminal_log is not None and identity.get("event_log_version") != terminal_log.get("version"):
            raise ValueError("Session event-log capability differs from its final owned usage")
        if "event_log_version" in identity:
            from labgoblin.session_log import VERSION, page
            if type(identity["event_log_version"]) is not int or identity["event_log_version"] != VERSION:
                raise ValueError("Unsupported session event-log version")
            return bounded_session_page({**page(session, args, terminal_log=terminal_log),
                    "run": envelope.id, "role": role, "live": row["phase"] != "quiescent",
                    "observed_at": time.time(),
                    "coverage": "Indexed original SDK events. Use event=sequence to page a full retained event; omitted records remain explicit. Not a scientific progress assessment."})
        if any(key in args for key in ("prompt", "kind", "tool_call_id", "event")):
            raise ValueError("This legacy event spool has no sequence index; use its byte-page next_offset")
        offset = integer(args.get("offset", 0), "event byte offset", zero=True)
        path = session / "events.jsonl"
        if path.resolve(strict=True) != path:
            raise ValueError("Session events cannot be a file alias")
        entries, read_bytes, incomplete_tail = [], 0, False
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if offset > size or offset and (stream.seek(offset - 1) is not None and stream.read(1) != b"\n"):
                raise ValueError("Use an event-page next_offset, not a position inside an event")
            stream.seek(offset)
            while len(entries) < 30 and read_bytes < 2 * 1024 * 1024:
                start = stream.tell()
                remaining = 2 * 1024 * 1024 - read_bytes
                body = stream.readline(min(1024 * 1024 + 2, remaining + 1))
                if not body:
                    break
                if len(body) > remaining:
                    stream.seek(start)
                    break
                if not body.endswith(b"\n"):
                    stream.seek(start)
                    if len(body) > 1024 * 1024:
                        raise ValueError("Retained event exceeds its declared bound")
                    incomplete_tail = True
                    break
                read_bytes += len(body)
                event = json.loads(body)
                kind, data = event.get("type"), event.get("data", {})
                if kind in ("assistant.message", "tool.execution_start", "tool.execution_complete", "session.error"):
                    encoded = json.dumps(data, ensure_ascii=False)
                    entries.append({"offset": start, "type": kind, "timestamp": event.get("timestamp"),
                                    "data": encoded[:8192], "truncated": len(encoded) > 8192})
            end = stream.tell()
        return bounded_session_page({"run": envelope.id, "role": role, "events": entries, "offset": offset,
                "next_offset": end if end < size else None, "observed_bytes": size,
                "scanned_bytes": read_bytes, "incomplete_tail": incomplete_tail,
                "retention_truncated": usage.get("events_truncated") if usage else None,
                "live": row["phase"] != "quiescent", "observed_at": time.time(),
                "coverage": "Selected retained public messages/tool events, not private reasoning or a complete action audit"})


class EvidenceTools:
    def __init__(self, evidence, directory):
        self.evidence, self.directory = evidence, Path(directory)
        self.count, self.images = 0, 0
        self.manifest = {}

    def capture(self, action, args):
        if self.count >= 128:
            raise ValueError("This question reached its 128 evidence-read bound")
        if args.get("image") and self.images >= 8:
            raise ValueError("This question reached its eight-image bound")
        value = getattr(self.evidence, action)(args) if action != "status" else self.evidence.status()
        if len(canonical(value)) > 6 * 1024 * 1024:
            raise ValueError("Evidence response exceeds its retention bound")
        self.count += 1
        self.images += int("image" in value)
        value = {**value, "citation": f"E{self.count}"}
        capture = {"action": action, "arguments": args, "result": value}
        meta = {
            key: item for key, item in value.items()
            if key in ("citation", "path", "scope", "observed_at", "live", "mime", "truncated", "coverage")}
        for name, contents in ((f"E{self.count}.json", capture), (f"E{self.count}.meta.json", meta)):
            publish_json(self.directory / name, contents)
            self.manifest[name] = digest(contents)
        return value

    def tools(self):
        from copilot import Tool, ToolResult
        from copilot import ToolBinaryResult

        async def handle(invocation):
            try:
                action = invocation.tool_name.removeprefix("labgoblin_inspect_")
                value = self.capture(action, invocation.arguments)
                image = value.pop("image", None)
                return ToolResult(text_result_for_llm=json.dumps(value),
                                  binary_results_for_llm=[ToolBinaryResult(
                                      data=image, mime_type=value["mime"], description=value["path"])] if image else None)
            except (OSError, ValueError, KeyError, TypeError) as error:
                return ToolResult(result_type="failure", error=str(error),
                                  text_result_for_llm=f"{type(error).__name__}: {error}")

        fields = {"scope": {"type": "string", "description": "latest, workspace, or an exact completed research run ID"},
                  "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_BYTES},
                  "image": {"type": "boolean"}, "query": {"type": "string"},
                  "run": {"type": "string"}, "role": {"type": "string"},
                  "prompt": {"type": "integer", "minimum": 1},
                  "event": {"type": "integer", "minimum": 0, "description": "Page one full indexed event; offset then means a byte position within that event"},
                  "kind": {"type": "string", "description": "Exact SDK event type, for example tool.execution_complete"},
                  "tool_call_id": {"type": "string"},
                  "sha256": {"type": "string", "description": "Previous page hash; reject changed evidence"}}
        definitions = {
            "status": ([], [], "Read current execution state and research spending; not scientific progress."),
            "ideas": ([], [], "Read operator idea revisions, pending state and reply pointers; never publish."),
            "read": (["scope", "path", "offset", "limit", "image", "sha256"], ["path"], "Read cited text pages or inspect a PNG/JPEG image. No execution."),
            "listing": (["scope", "path", "offset"], [], "List bounded evidence entries with paging."),
            "search": (["scope", "path", "query", "offset"], ["query"], "Search a bounded set of text files; returns explicit coverage."),
            "session": (["run", "role", "offset", "prompt", "kind", "tool_call_id", "event"], ["run"],
                        "Read indexed original session events, optionally filtered by prompt/type/tool call; page a full event by sequence. Legacy logs use byte offsets. Never attach to a session."),
            "execution": (["run", "offset"], ["run"], "Read the frozen original goal, stopping reviews, exact session roles and final usage."),
        }
        if self.evidence.terminal is not None:
            definitions.pop("ideas")
        return [Tool(f"labgoblin_inspect_{name}", description, handle, {
            "type": "object", "properties": {key: fields[key] for key in keys},
            "required": required, "additionalProperties": False}, skip_permission=True, defer="never")
                for name, (keys, required, description) in definitions.items()]
