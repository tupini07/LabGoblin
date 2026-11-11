import asyncio
from dataclasses import asdict, replace
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import venv
import zipfile

import pytest

from labgoblin import evaluation, runtime
from labgoblin.cli import main
from labgoblin.inspection_evidence import Evidence
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchReceipt, UncertainExecution
from labgoblin.storage import atomic_json, publish_json, read_json
from tests.conftest import arm


@pytest.fixture
def kit(tmp_path):
    root = tmp_path / "external-kit"
    shutil.copytree(Path(__file__).parents[1] / "examples" / "evaluation", root)
    return root


def admitted(project, ledger, kit):
    root, config, record = project
    config = replace(config, evaluation_spec=str(kit / "spec.json"))
    runtime.bound_ledger(root / ".labgoblin", config=config, bind=True)
    envelope = arm((root, config, record), ledger)
    candidate = root / "candidate"
    if not candidate.exists():
        shutil.copytree(kit / "candidate", candidate)
    return envelope, evaluation.Evaluator(envelope, lambda: time.monotonic() + 90)


def develop(evaluator, call="development"):
    return evaluator.evaluate(call, {"stage": "development", "candidate": "candidate"})


def confirm(evaluator, selected, call="confirmation"):
    return evaluator.evaluate(call, {"stage": "confirmation", "source_run": selected["run"],
                                     "source_check": selected["check"]})


def settle(envelope, ledger):
    supervisor = own_handle(envelope.id)
    assert ledger.claim_consumer(envelope, supervisor)
    receipt = LaunchReceipt(envelope.id, envelope.digest, "completed", 1, True, returncode=0,
                            usage={"totalNanoAiu": 0},
                            metadata={"supervisor": supervisor, "evaluations": evaluation.manifest(envelope.root)})
    publish_json(Path(envelope.root) / "receipt.json", asdict(receipt))
    ledger.finish_consumer(receipt)
    return receipt


def test_frozen_selection_reality_discrepancy_and_regression(project, ledger, kit):
    envelope, evaluator = admitted(project, ledger, kit)
    candidate = Path(envelope.project) / "candidate" / "model.json"
    atomic_json(candidate, {"lookup": {"0": 1, "1": 3}, "fallback": 0})
    selected = develop(evaluator)
    assert selected["summary"]["candidate_passed"] == 2
    assert selected["summary"]["baseline_passed"] == 0
    original = (Path(envelope.root) / "evaluations" / selected["check"] / "request.json").read_bytes()
    atomic_json(candidate, {"slope": 2, "intercept": 1})
    checked = confirm(evaluator, selected)
    assert checked["status"] == "completed" and checked["summary"]["candidate_passed"] == 0
    assert checked["candidate_sha256"] == selected["candidate_sha256"]
    assert "expected=-3; predicted=0" in checked["comparisons"][0]["candidate"]["diagnostic"]
    replayed = evaluator.evaluate("regression", {
        "stage": "regression", "candidate": "candidate",
        "source_run": envelope.id, "source_check": checked["check"]})
    assert replayed["summary"]["candidate_passed"] == 2
    assert replayed["stage"] == "regression" and replayed["source"]["check"] == checked["check"]
    assert (Path(envelope.root) / "evaluations" / selected["check"] / "request.json").read_bytes() == original
    with pytest.raises(ValueError, match="No unexposed"):
        confirm(evaluator, selected, "cannot-reuse")
    assert confirm(evaluator, selected) == checked
    with pytest.raises(ValueError, match="reused with different"):
        evaluator.evaluate("development", {"stage": "development", "candidate": "."})


