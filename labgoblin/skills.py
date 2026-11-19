"""Immutable campaign-local procedural proposals and receipt-bound human review."""

import codecs
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

from labgoblin.protocol import integer, table, text
from labgoblin.storage import canonical, digest, hash_file, publish_bytes, publish_json, read_json, read_text, run_lock


VERSION = 1
MAX_FILES = 4096
MAX_BYTES = 64 * 1024 * 1024
MANIFEST_BYTES = 1024 * 1024
DECISIONS = ("eligible", "reject", "challenge", "defer")


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 80:
        raise ValueError("Skill names must be lowercase hyphenated identifiers of at most 80 characters")
    return value


def exact_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("An exact execution/batch ID is required")
    return value


def validate_entrypoint(body, skill):
    lines = body.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("SKILL.md requires closed standard frontmatter with its exact name")
    frontmatter = "\n".join(lines[1:lines.index("---", 1)])
    names = re.findall(r"(?m)^name:[ \t]*(.*)$", frontmatter)
    if len(names) != 1 or names[0].strip() not in (skill, f'"{skill}"', f"'{skill}'"):
        raise ValueError("SKILL.md requires standard frontmatter with its exact name")
    if not re.search(r"(?m)^description:[ \t]*\S", frontmatter):
        raise ValueError("SKILL.md requires a discovery description")


def safe(root, relative="."):
    root = Path(root).absolute()
    relative = str(relative).replace("\\", "/")
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or any(
            p in ("..", "") or ":" in p or p.endswith((".", " ")) for p in parts):
        raise ValueError("Unsafe skill path")
    target = root.joinpath(*parts)
    for path in (root, *root.parents, *(root.joinpath(*parts[:i + 1]) for i in range(len(parts)))):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Skill paths cannot contain links or reparse points")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Skill path escapes its declared root")
    return target


def inventory(root):
    root = safe(root)
    files, total, visited = [], 0, 0
    for directory, subdirs, names in os.walk(root, followlinks=False):
        visited += len(subdirs) + len(names)
        if visited > MAX_FILES:
            raise ValueError("Skill folder exceeds 4096 entries; publication is incomplete")
        for item in sorted(subdirs + names):
            path = safe(root, Path(directory, item).relative_to(root))
            info = path.stat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Skill folders require regular, unlinked files")
            total += info.st_size
            if total > MAX_BYTES:
                raise ValueError("Skill folder exceeds 64 MiB; publication is incomplete")
            files.append({"path": path.relative_to(root).as_posix(), "bytes": info.st_size,
                          "sha256": hash_file(path)})
    if not any(item["path"] == "SKILL.md" for item in files):
        raise ValueError("A proposed skill requires SKILL.md")
    return {"files": sorted(files, key=lambda item: item["path"]), "bytes": total}


def copy_folder(source, destination, manifest):
    destination = safe(destination)
    for item in manifest["files"]:
        origin, target = safe(source, item["path"]), safe(destination, item["path"])
        if origin.stat().st_size != item["bytes"] or hash_file(origin) != item["sha256"]:
            raise ValueError("Skill bytes changed during capture")
        target.parent.mkdir(parents=True, exist_ok=True)
        with origin.open("rb") as reader, target.open("xb") as writer:
            remaining = item["bytes"]
            while remaining:
                block = reader.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("Skill file shrank during capture")
                writer.write(block)
                remaining -= len(block)
            if reader.read(1):
                raise ValueError("Skill file grew during capture")
            writer.flush()
            os.fsync(writer.fileno())
        if hash_file(target) != item["sha256"]:
            raise ValueError("Captured skill bytes changed")
    if inventory(source) != manifest or inventory(destination) != manifest:
        raise ValueError("Complete skill folder changed during publication")


