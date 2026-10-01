"""Local-only command surface with structured, bounded results."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys

from labgoblin import __version__, briefing, journal, reporting, results, workspace
from labgoblin.campaign import Campaign
from labgoblin.config import initial_config, load_config, parse_config
from labgoblin.evidence import Capture, atomic_bytes, contained, observation, publish_bytes, read_bytes, read_json, tail
from labgoblin.evidence import retention_candidates, storage_inventory, watermarks
from labgoblin.protocol import identifier
from labgoblin.paths import environment_value, present, project_paths
from labgoblin.scheduler import ResourceLedger, ledger_path
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


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _instructions(path: Path, state):
    old = read_bytes(path, 256 * 1024) if path.exists() else b""
    content = old.decode("utf-8")
    start, end = SECTION_START, SECTION_END
    if start in content or end in content:
        if (content.count(start) != 1 or content.count(end) != 1
                or content.index(end) < content.index(start)):
            raise ValueError("Instruction markers are ambiguous; preserve and repair the document explicitly")
        before, owned = content.split(start, 1)
        _, after = owned.split(end, 1)
        updated = before + INSTRUCTIONS.strip() + after
    else:
        updated = content + ("\n\n" if content else "") + INSTRUCTIONS
    if updated.encode("utf-8") == old:
        return
    if path.exists():
        backup = state.source("instruction_backup", old, origin="operator-command",
                              metadata={"path": str(path)})
        if read_bytes(path, 256 * 1024) != old:
            raise ValueError(f"Instructions changed concurrently; preserved original source {backup}")
        atomic_bytes(path, updated.encode("utf-8"))
    else:
        publish_bytes(path, updated.encode("utf-8"))


def initialize(args):
    root = Path(args.project).resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = project_paths(root)
    path = paths.config
    if present(path) and not args.existing_config:
        raise FileExistsError("Configuration already exists; init only creates a fresh local campaign")
    if args.existing_config and not path.is_file():
        raise FileNotFoundError("--existing-config requires an existing schema-3 labgoblin.toml")
    state_dir = paths.state
    if state_dir.exists() and any(state_dir.iterdir()):
        raise FileExistsError("Campaign state is not empty; old formats are not migrated")
    if args.existing_config:
        import tomllib
        raw = tomllib.loads(read_bytes(path, 65536).decode("utf-8"))
    else:
        raw = initial_config(root.name, args.agent)
    config = parse_config(raw, path)
    state = State.create(config, args.ledger or ledger_path())
    from labgoblin.processes import CampaignLease
    with CampaignLease(state.root):
        with state.db.read():
            pass
        return _finish_initialization(args, state, config, raw)


def _finish_initialization(args, state, config, raw):
    import tomli_w
    root, path = config.root, Path(config.config_path)
    if not args.existing_config:
        publish_bytes(path, tomli_w.dumps(raw).encode("utf-8"))
    goal = root / config.project.research_goal
    if not goal.exists():
        publish_bytes(goal, b"# Research goal\n\nDefine the objective, evaluation, evidence, constraints and stopping criteria.\n")
    journal.ingest_goal(state, config)
    _instructions(root / "CLAUDE.md", state)
    copilot = root / ".github" / "copilot-instructions.md"
    warnings = []
    if args.install_copilot_instructions:
        _instructions(copilot, state)
    elif copilot.exists():
        warnings.append("Existing .github/copilot-instructions.md may take precedence over CLAUDE.md; "
                        "install the owned section explicitly with labgoblin instructions --target copilot.")
    ignore = root / ".gitignore"
    previous = read_bytes(ignore, 256 * 1024).decode("utf-8") if ignore.exists() else ""
    state_name = state.root.name
    missing = [line for line in (f"{state_name}/", f"{state_name}.lock", f"{state_name}-archives/")
               if line not in previous.splitlines()]
    if missing:
        with ignore.open("a", encoding="utf-8") as stream:
            stream.write("\n# LabGoblin owned runtime state\n" + "\n".join(missing) + "\n")
    return {"campaign_id": state.id, "project": str(root), "schema_version": 3, "mode": "trusted",
            "warnings": warnings, "starter_limits": raw["campaign"],
            "next": "Define the goal and review finite limits; explicitly configure compatible machine capacity before run"}


def doctor(root, args):
    from labgoblin import agent, backends
    checks = []

    def check(name, action):
        try:
            value = action()
            checks.append({"check": name, "ok": True, "result": value})
            return value
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            checks.append({"check": name, "ok": False, "error": f"{type(error).__name__}: {error}"})
            return None

    state = None
    try:
        state = State.open(project_paths(root).state)
        checks.append({"check": "state", "ok": True, "campaign_id": state.id})
    except (OSError, ValueError, sqlite3.Error) as error:
        checks.append({"check": "state", "ok": False, "error": str(error)})
    if state:
        location, identity = state.ledger_identity()
        check("machine", lambda: ResourceLedger(location, expected_id=identity or None).capacity())
    config = check("configuration", lambda: load_config(root))
    if config:
        checks[-1]["result"] = {"revision": config.revision}
        name = args.runner or config.execution.default_runner
        if name not in config.runners:
            checks.append({"check": "runner", "ok": False, "error": f"Unknown runner: {name}"})
        else:
            check("runner:" + name, lambda: backends.validate_runner(asdict(config.runners[name])))
        check("storage", lambda: watermarks(config.storage))
        if args.provider:
            check("provider-help-only", lambda: agent.inspect_provider(config.agent))
    return {"checks": checks, "failed": sum(not item["ok"] for item in checks),
            "inference": "never", "scope": "Selected backend and optionally provider help; no canaries, image pulls or installations"}


def archive_campaign(state, confirm):
    from labgoblin.db import connection
    from labgoblin.processes import CampaignLease, process_state
    if confirm != state.id:
        raise ValueError("reset requires --confirm with the exact campaign ID from status")
    with CampaignLease(state.root, exclusive=True):
        with connection(state.path, lease=False) as conn:
            current = conn.execute("SELECT id,controller FROM campaign").fetchone()
            if current["id"] != confirm or not State._quiescent(conn):
                raise ValueError("Reset requires the same quiescent campaign and fully released allocations")
            if current["controller"] and process_state(json.loads(current["controller"])) != "dead":
                raise ValueError("Reset refuses a live or unknown controller")
            if conn.execute("SELECT 1 FROM attempts WHERE collection='pending' LIMIT 1").fetchone():
                raise ValueError("Reset requires pending collection to finish; use stop and reconcile")
            metadata = dict(conn.execute("SELECT key,value FROM meta"))
        ledger_path = Path(metadata["ledger_path"])
        if ledger_path.exists() or metadata["ledger_id"]:
            ledger = ResourceLedger(ledger_path, expected_id=metadata["ledger_id"] or None)
            with ledger.read() as conn:
                row = conn.execute("""SELECT g.token FROM grants g LEFT JOIN consumer_runs r ON r.token=g.token
                    WHERE g.state IN ('pending','granted') AND
                    (g.owner_id=? OR json_extract(g.owner,'$.campaign_id')=?
                    OR json_extract(r.envelope,'$.state_path')=?) LIMIT 1""",
                    (state.id, state.id, str(state.path))).fetchone()
            if row:
                raise ValueError(f"Reset refuses unreleased machine consumer {row['token']}")
        parent = contained(state.root.parent, state.root.name + "-archives")
        parent.mkdir(exist_ok=True)
        target = parent / ("campaign-" + identifier())
        state.root.rename(target)
        publish_bytes(target.with_name(target.name + ".lock"), b"\0")
    return {"archived_campaign": state.id, "archive": str(target),
            "retained": "Project configuration, goal, source files and shared machine ledger are unchanged",
            "next": "Use init --existing-config with an explicit --ledger to create fresh state; no automatic research restart"}


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS, help="Project root (default: current directory)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit structured JSON")
    root = Parser(prog="labgoblin", description="Local autonomous research for Claude and Copilot")
    root.add_argument("--project", default=environment_value("PROJECT", "."))
    root.add_argument("--json", action="store_true")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    def command(name, help):
        return commands.add_parser(name, help=help, parents=[common])

    init = command("init", "Create fresh schema-3 local campaign state")
    init.add_argument("--agent", choices=("claude", "copilot"), default="claude")
    init.add_argument("--ledger", type=Path)
    init.add_argument("--install-copilot-instructions", action="store_true")
    init.add_argument("--existing-config", action="store_true", help="Initialize fresh state using an existing supported config, without migration")
    instructions = command("instructions", "Explicitly install/update the owned research instructions section")
    instructions.add_argument("--target", choices=("claude", "copilot"), required=True)
    run = command("run", "Recover owned work and continue eligible research")
    run.add_argument("--no-agent", action="store_true")
    run.add_argument("--once", action="store_true")
    for name in ("pause", "resume", "stop", "reopen"):
        control = command(name, f"Request {name} with revision-fenced idempotency")
        control.add_argument("--request-id")
        control.add_argument("--expected-revision", type=int)
    cancel = command("cancel", "Cancel an exact owned attempt")
    cancel.add_argument("--id", required=True)
    machine = command("machine", "Inspect or explicitly configure the shared machine ledger")
    machine.add_argument("action", choices=("status", "configure", "reconcile"))
    machine.add_argument("--ledger", type=Path)
    machine.add_argument("--cpus", type=int)
    machine.add_argument("--memory-mb", type=int)
    machine.add_argument("--headroom-mb", type=int, default=2048)
    machine.add_argument("--gpus", default="")
    doctor_parser = command("doctor", "Run bounded model-free diagnostics, without inference or installation")
    doctor_parser.add_argument("--runner")
    doctor_parser.add_argument("--provider", action="store_true", help="Also inspect selected provider's help, never a model canary")
    validate = command("validate", "Validate configuration and optionally an experiment manifest, without copying or launch")
    validate.add_argument("--spec", type=Path)
    build = command("build", "Explicitly build a local Docker image using admitted resources and retained context")
    build.add_argument("--runner", required=True)
    build.add_argument("--context", type=Path, required=True)
    build.add_argument("--include", action="append", required=True, help="Explicit relative source file; repeat as needed (Dockerfile always included)")
    build.add_argument("--cpus", type=int, default=1, help="Build container CPUs; one additional client CPU is reserved")
    build.add_argument("--memory-mb", type=int, default=1024, help="Build container RAM; 256 MiB client RAM is additionally reserved")
    build.add_argument("--seconds", type=float, default=600, help="Positive build deadline and maximum capacity wait")
    storage = command("storage", "Inspect owned sizes/references or review bounded retention candidates")
    storage.add_argument("action", choices=("inventory", "retention"))
    storage.add_argument("--dry-run", action="store_true")
    storage.add_argument("--limit", type=int, default=25)
    storage.add_argument("--offset", type=int, default=0)
    dashboard = command("dashboard", "Serve the read-only loopback dashboard")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--chat", action=argparse.BooleanOptionalAction, default=None,
                           help="Enable on-demand observer chat (default); --no-chat overrides configuration")
    dashboard.add_argument("--open-browser", action="store_true")
    reset = command("reset", "Archive the exact supported quiescent campaign; never erase a ledger")
    reset.add_argument("--confirm", required=True, help="Exact campaign ID")
    submit = command("submit", "Enqueue one argv-form experiment manifest")
    submit.add_argument("--spec", type=Path, required=True)
    batch = command("batch-submit", "Enqueue all manifests, preserving per-item failures")
    batch.add_argument("--file", type=Path, required=True)
    command("status", "Observe campaign state without reconciliation or inference")
    command("budget", "Observe finite/unlimited admission budgets")
    reconcile = command("reconcile", "Recover recorded launches without loading current TOML")
    reconcile.add_argument("--state-dir", type=Path)
    for name in ("logs", "errors"):
        logs = command(name, "Read bounded owned stream tails")
        logs.add_argument("--id", required=True, help="Attempt or invocation ID")
        logs.add_argument("--stream", choices=("stdout", "stderr"), default="stderr" if name == "errors" else "stdout")
        logs.add_argument("--bytes", type=int, default=64 * 1024)
        logs.add_argument("--stage", choices=("main", "supervisor"), default="main")
    evidence = command("evidence", "Retrieve one exact observation or a bounded event byte page")
    evidence.add_argument("kind", choices=("observation", "event"))
    evidence.add_argument("--id", required=True)
    evidence.add_argument("--offset", type=int, default=0)
    evidence.add_argument("--bytes", type=int, default=64 * 1024)
    history = command("journal", "Page journal sources or retrieve a retained entry")
    history.add_argument("action", choices=("list", "entry", "search"))
    history.add_argument("--id")
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--offset", type=int, default=0)
    history.add_argument("--query")
    history.add_argument("--after", type=int, default=0)
    history.add_argument("--cutoff", type=int)
    steer = command("steer", "Record an attributed, versioned operator constraint")
    steering_text = steer.add_mutually_exclusive_group(required=True)
    steering_text.add_argument("--text")
    steering_text.add_argument("--file", type=Path)
    steer.add_argument("--scope", default="campaign")
    steer.add_argument("--supersedes")
    steer.add_argument("--request-id")
    source = command("source", "Read or commit an exact versioned goal/evaluation protocol")
    source.add_argument("action", choices=("show", "set"))
    source.add_argument("--kind", choices=("goal", "protocol"), required=True)
    source_text = source.add_mutually_exclusive_group()
    source_text.add_argument("--file", type=Path)
    source_text.add_argument("--text")
    source.add_argument("--expected-revision", type=int)
    compact = command("compact", "Queue one scoped safe-point compaction request")
    compact.add_argument("--no-agent", action="store_true", help="Only show archive coverage; do not request inference")
    view = command("view", "Retrieve bounded pages of an exact retained research/report source view")
    view.add_argument("--id", required=True)
    view.add_argument("--offset", type=int, default=0)
    view.add_argument("--limit", type=int, default=25)
    view.add_argument("--attempt", help="Retrieve exact byte-paged member detail instead of inventory rows")
    report = command("report", "Queue an immutable-source report, or publish an inventory without inference")
    report.add_argument("--no-agent", action="store_true")
    report.add_argument("--select", action="append", dest="selected", help="Select an attempt while retaining the full denominator")
    report.add_argument("--selection-reason", default="Complete generation inventory")
    result = command("results", "Page DB-backed results or export an immutable CSV snapshot")
    result.add_argument("--generation", type=int)
    result.add_argument("--hypothesis")
    result.add_argument("--limit", type=int, default=25)
    result.add_argument("--offset", type=int, default=0)
    result.add_argument("--export", type=Path)
    hypothesis = command("hypothesis", "Create/query a scientific claim with immutable evaluated statements")
    hypothesis.add_argument("action", choices=("set", "show"))
    hypothesis.add_argument("--id", required=True)
    hypothesis.add_argument("--statement")
    hypothesis.add_argument("--label", default="")
    hypothesis.add_argument("--supersedes")
    return root


def _record_retrieval(state, reference_id):
    turn_id = environment_value("TURN_ID")
    if turn_id:
        state.retrieved(turn_id, reference_id)


def execute(args):
    root = Path(args.project).resolve()
    if args.command == "init":
        return initialize(args)
    if args.command == "doctor":
        return doctor(root, args)
    paths = project_paths(root)
    if args.command == "validate":
        config = load_config(paths.config)
        return workspace.prepare_spec(config, read_json(args.spec, 65536), validate_only=True) if args.spec else {
            "valid": True, "revision": config.revision, "schema_version": 3, "inference": "never"}
    if args.command == "dashboard":
        from labgoblin.dashboard import run_dashboard
        if not 0 <= args.port <= 65535:
            raise ValueError("Dashboard port must be between 0 and 65535")
        run_dashboard(str(paths.config), args.port, chat=args.chat,
                      open_browser=args.open_browser, json_output=args.json)
        return None
    if args.command == "instructions":
        target = root / ("CLAUDE.md" if args.target == "claude" else ".github")
        if args.target == "copilot":
            target /= "copilot-instructions.md"
        _instructions(target, State.open(paths.state))
        return {"path": str(target), "owned_section": SECTION_START}
    if args.command == "machine":
        project_state = State.open(paths.state) if args.ledger is None and present(paths.state) else None
        recorded, identity = project_state.ledger_identity() if project_state else (ledger_path(), "")
        location = args.ledger or recorded
        if args.action == "configure":
            if args.cpus is None or args.memory_mb is None:
                raise ValueError("machine configure requires --cpus and --memory-mb")
            if identity and not Path(location).exists():
                raise ValueError("The recorded machine ledger is missing; it will not be replaced with empty capacity")
            ledger = ResourceLedger(location, expected_id=identity or None) if Path(location).exists() else ResourceLedger.create(location)
            ledger.configure(args.cpus, args.memory_mb, tuple(filter(None, args.gpus.split(","))), args.headroom_mb)
        else:
            ledger = ResourceLedger(location, expected_id=identity or None)
        recovery = ledger.recover_consumers() if args.action == "reconcile" else []
        return {"ledger_id": ledger.id, "path": str(ledger.path), "capacity": ledger.capacity(), "grants": ledger.rows(),
                "consumer_recovery": recovery, "errors": [row for row in recovery if row.get("error")]}
    state = State.open(args.state_dir if args.command == "reconcile" and args.state_dir else paths.state)
    if args.command == "build":
        if environment_value("TURN_ID"):
            raise ValueError("Image builds are explicit operator operations, not nested research-turn work")
        from labgoblin.backends import build
        return build(state, load_config(paths.config), args.runner, args.context, args.include,
                     cpus=args.cpus, memory_mb=args.memory_mb, timeout=args.seconds)
    if args.command == "storage":
        if args.action == "retention":
            if not args.dry_run:
                raise ValueError("Retention is dry-run only; pass --dry-run. No delete/apply mode exists.")
            return retention_candidates(state, limit=args.limit, offset=args.offset)
        return storage_inventory(state, limit=args.limit, offset=args.offset)
    if args.command == "reset":
        if environment_value("TURN_ID"):
            raise ValueError("A research turn cannot archive its own campaign")
        return archive_campaign(state, args.confirm)
    if args.command == "run":
        return Campaign(state=state).run(no_agent=args.no_agent, once=args.once)
    if args.command in ("pause", "resume", "stop", "reopen"):
        revision = state.campaign()["revision"] if args.expected_revision is None else args.expected_revision
        value = state.control(args.command, args.request_id or identifier(), revision)
        recovery = Campaign(state=state).reconcile()
        return {**value, "campaign": state.campaign(), "recovery": recovery, "errors": recovery["errors"]}
    if args.command == "cancel":
        value = state.cancel_attempt(args.id)
        recovery = Campaign(state=state).reconcile()
        return {**value, "attempt": state.attempt(args.id), "recovery": recovery, "errors": recovery["errors"]}
    if args.command == "status":
        return {"campaign": state.campaign(), "budget": state.budget(), "results": results.page(state.db)}
    if args.command == "budget":
        return state.budget()
    if args.command == "steer":
        if environment_value("TURN_ID"):
            raise ValueError("A research turn cannot author or revoke operator constraints")
        body = read_bytes(args.file, 65536).decode("utf-8") if args.file else args.text
        return state.directive(body, scope=args.scope, supersedes=args.supersedes, request_id=args.request_id)
    if args.command == "source":
        with state.db.read() as conn:
            head = conn.execute("SELECT source_id,revision FROM source_heads WHERE name=?", (args.kind,)).fetchone()
        if args.action == "show":
            if not head:
                raise ValueError("No retained source revision exists for this kind")
            _record_retrieval(state, head["source_id"])
            return {**journal.entry(state.db, head["source_id"]), "head_revision": head["revision"]}
        if environment_value("TURN_ID"):
            raise ValueError("Research turns cannot replace the operator goal or evaluation protocol")
        if args.file is None and args.text is None:
            raise ValueError("Source set requires --file or --text")
        body = read_bytes(args.file, 1024 * 1024) if args.file else args.text.encode("utf-8")
        body.decode("utf-8")
        metadata = {}
        if args.kind == "goal":
            config = load_config(state.root.parent)
            path = contained(config.root, config.project.research_goal)
            metadata["observed_file_digest"] = Capture.read(path, 1024 * 1024).digest
        source_id = state.source(args.kind, body, origin="operator-command", head=args.kind,
                                 expected_revision=args.expected_revision if args.expected_revision is not None else
                                 (head["revision"] if head else 0), metadata=metadata, notify=True)
        return journal.entry(state.db, source_id)
    if args.command == "compact":
        if args.no_agent:
            return {**journal.page(state.db), "inference": "not_requested",
                    "reason": "Exact archive access remains available without a summary invocation"}
        if environment_value("TURN_ID"):
            raise ValueError("Request researcher maintenance in the owned handoff, not as an operator command")
        return state.request_maintenance("compact")
    if args.command == "report":
        if environment_value("TURN_ID"):
            raise ValueError("Request report maintenance in the owned handoff")
        options = reporting.selection_options(args.selected, args.selection_reason)
        if args.no_agent:
            view = reporting.seal_view(state, **options)
            return reporting.publish_report(state, view["id"])
        return state.request_maintenance("report", options=options)
    if args.command == "view":
        if args.attempt:
            return reporting.member(state.db, args.id, args.attempt, offset=args.offset)
        value = reporting.page(state.db, args.id, offset=args.offset, limit=args.limit)
        turn_id = environment_value("TURN_ID")
        if turn_id:
            state.retrieved_view(turn_id, value)
        return value
    if args.command == "reconcile":
        return {**Campaign(state=state).reconcile(), "campaign": state.campaign()}
    if args.command in ("submit", "batch-submit"):
        config = load_config(paths.config)
        if args.command == "submit":
            attempt = workspace.submit(state, config, read_json(args.spec, 65536),
                                       turn_id=environment_value("TURN_ID"))
            return {"attempt_id": attempt["id"], "status": attempt["status"]}
        data = read_json(args.file)
        manifests = data.get("experiments") if isinstance(data, dict) else data
        if not isinstance(manifests, list) or not manifests:
            raise ValueError("Batch must contain a nonempty experiments array")
        items = []
        for manifest in manifests:
            try:
                attempt = workspace.submit(state, config, manifest, turn_id=environment_value("TURN_ID"))
                items.append({"ok": True, "attempt_id": attempt["id"], "status": attempt["status"]})
            except (OSError, ValueError, sqlite3.Error) as error:
                items.append({"ok": False, "error": str(error)[:4000]})
        return {"items": items, "failed": sum(not item["ok"] for item in items)}
    if args.command in ("logs", "errors"):
        if args.bytes > 64 * 1024:
            raise ValueError("Log responses cannot exceed 64 KiB")
        with state.db.read() as conn:
            row = conn.execute("SELECT envelope FROM launches WHERE work_id=? ORDER BY created DESC LIMIT 1",
                               (args.id,)).fetchone()
        if row is None:
            raise ValueError("No owned launch exists for this work ID")
        from labgoblin.protocol import LaunchEnvelope
        from labgoblin.worker import launch_directory
        envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
        directory = launch_directory(envelope)
        path = contained(state.root, directory / (
            f"supervisor.{args.stream}.log" if args.stage == "supervisor" else Path("main") / f"{args.stream}.log"))
        return {"work_id": args.id, "stream": args.stream, **tail(path, limit=args.bytes)}
    if args.command == "evidence":
        if args.kind == "event":
            value = briefing.event(state.db, args.id, offset=args.offset, limit=args.bytes)
        else:
            if args.offset:
                raise ValueError("Captured observations are retrieved as one exact bounded object")
            value = observation(state.db, args.id, limit=args.bytes)
            body = value.pop("body")
            if body is not None:
                import base64
                value["base64"] = base64.b64encode(body).decode("ascii")
        _record_retrieval(state, args.id)
        return value
    if args.command == "journal":
        if args.action == "entry":
            if not args.id:
                raise ValueError("journal entry requires --id")
            value = journal.entry(state.db, args.id)
            _record_retrieval(state, args.id)
            return value
        if args.action == "search":
            value = journal.search(state.db, args.query, after=args.after, cutoff=args.cutoff)
            for match in value["matches"]:
                _record_retrieval(state, match["id"])
            return value
        return journal.page(state.db, limit=args.limit, offset=args.offset, cutoff=args.cutoff)
    if args.command == "results":
        if args.export:
            if args.hypothesis:
                raise ValueError("CSV export scopes by generation, not a hidden hypothesis subset")
            return results.export(state.db, args.export, generation=args.generation)
        return results.page(state.db, generation=args.generation, hypothesis_id=args.hypothesis,
                            limit=args.limit, offset=args.offset)
    if args.command == "hypothesis":
        if args.action == "set":
            state.hypothesis(args.id, args.statement, label=args.label, supersedes=args.supersedes)
        return results.hypothesis(state.db, args.id)
    raise ValueError(f"Unknown command: {args.command}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        value = execute(args)
        if value is None:
            return 0
        failed = bool(value.get("failed") or value.get("errors") or value.get("unresolved")
                      or value.get("campaign", {}).get("blockers"))
        print(json.dumps(value, indent=2, allow_nan=False))
        return 1 if failed else 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        detail = {"error": {"type": type(error).__name__, "message": str(error)[:4000]}}
        if "--json" in argv:
            print(json.dumps(detail, allow_nan=False))
        else:
            print(f"labgoblin: {detail['error']['message']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
