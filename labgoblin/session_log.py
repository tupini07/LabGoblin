"""Original SDK events in bounded segments with a host-built navigation index."""

import hashlib
import json
import stat
from pathlib import Path

from labgoblin.protocol import integer, text
from labgoblin.storage import atomic_json, canonical, digest


VERSION = 1
SEGMENT_BYTES = 8 * 1024 * 1024
INDEX_LINE_BYTES = 4096
PAGE_SCAN_BYTES = 2 * 1024 * 1024
PUBLIC_EVENTS = {
    "user.message", "assistant.turn_start", "assistant.turn_end", "assistant.message",
    "tool.execution_start", "tool.execution_complete", "session.error", "session.idle",
    "session.compaction_start", "session.compaction_complete",
}


class EventLog:
    def __init__(self, directory, *, event_bytes, total_bytes, segment_bytes=SEGMENT_BYTES):
        self.directory = Path(directory)
        self.event_limit, self.total_limit, self.segment_limit = event_bytes, total_bytes, segment_bytes
        self.seen = self.retained = self.total_bytes = 0
        self.omitted = {}
        self.unindexed_from = None
        self.exhausted = self.closed = False
        self.segments = []
        self._open_segment()
        self.checkpoint()

    def _open_segment(self):
        number = len(self.segments)
        value = {"events": "events.jsonl" if number == 0 else f"events-{number:06}.jsonl",
                 "index": f"events-index-{number:06}.jsonl", "bytes": 0, "index_bytes": 0,
                 "first_sequence": self.seen, "end_sequence": self.seen,
                 "first_prompt": None, "last_prompt": None, "closed": False}
        self.events = (self.directory / value["events"]).open("xb")
        self.index = (self.directory / value["index"]).open("xb")
        self.event_hash, self.index_hash = hashlib.sha256(), hashlib.sha256()
        self.segments.append(value)

    def _close_segment(self):
        self.events.close()
        self.index.close()
        self.segments[-1].update(closed=True, sha256=self.event_hash.hexdigest(),
                                 index_sha256=self.index_hash.hexdigest())

    def append(self, value, *, prompt):
        if self.closed:
            raise ValueError("Cannot append to a closed session event log")
        sequence = self.seen
        self.seen += 1
        body = canonical(value) + b"\n"
        data = value.get("data") or {}
        metadata = {"type": str(value.get("type", "")), "timestamp": str(value.get("timestamp", "")),
                    "tool_call_id": str(data.get("toolCallId", data.get("tool_call_id", ""))),
                    "tool": str(data.get("toolName", data.get("tool_name", "")))}
        entry = {"sequence": sequence, "prompt": prompt,
                 **{key: item[:128] for key, item in metadata.items()},
                 "metadata_truncated": any(len(item) > 128 for item in metadata.values())}
        reason = "storage_limit" if self.exhausted else "event_size" if len(body) - 1 > self.event_limit else None
        if reason:
            entry["omitted"] = reason
        else:
            entry.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        segment = self.segments[-1]
        entry["offset"] = segment["bytes"]
        encoded = canonical(entry) + b"\n"
        required = len(encoded) + (0 if reason else len(body))
        if self.total_bytes + required > self.total_limit or self.exhausted:
            self.exhausted = True
            if self.unindexed_from is None:
                self.unindexed_from = sequence
            self.omitted["storage_limit"] = self.omitted.get("storage_limit", 0) + 1
            return
        if segment["index_bytes"] and (segment["index_bytes"] + len(encoded) > self.segment_limit
                                       or not reason and segment["bytes"] + len(body) > self.segment_limit):
            self._close_segment()
            self._open_segment()
            segment = self.segments[-1]
            segment["first_sequence"] = sequence
            entry["offset"] = 0
            encoded = canonical(entry) + b"\n"
            self.checkpoint()
        if not reason:
            self.events.write(body)
            self.events.flush()
            self.event_hash.update(body)
            segment["bytes"] += len(body)
            self.total_bytes += len(body)
            self.retained += 1
        else:
            self.omitted[reason] = self.omitted.get(reason, 0) + 1
        self.index.write(encoded)
        self.index.flush()
        self.index_hash.update(encoded)
        segment["index_bytes"] += len(encoded)
        segment["end_sequence"] = sequence + 1
        segment["first_prompt"] = prompt if segment["first_prompt"] is None else segment["first_prompt"]
        segment["last_prompt"] = prompt
        self.total_bytes += len(encoded)

    def checkpoint(self):
        value = self._manifest()
        if not self.closed:
            atomic_json(self.directory / "events-manifest.json", value)
        return {"version": VERSION, "manifest_sha256": digest(value), "closed": self.closed,
                "seen": self.seen, "retained": self.retained, "omitted": dict(self.omitted)}

    def _manifest(self):
        return {"version": VERSION, "closed": self.closed, "seen": self.seen, "retained": self.retained,
                 "omitted": self.omitted, "bytes": self.total_bytes,
                 "unindexed_from": self.unindexed_from,
                 "event_limit": self.event_limit, "total_limit": self.total_limit,
                 "segment_limit": self.segment_limit, "segments": self.segments}

    def close(self):
        if not self.closed:
            self._close_segment()
            self.closed = True
            atomic_json(self.directory / "events-manifest.json", self._manifest())


