"""Opt-in real-SDK setup acceptance with a scripted operator and synthetic files.

Run with --live only when setup model calls are authorized. The eight-prompt
ceiling belongs to this test, not to the product's interactive setup.
"""

import argparse
import contextvars
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

from copilot import CopilotClient, CopilotSession
import psutil

from labgoblin import cli, initialization, setup_assistant as setup
from labgoblin.config import load_config, parse_config
from labgoblin.evidence import atomic_json
from labgoblin.processes import process_state
from labgoblin.protocol import canonical
from labgoblin.state import State


PRIVATE_BODY = "SYNTHETIC_DATA_BODY_NOT_FOR_MODEL"
CURRENT_TOOL = contextvars.ContextVar("setup_acceptance_tool", default=None)


def inventory(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class Transcript:
    def __init__(self, root):
        self.root = root
        self.case = ""
        self.events = []
        with sqlite3.connect(root / "calls.db") as conn:
            conn.execute("CREATE TABLE calls(id INTEGER PRIMARY KEY, scenario TEXT, session TEXT, digest TEXT, created REAL)")

    def record(self, kind, value):
        event = {"case": self.case, "kind": kind, "value": value}
        self.events.append(event)
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def reserve(self, session, prompt):
        with sqlite3.connect(self.root / "calls.db") as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= 8:
                raise RuntimeError("The test's persisted eight-prompt allowance is exhausted")
            conn.execute("INSERT INTO calls(scenario,session,digest,created) VALUES(?,?,?,?)",
                         (self.case, session, hashlib.sha256(prompt.encode()).hexdigest(), time.time()))
        self.record("user_prompt", prompt)


class TTYCapture(io.StringIO):
    def isatty(self):
        return True


class Operator:
    def __init__(self, project, trace, *, cancel):
        self.project, self.trace, self.cancel = project, trace, cancel
        self.host = None
        self.corrected = False
        self.reviewed = False
        self.followups = 0
        self.output = []
        self.approvals = []

    def write(self, value):
        self.output.append(str(value))

    @property
    def pairs(self):
        return 5 if self.corrected else 3

    def intent(self):
        count = "five" if self.corrected else "three"
        return (
            "This is a synthetic setup rehearsal, not authorization to execute research. "
            "Create a campaign named Goblin arithmetic rehearsal. The future question is "
            "whether adding one to each member of the synthetic vector [2,4,6] shifts its mean "
            f"by one. No results have been measured. Plan exactly {count} paired replications "
            "of baseline versus +1, checking mean difference 1 in each pair; stop after assessing "
            f"all {count} pairs and state that this supports arithmetic only, not scientific generalization. "
            f"Include the exact sentence 'Plan exactly {count} paired replications.' in the protocol. "
            "Keep the starter native runner and its exact Python executable, campaign CPU/RAM "
            "and agent resource settings. Set execution.source_files to ['existing_experiment.py']. "
            f"Map inputs.SAMPLE to {str(self.project / 'sample.csv')!r}, identity 'synthetic-unverified', "
            "assurance 'declared', prompt_access false, no invented SHA. Research provider copilot, "
            "model and reasoning_effort both empty (provider defaults), timeout_seconds 120, retries 0. "
            f"Campaign max_seconds {1200 if self.corrected else 900}, "
            f"max_invocations {4 if self.corrected else 6}, max_jobs 1, no GPUs or GPU-hours. "
            "Include these exact operator constraints: 'No network access by experiments.' and "
            "'Do not expose raw input contents to research prompts.' "
            "Preserve unrelated existing instruction text. Do not generate experiment code or manifests. "
            "Use select_context to request the project folder, read only setup-brief.md with approval "
            "and inspect sample.csv metadata only. Propose the complete draft using propose_draft, "
            "then request the fixed native runner readiness check and the provider help check. "
            "I will approve the native check and decline the provider check; do not repeat a declined check "
            "or treat it as verified. No other readiness probes. All choices are supplied; "
            "leave unspecified facts unknown and stop for my /review when ready."
        )

    def problems(self):
        draft = self.host.draft
        config = parse_config(draft.configuration, self.project / "labgoblin.toml")
        problems = []
        expected = {
            "project name": (config.project.name, "Goblin arithmetic rehearsal"),
            "source files": (config.execution.source_files, ("existing_experiment.py",)),
            "research provider": (config.agent.provider, "copilot"),
            "research model": (config.agent.model, ""),
            "research effort": (config.agent.reasoning_effort, ""),
            "provider deadline": (config.agent.timeout_seconds, 120),
            "provider retries": (config.agent.retries, 0),
            "campaign seconds": (config.campaign.max_seconds.value, 1200 if self.corrected else 900),
            "campaign invocations": (config.campaign.max_invocations.value, 4 if self.corrected else 6),
            "campaign CPU": (config.campaign.resources.cpus, 2),
            "campaign RAM": (config.campaign.resources.memory_mb, 4096),
            "campaign GPUs": (config.campaign.resources.gpus, ()),
            "campaign jobs": (config.campaign.max_jobs, 1),
            "campaign GPU-hours": (config.campaign.max_gpu_hours, 0),
            "native Python": (config.runners[config.execution.default_runner].python, sys.executable),
        }
        for label, (actual, wanted) in expected.items():
            if actual != wanted:
                problems.append(f"{label}: expected {wanted!r}, got {actual!r}")
        sample = config.inputs.get("SAMPLE")
        if (not sample or Path(sample.path).resolve() != self.project / "sample.csv"
                or sample.prompt_access or sample.sha256 or sample.assurance != "declared"
                or sample.identity != "synthetic-unverified"):
            problems.append("SAMPLE must reference the supplied CSV, declared and not exposed to prompts; no invented hash")
        if not draft.goal or not draft.protocol:
            problems.append("Supply both a research goal and an evaluation protocol")
        elif f"Plan exactly {'five' if self.corrected else 'three'} paired replications." not in draft.protocol:
            problems.append("Include this exact protocol sentence: "
                            f"Plan exactly {'five' if self.corrected else 'three'} paired replications.")
        for constraint in ("No network access by experiments.", "Do not expose raw input contents to research prompts."):
            if constraint not in draft.constraints:
                problems.append(f"Include exact operator constraint: {constraint}")
        if not any(item["check"] == "runner" and item["status"] == "verified" for item in self.host.checks):
            problems.append("After the final proposal, request the fixed native runner readiness check")
        if not any(item["check"] == "provider" and item["status"] == "declined" for item in self.host.checks):
            problems.append("After the final proposal, request provider help so I can explicitly decline it")
        events = [event for event in self.trace.events if event["case"] == self.trace.case]
        for tool in ("read_context", "input_metadata"):
            if not any(event["kind"] == "tool_result" and event["value"]["tool"] == tool for event in events):
                problems.append(f"Complete the requested {tool} operation with its explicit scope")
        return problems

    async def ask(self, prompt):
        self.trace.record("operator_question", prompt)
        assert not (self.project / ".labgoblin").exists(), "Campaign was created before final approval"
        if prompt.startswith("What would you like"):
            answer = self.intent()
        elif "Enter a file/folder" in prompt:
            answer = str(self.project)
        elif prompt.startswith("Create exactly this proposal?"):
            assert not self.problems(), self.problems()
            self.reviewed = True
            atomic_json(self.project.parent / "reviewed.json", {
                "draft": asdict(self.host.draft), "checks": self.host.checks,
                "preview": "".join(self.output), "action": "cancel" if self.cancel else "apply"})
            answer = "No" if self.cancel else prompt.split("Type ", 1)[1].split(",", 1)[0]
        elif prompt.startswith("Correction,"):
            assert self.cancel
            answer = "/cancel"
        elif prompt.startswith("You ("):
            problems = self.problems()
            if problems:
                self.followups += 1
                if self.followups > 2:
                    raise RuntimeError("The live setup did not satisfy explicit choices: " + "; ".join(problems))
                answer = "Please correct/finish these points before I review:\n" + "\n".join(problems)
            elif not self.cancel and not self.corrected:
                self.corrected = True
                answer = (
                    "Correction: make this exactly FIVE paired replications, not three, and reflect that in "
                    "the goal, protocol and stopping criterion. Include the exact protocol sentence "
                    "'Plan exactly five paired replications.' Change campaign max_invocations to 4 and "
                    "max_seconds to 1200. All other choices and exact constraints stay unchanged. "
                    "Call propose_draft with the corrected complete draft, then perform the native "
                    "readiness check and offer the provider check again for this new proposal "
                    "(I will decline it). Leave the research model/effort empty. Then stop for /review."
                )
            else:
                answer = "/review"
        else:
            answer = "All choices are supplied; retain unknown facts explicitly. " + (
                f"The current protocol is exactly {self.pairs} paired replications. " + self.intent())
        self.trace.record("operator_answer", answer)
        return answer

    async def approve(self, prompt):
        request = CURRENT_TOOL.get()
        approved = False
        if request and request["tool"] == "read_context":
            approved = Path(request["arguments"]["path"]).resolve() == self.project / "setup-brief.md"
        elif request and request["tool"] == "check_readiness" and request["arguments"]["check"] == "runner":
            config = parse_config(self.host.draft.configuration, self.project / "labgoblin.toml")
            runner = config.runners[request["arguments"].get("runner", config.execution.default_runner)]
            approved = runner.kind == "native" and runner.python == sys.executable
        self.approvals.append({"prompt": prompt, "approved": approved})
        self.trace.record("permission", self.approvals[-1])
        return approved


def scenario(root, trace, *, cancel):
    root.mkdir()
    project = root / "campaign"
    project.mkdir()
    (project / ".github").mkdir()
    (project / "setup-brief.md").write_text(
        "# Synthetic fixture\n\nThis is an arithmetic-only setup rehearsal. "
        "The audit label is COPPER-KITE. No observations or experiments exist yet.\n", encoding="utf-8")
    (project / "sample.csv").write_text("value\n2\n4\n6\n" + PRIVATE_BODY, encoding="utf-8")
    (project / "existing_experiment.py").write_text("raise SystemExit('Not executed during setup')\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("# Human rules\nPreserve copper-kite instructions.\n", encoding="utf-8")
    (project / ".github" / "copilot-instructions.md").write_text("# Existing Copilot rules\nKeep this section.\n", encoding="utf-8")
    (project / ".gitignore").write_text("*.scratch\n", encoding="utf-8")
    before = inventory(project)
    operator = Operator(project, trace, cancel=cancel)
    runtime_handles = []
    base_tools, original_send, original_create = setup.SetupTools, CopilotSession.send, CopilotClient.create_session

    class ObservedTools(base_tools):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            operator.host = self

        async def invoke(self, name, arguments):
            request = {"tool": name, "arguments": arguments}
            token = CURRENT_TOOL.set(request)
            trace.record("tool_request", request)
            try:
                result = await super().invoke(name, arguments)
                trace.record("tool_result", {"tool": name, "result": result})
                return result
            except (ValueError, OSError, RuntimeError) as error:
                trace.record("tool_error", {"tool": name, "error": str(error)})
                raise
            finally:
                CURRENT_TOOL.reset(token)

    async def send(session, prompt, **kwargs):
        trace.reserve(session.session_id, prompt)
        return await original_send(session, prompt, **kwargs)

    async def create(client, **kwargs):
        session = await original_create(client, **kwargs)
        trace.record("session_created", session.session_id)
        for process in psutil.Process().children(recursive=True):
            runtime_handles.append({"pid": process.pid, "created": process.create_time()})
        trace.record("runtime_handles", runtime_handles)

        def event(event):
            if event.type.value == "assistant.message":
                trace.record("assistant_message", event.data.content)
            elif event.type.value == "assistant.usage":
                trace.record("usage", {key: getattr(event.data, key, None)
                                      for key in ("model", "input_tokens", "output_tokens", "cost")})

        session.on(event)
        return session

    output, errors = TTYCapture(), io.StringIO()
    with (patch.object(setup, "Terminal", return_value=operator),
          patch.object(setup, "SetupTools", ObservedTools),
          patch.object(CopilotSession, "send", send),
          patch.object(CopilotClient, "create_session", create),
          patch.object(sys.stdin, "isatty", return_value=True),
          redirect_stdout(output), redirect_stderr(errors)):
        code = cli.main(["init", "--project", str(project), "--agent", "copilot",
                         "--ledger", str(root / "unused-machine.db"), "--install-copilot-instructions"])
    result = {"exit_code": code, "stdout": output.getvalue(), "stderr": errors.getvalue(),
              "reviewed": operator.reviewed, "correction": operator.corrected,
              "permissions": operator.approvals, "files": inventory(project)}
    atomic_json(root / "cli-result.json", result)
    deadline = time.monotonic() + 10
    while any(process_state(handle) != "dead" for handle in runtime_handles) and time.monotonic() < deadline:
        time.sleep(0.1)
    cleanup = [{**handle, "state": process_state(handle)} for handle in runtime_handles]
    atomic_json(root / "runtime-cleanup.json", cleanup)
    assert cleanup and all(handle["state"] == "dead" for handle in cleanup), cleanup
    assert operator.reviewed, result
    assert not (root / "unused-machine.db").exists()
    assert not initialization.initialization_marker(project).exists()
    if cancel:
        assert code == 130, result
        assert inventory(project) == before, "Preview/cancel changed campaign files"
    else:
        assert code == 0, result
        status = json.loads(output.getvalue())
        assert status["assisted"] and operator.corrected
        config = load_config(project)
        assert config == parse_config(operator.host.draft.configuration, project / "labgoblin.toml")
        for name in ("setup-brief.md", "sample.csv", "existing_experiment.py"):
            assert inventory(project)[name] == before[name]
        assert "Preserve copper-kite" in (project / "CLAUDE.md").read_text()
        assert "Keep this section." in (project / ".github" / "copilot-instructions.md").read_text()
        state = State.open(project / ".labgoblin")
        assert state.campaign()["invocations"] == 0 and state.campaign()["operator_mode"] == "ready"
        with state.db.read() as conn:
            for name in ("attempts", "turns", "invocations", "allocations", "launches"):
                assert conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
            sources = {row["kind"]: dict(row) for row in conn.execute("SELECT * FROM sources")}
            assert {"goal", "protocol", "setup_approval"}.issubset(sources)
            assert "handoff" not in sources
            for kind, body in (("goal", operator.host.draft.goal), ("protocol", operator.host.draft.protocol)):
                assert sources[kind]["origin"] == "setup-assistant"
                assert sources[kind]["body"].decode() == body
                metadata = json.loads(sources[kind]["metadata"])
                assert metadata["operator_approved"] and metadata["approval"] == status["approval"]
            assert json.loads(sources["setup_approval"]["body"]) == {
                "digest": status["approval"], "constraints": list(operator.host.draft.constraints)}
    assert PRIVATE_BODY not in canonical(trace.events).decode(), "Dataset contents escaped metadata-only scope"
    return {"project": str(project), "cancelled": cancel, "corrected": operator.corrected,
            "research_invocations": 0, "readiness": operator.host.checks, "runtime_cleanup": cleanup}


def run(root):
    root.mkdir(parents=True, exist_ok=False)
    trace = Transcript(root)
    results, failures = [], []
    for name, cancel in (("cancel", True), ("apply", False)):
        trace.case = name
        print(f"Running live SDK setup scenario: {name}", flush=True)
        try:
            results.append(scenario(root / name, trace, cancel=cancel))
        except (AssertionError, OSError, ValueError, RuntimeError) as error:
            trace.record("scenario_failure", f"{type(error).__name__}: {error}")
            failures.append({"scenario": name, "error": f"{type(error).__name__}: {error}"})
    with sqlite3.connect(root / "calls.db") as conn:
        calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    result = {"scenarios": results, "failures": failures, "sdk_prompts": calls,
              "boundary": "Real SDK/model/tools/CLI/publication; scripted operator input, not physical keyboard automation"}
    atomic_json(root / "result.json", result)
    print(json.dumps(result, indent=2))
    if failures:
        raise RuntimeError("Setup acceptance failed; inspect result.json and events.jsonl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live requires explicit authorization for setup model calls")
    run(args.root.resolve())
