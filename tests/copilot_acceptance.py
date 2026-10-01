"""Explicit four-invocation synthetic acceptance; never discovered as a live test.

Run `python -m tests.copilot_acceptance --root NEW_PATH` for the provider double.
Add `--live` only with an explicitly authorized four-invocation allowance.
All launches, including ambiguous ones, consume a durable slot before spawning.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback

import tomli_w

# Model-free help probes intentionally strip PYTHONPATH before launching this script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labgoblin.campaign import Campaign
from labgoblin.cli import INSTRUCTIONS
from labgoblin.config import ChatSettings, initial_config, parse_config
from labgoblin.dashboard_chat import SDKObserver
from labgoblin.dashboard_data import EvidenceReader
from labgoblin.evidence import atomic_json
from labgoblin.protocol import canonical
from labgoblin.scheduler import ResourceLedger
from labgoblin.state import State


GOAL = """# Synthetic finite acceptance study

Determine whether the synthetic baseline score reproduces under the deliberately
different replication setting. This is a tiny harness exercise, not a scientific
generalization. No network/data dependencies, installs, other providers or agents.

First research turn: use the packet's cli_argv to batch-submit batch.json. The two
prewritten requests are the entire allowed study. Return an owned wait handoff;
do not execute the programs directly or wait for them. Explain why one baseline
alone cannot establish reproducibility.

Second research turn: the baseline will have finished, while the ALREADY ADMITTED
replication waits at a controller-owned barrier. Assess the delivered baseline
evidence using exact event/observation IDs and request finalize, without waiting.
The stopping criterion is assessment of BOTH admitted conditions, including any
late contradictory result and all limitations. Do not touch the barrier.

Final analysis: assess the complete sealed inventory, explicitly compare both
scores, and include the required assessment object and synthetic-study limitations.
Do not submit more work, request maintenance, or reopen research.
"""

EXPERIMENT = """import json,os,sys,time
from pathlib import Path
output=Path(os.environ['LABGOBLIN_OUTPUT_DIR'])
if sys.argv[1]=='999':
    (output/'ready').touch()
    deadline=time.monotonic()+420
    while not (output/'release').exists():
        if time.monotonic()>deadline: raise TimeoutError('finite replication barrier expired')
        time.sleep(.05)