class Store:
    def __init__(self, project):
        from labgoblin.runtime import project_record
        self.project, self.state, record = project_record(project)
        self.project_id = record["id"]
        self.root = safe(self.state / "skills")

    def rows(self):
        from labgoblin.runtime import bound_ledger
        ledger = bound_ledger(self.state)
        return ledger.runs(self.project_id, funded=True) if ledger else []

    def requests(self):
        return sorted((read_json(safe(self.root, path.relative_to(self.root)))
                       for path in (self.root / "requests").glob("*.json")),
                      key=lambda item: (item["observed_at"], item["id"]))

    def active(self, rows=None):
        selected = {}
        handled = set()
        for row in self.rows() if rows is None else rows:
            if row["purpose"] != "research-run" or (
                    row["receipt"] and json.loads(row["receipt"])["status"] == "not_started"):
                continue
            envelope = json.loads(row["envelope"])
            path = Path(envelope["root"]) / "prompt.json"
            if hash_file(path) != envelope["prompt_sha256"]:
                raise ValueError("Research skill selection prompt changed")
            binding = read_json(path).get("campaign_skills")
            if binding is not None:
                if binding["version"] != VERSION or digest(binding["active"]) != binding["active_sha256"]:
                    raise ValueError("Invalid frozen active skill selection")
                selected = binding["active"]
                handled.update(binding["handled"])
        return selected, handled

    def request_view(self, request, rows=None):
        rows = self.rows() if rows is None else rows
        row = next((r for r in rows if r["token"] == request["id"]), None)
        result = {**request, "status": "pending", "usage": None}
        skipped = safe(self.root, f"outcomes/{request['id']}.json")
        if skipped.exists():
            return {**result, **read_json(skipped)}
        if row:
            receipt = json.loads(row["receipt"]) if row["receipt"] else None
            result.update(status=receipt["status"] if receipt else row["phase"],
                          usage=receipt["usage"] if receipt else None)
            if receipt:
                result["reason"] = receipt["reason"]
                batch = receipt.get("metadata", {}).get("skills_batch")
                if batch:
                    result.update(batch_sha256=batch["sha256"], status=batch["outcome"])
        elif safe(self.root, f"dispatch/{request['id']}.json").exists():
            result["status"] = "dispatch_uncertain"
        return result

    def listing(self):
        rows = self.rows()
        active, handled = self.active(rows)
        return {"active": active, "attempts": [
            {**self.request_view(request, rows), "reviewed_for_continuation": request["id"] in handled}
            for request in self.requests()], "note": "Proposals are inactive until exact reviewed continuation"}

    def batch(self, batch_id):
        batch_id = exact_id(batch_id)
        row = next((r for r in self.rows() if r["token"] == batch_id), None)
        receipt = json.loads(row["receipt"]) if row and row["receipt"] else None
        binding = receipt.get("metadata", {}).get("skills_batch") if receipt else None
        if not binding or row["purpose"] != "skill-extraction" or receipt["status"] != "completed":
            raise ValueError("No complete settled proposal batch with that exact identity")
        batch = read_json(safe(self.root, f"batches/{batch_id}.json"))
        if digest(batch) != binding["sha256"]:
            raise ValueError("Skill batch differs from its owned receipt")
        for entry in batch["entries"]:
            if entry["action"] != "retire":
                self.revision(entry["revision"])
        return batch

    def revision(self, revision):
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("An exact skill revision digest is required")
        directory = safe(self.root, f"revisions/{revision}")
        record = read_json(directory / "manifest.json")
        if digest(record) != revision or inventory(directory / "files") != record["inventory"]:
            raise ValueError("Immutable skill revision changed or is incomplete")
        return record, directory / "files"

    def decisions(self, batch_id):
        batch = self.batch(batch_id)
        entries = {item["name"]: "eligible" for item in batch["entries"]}
        previous = None
        history = []
        for path in sorted((self.root / "reviews" / batch_id).glob("*.json")):
            item = read_json(safe(self.root, path.relative_to(self.root)))
            if (item["previous"] != previous or item["batch_sha256"] != digest(batch)
                    or item["name"] not in entries or item["decision"] not in DECISIONS):
                raise ValueError("Skill review history is not an exact append-only chain")
            previous = digest(item)
            entries[item["name"]] = item["decision"]
            history.append(item)
        return {"revision": previous, "entries": entries, "history": history}

    def review(self, batch_id, skill, decision, *, expected, feedback=""):
        with run_lock(self.project / ".labgoblin.lock", owner="skill reviewer or controller"):
            batch = self.batch(batch_id)
            current = self.decisions(batch_id)
            if current["revision"] != expected:
                raise ValueError("Skill review changed; inspect the exact batch again")
            if skill not in current["entries"] or decision not in DECISIONS:
                raise ValueError("Review must name an exact proposed skill and supported decision")
            if decision == "challenge":
                text(feedback, "challenge feedback")
            value = {"batch_sha256": digest(batch), "previous": expected, "name": skill,
                     "decision": decision, "feedback": feedback, "at": time.time()}
            if len(canonical(value)) > 16384:
                raise ValueError("Skill review exceeds 16 KiB")
            publish_json(safe(self.root, f"reviews/{batch_id}/{len(current['history']):08d}.json"), value)
            return {**value, "revision": digest(value)}

    def show(self, batch_id):
        batch = self.batch(batch_id)
        diffs = []
        for entry in batch["entries"]:
            old = self.revision(entry["base"])[1] if entry["base"] else None
            new = self.revision(entry["revision"])[1] if entry["action"] != "retire" else None
            old_files = inventory(old)["files"] if old else []
            new_files = inventory(new)["files"] if new else []
            for path in sorted({i["path"] for i in old_files + new_files}):
                before = next((i for i in old_files if i["path"] == path), None)
                after = next((i for i in new_files if i["path"] == path), None)
                if before == after:
                    continue
                change = {"skill": entry["name"], "path": path, "before": before, "after": after}
                try:
                    a = read_text(safe(old, path), 32768) if before else ""
                    b = read_text(safe(new, path), 32768) if after else ""
                    change["diff"] = "".join(difflib.unified_diff(
                        a.splitlines(True), b.splitlines(True), fromfile="previous/" + path, tofile="proposal/" + path))
                except (UnicodeError, ValueError):
                    change["coverage"] = "Binary or large file: exact hashes, sizes and retained bytes; no text diff"
                diffs.append(change)
        return {"batch": batch, "review": self.decisions(batch_id), "changes": diffs}

    def publish(self, envelope):
        run = Path(envelope.root)
        request = read_json(run / "prompt.json")["skill_extraction"]
        output = safe(self.root, f"proposals/{envelope.id}")
        proposal = read_json(output / "proposal.json")
        table(proposal, "skill proposal", {"entries", "coverage", "reason"})
        text(proposal.get("coverage"), "actual evidence coverage")
        text(proposal.get("reason"), "extraction rationale")
        entries = proposal.get("entries")
        if not isinstance(entries, list):
            raise ValueError("Skill proposal entries must be an array, possibly empty")
        names, published = set(), []
        active = request["active"]
        expected_dirs = {"proposal.json"}
        total = 0
        for entry in entries:
            table(entry, "proposed skill", {"name", "action", "base", "description", "rationale", "sources", "validation"})
            skill = name(entry.get("name"))
            if skill in names or entry.get("action") not in ("upsert", "retire"):
                raise ValueError("Duplicate skill or unsupported proposal action")
            names.add(skill)
            base = active.get(skill, {}).get("revision")
            if entry.get("base") != base or entry["action"] == "retire" and base is None:
                raise ValueError("Proposed revision must name the exact active predecessor")
            text(entry.get("description"), "skill description")
            text(entry.get("rationale"), "retention rationale")
            if not isinstance(entry.get("sources"), list) or not entry["sources"]:
                raise ValueError("Skills require exact captured evidence citations")
            sources = []
            for citation in entry["sources"]:
                if not isinstance(citation, str) or not re.fullmatch(r"E[1-9][0-9]*", citation):
                    raise ValueError("Provenance must cite captured evidence IDs")
                path = safe(run / "evidence", citation + ".json")
                captured = read_json(path, 6 * 1024 * 1024)
                sources.append({"citation": citation, "sha256": hash_file(path),
                                "action": captured["action"], "arguments": captured["arguments"],
                                "observation": {k: v for k, v in captured["result"].items()
                                                if k in ("path", "sha256", "scope", "observed_at", "live", "coverage", "truncated")}})
            text(entry.get("validation"), "validation performed and limitations")
            result = {**entry, "sources": sources, "revision": None}
            if entry["action"] == "upsert":
                expected_dirs.add(skill)
                source = safe(output, skill)
                captured = inventory(source)
                total += captured["bytes"]
                if total > MAX_BYTES:
                    raise ValueError("Proposal batch exceeds 64 MiB; no partial activation")
                validate_entrypoint(read_text(source / "SKILL.md"), skill)
                record = {"name": skill, "description": entry["description"], "base": base,
                          "inventory": captured}
                revision = digest(record)
                destination = safe(self.root, f"revisions/{revision}")
                if destination.exists():
                    self.revision(revision)
                else:
                    copy_folder(source, destination / "files", captured)
                    publish_json(destination / "manifest.json", record)
                result["revision"] = revision
            published.append(result)
        if {p.name for p in output.iterdir()} != expected_dirs:
            raise ValueError("Proposal contains undeclared supporting folders/files; nothing may be silently dropped")
        batch = {"version": VERSION, "id": envelope.id, "request_sha256": digest(request),
                 "source": request["source"], "entries": published,
                 "coverage": proposal["coverage"], "reason": proposal["reason"],
                 "validation_attribution": "Extractor-reported tests; host verifies folder and provenance integrity"}
        if len(canonical(batch)) > MANIFEST_BYTES:
            raise ValueError("Skill manifest exceeds 1 MiB; publication is incomplete")
        publish_json(safe(self.root, f"batches/{envelope.id}.json"), batch)
        return {"sha256": digest(batch), "outcome": "proposed" if published else "no-useful-skill"}

    def selection(self, rows, *, batch_id=None, defer=False):
        if batch_id and defer:
            raise ValueError("Choose an exact batch or defer all, not both")
        active, handled = self.active(rows)
        pending = [r for r in self.requests() if r["id"] not in handled]
        selected = dict(active)
        review, batch_hash = None, None
        if pending and not batch_id and not defer:
            raise ValueError("Skill proposals/attempts await review: select --skills-batch ID or --defer-skills")
        if batch_id:
            if batch_id not in {r["id"] for r in pending}:
                raise ValueError("Skill batch is not an unreviewed request")
            batch = self.batch(batch_id)
            review = self.decisions(batch_id)
            batch_hash = digest(batch)
            for entry in batch["entries"]:
                if review["entries"][entry["name"]] != "eligible":
                    continue
                if active.get(entry["name"], {}).get("revision") != entry["base"]:
                    raise ValueError("Skill proposal is stale relative to its active predecessor")
                if entry["action"] == "retire":
                    selected.pop(entry["name"], None)
                else:
                    selected[entry["name"]] = {"revision": entry["revision"], "description": entry["description"]}
        elif not defer and not pending and batch_id is None:
            pass
        research = [r for r in rows if r["purpose"] == "research-run"]
        source = ({"run_id": research[-1]["token"], "receipt_sha256": digest(json.loads(research[-1]["receipt"]))}
                  if research and research[-1]["receipt"] else None)
        return {"version": VERSION, "batch": batch_id, "batch_sha256": batch_hash,
                "decision_revision": review["revision"] if review else None,
                "deferred": defer, "source": source, "active": selected, "active_sha256": digest(selected),
                "handled": sorted(handled | ({r["id"] for r in pending} if defer else
                                             {batch_id} if batch_id else set())),
                "request_cutoff": digest(pending)}

    def freeze(self, directory, selection):
        total = 0
        for skill, entry in selection["active"].items():
            record, source = self.revision(entry["revision"])
            total += record["inventory"]["bytes"]
            if total > MAX_BYTES:
                raise ValueError("Active skill library exceeds 64 MiB; retire skills explicitly")
            copy_folder(source, Path(directory) / "skills" / skill, record["inventory"])
            publish_json(Path(directory) / "skill-manifests" / f"{skill}.json", record)
        publish_json(Path(directory) / "skills.json", selection)

    def verify_selection(self, envelope, rows):
        prompt = read_json(Path(envelope.root) / "prompt.json")
        selected = prompt.get("campaign_skills")
        if selected is None:
            return
        current = self.selection(rows, batch_id=selected["batch"], defer=selected["deferred"])
        if selected != current or read_json(Path(envelope.root) / "skills.json") != selected:
            raise ValueError("Skill batch, human decisions or research receipt changed before arming")
        for skill, entry in selected["active"].items():
            record, _ = self.revision(entry["revision"])
            if inventory(Path(envelope.root) / "skills" / skill) != record["inventory"]:
                raise ValueError("Frozen skill bytes changed before arming")