@pytest.mark.parametrize("cpus,memory_mb", [
    ("unlimited", 1024), (1, "unlimited"), ("unlimited", "unlimited"),
])
def test_paired_evaluation_preserves_optional_parent_limits(project, ledger, kit, monkeypatch, cpus, memory_mb):
    root, config, record = project
    envelope, evaluator = admitted((root, replace(config, cpus=cpus, memory_mb=memory_mb), record), ledger, kit)
    original, calls = evaluation.payload.run_process, []
    def run(spec):
        assert spec["cpus"] == (None if cpus == "unlimited" else cpus)
        assert spec["memory_mb"] == (None if memory_mb == "unlimited" else memory_mb)
        assert spec["cpu_ids"] == (None if cpus == "unlimited" else list(envelope.cpu_ids))
        calls.append(spec["token"])
        return original(spec)
    monkeypatch.setattr(evaluation.payload, "run_process", run)
    result = develop(evaluator)
    assert calls and result["status"] == "completed"
    assert result["summary"]["candidate_passed"] == 2


def test_external_edits_do_not_change_admitted_kit(project, ledger, kit):
    envelope, evaluator = admitted(project, ledger, kit)
    (kit / "evaluator" / "check.py").write_text("raise RuntimeError('not admitted')", encoding="utf-8")
    atomic_json(kit / "development.json", [{"id": "changed", "data": {"x": 0, "expected": 999}}])
    assert develop(evaluator)["summary"]["candidate_passed"] == 2
    frozen = Path(envelope.root) / "evaluation-definition" / "baseline" / "files" / "model.json"
    atomic_json(frozen, {"slope": 999, "intercept": 0})
    with pytest.raises(ValueError, match="capture bytes changed"):
        develop(evaluator, "tampered-baseline")


@pytest.mark.skipif(os.name == "nt", reason="Linux venv interpreter symlinks")
def test_legacy_evaluation_preserves_symlinked_venv_and_rejects_changed_python(
        tmp_path, project, ledger, kit, monkeypatch):
    directory = tmp_path / "evaluation-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(directory)
    python = directory / "bin" / "python"
    assert python.is_symlink()
    monkeypatch.setattr(evaluation, "sys", SimpleNamespace(executable=str(python), version=sys.version))
    script = kit / "evaluator" / "check.py"
    script.write_text(script.read_text(encoding="utf-8").replace(
        'f"x={x}; expected={expected}; predicted={predicted}"',
        'json.dumps({"python": sys.executable, "prefix": sys.prefix})'), encoding="utf-8")
    envelope, evaluator = admitted(project, ledger, kit)
    selected = develop(evaluator)
    assert selected["status"] == "completed", selected
    for row in selected["comparisons"]:
        for side in ("baseline", "candidate"):
            assert json.loads(row[side]["diagnostic"]) == {"python": str(python), "prefix": str(directory)}
    exposures = Path(envelope.project) / ".labgoblin" / "evaluation-exposures.json"
    before = exposures.read_bytes()
    python.unlink()
    python.write_bytes(b"changed interpreter")
    with pytest.raises(ValueError, match="interpreter changed after admission"):
        confirm(evaluator, selected)
    assert exposures.read_bytes() == before
    assert len(evaluation.manifest(envelope.root)) == 1


def test_research_environment_changes_fail_before_exposing_cases(tmp_path, project, ledger, kit):
    root = tmp_path / "separate-environment"
    record = runtime.initialize(root, project[1], goal="Use this separate environment.")
    envelope, evaluator = admitted((root, project[1], record), ledger, kit)
    config = root / ".venv" / "pyvenv.cfg"
    config.write_text(config.read_text() + "\n# changed after admission\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after admission"):
        develop(evaluator)
    assert not (root / ".labgoblin" / "evaluation-exposures.json").exists()
    assert not (Path(envelope.root) / "evaluations").exists()


