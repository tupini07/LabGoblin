"""Reviewed, conflict-checked creation of a fresh campaign."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import tomli_w

from labgoblin.config import initial_config, parse_config
from labgoblin.evidence import atomic_bytes, contained, read_bytes, read_json
from labgoblin.paths import present, project_paths
from labgoblin.processes import CampaignLease, own_handle, unlink_file
from labgoblin.protocol import canonical, fingerprint, identifier, strings, table, text
from labgoblin.state import State


SECTION_START = "<!-- labgoblin-research:start -->"
SECTION_END = "<!-- labgoblin-research:end -->"
INSTRUCTIONS = f"""{SECTION_START}
## Autonomous local research with LabGoblin

Read the controller's versioned packet before acting. It contains the goal,
operator constraints, governing rationale, event cutoff and owned result format.
Use `labgoblin status --json`, `labgoblin budget --json`, and exact `labgoblin evidence`
or `labgoblin journal entry` retrieval. Previews and searches have bounded coverage.

Submit heavy work with `labgoblin submit --spec work.json --json`. A manifest uses
a stable idempotency `key`, `argv` array, explicit `source_files`, optional runner
and hypothesis ID/statement, CPU/RAM/GPU request, finite `seconds`, and relative
`artifacts`. Write outputs to LABGOBLIN_OUTPUT_DIR; metrics.json is a finite numeric
JSON object. Access declared inputs through LABGOBLIN_INPUT_NAME. Do not run heavy
work outside the queue or wait for it while holding a reasoning grant.

Write exactly one JSON handoff to the supplied result path. Explain observations,
what changed and why, the governing next step, evidence dispositions, and a
continue/wait/blocked/finalize decision. Finalize names the goal stopping criterion
and limitations. A report, milestone, empty queue or successful process is not
automatically successful research. The journal is projected from owned handoffs:
do not write an independent checkpoint or overwrite research authority.

