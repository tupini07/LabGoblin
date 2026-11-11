"""A small run supervisor; scientific work stays in ordinary files."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from labgoblin import closing, credits, guidance, payload, provider, research_environment
from labgoblin.config import Config
from labgoblin.processes import background_options, bound_diagnostics, own_handle, process_state
from labgoblin.protocol import (
    CONFIG_VERSION, LaunchEnvelope, LaunchReceipt, Resources, UncertainExecution, boolean, identifier, integer,
    number, require_version, table, text,
)
from labgoblin.scheduler import ResourceLedger
from labgoblin.storage import (
    canonical, digest, hash_file, publish_bytes, publish_json, read_json, read_text, run_lock, snapshot,
)


PLACEHOLDER = """# Research brief

Replace these prompts with the operator's goal and relevant context. The headings
are optional; a clear free-form brief is equally valid. Do not invent missing facts,
restrictions, prototype phases or smaller completion targets.

The researcher owns the evolving plan in research-notes.md: scientific formulation,
methods, experiments, evaluation and sequencing. That plan can change with evidence;
it does not redefine the original goal or add operator constraints.

## Goal and intended use

[What should this research discover or achieve, and how will the answer be used?
Describe the full objective and any evidence requirements the operator actually
specified, not a convenient benchmark or an intermediate prototype.]

## Context and available inputs