(output/'metrics.json').write_text(json.dumps({'score':int(sys.argv[1])}),encoding='utf-8')
"""


def reserve_call(path, kind, arguments):
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        counts = dict(conn.execute("SELECT kind,COUNT(*) FROM calls GROUP BY kind"))
        if sum(counts.values()) >= 4 or counts.get(kind, 0) >= {"research": 3, "observer": 1}[kind]:
            raise RuntimeError("The persisted four-call validation allowance is exhausted")
        cursor = conn.execute("INSERT INTO calls(kind,created,digest) VALUES(?,?,?)",
                              (kind, time.time(), hashlib.sha256(canonical(arguments)).hexdigest()))
        return cursor.lastrowid


def provider_double(arguments):
    from tests.test_providers import HELP
    if "--help" in arguments:
        print(HELP)
        return 0
    prompt = arguments[arguments.index("-p") + 1]
    packet_path, result_path = [json.loads(value) for value in re.findall(r'"(?:\\.|[^"\\])*"', prompt)]
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    final = packet["kind"] == "final_analysis"
    first = not (Path(packet["project"]) / "double-submitted").exists()
    if first:
        subprocess.run([*packet["cli_argv"], "batch-submit", "--file", "batch.json", "--json"],
                       cwd=packet["project"], check=True, timeout=30,
                       env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
        Path(packet["project"], "double-submitted").touch()
    result = dict(
        turn_id=packet["turn_id"], packet_id=packet["packet_id"], summary="Synthetic finite comparison",
        rationale="One baseline alone cannot establish reproducibility; retain the admitted replication.",
        next_step="Wait for admitted evidence." if first else "Assess the sealed inventory.",
        disposition="wait" if first else "finalize", reason="Synthetic comparison, not generalization.",
        evidence=[dict(event_id=event["id"], disposition="assessed", reason="Synthetic evidence read.",
                       references=[item["id"] for item in event["observations"]])
                  for event in packet["events"]],
        stopping_criterion="Assess both admitted conditions and their limitations.")
    if final:
        inventory = packet["inventory"]
        scores = sorted(observation["metrics"]["score"] for attempt in inventory["attempts"]
                        for observation in attempt["observations"] if "score" in observation["metrics"])
        result["rationale"] = f"Scores {scores}: replication contradicts invariance."
        result["assessment"] = dict(
            view_id=inventory["id"], inventory_digest=inventory["metadata"]["inventory_digest"],
            assess_all=True, exclusions=[], limitations="Two deliberately different synthetic conditions only.")
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    return 0


def forward(configuration, arguments):
    settings = json.loads(Path(configuration).read_text(encoding="utf-8"))
    if "-p" in arguments:
        reserve_call(settings["ledger"], "research", arguments)
    elif "--help" not in arguments:
        raise ValueError("Validation wrapper permits only help or one explicit prompt")
    if settings["live"]:
        return subprocess.call([settings["binary"], *arguments])
    return provider_double(arguments)


def until(action, predicate, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = action()
        if result.get("errors"):
            raise RuntimeError(json.dumps(result["errors"]))
        if predicate():
            return
        time.sleep(.1)
    raise TimeoutError("Synthetic acceptance did not reach its required owned state")


def database_snapshot(state):
    with state.db.read() as conn:
        return list(conn.iterdump())


def run(root, *, live=False):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    calls = root / "validation-calls.db"
    with sqlite3.connect(calls) as conn:
        conn.execute("CREATE TABLE calls(slot INTEGER PRIMARY KEY,kind TEXT NOT NULL,created REAL NOT NULL,digest TEXT NOT NULL)")
    binary = shutil.which("copilot")
    if live and not binary:
        raise RuntimeError("Live validation requires an already installed and authenticated Copilot CLI")
    wrapper = root / "provider.json"
    atomic_json(wrapper, {"ledger": str(calls), "live": live, "binary": binary})
    project = root / "project"
    project.mkdir()
    project.joinpath("research_goal.md").write_text(GOAL, encoding="utf-8")
    project.joinpath("CLAUDE.md").write_text(INSTRUCTIONS, encoding="utf-8")
    project.joinpath("experiment.py").write_text(EXPERIMENT, encoding="utf-8")
    batch = [{"key": name, "argv": ["python", "experiment.py", str(score)],
              "source_files": ["experiment.py"], "cpus": 1, "memory_mb": 128,
              "seconds": 450, "artifacts": ["metrics.json"]}
             for name, score in (("baseline", 42), ("replication", 999))]
    atomic_json(project / "batch.json", batch)
    raw = initial_config("bounded-copilot-acceptance", "copilot")
    raw["campaign"]["max_invocations"] = 3
    raw["agent"].update(
        command=[sys.executable, str(Path(__file__).resolve()), "--forward", str(wrapper),
                 "--allow-all-tools", "--disable-builtin-mcps", "--no-custom-instructions"],
        timeout_seconds=180 if live else 20, retries=0, resources={"cpus": 1, "memory_mb": 2048})
    config = parse_config(raw, project / "labgoblin.toml")
    project.joinpath("labgoblin.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")
    ledger = ResourceLedger.create(root / "machine.db")
    ledger.configure(2, 4096, (), 256)
    state = State.create(config, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    state.source("goal", GOAL.encode(), origin="operator", head="goal")
    fallback = state.source("journal_import", b"One baseline alone cannot establish reproducibility.",
                            origin="synthetic-validation")
    results = {"live": live, "research": None, "observer": None, "errors": []}
    replication_output = None
    try:
        with Campaign(config, state=state, ledger=ledger) as controller:
            controller.step()
            until(controller.reconcile, lambda: state.campaign()["progress"] == "wait" and not state.active_launches())
            assert len(state.attempts()) == 2 and state.campaign()["invocations"] == 1
            attempts = {value["idempotency_key"]: value for value in state.attempts()}
            baseline, replication = attempts["baseline"], attempts["replication"]
            replication_output = Path(json.loads(replication["spec"])["output"])
            until(lambda: controller.step(no_agent=True),
                  lambda: state.collection(baseline["id"])["collection"] == "complete"
                  and (replication_output / "ready").exists())
            controller.step()
            until(controller.reconcile, lambda: state.campaign()["generation_state"] == "sealed"
                  and not controller._turn())
            assert state.attempt(replication["id"])["status"] in ("starting", "running")
            assert state.campaign()["invocations"] == 2
            replication_output.joinpath("release").touch()
            until(controller.reconcile, lambda: not state.active_launches() and not ledger.rows())
            until(controller.step, lambda: state.campaign()["generation_state"] == "closed")
        assert state.campaign()["research_outcome"] == "assessed"
        with state.db.read() as conn:
            scores = sorted(json.loads(row[0])["metrics"]["score"] for row in conn.execute(
                "SELECT metadata FROM observations WHERE kind='metrics'"))
            assert scores == [42, 999]
            assert conn.execute("SELECT COUNT(*) FROM turns WHERE kind='final_analysis'").fetchone()[0] == 1
        results["research"] = {"scores": scores, "campaign": state.campaign()}
    except (OSError, ValueError, RuntimeError, AssertionError, sqlite3.Error) as error:
        results["errors"].append({"phase": "research", "error": str(error), "traceback": traceback.format_exc()})
    finally:
        for attempt in state.attempts():
            output = Path(json.loads(attempt["spec"])["output"])
            if output.exists():
                (output / "release").touch()
        try:
            until(Campaign(state=state, ledger=ledger).reconcile,
                  lambda: not state.active_launches() and not ledger.rows(), timeout=210)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            results["errors"].append({"phase": "drain", "error": str(error), "traceback": traceback.format_exc()})
    try:
        with state.db.read() as conn:
            old = conn.execute("SELECT source_id FROM handoffs ORDER BY created LIMIT 1").fetchone()
            ids = [row[0] for row in conn.execute("SELECT id FROM observations WHERE kind='metrics' ORDER BY created")]
        source_id = old[0] if old else fallback
        question = (f"Read campaign_status, search archive_search for 'baseline', retrieve source_entry {source_id}, "
                    f"and evidence_observation for each of {ids}. Explain the older rationale and compare the exact "
                    "captured scores, cite source revisions, and distinguish synthetic conclusions from generalization.")
        before = database_snapshot(state)
        emissions = []
        reserve_call(calls, "observer", [question])
        if live:
            driver = SDKObserver()
            answer = asyncio.run(driver.answer(
                ChatSettings(cli_path=binary, cpus=1, memory_mb=2048, max_invocations=1, timeout_seconds=180),
                EvidenceReader(config.config_path), question, lambda kind, value: emissions.append((kind, value))))
            tools = {value["name"] for kind, value in emissions if kind == "tool"}
            assert {"campaign_status", "archive_search", "source_entry"} <= tools, tools
            if ids:
                assert "evidence_observation" in tools, tools
            assert driver.usage()["committed"] == 1
        else:
            reader = EvidenceReader(config.config_path)
            answer = {"source": reader.read("source_entry", {"id": source_id}),
                      "evidence": [reader.read("evidence_observation", {"id": value}) for value in ids]}
        assert before == database_snapshot(state)
        assert not ledger.rows()
        results["observer"] = {"answer": answer, "events": emissions}
    except (OSError, ValueError, RuntimeError, AssertionError, sqlite3.Error) as error:
        results["errors"].append({"phase": "observer", "error": str(error), "traceback": traceback.format_exc()})
    with sqlite3.connect(calls) as conn:
        results["calls"] = list(conn.execute("SELECT slot,kind,created,digest FROM calls ORDER BY slot"))
    results["remaining_grants"] = ledger.rows()
    atomic_json(root / "result.json", results)
    return results


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--forward":
        raise SystemExit(forward(sys.argv[2], sys.argv[3:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    result = run(args.root, live=args.live)
    print(json.dumps({"calls": len(result["calls"]), "errors": result["errors"],
                      "remaining_grants": len(result["remaining_grants"])}, indent=2))
    raise SystemExit(bool(result["errors"] or result["remaining_grants"]))