Do not start providers, subagents or controllers, self-expand the goal, modify
shared environments or inputs, install into shared environments, push code,
create GitHub issues/PRs, upload data, or use remote compute. Report/compact are
fixed safe-point maintenance requests, not independently launched providers.
Trusted local execution is not filesystem isolation. Effective provider defaults
may be unknown; configured model and effort are separate from confirmed values.
{SECTION_END}
"""
DEFAULT_GOAL = "# Research goal\n\nDefine the objective, evaluation, evidence, constraints and stopping criteria.\n"


def instruction_text(content: str) -> str:
    start, end = SECTION_START, SECTION_END
    if start in content or end in content:
        if (content.count(start) != 1 or content.count(end) != 1
                or content.index(end) < content.index(start)):
            raise ValueError("Instruction markers are ambiguous; preserve and repair the document explicitly")
        before, owned = content.split(start, 1)
        _, after = owned.split(end, 1)
        return before + INSTRUCTIONS.strip() + after
    return content + ("\n\n" if content else "") + INSTRUCTIONS


def initialization_marker(root: Path) -> Path:
    return root / ".labgoblin.initializing"


def preflight(root: Path, *, existing_config=False):
    paths = project_paths(root)
    if initialization_marker(root).exists():
        raise ValueError(f"Incomplete or concurrent initialization: {initialization_marker(root)}. "
                         "Inspect its exact owned files and error before retrying; do not reset research state.")
    if present(paths.config) and not existing_config:
        raise FileExistsError("Configuration already exists; init only creates a fresh local campaign")
    if existing_config and not paths.config.is_file():
        raise FileNotFoundError("--existing-config requires an existing schema-3 labgoblin.toml")
    if paths.state.exists() and any(paths.state.iterdir()):
        raise FileExistsError("Campaign state is not empty; old formats are not migrated")
    if root.resolve() != root or paths.state.resolve() != paths.state:
        raise ValueError("Initialization destination changed through a symlink/junction")


@dataclass(frozen=True)
class InitDraft:
    configuration: dict
    goal: str | None = None
    protocol: str = ""
    constraints: tuple[str, ...] = ()
    assisted: bool = False

    @classmethod
    def parse(cls, value: dict, root: Path) -> "InitDraft":
        value = table(value, "initialization draft", {"configuration", "goal", "protocol", "constraints"})
        raw = table(value.get("configuration"), "configuration")
        parse_config(raw, project_paths(root).config)
        if len(canonical(raw)) > 65536:
            raise ValueError("Configuration exceeds 64 KiB")
        goal = text(value.get("goal"), "research goal")
        protocol = text(value.get("protocol", ""), "evaluation protocol", empty=True)
        constraints = strings(value.get("constraints", []), "operator constraints", empty=True)
        for name, body, limit in (("goal", goal, 1024 * 1024), ("protocol", protocol, 1024 * 1024),
                                  *(("constraint", item, 65536) for item in constraints)):
            if len(body.encode("utf-8")) > limit:
                raise ValueError(f"Draft {name} exceeds its {limit}-byte allowance")
        return cls(json.loads(canonical(raw)), goal, protocol, tuple(constraints), True)


@dataclass(frozen=True)
class FileChange:
    path: Path
    before: bytes | None
    after: bytes
    identity: tuple[int, int] | None


@dataclass(frozen=True)
class PreparedInit:
    root: Path
    ledger: Path
    draft: InitDraft
    changes: tuple[FileChange, ...]
    existing_config: bool
    warnings: tuple[str, ...]
    digest: str


def prepare(root: Path, ledger: Path, draft: InitDraft, *, existing_config=False,
            install_copilot_instructions=False) -> PreparedInit:
    root = root.resolve()
    preflight(root, existing_config=existing_config)
    config = parse_config(draft.configuration, project_paths(root).config)
    for name, body, limit in (("protocol", draft.protocol, 1024 * 1024),
                             ("goal", draft.goal or DEFAULT_GOAL, 1024 * 1024),
                             *(("constraint", item, 65536) for item in draft.constraints)):
        if len(text(body, name, empty=True).encode("utf-8")) > limit:
            raise ValueError(f"Draft {name} exceeds its {limit}-byte allowance")
    if len(canonical(draft.constraints)) > 1024 * 1024 - 512:
        raise ValueError("Combined constraints exceed the setup-approval source allowance")
    changes = []

    def add(relative, body=None, transform=None, *, limit=1024 * 1024):
        path = contained(root, relative)
        before = read_bytes(path, limit) if present(path) else None
        stat = path.stat() if before is not None else None
        after = transform((before or b"").decode("utf-8")).encode("utf-8") if transform else body
        changes.append(FileChange(path, before, after if after is not None else
                                  (before if before is not None else DEFAULT_GOAL.encode()),
                                  (stat.st_dev, stat.st_ino) if stat else None))

    add("labgoblin.toml", None if existing_config else tomli_w.dumps(draft.configuration).encode())
    if existing_config:
        import tomllib
        if parse_config(tomllib.loads(changes[-1].after.decode()), config.config_path).revision != config.revision:
            raise ValueError("Existing configuration changed during initialization preparation")
    add(config.project.research_goal, draft.goal.encode("utf-8") if draft.goal is not None else None)
    add("CLAUDE.md", transform=instruction_text, limit=256 * 1024)
    warnings = []
    if install_copilot_instructions:
        add(Path(".github") / "copilot-instructions.md", transform=instruction_text, limit=256 * 1024)
    elif (root / ".github" / "copilot-instructions.md").exists():
        warnings.append("Existing .github/copilot-instructions.md may take precedence over CLAUDE.md; "
                        "install the owned section explicitly with labgoblin instructions --target copilot.")

    def ignore(content):
        missing = [name for name in (".labgoblin/", ".labgoblin.lock", ".labgoblin.initializing",
                                    ".labgoblin-archives/") if name not in content.splitlines()]
        return content + ("\n# LabGoblin owned runtime state\n" + "\n".join(missing) + "\n" if missing else "")

    add(".gitignore", transform=ignore, limit=256 * 1024)
    if len({change.path for change in changes}) != len(changes):
        raise ValueError("Research goal conflicts with another initialization output")
    for change in changes:
        if (change.path.is_relative_to(config.state_dir) or change.path == initialization_marker(root)
                or change.path == config.state_dir.with_name(config.state_dir.name + ".lock")):
            raise ValueError("An initialization document cannot occupy runtime state")
    digest = fingerprint({"draft": {"configuration": draft.configuration, "protocol": draft.protocol,
                                    "constraints": draft.constraints, "assisted": draft.assisted},
                          "ledger": str(ledger.resolve()),
                          "files": [{"path": str(c.path), "before": hashlib.sha256(c.before).hexdigest()
                                     if c.before is not None else None, "after": hashlib.sha256(c.after).hexdigest(),
                                     "identity": c.identity}
                                    for c in changes]})
    return PreparedInit(root, ledger.resolve(), draft, tuple(changes), existing_config, tuple(warnings), digest)


def basic(root, ledger, *, provider="claude", existing_config=False, install_copilot_instructions=False):
    preflight(root, existing_config=existing_config)
    if existing_config:
        import tomllib
        raw = tomllib.loads(read_bytes(project_paths(root).config, 65536).decode("utf-8"))
    else:
        raw = initial_config(root.name, provider)
    return prepare(root, ledger, InitDraft(raw), existing_config=existing_config,
                   install_copilot_instructions=install_copilot_instructions)


def apply(prepared: PreparedInit) -> dict:
    root, draft = prepared.root, prepared.draft
    preflight(root, existing_config=prepared.existing_config)
    verified = prepare(root, prepared.ledger, draft, existing_config=prepared.existing_config,
                       install_copilot_instructions=any(c.path.name == "copilot-instructions.md"
                                                        for c in prepared.changes))
    if verified.digest != prepared.digest:
        raise ValueError("Approved proposal or files changed; review initialization again")
    config = parse_config(draft.configuration, project_paths(root).config)
    root.mkdir(parents=True, exist_ok=True)
    marker = initialization_marker(root)
    token = identifier()
    record = {"owner": own_handle(token), "approval": prepared.digest, "phase": "publishing",
              "files": [{"path": str(c.path), "before": hashlib.sha256(c.before).hexdigest()
                         if c.before is not None else None, "after": hashlib.sha256(c.after).hexdigest()}
                        for c in prepared.changes], "state": str(config.state_dir)}
    with marker.open("xb") as stream:
        stream.write(canonical(record))
        stream.flush()
        os.fsync(stream.fileno())
    written, created_dirs = [], []
    state_created = False
    lock = config.state_dir.with_name(config.state_dir.name + ".lock")
    had_lock, had_state = lock.exists(), config.state_dir.exists()
    try:
        for change in prepared.changes:
            if contained(root, change.path) != change.path:
                raise ValueError("An approved destination changed through a symlink/junction")
            current = read_bytes(change.path, 1024 * 1024) if present(change.path) else None
            stat = change.path.stat() if current is not None else None
            if current != change.before or ((stat.st_dev, stat.st_ino) if stat else None) != change.identity:
                raise ValueError(f"Approved file changed; review again: {change.path}")
        for change in prepared.changes:
            if change.before == change.after:
                continue
            missing = []
            parent = change.path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_dirs.append(directory)
            written.append(change)
            if change.before is None:
                with change.path.open("xb") as stream:
                    stream.write(change.after)
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                if read_bytes(change.path, 1024 * 1024) != change.before:
                    raise ValueError(f"Approved file changed; review again: {change.path}")
                atomic_bytes(change.path, change.after)
        state_created = True
        state = State.create(config, prepared.ledger, initialization_token=token)
        goal = next(c.after for c in prepared.changes if c.path == contained(root, config.project.research_goal))
        origin = "setup-assistant" if draft.assisted else "initialization"
        metadata = {"approval": prepared.digest, "operator_approved": draft.assisted}
        state.source("goal", goal, origin=origin, head="goal",
                     metadata={**metadata, "observed_file_digest": hashlib.sha256(goal).hexdigest()}, notify=True)
        if draft.protocol:
            state.source("protocol", draft.protocol.encode(), origin=origin, head="protocol",
                         metadata=metadata, notify=True)
        for constraint in draft.constraints:
            state.directive(constraint)
        if draft.assisted:
            state.source("setup_approval", canonical({"digest": prepared.digest, "constraints": draft.constraints}),
                         origin="operator-approved-setup")
        for change in written:
            if change.before is not None and change.path.name in ("CLAUDE.md", "copilot-instructions.md"):
                state.source("instruction_backup", change.before, origin="initialization",
                             metadata={"path": str(change.path)})
        unlink_file(marker)
        return {"campaign_id": state.id, "project": str(root), "schema_version": 3, "mode": "trusted",
                "assisted": draft.assisted, "approval": prepared.digest, "warnings": list(prepared.warnings),
                "starter_limits": draft.configuration["campaign"],
                "next": "Review readiness with labgoblin doctor --provider --json; configure compatible machine "
                        "capacity explicitly before labgoblin run. Research has not started."}
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyboardInterrupt) as error:
        try:
            if state_created and config.state_dir.exists():
                with CampaignLease(config.state_dir, exclusive=True):
                    files = list(config.state_dir.iterdir())
                    if any(p.name not in ("labgoblin.db", "labgoblin.db-wal", "labgoblin.db-shm") for p in files):
                        raise ValueError("Unexpected state files prevent safe initialization rollback")
                    if files:
                        identity = read_json(marker).get("database_identity")
                        database = config.state_dir / "labgoblin.db"
                        stat = database.stat()
                        if identity != [stat.st_dev, stat.st_ino]:
                            raise ValueError("Database creation ownership is unproven; retaining concurrent state")
                    for path in files:
                        unlink_file(path)
                if not had_state:
                    config.state_dir.rmdir()
                if not had_lock:
                    unlink_file(lock, missing_ok=True)
            for change in reversed(written):
                current = read_bytes(change.path, 1024 * 1024) if change.path.is_file() else None
                if current == change.before:
                    continue
                if current != change.after:
                    raise ValueError(f"Concurrent edit prevents safe rollback: {change.path}")
                if change.before is None:
                    unlink_file(change.path)
                else:
                    atomic_bytes(change.path, change.before)
            for directory in reversed(created_dirs):
                directory.rmdir()
            unlink_file(marker)
        except (OSError, ValueError, RuntimeError) as cleanup_error:
            record = read_json(marker)
            record.update(phase="incomplete", error=str(error), rollback_error=str(cleanup_error))
            atomic_bytes(marker, canonical(record))
            raise RuntimeError(f"Initialization failed: {error}; rollback incomplete: {cleanup_error}. "
                               f"Inspect {marker}; this campaign is not ready.") from error
        raise