def test_owned_research_installs_local_package_and_scores_with_the_same_python(
        tmp_path, project, ledger, kit, monkeypatch):
    from tests.test_simple_runtime import fixture_provider
    root = tmp_path / "scientific-research"
    config = replace(project[1], timeout_seconds=180, evaluation_spec=str(kit / "spec.json"))
    record = runtime.initialize(root, config, goal="Use only the supplied local fixture; no network access.")
    wheel = tmp_path / "lg_science_fixture-0.0.1-py3-none-any.whl"
    metadata = "lg_science_fixture-0.0.1.dist-info"
    with zipfile.ZipFile(wheel, "x") as archive:
        archive.writestr("lg_science_fixture.py",
                        "def fit(points):\n"
                        "    (x0,y0),(x1,y1)=points\n"
                        "    slope=(y1-y0)/(x1-x0)\n"
                        "    return {'slope':slope,'intercept':y0-slope*x0}\n")
        archive.writestr(metadata + "/METADATA",
                        "Metadata-Version: 2.1\nName: lg-science-fixture\nVersion: 0.0.1\n")
        archive.writestr(metadata + "/WHEEL",
                        "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(metadata + "/RECORD", "")
    script = kit / "evaluator" / "check.py"
    original = script.read_text(encoding="utf-8")
    child = ("import json,sys; from lg_science_fixture import fit;"
             "print(json.dumps({'python':sys.executable,'weights':fit([(0,1),(1,3)])}))")
    script.write_text(
        "import subprocess,sys\n"
        f"identity=subprocess.check_output([sys.executable,'-I','-B','-c',{child!r}],text=True,timeout=10).strip()\n"
        + original.replace('"diagnostic": f"x={x}; expected={expected}; predicted={predicted}"',
                           '"diagnostic": identity'),
        encoding="utf-8")
    fixture_provider(root, monkeypatch, "evaluation")
    atomic_json(root / "fixture-provider.json", {
        "model_free": True, "root": str(root), "mode": "evaluation", "scientific_wheel": str(wheel)})
    result = runtime.run(root)
    assert result["status"] == "completed", result
    directory = root / ".labgoblin" / "runs" / result["id"]
    python = record["research_environment"]["python"]
    trained = read_json(directory / "snapshot" / "artifacts" / "science-model.json")
    assert trained == {"python": python, "prefix": str(root / ".venv"),
                       "weights": {"slope": 2, "intercept": 1}}
    definition = read_json(directory / "evaluation-definition" / "definition.json")
    assert definition["interpreter"] == python
    checks = read_json(directory / "snapshot" / "artifacts" / "evaluation-fixture.json")
    assert [checks[stage]["summary"]["candidate_passed"]
            for stage in ("development", "confirmation", "regression")] == [2, 0, 2]
    for checked in checks.values():
        for row in checked["comparisons"]:
            for side in ("baseline", "candidate"):
                identity = json.loads(row[side]["diagnostic"])
                assert identity == {"python": python, "weights": {"slope": 2, "intercept": 1}}
    assert not (directory / "snapshot" / ".venv").exists()
    assert importlib.util.find_spec("lg_science_fixture") is None
    assert not ledger.rows()


def test_failed_confirmation_is_spent_across_restart_and_renamed_cases(project, ledger, kit):
    script = kit / "evaluator" / "check.py"
    original = script.read_text(encoding="utf-8")
    script.write_text(original.replace(
        'x, expected = case["data"]["x"], case["data"]["expected"]',
        'x, expected = case["data"]["x"], case["data"]["expected"]\n'
        '    if x < 0: raise RuntimeError("deliberate confirmation failure")'), encoding="utf-8")
    envelope, evaluator = admitted(project, ledger, kit)
    selected = develop(evaluator)
    checked = confirm(evaluator, selected)
    assert checked["status"] == "failed" and "baseline evaluator failed" in checked["reason"]
    assert "summary" not in checked
    settle(envelope, ledger)
    values = read_json(kit / "confirmation.json")
    values[0]["id"] = "renamed-negative"
    values[1]["id"] = "renamed-three"
    atomic_json(kit / "confirmation.json", values)
    script.write_text(original, encoding="utf-8")
    _, resumed = admitted(project, ledger, kit)
    again = develop(resumed)
    with pytest.raises(ValueError, match="No unexposed"):
        confirm(resumed, again)
    replayed = resumed.evaluate("repaired-evaluator", {
        "stage": "regression", "candidate": "candidate",
        "source_run": envelope.id, "source_check": checked["check"]})
    assert replayed["summary"]["candidate_passed"] == 2 and replayed["stage"] == "regression"


@pytest.mark.parametrize("change", ["duplicate", "omitted", "nan", "bool", "metric", "passed"])
def test_incomplete_or_invalid_measurements_fail_closed(change):
    values = [{"id": "one", "data": 1}, {"id": "two", "data": 2}]
    result = {"version": 1, "cases": [
        {"id": name, "passed": True, "metrics": {"loss": 0}, "diagnostic": ""} for name in ("one", "two")]}
    if change == "duplicate":
        result["cases"][1]["id"] = "one"
    elif change == "omitted":
        result["cases"].pop()
    elif change in ("nan", "bool"):
        result["cases"][0]["metrics"]["loss"] = float("nan") if change == "nan" else True
    elif change == "metric":
        result["cases"][0]["metrics"] = {"unapproved": 1}
    else:
        del result["cases"][0]["passed"]
    with pytest.raises(ValueError):
        evaluation.measurements(result, values, {"loss": "minimize"})


def test_overlapping_confirmation_and_unreviewed_paths_are_rejected(project, ledger, kit):
    atomic_json(kit / "confirmation.json", read_json(kit / "development.json"))
    with pytest.raises(ValueError, match="distinct case data"):
        admitted(project, ledger, kit)
    spec = read_json(kit / "spec.json")
    spec["evaluator"] = ".."
    atomic_json(kit / "spec.json", spec)
    with pytest.raises(ValueError, match="relative evaluation path"):
        admitted(project, ledger, kit)


def test_preparation_failure_is_visible_without_spending_confirmation(project, ledger, kit):
    envelope, evaluator = admitted(project, ledger, kit)
    publish_json(Path(envelope.project) / "candidate" / ".env", {"not": "captured"})
    with pytest.raises(ValueError, match="self-contained"):
        develop(evaluator)
    listed = evaluation.page(envelope)
    assert listed["checks"][0]["status"] == "not_started"
    assert "self-contained" in listed["checks"][0]["reason"]
    assert not (Path(envelope.project) / ".labgoblin" / "evaluation-exposures.json").exists()


def test_terminal_evidence_is_read_only_and_receipt_bound(project, ledger, kit, capsys):
    envelope, evaluator = admitted(project, ledger, kit)
    checked = develop(evaluator)
    settle(envelope, ledger)
    evidence = Evidence(envelope.project, envelope.project_id)
    assert evidence.evaluations({"run": envelope.id, "check": checked["check"]})["live"] is False
    before = evaluation.manifest(envelope.root)
    assert main(["evaluations", envelope.project, "--run", envelope.id, "--check", checked["check"]]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["candidate_passed"] == 2
    assert evaluation.manifest(envelope.root) == before
    path = Path(envelope.root) / "evaluations" / checked["check"] / "result.json"
    value = read_json(path)
    value["summary"]["candidate_passed"] = 999
    atomic_json(path, value)
    with pytest.raises(ValueError, match="owned terminal receipt"):
        evidence.evaluations({"run": envelope.id})


def test_candidate_and_evaluator_mutation_invalidates_the_pair(project, ledger, kit):
    script = kit / "evaluator" / "check.py"
    script.write_text(script.read_text(encoding="utf-8") +
                      '\n(Path(request["artifact"]) / "injected.txt").write_text("changed")\n', encoding="utf-8")
    _, evaluator = admitted(project, ledger, kit)
    checked = develop(evaluator)
    assert checked["status"] == "failed" and "inventory changed" in checked["reason"]
    assert "summary" not in checked


def test_timeout_and_cancellation_retire_owned_evaluator(project, ledger, kit):
    script = kit / "evaluator" / "check.py"
    script.write_text("import time\nwhile True: print('busy',flush=True); time.sleep(.01)\n", encoding="utf-8")
    envelope, evaluator = admitted(project, ledger, kit)
    evaluator.deadline = lambda: time.monotonic() + 10.3
    checked = develop(evaluator)
    assert checked["status"] == "failed" and "timed_out" in checked["reason"]
    handle_root = Path(envelope.root) / "evaluations" / checked["check"] / "baseline" / "work" / "process"
    from labgoblin.payload import tree_state
    handle = read_json(handle_root / "process-handle.json")
    assert tree_state(handle_root, handle["token"]) == "dead"
    evaluator.deadline = lambda: time.monotonic() + 90
    history = Path(envelope.root) / "evaluations"
    previous = set(history.rglob("process-handle.json"))
    finished = threading.Event()
    cancelled = threading.Event()
    def cancel_running():
        while not finished.wait(.02):
            if set(history.rglob("process-handle.json")) - previous:
                (Path(envelope.root) / "cancel").touch()
                cancelled.set()
                return
    thread = threading.Thread(target=cancel_running)
    thread.start()
    try:
        checked = develop(evaluator, "cancel")
    finally:
        finished.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and cancelled.is_set()
    assert checked["status"] == "failed" and "cancelled" in checked["reason"]
    handle_root = Path(envelope.root) / "evaluations" / checked["check"] / "baseline" / "work" / "process"
    handle = read_json(handle_root / "process-handle.json")
    assert tree_state(handle_root, handle["token"]) == "dead"


def test_only_bound_researcher_can_execute_and_duplicate_calls_do_not_reexecute(project, ledger, kit):
    envelope, evaluator = admitted(project, ledger, kit)
    caller = SimpleNamespace(id="bound-session", waiting=True, idle=asyncio.Event(), aborting=False,
                             limit_reached=False, failure="", consultation_call_id=None)
    tool = evaluator.tool(caller)
    call = SimpleNamespace(session_id="other", tool_call_id="call", arguments={
        "stage": "development", "candidate": "candidate"})
    denied = asyncio.run(tool.handler(call))
    assert denied.result_type == "failure" and not evaluation.manifest(envelope.root)
    call.session_id = "bound-session"
    first = asyncio.run(tool.handler(call))
    second = asyncio.run(tool.handler(call))
    assert first.result_type == "success" and first.text_result_for_llm == second.text_result_for_llm
    assert len(evaluation.manifest(envelope.root)) == 1
    caller.waiting = False
    denied = asyncio.run(tool.handler(call))
    assert denied.result_type == "failure"


def test_uncertain_evaluation_cancels_callback_and_recovery_retains_attempt(project, ledger, kit, monkeypatch):
    envelope, evaluator = admitted(project, ledger, kit)
    caller = SimpleNamespace(id="researcher", waiting=True, idle=asyncio.Event(), aborting=False,
                             limit_reached=False, failure="", consultation_call_id=None)
    def uncertain(spec):
        raise UncertainExecution("Synthetic uncertain process tree")
    monkeypatch.setattr(evaluation.payload, "run_process", uncertain)
    call = SimpleNamespace(session_id=caller.id, tool_call_id="uncertain",
                           arguments={"stage": "development", "candidate": "candidate"})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(evaluator.tool(caller).handler(call))
    assert (Path(envelope.root) / "cancel").exists()
    assert ledger.claim_consumer(envelope, own_handle(envelope.id))
    monkeypatch.setattr(runtime, "process_state", lambda handle: "dead")
    monkeypatch.setattr(runtime.payload, "tree_state", lambda *args: "dead")
    receipt = runtime.settle_dead(ledger, ledger.consumer_run(envelope.id))
    assert receipt.status == "interrupted" and len(receipt.metadata["evaluations"]) == 1
    item = Evidence(envelope.project, envelope.project_id).evaluations({"run": envelope.id})["checks"][0]
    assert item["status"] == "incomplete"


def test_evaluation_reader_preserves_adviser_read_limit(project, ledger, kit, monkeypatch):
    from labgoblin.consultation import EvidenceAudit, MAX_READS
    _, evaluator = admitted(project, ledger, kit)
    audit = EvidenceAudit(Path(evaluator.envelope.root) / "audit")
    audit.count = MAX_READS
    def forbidden(*args):
        raise AssertionError("Exhausted adviser must not read evaluation files")
    monkeypatch.setattr(Evidence, "evaluations", forbidden)
    result = asyncio.run(evaluator.reader(audit).handler(SimpleNamespace(arguments={})))
    assert result.result_type == "failure" and "retained reads" in result.error
    assert audit.count == MAX_READS


def test_aggregate_case_definition_cannot_exceed_its_reader(project, ledger, kit):
    atomic_json(kit / "development.json", [{"id": "large-dev", "data": "a" * 600000}])
    atomic_json(kit / "confirmation.json", [{"id": "large-confirm", "data": "b" * 600000}])
    with pytest.raises(ValueError, match="one-MiB"):
        admitted(project, ledger, kit)


def test_non_ascii_diagnostics_page_without_losing_cases(project, ledger, kit):
    values = [{"id": "\u754c" * 40 + str(i), "data": {"x": i, "expected": 2 * i + 1}} for i in range(5)]
    atomic_json(kit / "development.json", values)
    spec = read_json(kit / "spec.json")
    spec.update(confirmation=[], purpose="\u754c" * 660)
    atomic_json(kit / "spec.json", spec)
    script = kit / "evaluator" / "check.py"
    script.write_text(script.read_text(encoding="utf-8").replace(
        'f"x={x}; expected={expected}; predicted={predicted}"', repr("\u754c" * 330)), encoding="utf-8")
    envelope, evaluator = admitted(project, ledger, kit)
    checked = develop(evaluator)
    found = list(checked["comparisons"])
    while checked["next_offset"] is not None:
        assert len(json.dumps(checked).encode()) <= evaluation.PAGE_BYTES
        checked = evaluation.page(envelope, check=checked["check"], offset=checked["next_offset"])
        found.extend(checked["comparisons"])
    assert [row["id"] for row in found] == [case["id"] for case in values]
    assert all(row["candidate"]["diagnostic"] == "\u754c" * 330 for row in found)


def test_real_sdk_evaluation_loop_and_read_only_coach(project, ledger, kit, monkeypatch):
    from tests.test_simple_runtime import fixture_provider
    root, config, _ = project
    (root / "labgoblin.toml").write_text(
        replace(config, coach=True, evaluation_spec=str(kit / "spec.json")).toml(), encoding="utf-8")
    fixture_provider(root, monkeypatch, "evaluation")
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert len(result["metadata"]["evaluations"]) == 3
    assert result["usage"]["totalNanoAiu"] == 40_000_000_000
    retained = read_json(root / "artifacts" / "evaluation-fixture.json")
    assert retained["development"]["summary"]["candidate_passed"] == 2
    assert retained["confirmation"]["summary"]["candidate_passed"] == 0
    assert retained["regression"]["summary"]["candidate_passed"] == 2
    for session in result["usage"]["sessions"][1:]:
        options = read_json(root / f"fixture-session-{session['session_id']}.json")
        serialized = json.dumps(options)
        assert "labgoblin_read_evaluation" in serialized and "labgoblin_evaluate\"" not in serialized
    evidence = Evidence(root, runtime.project_record(root)[2]["id"])
    for item in retained.values():
        assert evidence.evaluations({"run": result["id"], "check": item["check"]})["status"] == "completed"
    assert not ledger.rows()
