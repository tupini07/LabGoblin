"""Chat drafts stay outside research until the human publishes exact captured bytes."""

import json
from pathlib import Path

from labgoblin import ideas
from labgoblin.inspection_evidence import safe_path
from labgoblin.protocol import identifier, table, text
from labgoblin.storage import digest, hash_file, publish_bytes, publish_json, read_json, read_text


class Drafts:
    def __init__(self, evidence, directory, run_id):
        self.evidence, self.directory, self.run_id = evidence, Path(directory), run_id
        self.records = []

    def prepare(self, args):
        table(args, "idea draft", {"title", "message", "sources", "idea_id", "base_revision"})
        heading, message = ideas.title(args.get("title")), text(args.get("message"), "idea message")
        if len(message.encode("utf-8")) > 65536 or len(self.records) >= 8:
            raise ValueError("Prepare at most eight drafts per question, each with at most 64 KiB of text")
        sources = args.get("sources", [])
        if not isinstance(sources, list) or len(sources) > 16:
            raise ValueError("Select at most sixteen existing evidence files")
        inbox = ideas.Inbox(self.evidence.root)
        item_id = ideas.key(identifier() if args.get("idea_id") is None else args["idea_id"])
        base = args.get("base_revision")
        head = inbox.index()["items"].get(item_id)
        if (head["revision"] if head else None) != base:
            raise ValueError("Use the current idea revision for an update; inspect the inbox first")
        selected, origins = [], []
        for source in sources:
            table(source, "draft source", {"scope", "path"})
            root, inventory, context = self.evidence.scope(source.get("scope", "latest"))
            path = safe_path(root, source.get("path"))
            if not path.is_file() or inventory is not None and str(path.relative_to(root)) not in inventory:
                raise ValueError("Select an ordinary captured evidence file")
            selected.append(path)
            origins.append({**context, "path": str(path.relative_to(root)),
                            "sha256": inventory[str(path.relative_to(root))]["sha256"] if inventory is not None else None})
        draft_id = identifier()
        directory = self.directory / "idea-drafts" / draft_id
        publish_bytes(directory / "message.md", message.encode("utf-8"))
        files = ideas.copy_sources(directory, [directory / "message.md", *selected])
        for origin, captured in zip(origins, files[1:]):
            if origin["sha256"] is not None and origin["sha256"] != captured["sha256"]:
                raise ValueError("Draft source differs from its owned execution snapshot")
        value = {"version": ideas.VERSION, "project_id": inbox.project_id, "run_id": self.run_id,
                 "id": draft_id, "idea_id": item_id, "base_revision": base, "title": heading,
                 "files": files, "sources": origins}
        publish_json(directory / "draft.json", value)
        record = {"id": draft_id, "sha256": digest(value), "title": heading}
        self.records.append(record)
        return {**record, "status": "draft_only",
                "instruction": "The human must inspect and click Publish idea in this chat. Nothing was sent to the researcher."}

    def tools(self):
        from copilot import Tool, ToolResult

        async def handle(invocation):
            try:
                return ToolResult(result_type="success", text_result_for_llm=json.dumps(self.prepare(invocation.arguments)))
            except (OSError, ValueError, KeyError, TypeError) as error:
                message = f"{type(error).__name__}: {error}"
                return ToolResult(result_type="failure", error=message, text_result_for_llm=message)

        return [Tool("labgoblin_prepare_idea",
                     "Prepare an attributed idea draft for human review. Never publishes, edits research, or changes limits. "
                     "Attach only existing files inside curated evidence scopes; larger documents stay in files.",
                     handle, {"type": "object", "additionalProperties": False, "required": ["title", "message"],
                              "properties": {
                                  "title": {"type": "string"}, "message": {"type": "string"},
                                  "idea_id": {"type": "string"}, "base_revision": {"type": "string"},
                                  "sources": {"type": "array", "maxItems": 16, "items": {
                                      "type": "object", "additionalProperties": False, "required": ["path"],
                                      "properties": {"scope": {"type": "string"}, "path": {"type": "string"}}}}}},
                     skip_permission=True, defer="never")]


def retained(directory, project_id, run_id, expected):
    draft_id = ideas.revision(expected["id"])
    directory = Path(directory) / "idea-drafts" / draft_id
    if directory.resolve(strict=True) != directory:
        raise ValueError("Idea draft storage cannot be an alias")
    value = read_json(directory / "draft.json")
    if (digest(value) != expected["sha256"] or value.get("project_id") != project_id
            or value.get("run_id") != run_id or value.get("id") != draft_id or value.get("version") != ideas.VERSION):
        raise ValueError("Idea draft differs from its owned inspection receipt")
    for item in value["files"]:
        path = directory / item["path"]
        if (path.resolve(strict=True) != path or not path.is_relative_to(directory)
                or path.stat().st_size != item["bytes"] or hash_file(path) != item["sha256"]):
            raise ValueError("Captured idea draft bytes changed; ask for a new draft")
    return directory, {**value, "sha256": expected["sha256"], "message": read_text(directory / "idea.md")}
