from dataclasses import asdict
import json
from pathlib import Path
import sys

import psutil
import pytest

from labgoblin.config import Config
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchEnvelope, Resources, identifier
from labgoblin.runtime import freeze_runtime, initialize
from labgoblin.scheduler import MachineSample, ResourceLedger
from labgoblin.storage import hash_file, publish_json


@pytest.fixture
def ledger(tmp_path):
    cpus = tuple(psutil.Process().cpu_affinity()[:2])
    sample = MachineSample(cpus, 32768, 32768)
    value = ResourceLedger.create(tmp_path / "machine.db", sampler=lambda *_: sample)
    return value


@pytest.fixture
def project(tmp_path, ledger):
    root = tmp_path / "project"
    config = Config(model="auto", reasoning_effort="medium",
                    cpus=1, memory_mb=1024, ledger=str(ledger.path), max_ai_credits=300,
                    timeout_seconds=60, coach=False, consultant=False, closure_critic=False,
                    coach_model=None, closure_critic_model=None, advice_model=None, claim_review_model=None,
                    archive_workspace=True)
    record = initialize(root, config, goal="Determine whether adding one increases the mean by one.",
                        research_venv=False, git=False)
    return root, config, record


def arm(project, ledger, *, coach=False, consultant=False, allowance=300_000_000_000, launcher=None, ideas=False,
        checkins=False, appointment=None, reviewed=None):
    root, config, record = project
    config = Config.parse({**config.to_dict(), "coach": coach, "consultant": consultant})
    state, run_id = root / ".labgoblin", identifier()
    directory = state / "runs" / run_id
    directory.mkdir()
    frozen = freeze_runtime(state)
    prompt = {"goal": "Answer the original question", "instructions": "Research carefully",
              "goal_prompt": "Answer the original question", "provider_command": ["copilot"]}
    if ideas:
        prompt["operator_ideas_version"] = 1
    if checkins:
        prompt["research_checkins_version"] = 1
    if appointment is not None:
        prompt["supervisor_review"] = {"version": 1, "due_at": appointment}
    if reviewed is not None:
        prompt["supervisor_reviewed"] = reviewed
    if config.closure_critic:
        prompt["closure_response_version"] = 1
    environment = None
    if "research_environment" in record:
        from labgoblin.research_environment import inspect
        environment = inspect(root, record["research_environment"])
        prompt["research_environment"] = environment
    if config.evaluation_spec is not None:
        from labgoblin.evaluation import prepare
        prompt["evaluation"] = prepare(root, directory, config.evaluation_spec, config.max_snapshot_bytes,
                                       research_environment=environment)
    publish_json(directory / "prompt.json", prompt)
    launcher = launcher or own_handle(record["id"])
    grant = ledger.request(run_id, record["id"], run_id, "research-run", config,
                           {"kind": "research-run", "handle": launcher}, native=True)
    assert grant["state"] == "granted", grant
    request = json.loads(grant["request"])
    command = (sys.executable, "-I", "-B", str(Path(frozen["root"]) / "bootstrap.py"),
               "provider", str(directory / "envelope.json"))
    envelope = LaunchEnvelope(run_id, record["id"], str(root), str(directory), str(ledger.path), ledger.id,
                              command, Resources.parse(request["resources"]), tuple(request["cpu_ids"]),
                              config.to_dict(), allowance, launcher, frozen, hash_file(directory / "prompt.json"))
    publish_json(directory / "envelope.json", asdict(envelope))
    ledger.arm_consumer(envelope)
    return envelope
