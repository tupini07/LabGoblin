"""Bounded evidence for an optional, read-only scientific adviser."""

import hashlib
import ipaddress
import itertools
import json
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit

from labgoblin.protocol import boolean, integer, table, text
from labgoblin.storage import canonical, excluded_name, inside, publish_json


MAX_QUESTION = 4096
MAX_BRIEF = 16384
MAX_READS = 128
CONSULT_SECONDS = 300


def failure(error):
    from copilot import ToolResult
    message = f"{type(error).__name__}: {error}"
    return ToolResult(result_type="failure", error=message, text_result_for_llm=message)


def read_evidence(project, args, *, listing=False):
    table(args, "read request", {"path", "offset", "limit"})
    root = Path(project).resolve(strict=True)
    path = inside(root, args.get("path", "."))
    relative = path.relative_to(root)
    if any(part.casefold() != "inputs" and excluded_name(part) for part in relative.parts):
        raise ValueError("Runtime state, credentials and environment files are not coach evidence or adviser context")
    offset = integer(args.get("offset", 0), "offset", zero=True)
    limit = integer(args.get("limit", 256 if listing else 8192), "limit")
    if listing:
        if limit > 256:
            raise ValueError("List at most 256 entries per page")
        entries = list(itertools.islice(path.iterdir(), 10001))
        if len(entries) > 10000:
            raise ValueError("Directory exceeds the 10000-entry inspection bound; use a narrower path")
        entries.sort(key=lambda item: item.name)
        return {"entries": [item.name + ("/" if item.is_dir() else "")
                            for item in entries[offset:offset + limit]],
                "total": len(entries), "next_offset": offset + limit if offset + limit < len(entries) else None}
    if limit > 16384:
        raise ValueError("Read at most 16 KiB per page")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Research evidence must be a regular file")
    with path.open("rb") as stream:
        stream.seek(offset)
        body = stream.read(limit + 1)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("Evidence changed while being read; request a fresh page")
    return {"text": body[:limit].decode("utf-8", errors="replace"),
            "next_offset": offset + limit if len(body) > limit else None,
            "path": str(relative), "offset": offset,
            "page_sha256": hashlib.sha256(body[:limit]).hexdigest(), "observed_at": time.time()}


def evidence_tools(project, *, audit=None):
    from copilot import Tool, ToolResult

    async def handle(invocation):
        try:
            if audit is not None:
                audit.check()
            value = read_evidence(project, invocation.arguments,
                                  listing=invocation.tool_name == "labgoblin_list_evidence")
            if audit is not None:
                audit.record(invocation.tool_name, invocation.arguments, value)
            return ToolResult(result_type="success", text_result_for_llm=json.dumps(value))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return failure(error)

    schema = {"type": "object", "properties": {"path": {"type": "string"},
              "offset": {"type": "integer", "minimum": 0},
              "limit": {"type": "integer", "minimum": 1, "maximum": 16384}},
              "required": ["path"], "additionalProperties": False}
    listing = {**schema, "properties": {
        **schema["properties"], "limit": {"type": "integer", "minimum": 1, "maximum": 256}}}
    return [
        Tool("labgoblin_read_evidence", "Read up to 16 KiB of ordinary workspace evidence; no external files, runtime state or credentials.",
             handle, schema, skip_permission=True, defer="never"),
        Tool("labgoblin_list_evidence", "List up to 256 ordinary workspace entries; runtime state and credentials are excluded.",
             handle, listing, skip_permission=True, defer="never"),
    ]


def web_arguments(args):
    table(args, "public fetch", {"url", "max_length", "start_index", "raw"})
    url = text(args.get("url"), "url")
    parsed = urlsplit(url)
    if (len(url) > 2048 or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
        raise ValueError("Use a public HTTPS URL without credentials or a custom port")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("Public retrieval cannot access local hosts")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Public retrieval cannot access non-public addresses")
    limit = integer(args.get("max_length", 5000), "max_length")
    if limit > 16384:
        raise ValueError("Fetch at most 16384 characters per page")
    integer(args.get("start_index", 0), "start_index", zero=True)
    boolean(args.get("raw", False), "raw")


class EvidenceAudit:
    def __init__(self, directory):
        self.directory = directory
        self.count = 0
        self.pending_web = 0
        self.error = ""

    def check(self):
        if self.error:
            raise ValueError(self.error)
        if self.count + self.pending_web >= MAX_READS:
            raise ValueError(f"Consultation evidence is limited to {MAX_READS} retained reads")

    def record(self, name, args, result):
        value = {"tool": name, "arguments": args, "result": result, "observed_at": time.time()}
        try:
            self.check()
            if len(canonical(value)) > 128 * 1024:
                raise ValueError("Consultation evidence result exceeds the 128 KiB retention bound")
            publish_json(self.directory / f"evidence-{self.count}.json", value)
            self.count += 1
        except (OSError, ValueError) as error:
            self.error = f"Consultation evidence retention failed: {error}"
            raise

    def before_web(self, request, context):
        try:
            self.check()
            web_arguments(request["toolArgs"])
            self.pending_web += 1
            return {"permissionDecision": "allow"}
        except (ValueError, KeyError, TypeError) as error:
            return {"permissionDecision": "deny", "permissionDecisionReason": str(error)}

    def after_web(self, request, context):
        if request["toolName"] == "web_fetch":
            try:
                if self.pending_web <= 0:
                    raise ValueError("Public fetch result has no admitted evidence-read slot")
                self.pending_web -= 1
                self.record("web_fetch", request["toolArgs"], request.get("toolResult", {"error": request.get("error")}))
            except (OSError, ValueError) as error:
                self.error = f"Consultation evidence retention failed: {error}"
                return {"additionalContext": f"Evidence retention failed; stop this consultation: {error}"}
        return {}