def tools(envelope):
    from copilot import Tool, ToolResult
    root = Path(envelope.root)
    selection = read_json(root / "prompt.json").get("campaign_skills")
    if selection is None:
        return []
    if selection["version"] != VERSION or digest(selection["active"]) != selection["active_sha256"]:
        raise ValueError("Unsupported frozen skill library")

    async def handle(invocation):
        try:
            args = invocation.arguments
            table(args, "skill read", {"name", "path", "offset"})
            offset = integer(args.get("offset", 0), "skill offset", zero=True)
            if "name" not in args:
                entries = sorted(selection["active"].items())
                value = {"entries": {key: {**entry, "description": entry["description"][:512],
                                          "description_truncated": len(entry["description"]) > 512}
                                     for key, entry in entries[offset:offset + 16]},
                         "next_offset": offset + 16 if len(entries) > offset + 16 else None}
                while len(json.dumps(value).encode("utf-8")) > 14000:
                    value["entries"].pop(next(reversed(value["entries"])))
                    value["next_offset"] = offset + len(value["entries"])
            else:
                skill = name(args["name"])
                if skill not in selection["active"]:
                    raise ValueError("Skill is not in this execution's frozen active set")
                record = read_json(safe(root / "skill-manifests", f"{skill}.json"))
                if digest(record) != selection["active"][skill]["revision"]:
                    raise ValueError("Frozen skill manifest changed")
                folder = safe(root / "skills", skill)
                if inventory(folder) != record["inventory"]:
                    raise ValueError("Frozen skill bytes changed")
                relative = args.get("path", "SKILL.md")
                if relative == ".":
                    value = {"files": record["inventory"]["files"][offset:offset + 64],
                             "next_offset": offset + 64 if len(record["inventory"]["files"]) > offset + 64 else None}
                    while len(value["files"]) > 1 and len(json.dumps(value).encode("utf-8")) > 14000:
                        value["files"].pop()
                        value["next_offset"] = offset + len(value["files"])
                else:
                    path = safe(folder, relative)
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        body = stream.read(2048)
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    decoded = decoder.decode(body, final=offset + len(body) >= path.stat().st_size)
                    consumed = len(body) - len(decoder.getstate()[0])
                    value = {"name": skill, "revision": selection["active"][skill]["revision"],
                             "path": relative, "text": decoded,
                             "next_offset": offset + consumed if path.stat().st_size > offset + consumed else None}
            encoded = json.dumps(value)
            if len(encoded.encode("utf-8")) > 16000:
                raise ValueError("Skill response exceeds the bounded tool payload")
            return ToolResult(text_result_for_llm=encoded)
        except (OSError, ValueError, KeyError) as error:
            return ToolResult(result_type="failure", text_result_for_llm=str(error), error=str(error))

    return [Tool("labgoblin_skill", "List this execution's frozen active skills, or read a skill/supporting file. "
                 "Omit name for the paged index; path='.' lists all supporting files. Reading never executes scripts.",
                 handle, {"type": "object", "properties": {"name": {"type": "string"}, "path": {"type": "string"},
                          "offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
                 skip_permission=True, defer="never")]
