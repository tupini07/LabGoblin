"""Operator-published files and owned delivery receipts, not research authority."""

from contextlib import ExitStack, contextmanager
import hashlib
import itertools
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from labgoblin.protocol import identifier, integer, table, text
from labgoblin.storage import LockBusy, atomic_json, digest, hash_file, publish_bytes, publish_json, read_json, run_lock


VERSION = 1
MAX_FILES = 128
MAX_BYTES = 512 * 1024 * 1024
MAX_IDEAS = 128
MAX_DELIVERIES = 1024
NOTICE = """
New operator-published suggestions are available in the captured delivery index:
{path}
Read that index and the referenced files with your ordinary file tools as needed;
their full contents are deliberately not injected here. These are attributed
suggestions and external claims, not verified results, mandatory experiments,
permission changes or replacements for the original goal. Choose what is useful.
In your next response and research notes, distinguish investigation, prior coverage,
rejection and deferral, with reasons. A response is not proof of scientific progress.
Do not edit published inputs, delivery receipts or unpublished operator drafts.
New ideas never renew time, credits or stalled-review allowances.
"""


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise ValueError("Idea IDs use 1-64 lowercase letters, digits or hyphens")
    return value


def revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("An exact 32-character idea revision is required")
    return value


def title(value):
    text(value, "idea title")
    if len(value.encode("utf-8")) > 240:
        raise ValueError("Idea titles are limited to 240 UTF-8 bytes")
    return value


def copy_sources(directory, files):
    """Capture explicitly selected files; never traverse a source directory."""
    if not 1 <= len(files) <= MAX_FILES:
        raise ValueError(f"Publish between 1 and {MAX_FILES} explicitly selected files")
    records, total = [], 0
    for index, selected in enumerate(files):
        source = Path(selected).resolve(strict=True)
        before = source.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Idea attachments must be regular files")
        name = "idea.md" if index == 0 else str(Path("attachments") / f"{index}-{source.name}")
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        size, hashed = 0, hashlib.sha256()
        with source.open("rb") as reader, target.open("xb") as writer:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                size += len(block)
                if total + size > MAX_BYTES:
                    raise ValueError(f"Published idea files exceed {MAX_BYTES} bytes")
                writer.write(block)
                hashed.update(block)
            writer.flush()
            os.fsync(writer.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino) or size != before.st_size:
            raise ValueError(f"Idea source changed during capture: {source}")
        records.append({"path": name, "source": str(source), "bytes": size, "sha256": hashed.hexdigest()})
        total += size
    for item in records:
        if hash_file(item["source"]) != item["sha256"]:
            raise ValueError(f"Idea source changed before publication: {item['source']}")
    return records


