"""Local research without a scientific workflow engine."""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

from labgoblin import credits, runtime
from labgoblin.config import Config
from labgoblin.paths import machine_ledger_path
from labgoblin.protocol import UncertainExecution
from labgoblin.scheduler import ResourceLedger
from labgoblin.storage import read_text


def parser():
    root = argparse.ArgumentParser(description="LabGoblin: an ordinary Copilot researcher with optional review and consultation")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a fresh local project; never starts a model")
    init.add_argument("project", nargs="?", default=".")
    init.add_argument("--goal-file", type=Path)
    init.add_argument("--model", default="auto")
    init.add_argument("--reasoning-effort", default="medium")
    for role in ("coach", "closure-critic", "advice", "claim-review"):
        init.add_argument(f"--{role}-model",
                          help=f"Model for {role}; defaults to --model and does not enable the role")
    init.add_argument("--credits", default="1000")
    init.add_argument("--timeout", type=float, default=5400)
    init.add_argument("--cpus", type=int, default=2)
    init.add_argument("--memory-mb", type=int, default=4096)
    init.add_argument("--gpu", action="append", default=[])
    init.add_argument("--coach", action=argparse.BooleanOptionalAction, default=False)
    init.add_argument("--max-stalled-reviews", type=int, default=10,
                      help="Consecutive follow-up reviews without meaningful progress before an incomplete checkpoint")
    init.add_argument("--consultant", action=argparse.BooleanOptionalAction, default=False,
                      help="Allow bounded read-only adviser calls and public literature retrieval within the project pool")
    init.add_argument("--closure-critic", action=argparse.BooleanOptionalAction, default=False,
                      help="Require independent criticism, researcher response and fresh adjudication before final acceptance; requires coach and consultant")
    init.add_argument("--ledger", type=Path)
    run = commands.add_parser("run", help="Run one persistent researcher with configured reviews and consultations")
    run.add_argument("project", nargs="?", default=".")
    run.add_argument("--resume", action="store_true", help="Explicitly continue from files, preserving all prior spending")
    for name, help_text in (("status", "Read state and accounting without mutation"),
                            ("stop", "Request bounded cancellation of this project's owned tree"),
                            ("reconcile", "Recover only proven-dead owned executions; never starts research")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("project", nargs="?", default=".")
        if name == "reconcile":
            command.add_argument("--inspection", action="store_true",
                                 help="Recover only separately owned inspector executions")
    record = commands.add_parser("record-usage", help="Append operator-confirmed final usage or an explicitly labeled estimate")
    record.add_argument("project", nargs="?", default=".")
    record.add_argument("--run", required=True)
    record.add_argument("--credits", required=True)
    record.add_argument("--reason", required=True)
    record.add_argument("--confirm", required=True, help="Exact project ID")
    record.add_argument("--estimate", action="store_true")
    record.add_argument("--inspection", action="store_true", help="Record only separate inspection spending")
    inspect = commands.add_parser("inspect", help="Open campaign chat and evidence browsing; ideas need human publication")
    inspect.add_argument("project", nargs="?", default=".")
    inspect.add_argument("--port", type=int, default=0, help="Loopback port; zero chooses an available port")
    inspect.add_argument("--open", action="store_true", help="Open the printed token-qualified URL in your browser")
    inspect.add_argument("--credits", help="Explicit separate cumulative inspection soft cap; omitted means view-only")
    inspect.add_argument("--model", help="Explicit inspection model, required when enabling paid questions")
    inspect.add_argument("--reasoning-effort", default="medium")
    inspect.add_argument("--timeout", type=float, default=300, help="Per-question deadline including shutdown")
    ideas = commands.add_parser("ideas", help="Publish immutable operator suggestions or read inbox status; no model call")
    ideas_commands = ideas.add_subparsers(dest="ideas_command", required=True)
    idea_status = ideas_commands.add_parser("status")
    idea_status.add_argument("project", nargs="?", default=".")
    publish = ideas_commands.add_parser("publish", help="Explicitly publish exact draft files; later edits need a new revision")
    publish.add_argument("project", nargs="?", default=".")
    publish.add_argument("--file", type=Path, required=True)
    publish.add_argument("--title", required=True)
    publish.add_argument("--id", help="Stable idea ID; generated if omitted")
    publish.add_argument("--base", help="Exact current revision required for an update")
    publish.add_argument("--sha256", help="Require these exact primary-file bytes")
    publish.add_argument("--attachment", action="append", type=Path, default=[])
    publish.add_argument("--attribution", default="Explicit operator CLI publication")
    machine = commands.add_parser("machine", help="Explicit shared native resource capacity")
    machine_commands = machine.add_subparsers(dest="machine_command", required=True)
    show = machine_commands.add_parser("status")
    show.add_argument("--ledger", type=Path, default=machine_ledger_path())
    configure = machine_commands.add_parser("configure")
    configure.add_argument("--ledger", type=Path, default=machine_ledger_path())
    configure.add_argument("--cpus", type=int, required=True)
    configure.add_argument("--memory-mb", type=int, required=True)
    configure.add_argument("--headroom-mb", type=int, default=2048)
    configure.add_argument("--gpu", action="append", default=[])
    return root


def credit_value(value):
    if value == "unlimited":
        return value
    try:
        scaled = Decimal(value)
    except ArithmeticError as error:
        raise ValueError("Credits must be a number or 'unlimited'") from error
    if not scaled.is_finite() or scaled < 0 or scaled * credits.NANO != (scaled * credits.NANO).to_integral_value():
        raise ValueError("Credits must be nonnegative and have at most nine decimal places")
    return int(scaled) if scaled == scaled.to_integral_value() else float(scaled)


def main(arguments=None):
    args = parser().parse_args(arguments)
    try:
        if args.command == "init":
            config = Config(model=args.model, reasoning_effort=args.reasoning_effort,
                            max_ai_credits=credit_value(args.credits), timeout_seconds=args.timeout,
                            cpus=args.cpus, memory_mb=args.memory_mb, gpus=tuple(args.gpu),
                            coach=args.coach, max_stalled_reviews=args.max_stalled_reviews, consultant=args.consultant,
                            closure_critic=args.closure_critic,
                            coach_model=args.coach_model, closure_critic_model=args.closure_critic_model,
                            advice_model=args.advice_model, claim_review_model=args.claim_review_model,
                            ledger=str(args.ledger.resolve()) if args.ledger else "")
            result = runtime.initialize(args.project, config,
                                        goal=read_text(args.goal_file) if args.goal_file else None)
        elif args.command == "run":
            result = runtime.run(args.project, resume=args.resume)
        elif args.command == "status":
            result = runtime.status(args.project)
        elif args.command == "stop":
            result = runtime.stop(args.project)
        elif args.command == "reconcile":
            if args.inspection:
                from labgoblin.inspector import Inspector
                result = Inspector(args.project).reconcile()
            else:
                result = runtime.reconcile(args.project)
        elif args.command == "record-usage":
            value = credit_value(args.credits)
            if value == "unlimited":
                raise ValueError("Usage must be an actual numeric amount")
            amount = int(Decimal(str(value)) * credits.NANO)
            if args.inspection:
                from labgoblin.inspector import Inspector
                result = Inspector(args.project).record_usage(args.run, amount, args.reason,
                                                              confirmation=args.confirm, estimated=args.estimate)
            else:
                result = runtime.record_usage(args.project, args.run, amount, args.reason,
                                              confirmation=args.confirm, estimated=args.estimate)
        elif args.command == "inspect":
            from labgoblin.inspector import Inspector
            from labgoblin.inspection_server import serve
            inspector = Inspector(args.project)
            if args.credits is not None:
                inspector.approve(credit_value(args.credits), model=args.model,
                                  reasoning_effort=args.reasoning_effort, timeout=args.timeout)
            serve(inspector, port=args.port, chat=args.credits is not None, open_browser=args.open)
            return 0
        elif args.command == "ideas":
            from labgoblin.ideas import Inbox
            inbox = Inbox(args.project)
            if args.ideas_command == "publish":
                result = inbox.publish([args.file, *args.attachment], idea_id=args.id, heading=args.title,
                                       base=args.base, attribution=args.attribution, expected_sha256=args.sha256)
            else:
                result = {**inbox.status(), "entries": inbox.entries()}
        else:
            if args.machine_command == "configure":
                ledger = (ResourceLedger(args.ledger) if args.ledger.exists()
                          else ResourceLedger.create(args.ledger))
                ledger.configure(args.cpus, args.memory_mb, tuple(args.gpu), args.headroom_mb)
            else:
                ledger = ResourceLedger(args.ledger)
            result = {"id": ledger.id, "path": str(ledger.path), "capacity": ledger.capacity(), "grants": ledger.rows()}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.command == "run" and result["status"] != "completed":
            return 1
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"error": ("Inspector closed; research was not stopped" if args.command == "inspect" else
                                  "Cancellation requested; allow the owned worker to settle, then inspect status")}),
              file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error, UncertainExecution) as error:
        print(json.dumps({"error": {"type": type(error).__name__, "message": str(error)}},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
