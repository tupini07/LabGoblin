"""Local-first CLI surface, kept separate from the legacy cluster commands."""

import json
from pathlib import Path
import sys

import tomli_w

from xgenius.campaign import Campaign
from xgenius.config import AGENT_COMMANDS, load_config
from xgenius.scheduler import ResourceLedger
from xgenius.workspace import read_json


INSTRUCTIONS = """
<!-- xgenius-local:start -->
## xgenius local research

Read research_goal.md and `xgenius journal read` before acting. Use `xgenius
status --json` and `xgenius budget --json` to inspect state. Never start another
controller from a research-agent turn.

Run experiments through `xgenius submit --spec experiment.json --json`. The
manifest contains a stable `key`, `argv` array, explicit `source_files`, optional
`runner` and `hypothesis_id`, `cpus`, `memory_mb`, `gpus` (physical UUIDs),
`seconds`, and relative `artifacts` paths. Source files are copied into a
per-attempt snapshot. Write outputs under the XGENIUS_OUTPUT_DIR environment
variable; read declared inputs through XGENIUS_INPUT_NAME. Metrics may be a
numeric object in output/metrics.json. Do not run heavy work outside the queue.

When assigning a hypothesis_id, also supply hypothesis_description: a plain-language
statement of what is being tested, not a copy of the ID. Reuse the same statement
for experiments testing the same hypothesis. For an existing record, use
`xgenius db hypothesis-update --id H --description "Statement"` to deliberately
add or revise its statement, and record rationale and outcomes in the journal.

Keep declared source inputs and shared environments unchanged. New code,
environments, scratch, artifacts, and reports belong in the campaign workspace.
No automatic pushes, issue/PR creation, uploads, or remote compute. Do not
install into shared environments. Request human help through a blocked turn.

Always record findings, failures, and next steps with `xgenius journal write`.
When the controller requests a turn-result JSON, return its exact turn ID,
only the event IDs actually processed, a disposition, and an explanation.
A successful process exit is not evidence of scientific validity.

This is trusted local execution, not a sandbox around the agent.
<!-- xgenius-local:end -->
"""


def initialize(args):
    root = Path.cwd()
    path = root / "xgenius.toml"
    if path.exists() and not args.force:
        raise ValueError("xgenius.toml already exists; use --force only to replace its configuration")
    if path.exists():
        from xgenius.state import identifier
        previous = load_config(str(path))
        if previous.local:
            from xgenius.agent_policy import require_idle_agent
            from xgenius.backends import alive
            old = Campaign(previous)
            require_idle_agent(previous)
            if any(a["status"] not in ("completed", "failed", "cancelled", "timed_out", "interrupted")
                   for a in old.state.attempts()) or (
                    old.state.campaign()["controller"] and
                    alive(json.loads(old.state.campaign()["controller"]))):
                raise ValueError("Cannot replace configuration during active or unresolved work")
        path.replace(path.with_name(f"xgenius.toml.backup-{identifier()}"))
    config = {
        "schema_version": 2,
        "project": {"name": root.name, "research_goal": "research_goal.md"},
        "execution": {"default_runner": "native", "source_files": []},
        "runners": {"native": {"kind": "local", "python": sys.executable}},
        "campaign": {"cpus": 2, "memory_mb": 2048, "gpus": [], "max_jobs": 1,
                     "max_gpu_hours": 0, "max_seconds": 3600},
        "agent": {"command": AGENT_COMMANDS[args.agent].split(), "max_turns": 10,
                  "timeout_seconds": 600, "retries": 1, "sandbox": False},
    }
    with path.open("wb") as f:
        tomli_w.dump(config, f)
    goal = root / "research_goal.md"
    if not goal.exists():
        goal.write_text("# Research goal\n\nDefine the objective, evidence, evaluation, and stop conditions.\n",
                        encoding="utf-8")
    instructions = root / "CLAUDE.md"
    existing = instructions.read_text(encoding="utf-8") if instructions.exists() else "# Research instructions\n"
    if "<!-- xgenius-local:start -->" not in existing:
        instructions.write_text(existing + "\n" + INSTRUCTIONS, encoding="utf-8")
    else:
        before, section = existing.split("<!-- xgenius-local:start -->", 1)
        _, after = section.split("<!-- xgenius-local:end -->", 1)
        instructions.write_text(before + INSTRUCTIONS.strip() + after, encoding="utf-8")
    campaign = Campaign(load_config(str(path)))
    (root / "results").mkdir(exist_ok=True)
    ignore = root / ".gitignore"
    text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if ".xgenius/" not in text.splitlines():
        ignore.write_text(text + "\n# xgenius local execution state\n.xgenius/\n.xgenius-archives/\n",
                          encoding="utf-8")
    return {"status": "initialized", "campaign_id": campaign.state.id,
            "mode": "trusted", "next": "Review campaign limits and configure shared machine capacity before run"}