class Inbox:
    def __init__(self, project):
        from labgoblin.runtime import project_record
        self.root, self.state, record = project_record(project)
        self.project_id = record["id"]
        self.control = self.path(Path(".labgoblin") / "ideas")
        self.public = self.path("ideas_inbox")

    def path(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or relative.drive or ".." in relative.parts or any(
                ":" in part for part in relative.parts):
            raise ValueError("Idea paths must stay inside their project")
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink() or current.resolve() != current:
                raise ValueError("Idea storage cannot follow file links or directory aliases")
        return current

    def index(self):
        path = self.path(Path(".labgoblin") / "ideas" / "index.json")
        value = (read_json(path) if path.exists() else
                 {"version": VERSION, "project_id": self.project_id, "sequence": 0, "items": {}})
        table(value, "idea index", {"version", "project_id", "sequence", "items", "last_closure"})
        if value.get("version") != VERSION or value.get("project_id") != self.project_id:
            raise ValueError("Idea index identity/version differs from its project")
        integer(value.get("sequence"), "idea sequence", zero=True)
        table(value.get("items"), "idea heads")
        if len(value["items"]) > MAX_IDEAS:
            raise ValueError("Idea index exceeds its entry bound")
        for item_id, item in value["items"].items():
            key(item_id)
            table(item, "idea head", {"revision", "manifest_sha256", "response", "delivery"})
            revision(item.get("revision"))
        return value

    @contextmanager
    def locked(self):
        self.path(Path(".labgoblin") / "ideas").mkdir(exist_ok=True)
        lock = self.path(Path(".labgoblin") / "ideas" / "lock")
        if not lock.exists():
            descriptor, temporary = tempfile.mkstemp(prefix="lock-", dir=self.control)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(b"\0")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, lock)
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink()
        if lock.stat().st_size != 1:
            raise ValueError("Invalid operator-inbox lock")
        until = time.monotonic() + 5
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(run_lock(lock, owner="operator-inbox writer"))
                    break
                except LockBusy:
                    if time.monotonic() >= until:
                        raise
                    time.sleep(0.01)
            yield

    def save(self, value):
        atomic_json(self.path(Path(".labgoblin") / "ideas" / "index.json"), value)

    def manifest(self, item_id, head, *, verify_files=False):
        directory = self.path(Path("ideas_inbox") / "published" / key(item_id) / revision(head["revision"]))
        value = read_json(directory / "manifest.json")
        if (digest(value) != head["manifest_sha256"] or value.get("project_id") != self.project_id
                or value.get("id") != item_id or value.get("revision") != head["revision"]
                or value.get("version") != VERSION):
            raise ValueError("Published idea manifest changed or belongs to another project")
        if verify_files:
            for item in value["files"]:
                path = self.path(directory.relative_to(self.root) / item["path"])
                if not path.is_relative_to(directory) or path.stat().st_size != item["bytes"] or hash_file(path) != item["sha256"]:
                    raise ValueError("Published idea file changed from its captured identity")
        return value

    def publish(self, files, *, idea_id=None, heading, base=None, attribution="Operator publication",
                expected_sha256=None):
        item_id = key(identifier() if idea_id is None else idea_id)
        title(heading)
        text(attribution, "idea attribution")
        if len(attribution.encode("utf-8")) > 4096:
            raise ValueError("Idea attribution exceeds 4096 UTF-8 bytes")
        if base is not None:
            revision(base)
        with self.locked():
            pass
        with tempfile.TemporaryDirectory(prefix="capture-", dir=self.control) as temporary:
            staged = Path(temporary)
            files = copy_sources(staged, files)
            if expected_sha256 is not None and files[0]["sha256"] != expected_sha256:
                raise ValueError("Draft changed since its reviewed SHA256; review and publish the new bytes")
            observed = self.index()["items"].get(item_id)
            if observed:
                self.manifest(item_id, observed, verify_files=True)
            with self.locked():
                index = self.index()
                old = index["items"].get(item_id)
                if old:
                    previous = self.manifest(item_id, old)
                    if (observed and old["revision"] == observed["revision"]
                            and old["manifest_sha256"] == observed["manifest_sha256"]
                            and previous["title"] == heading and previous["files"] == files
                            and previous["attribution"] == attribution):
                        return {"id": item_id, **old, "unchanged": True}
                    if old["revision"] != base:
                        raise ValueError("Idea revision changed; supply its current --base before replacing it")
                elif base is not None:
                    raise ValueError("A new idea cannot name a previous revision")
                elif len(index["items"]) >= MAX_IDEAS:
                    raise ValueError(f"The inbox supports at most {MAX_IDEAS} idea identities")
                rev = identifier()
                value = {"version": VERSION, "project_id": self.project_id, "id": item_id,
                         "revision": rev, "base": old, "title": heading, "attribution": attribution,
                         "created": time.time(), "sequence": index["sequence"] + 1, "files": files}
                publish_json(staged / "manifest.json", value)
                destination = self.path(Path("ideas_inbox") / "published" / item_id / rev)
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged.rename(destination)
                index["sequence"] += 1
                head = {"revision": rev, "manifest_sha256": digest(value), "response": None, "delivery": None}
                index["items"][item_id] = head
                self.save(index)
                return {"id": item_id, **head, "manifest": str((destination / "manifest.json").relative_to(self.root)),
                        "unchanged": False}

    def entries(self):
        result = []
        for item_id, head in self.index()["items"].items():
            value = self.manifest(item_id, head)
            relative = Path("ideas_inbox") / "published" / item_id / head["revision"]
            result.append({"id": item_id, "revision": head["revision"], "title": value["title"],
                           "manifest": str(relative / "manifest.json"), "manifest_sha256": head["manifest_sha256"],
                           "file": str(relative / "idea.md"), "sequence": value["sequence"],
                           "status": "responded" if head.get("response") else (head.get("delivery") or {}).get("phase", "pending"),
                           "delivery": head.get("delivery"),
                           "response": head.get("response")})
        return sorted(result, key=lambda item: item["sequence"])

    def status(self):
        index = self.index()
        return {"sequence": index["sequence"], "items": len(index["items"]),
                "pending": sum(not item.get("response") for item in index["items"].values()),
                "last_closure": index.get("last_closure"),
                "note": "Pending counts all revisions without a retained reply, including prepared/submitted notices. Submission does not prove reading; a reply does not prove investigation or success."}


