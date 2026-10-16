from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil
import pytest

from labgoblin import credits, runtime
from labgoblin.processes import own_handle, process_state
from labgoblin.protocol import UncertainExecution
from labgoblin.storage import publish_json
from tests.conftest import arm
from tests.test_simple_runtime import fixture_provider


def wait_until(predicate, seconds=20):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for the exact owned fixture state")


def started(root):
    path = root / "fixture-rpc.jsonl"
    return path.exists() and '"session.send"' in path.read_text()


def test_dead_launcher_is_fenced_before_late_claim(project, ledger):
    root, config, record = project
    process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        handle = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(), "token": record["id"]}
        envelope = arm(project, ledger, launcher=handle)
    finally:
        process.terminate()
        process.wait(timeout=5)
    receipt = runtime.settle_dead(ledger, ledger.consumer_run(envelope.id))
    assert receipt.status == "not_started" and not receipt.executed
    assert ledger.grant(envelope.id)["state"] == "released"
    with pytest.raises(ValueError, match="matching armed grant"):
        ledger.claim_consumer(envelope, own_handle(envelope.id))
    budget = credits.summarize(ledger.runs(record["id"]), 300)
    assert budget["used_nano"] == 0 and budget["remaining"] == 300


def test_preclaim_recovery_rechecks_a_competing_claim(project, ledger, monkeypatch):
    root, config, record = project
    process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        handle = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(), "token": record["id"]}
        envelope = arm(project, ledger, launcher=handle)
    finally:
        process.terminate()
        process.wait(timeout=5)
    original = ledger.finish_consumer
    def raced(receipt):
        assert ledger.claim_consumer(envelope, own_handle(envelope.id))
        original(receipt)
    monkeypatch.setattr(ledger, "finish_consumer", raced)
    with pytest.raises(ValueError, match="committed supervisor"):
        runtime.settle_dead(ledger, ledger.consumer_run(envelope.id))
    assert ledger.grant(envelope.id)["state"] == "granted"
    assert ledger.consumer_run(envelope.id)["phase"] == "executing"
    assert not (Path(envelope.root) / "receipt.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Exact Windows Job Object retirement after supervisor death")
def test_supervisor_death_retires_tree_but_does_not_invent_a_final_bill(project, ledger, monkeypatch):
    root, config, record = project
    fixture_provider(root, monkeypatch, "wait")
    errors = []
    def interrupt():
        try:
            wait_until(lambda: started(root))
            row = ledger.runs(record["id"])[0]
            envelope = json.loads(row["envelope"])
            wait_until(lambda: (Path(envelope["root"]) / "usage-progress.json").exists())
            handle = json.loads(row["supervisor"])
            process = psutil.Process(handle["pid"])
            assert process.create_time() == handle["created"]
            process.kill()
            process.wait(timeout=5)
        except (OSError, ValueError, RuntimeError, AssertionError, psutil.Error) as error:
            errors.append(str(error))
            runtime.stop(root)
    thread = threading.Thread(target=interrupt)
    thread.start()
    try:
        result = runtime.run(root)
    finally:
        thread.join(timeout=25)
    assert not thread.is_alive() and not errors
    assert result["status"] == "interrupted"
    assert result["usage"] is None and result["executed"]
    assert ledger.grant(result["id"])["state"] == "released"
    assert runtime.status(root)["budget"]["uncovered_unknown"] == [result["id"]]
    with pytest.raises(UncertainExecution, match="Final credit usage"):
        runtime.run(root, resume=True)


def test_controller_death_does_not_duplicate_or_kill_the_independent_researcher(project, ledger):
    root, config, record = project
    publish_json(root / "fixture-provider.json", {
        "model_free": True, "root": str(root), "mode": "wait_for_release"})
    out_path = root.parent / "controller.stdout"
    err_path = root.parent / "controller.stderr"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        controller = subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("fixed_controller.py")),
                                       str(root)], stdout=out, stderr=err)
    try:
        wait_until(lambda: started(root))
        initial = ledger.runs(record["id"])
        assert len(initial) == 1
        supervisor = json.loads(initial[0]["supervisor"])
        controller.terminate()
        controller.wait(timeout=5)
        assert process_state(supervisor) == "alive"
        timer = threading.Timer(0.5, lambda: (root / "finish-fixture").touch())
        timer.start()
        try:
            result = runtime.run(root)
        finally:
            timer.join()
        assert result["id"] == initial[0]["token"]
        assert result["status"] == "completed", (result, err_path.read_text())
        assert len(ledger.runs(record["id"])) == 1
        assert not ledger.rows()
    finally:
        if controller.poll() is None:
            controller.terminate()
            controller.wait(timeout=5)
        (root / "finish-fixture").touch(exist_ok=True)
