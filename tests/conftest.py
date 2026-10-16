from dataclasses import asdict
import json
from pathlib import Path
import sys

import psutil
import pytest

from labgoblin.config import Config
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchEnvelope, identifier
from labgoblin.runtime import freeze_runtime, initialize
from labgoblin.scheduler import MachineSample, ResourceLedger
from labgoblin.storage import hash_file, publish_json


@pytest.fixture
def ledger(tmp_path):
    cpus = tuple(psutil.Process().cpu_affinity()[:2])
    sample = MachineSample(cpus, 32768, 32768)
    value = ResourceLedger.create(tmp_path / "machine.db", sampler=lambda *_: sample)
    value.configure(len(cpus), 8192, headroom_mb=0)
    return value


@pytest.fixture
def project(tmp_path, ledger):
    root = tmp_path / "project"
    config = Config(cpus=1, memory_mb=1024, ledger=str(ledger.path), max_ai_credits=300,
                    timeout_seconds=60, coach=False)
    record = initialize(root, config, goal="Determine whether adding one increases the mean by one.")
    return root, config, record


def arm(project, ledger, *, coach=False, consultant=False, allowance=300_000_000_000, launcher=None):
    root, config, record = project
    config = Config.parse({**config.to_dict(), "coach": coach, "consultant": consultant})
    state, run_id = root / ".labgoblin", identifier()
    directory = state / "runs" / run_id
    directory.mkdir()
    frozen = freeze_runtime(state)
    prompt = {"goal": "Answer the original question", "instructions": "Research carefully",
              "goal_prompt": "Answer the original question", "provider_command": ["copilot"]}
    publish_json(directory / "prompt.json", prompt)
    launcher = launcher or own_handle(record["id"])
    ledger.request(run_id, record["id"], run_id, "research-run", config.resources,
                   {"kind": "research-run", "handle": launcher}, native=True)
    grant = ledger.reserve(run_id)
    assert grant["state"] == "granted", grant
    command = (sys.executable, "-I", "-B", str(Path(frozen["root"]) / "bootstrap.py"),
               "provider", str(directory / "envelope.json"))
    envelope = LaunchEnvelope(run_id, record["id"], str(root), str(directory), str(ledger.path), ledger.id,
                              command, config.resources, tuple(json.loads(grant["native_cpus"])),
                              config.to_dict(), allowance, launcher, frozen, hash_file(directory / "prompt.json"))
    publish_json(directory / "envelope.json", asdict(envelope))
    ledger.arm_consumer(envelope)
    return envelope