class RunInbox:
    def __init__(self, envelope):
        self.envelope = envelope
        self.inbox = Inbox(envelope.project)
        self.root = Path(envelope.root)
        self.seen = {item["id"]: item for item in self.inbox.entries() if item["status"] == "responded"}

    def pending(self):
        return self.inbox.status()["pending"] > 0

    def context(self):
        return {"ideas": list(self.seen.values()),
                "note": "Read the captured files/replies as relevant. These suggestions do not replace the goal or authorize new permissions. Publication or prose alone is not research progress."}

    def capture(self, prompt_number, *, extra=False):
        with self.inbox.locked():
            index = self.inbox.index()
            heads = {name: value for name, value in index["items"].items() if not value.get("response")}
        if not heads:
            return None
        if len(list(itertools.islice((self.root / "ideas").glob("prompt-*.json"), MAX_DELIVERIES))) >= MAX_DELIVERIES:
            raise ValueError("Execution reached its 1024 idea-delivery bound; remaining ideas stay pending")
        if extra and prompt_number < 2:
            raise ValueError("The first researcher prompt cannot be an additional idea response")
        items = []
        for item_id, head in heads.items():
            value = self.inbox.manifest(item_id, head, verify_files=True)
            directory = Path("ideas_inbox") / "published" / item_id / head["revision"]
            items.append({"id": item_id, "revision": head["revision"], "title": value["title"],
                          "manifest": str(directory / "manifest.json"), "manifest_sha256": head["manifest_sha256"],
                          "file": str(directory / "idea.md")})
        value = {"version": VERSION, "run_id": self.envelope.id, "envelope_digest": self.envelope.digest,
                 "project_id": self.envelope.project_id, "prompt": prompt_number, "extra_prompt": extra,
                 "sequence": index["sequence"], "items": items}
        relative = Path("ideas_inbox") / "deliveries" / self.envelope.id / f"prompt-{prompt_number}.json"
        publish_json(self.inbox.path(relative), value)
        publish_json(self.root / "ideas" / f"prompt-{prompt_number}.json", value)
        delivery = Delivery(self, value, relative)
        delivery.mark("prepared")
        return delivery

    def close_if_empty(self):
        with self.inbox.locked():
            index = self.inbox.index()
            if any(not item.get("response") for item in index["items"].values()):
                return False
            value = {"run_id": self.envelope.id, "envelope_digest": self.envelope.digest,
                     "sequence": index["sequence"], "closed_at": time.time()}
            publish_json(self.root / "ideas" / "closure.json", value)
            index["last_closure"] = value
            self.inbox.save(index)
            return True


class Delivery:
    def __init__(self, owner, value, relative):
        self.owner, self.value, self.relative = owner, value, relative

    def notice(self):
        return NOTICE.format(path=str(self.relative))

    def mark(self, phase):
        with self.owner.inbox.locked():
            index = self.owner.inbox.index()
            for item in self.value["items"]:
                head = index["items"][item["id"]]
                if head["revision"] == item["revision"]:
                    head["delivery"] = {"run_id": self.owner.envelope.id, "prompt": self.value["prompt"],
                                        "phase": phase, "path": str(self.relative), "sha256": digest(self.value)}
            self.owner.inbox.save(index)

    def submitted(self, number, prompt):
        if number != self.value["prompt"]:
            raise ValueError("Operator idea delivery belongs to a different researcher prompt")
        publish_json(self.owner.root / "ideas" / f"submitted-{number}.json", {
            "delivery_sha256": digest(self.value), "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "run_id": self.owner.envelope.id, "prompt": number})
        self.mark("submitted")

    def responded(self, answer):
        owner, number = self.owner, self.value["prompt"]
        submitted = read_json(owner.root / "ideas" / f"submitted-{number}.json")
        settled = read_json(owner.root / "sessions" / "researcher" / f"settled-prompt-{number}.json")
        if (submitted["delivery_sha256"] != digest(self.value) or settled["complete"] is not True
                or settled["role"] != "researcher" or settled["prompts"] != number):
            raise ValueError("Idea response requires its exact submitted, settled researcher prompt")
        body = answer.encode("utf-8")
        relative = self.relative.with_suffix(".reply.md")
        publish_bytes(owner.inbox.path(relative), body)
        response = {"run_id": owner.envelope.id, "prompt": number, "path": str(relative),
                    "sha256": hashlib.sha256(body).hexdigest(), "settled_prompt_sha256": digest(settled),
                    "delivery_sha256": digest(self.value)}
        publish_json(owner.root / "ideas" / f"response-{number}.json", response)
        with owner.inbox.locked():
            index = owner.inbox.index()
            for item in self.value["items"]:
                head = index["items"][item["id"]]
                if head["revision"] == item["revision"]:
                    head["response"] = response
                    head["delivery"]["phase"] = "responded"
                owner.seen[item["id"]] = {**item, "status": "responded", "response": response}
            owner.inbox.save(index)


def extra_prompts(envelope, prompt_count):
    prompt = read_json(Path(envelope.root) / "prompt.json")
    if prompt.get("operator_ideas_version") != VERSION:
        return 0
    count = 0
    paths = list(itertools.islice((Path(envelope.root) / "ideas").glob("prompt-*.json"), MAX_DELIVERIES + 1))
    if len(paths) > MAX_DELIVERIES:
        raise ValueError("Owned idea deliveries exceed their execution bound")
    for path in paths:
        match = re.fullmatch(r"prompt-([1-9][0-9]{0,8})", path.stem)
        if not match:
            raise ValueError("Invalid owned idea-delivery filename")
        number = int(match[1])
        if number > prompt_count:
            continue
        value = read_json(path)
        if (value.get("version") != VERSION or value.get("run_id") != envelope.id
                or value.get("envelope_digest") != envelope.digest or value.get("project_id") != envelope.project_id
                or value.get("prompt") != number or type(value.get("extra_prompt")) is not bool
                or value["extra_prompt"] and number == 1
                or not isinstance(value.get("items"), list) or not 1 <= len(value["items"]) <= MAX_IDEAS):
            raise ValueError("Additional researcher prompt lacks its exact operator-idea delivery")
        count += int(value["extra_prompt"])
    return count