[Identify relevant data, code, access paths and prior work. Separate established
facts from assumptions, unverified findings and known gaps. Include useful context
without prescribing the researcher's methods or sequence of experiments.]

## Permissions and boundaries

[Record genuine input, privacy, resource and environment boundaries. Use the
recorded local-research authorization when enabled; add campaign-specific
restrictions or explicit approvals where needed. Examples of scientific
techniques are not an exhaustive permission whitelist. Operational limits are
ceilings, not requirements to spend the allowance or run until the deadline.]
"""
# Unfilled briefs from earlier initializations must still fail admission.
_PREVIOUS_PLACEHOLDER = "# Research goal\n\nDescribe the question, available inputs and evidence that would answer it.\n"
BOOTSTRAP = """import hashlib,json,sys
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
for name,expected in manifest.items():
    path=(root/name).resolve(strict=True)
    if not path.is_relative_to(root) or path.stat().st_size>1048576 or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        raise RuntimeError('Frozen runtime identity mismatch: '+name)
sys.path.insert(0,str(root))
if sys.argv[1]=='worker':
    from labgoblin.runtime import worker_main
    raise SystemExit(worker_main(sys.argv[2]))
elif sys.argv[1]=='provider':
    from labgoblin.provider import main
    raise SystemExit(main(sys.argv[2]))
elif sys.argv[1]=='inspection':
    from labgoblin.inspector import provider_main
    raise SystemExit(provider_main(sys.argv[2]))
elif sys.argv[1]=='inspection-worker':
    from labgoblin.runtime import worker_main
    raise SystemExit(worker_main(sys.argv[2], inspection=True))
else:
    raise ValueError('Unknown owned runtime entry point')
"""


def prepare_git(root):
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("Git is required for workspace history; install it explicitly or use init --no-git")
    command = [executable, "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root)]
    existing = any((parent / ".git").exists() or (parent / ".git").is_symlink()
                   for parent in (root, *root.parents))
    ignore = root / ".gitignore"
    if ignore.is_symlink() or ignore.resolve() != ignore or ignore.exists() and ignore.stat().st_nlink != 1:
        raise ValueError("Initialization cannot update a .gitignore file alias")
    previous = read_text(ignore) if ignore.exists() else ""
    patterns = ("/.labgoblin/", "/.labgoblin.lock", "/.venv/", "/inputs/", "/.cache/",
                "__pycache__/", ".pytest_cache/", "node_modules/", ".env", ".env.*",
                ".ssh/", ".aws/", ".azure/", ".copilot/", ".docker/",
                ".npmrc", ".pypirc", ".netrc", ".git-credentials")
    missing = [pattern for pattern in patterns if pattern not in previous.splitlines()]
    if not existing:
        payload.probe([*command, "init", "--quiet", "--template=", "--initial-branch=main"])
    repository = Path(payload.probe([*command, "rev-parse", "--show-toplevel"])).resolve(strict=True)
    if not root.is_relative_to(repository):
        raise ValueError("Git resolved a repository outside the research workspace's ancestry")
    if missing:
        suffix = ("" if not previous or previous.endswith("\n") else "\n")
        suffix += "\n# LabGoblin local state, environments and private inputs\n" + "\n".join(missing) + "\n"
        with ignore.open("ab") as stream:
            stream.write(suffix.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())


def initialize(project, config, *, goal=None, research_venv=True, git=True):
    boolean(research_venv, "research_venv")
    boolean(git, "git")
    root = Path(project).resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".labgoblin"
    if state.exists() or (root / ".labgoblin.lock").exists():
        raise ValueError("Project state already exists; initialization never resets or migrates it")
    if (root / "labgoblin.toml").exists():
        raise ValueError("labgoblin.toml already exists; initialize a fresh project instead of overwriting configuration")
    target_goal = root / "research_goal.md"
    if target_goal.exists() and goal is not None and read_text(target_goal) != goal:
        raise ValueError("The supplied goal differs from the existing research_goal.md")
    files = {root / "labgoblin.toml": config.toml().encode(),
             root / ".labgoblin.lock": b"\0"}
    if not target_goal.exists():
        files[target_goal] = (goal if goal is not None else PLACEHOLDER).encode("utf-8")
    instructions = root / ".github" / "copilot-instructions.md"
    if not instructions.exists():
        files[instructions] = guidance.RESEARCH.encode()
    created = []
    try:
        state.mkdir()
        for path, body in files.items():
            publish_bytes(path, body)
            created.append(path)
        for name in ("runs", "runtimes", "accounting"):
            (state / name).mkdir()
        value = {"version": CONFIG_VERSION, "id": identifier(), "root": str(root), "created": time.time()}
        if research_venv:
            value["research_environment"] = research_environment.prepare(root)
        if git:
            prepare_git(root)
        publish_json(state / "project.json", value)
    except BaseException:
        for path in reversed(created):
            if path.is_file() and path.read_bytes() == files[path]:
                path.unlink()
        raise
    return value


def project_record(project):
    root = Path(project).resolve(strict=True)
    state = root / ".labgoblin"
    if (state / "labgoblin.db").exists():
        raise ValueError("This is a legacy campaign; use its preserved runtime. No migration or reset was performed.")
    value = read_json(state / "project.json")
    table(value, "project identity", {"version", "id", "root", "created", "research_environment"})
    require_version(value.get("version"), CONFIG_VERSION, "project")
    if not re.fullmatch(r"[a-f0-9]{32}", text(value.get("id"), "project id")):
        raise ValueError("Invalid project identity")
    number(value.get("created"), "project creation time")
    if value.get("root") != str(root) or not (root / ".labgoblin.lock").is_file():
        raise ValueError("Project identity or controller lock does not match its recorded root")
    if "research_environment" in value:
        research_environment.validate(root, value["research_environment"])
    return root, state, value


def bound_ledger(state, *, config=None, bind=False):
    path = state / "ledger.json"
    if path.exists():
        value = read_json(path)
        if config is not None and str(config.ledger_path) != value["path"]:
            raise ValueError("Changing the ledger would discard accounting; use this project's existing ledger")
        return ResourceLedger(value["path"], expected_id=value["id"])
    if config is None:
        return None
    ledger = ResourceLedger.ensure(config.ledger_path) if bind else ResourceLedger(config.ledger_path)
    if bind:
        publish_json(path, {"path": str(ledger.path), "id": ledger.id})
    return ledger


def adjustments(state, rows):
    receipts = {row["token"]: json.loads(row["receipt"]) for row in rows if row["receipt"]}
    selected = {}
    for path in sorted((state / "accounting").glob("*.json")):
        value = read_json(path)
        receipt = receipts.get(value["run_id"])
        if receipt is None or value["receipt_sha256"] != digest(receipt):
            raise ValueError(f"Accounting evidence does not bind an existing immutable receipt: {path}")
        credits.usage_nano(value["usage"])
        if type(value["estimated"]) is not bool:
            raise ValueError("Accounting evidence must distinguish reported usage from estimates")
        old = selected.get(value["run_id"])
        if old and old["estimated"] == value["estimated"] and old["usage"] != value["usage"]:
            raise ValueError("Conflicting accounting evidence requires operator investigation")
        if old is None or not value["estimated"]:
            selected[value["run_id"]] = value
    return selected


def status(project):
    from labgoblin.ideas import Inbox
    from labgoblin.supervisor import observation
    root, state, record = project_record(project)
    ledger = bound_ledger(state)
    rows = ledger.runs(record["id"]) if ledger else []
    candidate, candidate_error = None, None
    try:
        candidate = Config.load(root)
    except (OSError, ValueError) as error:
        candidate_error = f"{type(error).__name__}: {error}"
        if not rows:
            raise
    config = Config.parse(json.loads(rows[-1]["envelope"])["config"], frozen=True) if rows else candidate
    budget = credits.summarize(rows, config.credit_limit, adjustments=adjustments(state, rows))
    runs = []
    for row in rows:
        envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
        receipt = json.loads(row["receipt"]) if row["receipt"] else None
        handle = json.loads(row["supervisor"]) if row["supervisor"] else envelope.launcher
        runs.append({"id": envelope.id, "phase": row["phase"], "owner": process_state(handle),
                     "status": receipt["status"] if receipt else "running",
                     "reason": receipt["reason"] if receipt else "",
                     "checkpoint_reason": (receipt.get("metadata", {}).get("provider", {}).get("checkpoint_reason")
                                           if receipt else None),
                     "archive": receipt.get("metadata", {}).get("archive") if receipt else None,
                     "directory": envelope.root, "config": envelope.config})
    return {"project_id": record["id"], "root": str(root), "budget": budget, "runs": runs,
            "supervisor_review": observation(rows),
            "ideas": Inbox(root).status(),
            "state": runs[-1]["status"] if runs else "initialized",
            "candidate_config": candidate.to_dict() if candidate else None, "candidate_error": candidate_error,
            "note": "Process completion and coach acceptance are not independent scientific validation. "
                    "TOML changes apply only to a newly admitted execution."}


def freeze_runtime(state):
    source = Path(__file__).parent
    root = state / "runtimes" / identifier()
    root.mkdir()
    manifest = {}
    for path in sorted(source.glob("*.py")):
        relative = str(Path("labgoblin") / path.name)
        body = path.read_bytes()
        publish_bytes(root / relative, body)
        manifest[relative] = hash_file(root / relative)
    publish_bytes(root / "bootstrap.py", BOOTSTRAP.encode())
    manifest["bootstrap.py"] = hash_file(root / "bootstrap.py")
    publish_json(root / "manifest.json", manifest)
    return {"root": str(root), "files": manifest}


def verify_runtime(envelope, *, entrypoint="provider"):
    root = Path(envelope.runtime["root"]).resolve(strict=True)
    if not root.is_relative_to(Path(envelope.project) / ".labgoblin" / "runtimes"):
        raise ValueError("Frozen runtime is outside its project")
    if read_json(root / "manifest.json") != envelope.runtime["files"]:
        raise ValueError("Frozen runtime manifest differs from the armed authorization")
    for name, expected in envelope.runtime["files"].items():
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or hash_file(path) != expected:
            raise ValueError(f"Frozen runtime mismatch: {name}")
    if hash_file(Path(envelope.root) / "prompt.json") != envelope.prompt_sha256:
        raise ValueError("Research goal/instruction snapshot changed after admission")
    expected = (sys.executable, "-I", "-B", str(root / "bootstrap.py"),
                entrypoint, str(Path(envelope.root) / "envelope.json"))
    config = Config.parse(envelope.config, frozen=True)
    if (envelope.argv != expected or config.gpus != envelope.resources.gpus
            or any(limit != "unlimited" and limit != observed for limit, observed in (
                (config.cpus, envelope.resources.cpus), (config.memory_mb, envelope.resources.memory_mb)))):
        raise ValueError("Provider command or resources differ from their frozen configuration")


def usage_record(envelope, *, complete):
    path = Path(envelope.root) / ("usage.json" if complete else "usage-progress.json")
    if not path.exists():
        return None
    value = read_json(path)
    if (value.get("run_id") != envelope.id or value.get("envelope_digest") != envelope.digest
            or value.get("complete") is not complete):
        raise ValueError("Usage record is not bound to this owned execution and completeness state")
    credits.usage_nano(value)
    sessions = value.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("Usage must identify its owned provider sessions")
    roles, amount, research_prompts = set(), 0, 0
    goal_hooks, researcher_prompt_count = {}, 0
    adjudications = []
    progress_reviews = "max_stalled_reviews" in envelope.config
    fresh_closure = closing.enabled(read_json(Path(envelope.root) / "prompt.json"))
    from labgoblin.checkins import enabled as checkins_enabled
    timed_reviews = (checkins_enabled(read_json(Path(envelope.root) / "prompt.json"))
                     and envelope.config["coach"] and envelope.config.get("goal_review_interval_seconds", 0) > 0)
    for session in sessions:
        table(session, "provider session")
        role = session.get("role")
        allowed = {"researcher", "coach-0", "coach-1"} if envelope.config["coach"] else {"researcher"}
        coach = (envelope.config["coach"] and progress_reviews
                 and isinstance(role, str) and re.fullmatch(r"coach-(0|[1-9][0-9]{0,5})", role))
        consultant = (envelope.config.get("consultant", False)
                      and isinstance(role, str) and re.fullmatch(r"consult-(0|[1-9][0-9]{0,5})", role))
        judge = (fresh_closure and envelope.config.get("closure_critic", False)
                 and isinstance(role, str) and re.fullmatch(r"adjudicator-([1-9][0-9]{0,5})", role))
        goal_review = (timed_reviews and isinstance(role, str)
                       and re.fullmatch(r"goal-review-(0|[1-9][0-9]{0,5})", role))
        if (role not in allowed and not consultant and not coach and not judge and not goal_review) or role in roles:
            raise ValueError("Usage contains an unauthorized or duplicated provider role")
        expected = str(uuid.UUID(envelope.id) if role == "researcher" else uuid.uuid5(uuid.UUID(envelope.id), role))
        if session.get("session_id") != expected:
            raise ValueError("Provider usage belongs to a different owned session")
        if goal_review:
            request = read_json(Path(envelope.root) / "sessions" / role / "request.json")
            hook_id = request.get("hook_id")
            if (session.get("purpose") != "coach" or request.get("purpose") != "goal_review"
                    or request.get("caller") != "researcher" or request.get("tool_call_id") is not None
                    or request.get("caller_session_id") != str(uuid.UUID(envelope.id))
                    or not isinstance(hook_id, str) or not re.fullmatch(r"[0-9a-f]{32}", hook_id)):
                raise ValueError("Goal review lacks its exact researcher hook")
            hook = read_json(Path(envelope.root) / "checkins" / hook_id / "request.json")
            if (hook.get("run_id") != envelope.id or hook.get("envelope_digest") != envelope.digest
                    or hook.get("session_id") != str(uuid.UUID(envelope.id)) or hook.get("hook_id") != hook_id):
                raise ValueError("Goal review hook belongs to another execution")
            if (hook_id in goal_hooks or hook.get("trigger") != "elapsed_interval"
                    or hook.get("interval_seconds") != envelope.config["goal_review_interval_seconds"]):
                raise ValueError("Goal review must use its unique frozen timed hook")
            goal_hooks[hook_id] = integer(hook.get("prompt"), "goal-review caller prompt")
        boolean(session.get("complete"), "session usage completeness")
        if session["complete"] and session.get("usage") is None or complete and not session["complete"]:
            raise ValueError("Final usage cannot contain an unsettled session")
        prompts = integer(session.get("prompts"), "session prompt count", zero=True)
        if role == "researcher":
            from labgoblin.ideas import extra_prompts
            researcher_prompt_count = prompts
            research_prompts = prompts - extra_prompts(envelope, prompts)
        adjudication = (not fresh_closure and coach and role != "coach-0" and envelope.config.get("closure_critic", False)
                        and envelope.config.get("consultant", False))
        if ((role != "researcher" and prompts > (2 if adjudication else 1))
                or (role == "researcher" and not (progress_reviews and envelope.config["coach"])
                    and research_prompts > (2 if envelope.config["coach"] else 1))):
            raise ValueError("Usage exceeds the bounded research/review conversation")
        if adjudication and prompts == 2:
            reference = read_json(Path(envelope.root) / "sessions" / role / "closure-adjudication.json")
            proposal = read_json(Path(envelope.root) / f"review-{role.split('-')[1]}-proposal.json")
            if proposal.get("decision") != "accept" or not reference.get("artifact_sha256"):
                raise ValueError("Extra coach prompt lacks a retained proposed acceptance and closure critique")
            adjudications.append((role, reference))
        amount += credits.usage_nano(session["usage"]) if session.get("usage") is not None else 0
        roles.add(role)
    if "researcher" not in roles or ("coach-1" in roles and "coach-0" not in roles) or amount != credits.usage_nano(value):
        raise ValueError("Aggregate usage does not match its exact owned sessions")
    if any(prompt > researcher_prompt_count for prompt in goal_hooks.values()):
        raise ValueError("Goal review does not name an admitted researcher prompt")
    closure_prompts = closing.response_prompts(envelope, {item["role"]: item for item in sessions})
    for number in closure_prompts:
        idea_path = Path(envelope.root) / "ideas" / f"prompt-{number}.json"
        if idea_path.exists() and read_json(idea_path).get("extra_prompt"):
            raise ValueError("One researcher prompt cannot claim two extra-prompt allowances")
    research_prompts -= len(closure_prompts)
    advisers = {role for role in roles if role.startswith("consult-")}
    if advisers != {f"consult-{index}" for index in range(len(advisers))}:
        raise ValueError("Consultation usage must retain every admitted adviser session")
    for coach_role, reference in adjudications:
        adviser_role = reference.get("adviser_role")
        if adviser_role not in advisers:
            raise ValueError("Adjudication does not name an owned adviser")
        directory = Path(envelope.root) / "sessions" / adviser_role
        request, response = read_json(directory / "request.json"), read_json(directory / "response.json")
        if (request.get("purpose") != "closure_critique" or request.get("caller") != coach_role
                or request.get("caller_session_id") != str(uuid.uuid5(uuid.UUID(envelope.id), coach_role))
                or response.get("status") != "completed" or response.get("purpose") != "closure_critique"
                or any(response.get(key) != reference.get(key) for key in ("artifact", "artifact_sha256", "adviser_role"))):
            raise ValueError("Extra coach prompt is not bound to its completed independent closure critique")
    coaches = {role for role in roles if role.startswith("coach-")}
    if coaches != {f"coach-{index}" for index in range(len(coaches))}:
        raise ValueError("Stopping-review usage must retain every admitted coach session")
    goal_reviews = {role for role in roles if role.startswith("goal-review-")}
    if goal_reviews != {f"goal-review-{index}" for index in range(len(goal_reviews))}:
        raise ValueError("Goal-review usage must retain every admitted coach session")
    if progress_reviews and envelope.config["coach"] and not len(coaches) <= research_prompts <= len(coaches) + 1:
        raise ValueError("Research prompts must alternate with their exact owned stopping reviews")
    return value


def settle_dead(ledger, row, *, inspection=False):
    envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
    verify_runtime(envelope, entrypoint="inspection" if inspection else "provider")
    if inspection:
        from labgoblin.inspector import usage_record as read_usage
    else:
        read_usage = usage_record
    root = Path(envelope.root)
    receipt_path = root / "receipt.json"
    if receipt_path.exists():
        receipt = LaunchReceipt.parse(read_json(receipt_path))
        ledger.finish_consumer(receipt)
        return receipt
    handle = json.loads(row["supervisor"]) if row["supervisor"] else envelope.launcher
    if process_state(handle) != "dead":
        raise UncertainExecution("The exact execution owner is not proven dead; its grant remains held")
    if row["phase"] == "armed":
        receipt = LaunchReceipt(envelope.id, envelope.digest, "not_started", 0, False,
                                reason="Exact launcher died before a committed supervisor claim",
                                metadata={"launcher": envelope.launcher})
    else:
        if payload.tree_state(root / "process", envelope.id) != "dead":
            raise UncertainExecution("Owned payload tree is not proven dead; its grant remains held")
        metadata = {"supervisor": handle, "partial_usage": read_usage(envelope, complete=False)}
        if not inspection and "evaluation" in read_json(root / "prompt.json"):
            from labgoblin.evaluation import manifest
            metadata["evaluations"] = manifest(root)
        receipt = LaunchReceipt(envelope.id, envelope.digest, "interrupted", 0, True,
                                reason="Owned tree died without a terminal receipt; no research is replayed",
                                usage=read_usage(envelope, complete=True), metadata=metadata)
    ledger.finish_consumer(receipt)
    publish_json(receipt_path, asdict(receipt))
    return receipt


def recover_project(state, record, ledger):
    outcomes = []
    for row in ledger.runs(record["id"]):
        if row["phase"] != "quiescent":
            outcomes.append(asdict(settle_dead(ledger, row)))
        else:
            envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
            path = Path(envelope.root) / "receipt.json"
            value = json.loads(row["receipt"])
            if path.exists():
                if read_json(path) != value:
                    raise ValueError("Receipt file differs from its durable ledger receipt")
            else:
                publish_json(path, value)
    with ledger.read() as conn:
        pending = [dict(row) for row in conn.execute("""SELECT * FROM grants
            WHERE owner_id=? AND kind='research-run' AND state IN ('pending','granted')
            AND token NOT IN (SELECT token FROM consumer_runs)""", (record["id"],))]
    for row in pending:
        owner = json.loads(row["owner"])
        if process_state(owner["handle"]) != "dead":
            raise UncertainExecution("An unarmed request still has a live or unknown owner")
        ledger.release(row["token"], owner_id=record["id"])
    return outcomes


def reconcile(project):
    root, state, record = project_record(project)
    with run_lock(root / ".labgoblin.lock"):
        ledger = bound_ledger(state)
        return recover_project(state, record, ledger) if ledger else []


def stop(project):
    root, state, record = project_record(project)
    ledger = bound_ledger(state)
    if ledger is None:
        raise ValueError("This project has no admitted execution to stop")
    stopped = []
    for row in ledger.runs(record["id"]):
        if row["phase"] != "quiescent":
            envelope = LaunchEnvelope.parse(json.loads(row["envelope"]))
            path = Path(envelope.root) / "cancel"
            try:
                publish_bytes(path, b"operator stop\n")
            except FileExistsError:
                if path.read_bytes() != b"operator stop\n":
                    raise ValueError("Existing stop marker has unexpected contents")
            stopped.append(envelope.id)
    if not stopped:
        raise ValueError("This project has no active execution to stop")
    return {"stop_requested": stopped, "note": "Wait for owned-tree retirement and final usage before resuming"}


def record_usage(project, run_id, amount, reason, *, confirmation, estimated):
    root, state, record = project_record(project)
    if confirmation != record["id"]:
        raise ValueError("Explicit project identity confirmation is required")
    with run_lock(root / ".labgoblin.lock"):
        ledger = bound_ledger(state)
        if ledger is None:
            raise ValueError("Project has no admitted executions")
        rows = ledger.runs(record["id"])
        return append_usage(state, rows, Config.load(root).credit_limit, run_id, amount, reason,
                            confirmation=confirmation, estimated=estimated)


def append_usage(state, rows, limit, run_id, amount, reason, *, confirmation, estimated):
    """Called under the owning controller/accounting lock with its qualified rows."""
    text(reason, "accounting reason")
    boolean(estimated, "estimated usage")
    integer(amount, "nano-credits", zero=True)
    row = next((row for row in rows if row["token"] == run_id), None)
    if row is None or row["phase"] != "quiescent":
        raise ValueError("Accounting evidence requires an exact quiescent owned execution")
    receipt = json.loads(row["receipt"])
    if not receipt["executed"] or receipt["usage"] is not None:
        raise ValueError("Only an executed invocation with missing final usage needs reconciliation")
    value = {"run_id": run_id, "receipt_sha256": digest(receipt), "usage": {"totalNanoAiu": amount},
             "estimated": estimated, "reason": reason, "created": time.time(), "operator_confirmed": confirmation}
    current = adjustments(state, rows)
    previous = current.get(run_id)
    if previous and (not previous["estimated"] or estimated):
        raise ValueError("Accounting evidence already exists; only final usage may supersede an estimate")
    current[run_id] = value
    credits.summarize(rows, limit, adjustments=current)
    target = state / "accounting" / f"{identifier()}.json"
    publish_json(target, value)
    return value


def run(project, *, resume=False, handoff=False, reviewed=None):
    from labgoblin import supervisor
    root, state, record = project_record(project)
    with run_lock(root / ".labgoblin.lock"):
        ledger = bound_ledger(state)
        rows = ledger.runs(record["id"]) if ledger else []
        active = [row for row in rows if row["phase"] != "quiescent"]
        if active:
            if handoff or reviewed is not None:
                raise ValueError("Select --handoff/--reviewed when admitting a new execution, not while attaching to existing work")
            if len(active) != 1:
                raise UncertainExecution("Multiple unsettled executions require operator investigation")
            return wait_for(ledger, LaunchEnvelope.parse(json.loads(active[0]["envelope"])))
        if ledger:
            recover_project(state, record, ledger)
        if rows and not resume:
            raise ValueError("This project has prior executions; use run --resume explicitly, retaining cumulative spending")
        review_confirmation = supervisor.continuation(rows, reviewed, resume=resume)
        config = Config.load(root)
        ledger = bound_ledger(state, config=config, bind=True)
        rows = ledger.runs(record["id"])
        audited = adjustments(state, rows)
        allowance = credits.available(credits.summarize(rows, config.credit_limit, adjustments=audited),
                                      coach=config.coach, consultant=config.consultant)
        goal = read_text(root / "research_goal.md")
        if not goal.strip() or goal.split() in (PLACEHOLDER.split(), _PREVIOUS_PLACEHOLDER.split()):
            raise ValueError("Write a real research question in research_goal.md before running")
        environment = (research_environment.inspect(root, record["research_environment"])
                       if "research_environment" in record else None)
        capabilities = provider.capabilities()
        from labgoblin.handoffs import prepare as prepare_handoff
        report_approval = prepare_handoff(root) if handoff else None
        instructions = read_text(root / ".github" / "copilot-instructions.md")
        if instructions != guidance.RESEARCH:
            instructions = guidance.RESEARCH + "\n\nProject-specific research guidance:\n" + instructions
        if environment is not None:
            instructions += research_environment.instructions(environment)
        run_id, launcher = identifier(), own_handle(record["id"])
        directory = state / "runs" / run_id
        directory.mkdir()
        frozen = freeze_runtime(state)
        from labgoblin.ideas import VERSION as IDEAS_VERSION
        environment_hint = (
            "\nScientific Python: " + environment["python"] + "\nUse the recorded local research authorization."
            if environment is not None else
            "\nFor scientific work, use the separate research environment explicitly authorized in the goal, "
            "if any; otherwise use this existing interpreter.")
        prompt = {"goal": goal, "instructions": instructions, "operator_ideas_version": IDEAS_VERSION,
                  "research_checkins_version": 1,
                  "provider_command": capabilities["command"],
                  "goal_prompt": goal + "\n\nHarness interpreter (do not modify its environment): " + sys.executable +
                  environment_hint +
                  "\nContinue from retained research-notes.md and artifacts if present. "
                  "Do not rerun completed work merely because this is a new controller execution."}
        if config.supervisor_review is not None:
            prompt["supervisor_review"] = {
                "version": supervisor.VERSION, "due_at": config.supervisor_review.next_after(time.time())}
        if review_confirmation is not None:
            prompt["supervisor_reviewed"] = review_confirmation
        if environment is not None:
            prompt["research_environment"] = environment
        if config.closure_critic:
            prompt["closure_response_version"] = closing.VERSION
        if report_approval is not None:
            prompt["handoff"] = report_approval
        if config.evaluation_spec is not None:
            from labgoblin.evaluation import prepare
            prompt["evaluation"] = prepare(root, directory, config.evaluation_spec, config.max_snapshot_bytes,
                                           research_environment=environment)
        publish_json(directory / "prompt.json", prompt)
        owner = {"kind": "research-run", "handle": launcher}
        grant = ledger.request(run_id, record["id"], run_id, "research-run", config, owner, native=True)
        armed = False
        try:
            if grant["state"] != "granted":
                raise ValueError(f"Execution admission unavailable: {grant['reason']}")
            request = json.loads(grant["request"])
            command = (sys.executable, "-I", "-B", str(Path(frozen["root"]) / "bootstrap.py"),
                       "provider", str(directory / "envelope.json"))
            envelope = LaunchEnvelope(
                run_id, record["id"], str(root), str(directory), str(ledger.path), ledger.id,
                command, Resources.parse(request["resources"]), tuple(request["cpu_ids"]), config.to_dict(),
                allowance, launcher, frozen, hash_file(directory / "prompt.json"))
            publish_json(directory / "envelope.json", asdict(envelope))
            verify_runtime(envelope)
            ledger.arm_consumer(envelope, adjustments=audited)
            armed = True
            worker = [sys.executable, "-I", "-B", str(Path(frozen["root"]) / "bootstrap.py"),
                      "worker", str(directory / "envelope.json")]
            with (directory / "supervisor.stdout.log").open("xb") as out, (directory / "supervisor.stderr.log").open("xb") as err:
                process = subprocess.Popen(worker, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                           **background_options(independent=True))
            try:
                return wait_for(ledger, envelope, process=process)
            finally:
                if process.poll() is not None:
                    process.wait()
        finally:
            if not armed:
                ledger.release(run_id, owner_id=record["id"])


def wait_for(ledger, envelope, *, process=None, inspection=False):
    try:
        while True:
            row = ledger.consumer_run(envelope.id)
            if row["phase"] == "quiescent":
                receipt = LaunchReceipt.parse(json.loads(row["receipt"]))
                path = Path(envelope.root) / "receipt.json"
                if not path.exists():
                    publish_json(path, asdict(receipt))
                return asdict(receipt)
            handle = json.loads(row["supervisor"]) if row["supervisor"] else envelope.launcher
            if process_state(handle) == "dead":
                return asdict(settle_dead(ledger, row, inspection=inspection))
            if process is not None and process.poll() is not None and row["phase"] == "armed":
                raise UncertainExecution("Supervisor exited before claiming; exit this controller, then reconcile the fenced launch")
            time.sleep(0.5)
    except KeyboardInterrupt:
        path = Path(envelope.root) / "cancel"
        if not path.exists():
            publish_bytes(path, b"operator stop\n")
        raise


def worker_main(path, *, inspection=False):
    bound_diagnostics()
    envelope = LaunchEnvelope.parse(read_json(path))
    verify_runtime(envelope, entrypoint="inspection" if inspection else "provider")
    if inspection:
        from labgoblin.inspector import usage_record as read_usage
    else:
        read_usage = usage_record
    root, config = Path(envelope.root), Config.parse(envelope.config, frozen=True)
    environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    if not inspection:
        prompt = read_json(root / "prompt.json")
        if "research_environment" in prompt:
            research_environment.check(envelope.project, prompt["research_environment"])
            environment.update(research_environment.variables(prompt["research_environment"]))
    ledger = ResourceLedger(envelope.ledger_path, expected_id=envelope.ledger_id)
    supervisor = own_handle(envelope.id)
    if not ledger.claim_consumer(envelope, supervisor):
        raise ValueError("This one-use execution was already claimed or retired")
    outcome = payload.run_process({
        "argv": envelope.argv, "cwd": envelope.project, "root": str(root / "process"),
        "token": envelope.id, **payload.resource_spec(envelope),
        "seconds": config.timeout_seconds, "log_bytes": 64 * 1024 * 1024,
        "cancel_path": str(root / "cancel"), "provider": "copilot", "shutdown_grace_seconds": provider.GRACE_SECONDS,
        "job_name": f"labgoblin-{envelope.id}",
        "environment": environment,
    })
    status, reason = outcome["status"], outcome["reason"]
    metadata = {"supervisor": supervisor, "process": outcome,
                "primary_outcome": {"status": status, "reason": reason}}
    usage = None
    stage = "provider result"
    try:
        if (root / "result.json").exists():
            result = read_json(root / "result.json")
            if result["run_id"] != envelope.id or result["envelope_digest"] != envelope.digest:
                raise ValueError("Provider result identity mismatch")
            metadata["provider"] = result
            if outcome["returncode"] is not None:
                status, reason = result["status"], result["reason"]
        elif outcome["executed"]:
            status, reason = "failed", "Owned provider ended without its completion record"
        metadata["primary_outcome"] = metadata.get("provider", {}).get(
            "primary_outcome", {"status": status, "reason": reason})
        stage = "final provider usage"
        usage = read_usage(envelope, complete=True)
        stage = "partial provider usage"
        metadata["partial_usage"] = read_usage(envelope, complete=False)
        if outcome["executed"] and usage is None:
            status = status if status in ("cancelled", "timed_out") else "failed"
            reason = (reason + "; final provider spending is unknown").lstrip("; ")
        if not inspection:
            stage = "evaluation evidence"
            if "evaluation" in read_json(root / "prompt.json"):
                from labgoblin.evaluation import manifest
                metadata["evaluations"] = manifest(root)
    except (OSError, ValueError, RuntimeError) as error:
        metadata["finalization_errors"] = [provider.error_details(stage, error)]
        status = "failed"
        reason = "; ".join(filter(None, [reason, f"{stage}: {type(error).__name__}: {error}"]))
    if not inspection:
        metadata["archive"] = {"status": "disabled", "reason": "Full workspace archival was not requested"}
        if config.archive_workspace:
            try:
                metadata["snapshot"] = snapshot(Path(envelope.project), root / "snapshot", config.max_snapshot_bytes)
                metadata["archive"] = {"status": "completed"}
            except (OSError, ValueError, RuntimeError) as error:
                metadata["archive"] = {"status": "failed", **provider.error_details("workspace archive", error)}
                print(f"Workspace archival failed; research outcome is unchanged: {type(error).__name__}: {error}",
                      file=sys.stderr, flush=True)
    receipt = LaunchReceipt(envelope.id, envelope.digest, status, outcome["elapsed"], outcome["executed"],
                            returncode=outcome["returncode"], reason=reason, usage=usage, metadata=metadata)
    publish_json(root / "receipt.json", asdict(receipt))
    ledger.finish_consumer(receipt)
    if not inspection:
        from labgoblin.handoffs import automatic
        result = automatic(envelope)
        if result is not None and result["status"] != "completed":
            print(f"Independent handoff {result['status']}: {result['reason']}", file=sys.stderr, flush=True)
    return (0 if status == "completed" or status == "checkpointed" and
            metadata.get("provider", {}).get("checkpoint_reason") == "supervisor_review" else 1)