def register(subparsers, parent):
    for name in ("run", "resume"):
        p = subparsers.add_parser(name, parents=[parent], help=f"{name.capitalize()} local research campaign")
        p.add_argument("--no-agent", action="store_true", help="Execute queued jobs without research turns")
        p.add_argument("--once", action="store_true", help="Perform one control cycle")
        p.set_defaults(local_action=name)
    for name in ("pause", "stop", "doctor"):
        p = subparsers.add_parser(name, parents=[parent], help=f"{name.capitalize()} local research campaign")
        p.set_defaults(local_action=name)
    p = subparsers.add_parser("machine", parents=[parent], help="Shared local capacity and reservations")
    p.add_argument("machine_action", choices=["configure", "status"])
    p.add_argument("--cpus", type=int)
    p.add_argument("--memory-mb", type=int)
    p.add_argument("--headroom-mb", type=int, default=2048)
    p.add_argument("--gpus", default="", help="Comma-separated physical GPU UUIDs")
    p.set_defaults(local_action="machine")


def execute(args, config=None):
    action = getattr(args, "local_action", args.command)
    if action == "machine":
        ledger = ResourceLedger()
        if args.machine_action == "configure":
            if args.cpus is None or args.memory_mb is None:
                raise ValueError("machine configure requires --cpus and --memory-mb")
            ledger.configure(args.cpus, args.memory_mb,
                             [x for x in args.gpus.split(",") if x], args.headroom_mb)
        return {"capacity": ledger.capacity(), "reservations": ledger.rows()}
    campaign = Campaign(config or load_config(args.config))
    if action == "submit":
        if args.cluster or args.command_text:
            raise ValueError("Local submission uses --spec with argv, not --cluster/--command")
        if not args.spec:
            raise ValueError("Local submission requires --spec FILE")
        request = read_json(Path(args.spec))
        if args.runner:
            if request.get("runner") not in (None, args.runner):
                raise ValueError("--runner conflicts with the manifest runner")
            request["runner"] = args.runner
        job_id = campaign.submit(request)
        return {"success": True, "job_id": job_id}
    if action == "batch-submit":
        data = read_json(Path(args.file))
        requests = data if isinstance(data, list) else data.get("experiments") if isinstance(data, dict) else None
        if not isinstance(requests, list) or not requests:
            raise ValueError("Batch must contain a nonempty experiments array")
        result = []
        for request in requests:
            try:
                result.append({"success": True, "job_id": campaign.submit(request)})
            except (ValueError, OSError, RuntimeError) as e:
                result.append({"success": False, "error": str(e)})
        return result
    if action in ("run", "resume", "watch"):
        if action == "resume":
            campaign.control("resume")
        campaign.run(no_agent=getattr(args, "no_agent", False), once=getattr(args, "once", False))
        result = campaign.state.campaign()
        if getattr(args, "no_agent", False):
            result["failed_attempts"] = sum(
                a["status"] in ("failed", "timed_out", "interrupted", "recovery_required")
                or (a["status"] == "completed" and bool(a["reason"]))
                for a in campaign.state.attempts())
        return result
    if action in ("pause", "stop"):
        campaign.control(action)
        return campaign.state.campaign()
    if action == "doctor":
        return campaign.doctor()
    if action in ("status", "reconcile", "check-completions"):
        campaign.reconcile()
        attempts = campaign.state.attempts()
        return {"campaign": campaign.state.campaign(),
                "jobs": [{k: v for k, v in a.items() if k != "spec"} for a in attempts],
                "events": campaign.state.pending_events(), "reservations": campaign.ledger.rows()}
    if action == "build":
        from dataclasses import asdict
        from xgenius.backends import docker_prefix, validate_docker_endpoint
        from xgenius.container import _run_step
        runner = campaign.local.runners.get(args.runner or campaign.local.default_runner)
        if not runner or runner.kind != "docker":
            raise ValueError("Local build requires --runner selecting a configured Docker runner")
        if args.registry or args.step not in (None, "docker"):
            raise ValueError("Local build only builds an image; no registry/SIF pipeline. Test through submit.")
        validate_docker_endpoint(runner.context)
        result = _run_step(
            [*docker_prefix(asdict(runner)), "build", "--pull=false",
             "--network=none" if not runner.network else "--network=default",
             "-t", runner.image, "-f", args.dockerfile or "Dockerfile", "."],
            "Explicit local Docker build (review .dockerignore before invoking)",
            cwd=str(campaign.project), timeout=1200)
        if not result["success"]:
            raise RuntimeError(result["stderr"])
        return result
    if action == "budget":
        return campaign.budget()
    if action == "cancel":
        for job_id in args.job_ids.split(","):
            campaign.cancel(job_id.strip())
        return {"status": "cancellation_requested"}
    if action in ("logs", "errors"):
        attempts = campaign.state.attempts()
        chosen = [a for a in attempts if (args.job_id and a["id"] == args.job_id)
                  or (args.experiment_id and json.loads(a["spec"])["experiment_id"] == args.experiment_id)]
        if len(chosen) != 1:
            raise ValueError("Select exactly one attempt using --job-id")
        spec = json.loads(chosen[0]["spec"])
        path = Path(spec["root"]) / ("stdout.log" if action == "logs" else "stderr.log")
        return {"job_id": chosen[0]["id"],
                "output": "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-args.lines:])}
    if action in ("sync", "pull", "push-image", "verify-image", "ls"):
        raise ValueError(f"{action} is a SLURM-only operation; local outputs are already on this machine")
    raise ValueError(f"Unsupported local command: {action}")