def page(directory, args, *, terminal_log=None):
    from labgoblin.inspection_evidence import bounded_bytes, record

    directory = Path(directory)
    manifest = record(directory / "events-manifest.json")
    if manifest.get("version") != VERSION:
        raise ValueError("Unsupported session event-log version")
    if terminal_log is not None and digest(manifest) != terminal_log.get("manifest_sha256"):
        raise ValueError("Session event manifest differs from its final owned usage")
    segments = manifest["segments"]
    if len(segments) > 1024:
        raise ValueError("Session event segment inventory exceeds its read bound")
    offset = integer(args.get("offset", 0), "event sequence offset", zero=True)
    selected = args.get("event")
    if selected is not None:
        selected = integer(selected, "event sequence", zero=True)
    prompt = args.get("prompt")
    if prompt is not None:
        prompt = integer(prompt, "researcher prompt number")
    for key in ("kind", "tool_call_id"):
        if key in args:
            text(args[key], key)
            if len(args[key]) > 128:
                raise ValueError("Indexed metadata is bounded to 128 characters; inspect unfiltered original events for longer values")
    start = selected if selected is not None else offset
    result = {"events": [], "offset": offset, "offset_unit": "event_sequence",
              "next_offset": None, "scanned_bytes": 0, "incomplete_tail": False,
              "retention_truncated": bool(manifest["omitted"]), "omitted": manifest["omitted"],
              "observed_events": manifest["seen"], "manifest_closed": manifest["closed"],
              "receipt_verified": terminal_log is not None,
              "unindexed_range": ([manifest["unindexed_from"], manifest["seen"]]
                                  if manifest.get("unindexed_from") is not None else None)}
    payload_bytes = 0
    for number, segment in enumerate(segments):
        if segment["closed"] and segment["end_sequence"] <= start:
            continue
        if prompt is not None and segment["last_prompt"] is not None and segment["closed"]:
            if not segment["first_prompt"] <= prompt <= segment["last_prompt"]:
                continue
        expected = "events.jsonl" if number == 0 else f"events-{number:06}.jsonl"
        if segment["events"] != expected or segment["index"] != f"events-index-{number:06}.jsonl":
            raise ValueError("Invalid session event segment path")
        index_path, event_path = directory / segment["index"], directory / segment["events"]
        if index_path.resolve(strict=True) != index_path or event_path.resolve(strict=True) != event_path:
            raise ValueError("Session event segments cannot be file aliases")
        for path, maximum in ((index_path, SEGMENT_BYTES + INDEX_LINE_BYTES),
                              (event_path, SEGMENT_BYTES + 1024 * 1024 + 1)):
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError("Session event segment exceeds its regular-file read bound")
        if segment["closed"]:
            indexed = bounded_bytes(index_path, SEGMENT_BYTES + INDEX_LINE_BYTES)
            if hashlib.sha256(indexed).hexdigest() != segment["index_sha256"]:
                raise ValueError("Session index differs from its sealed segment")
        with index_path.open("rb") as index:
            while True:
                body = index.readline(INDEX_LINE_BYTES + 1)
                if not body:
                    break
                if len(body) > INDEX_LINE_BYTES:
                    raise ValueError("Session index row exceeds its declared bound")
                if not body.endswith(b"\n"):
                    result["incomplete_tail"] = True
                    return result
                entry = json.loads(body)
                sequence = integer(entry.get("sequence"), "indexed event sequence", zero=True)
                result["observed_events"] = max(result["observed_events"], sequence + 1)
                if sequence < start:
                    continue
                if (result["scanned_bytes"] + len(body) > PAGE_SCAN_BYTES
                        or len(result["events"]) >= 30 or payload_bytes >= PAGE_SCAN_BYTES):
                    result["next_offset"] = sequence
                    return result
                result["scanned_bytes"] += len(body)
                if selected is not None and sequence != selected:
                    raise ValueError("Requested event is not retained in the available index")
                if (prompt is not None and entry["prompt"] != prompt
                        or args.get("kind") and entry["type"] != args["kind"]
                        or args.get("tool_call_id") and entry["tool_call_id"] != args["tool_call_id"]):
                    continue
                if selected is None and not args.get("kind") and entry["type"] not in PUBLIC_EVENTS and "omitted" not in entry:
                    continue
                if "omitted" in entry:
                    result["events"].append(entry)
                    if selected is not None:
                        return result
                    continue
                size = integer(entry.get("bytes"), "indexed event bytes")
                position = integer(entry.get("offset"), "indexed event offset", zero=True)
                if size > 1024 * 1024 + 1:
                    raise ValueError("Retained event exceeds its declared bound")
                if payload_bytes + size > PAGE_SCAN_BYTES:
                    result["next_offset"] = sequence
                    return result
                with event_path.open("rb") as stream:
                    stream.seek(position)
                    raw = stream.read(size)
                if len(raw) != size or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise ValueError("Session event differs from its indexed bytes")
                payload_bytes += size
                event = json.loads(raw)
                if selected is not None:
                    if offset > len(raw):
                        raise ValueError("Event byte offset exceeds its retained record")
                    end = min(offset + 16384, len(raw))
                    while end < len(raw) and raw[end] & 0xC0 == 0x80:
                        end -= 1
                    try:
                        fragment = raw[offset:end].decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise ValueError("Use the event-page next_offset, not a position inside a UTF-8 character") from error
                    return {**result, "event": selected, "offset_unit": "event_bytes",
                            "text": fragment,
                            "sha256": entry["sha256"], "bytes": len(raw),
                            "next_offset": end if end < len(raw) else None}
                data = json.dumps(event.get("data", {}), ensure_ascii=False)
                result["events"].append({**entry, "data": data[:8192], "truncated": len(data) > 8192})
    if selected is not None:
        raise ValueError("Requested event is not retained in the available index")
    return result
