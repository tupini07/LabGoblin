from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import time

import psutil
import pytest

from labgoblin import backends, worker, workspace
from labgoblin.config import initial_config, parse_config
from labgoblin.evidence import SizeLimitError, observation, read_json
from labgoblin.protocol import LaunchReceipt, Resources
from labgoblin.scheduler import MachineSample, ResourceLedger
from labgoblin.state import State


PROGRAM = (
    "import json,os,pathlib\n"
    "pathlib.Path(os.environ['LABGOBLIN_OUTPUT_DIR'],'metrics.json').write_text("
    "json.dumps({'score':42}),encoding='utf-8')\n"
)


def setup(tmp_path, *, inputs=None, storage=None):
    project = tmp_path / "project"
    project.mkdir()
    (project / "experiment.py").write_text(PROGRAM, encoding="utf-8")
    raw = initial_config("evidence-fixture")
    raw["execution"]["source_files"] = ["experiment.py"]
    raw["inputs"] = inputs or {}
    if storage:
        raw["storage"] = storage
    config = parse_config(raw, project / "labgoblin.toml")
    cpus = tuple(psutil.Process().cpu_affinity()[:2])
    ledger = ResourceLedger.create(tmp_path / "machine.db",
                                   sampler=lambda g: MachineSample(cpus, 8192, 8192),
                                   eligibility=lambda row: (True, ""))
    ledger.configure(2, 4096, (), 0)
    state = State.create(config, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    return config, state, ledger


def request(**kwargs):
    return {"key": "baseline", "argv": ["python", "experiment.py"], "cpus": 1,
            "memory_mb": 256, "seconds": 5, "artifacts": ["metrics.json"], **kwargs}


def admit(config, state, ledger, manifest=None):
    attempt = workspace.submit(state, config, manifest or request())
    resources = Resources(attempt["cpus"], attempt["memory_mb"])
    allocation = state.allocation(attempt["id"], "attempt", {"resources": asdict(resources)}, 0)
    ledger.request(allocation["token"], state.id, attempt["id"], "attempt", resources,
                   {"kind": "campaign", "state_dir": str(state.root), "generation": 1, "revision": 0},
                   native=True)
    grant = ledger.reserve(allocation["token"])
    assert state.granted(grant["token"])
    return workspace.prepare_envelope(state, config, attempt["id"], grant)


def finish(state, envelope):
    frozen = worker.prepare_runtime(state, envelope)
    state.arm(frozen)
    assert worker.execute(frozen, lambda value: LaunchReceipt(value.key, value.digest, "completed",
                                                            True, 0.1, returncode=0)) == 0
    return frozen


def wait(state, attempt_id):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        value = state.attempt(attempt_id)
        if value["status"] not in ("queued", "starting", "running", "recovery_required"):
            worker.reconcile(state, backends.inspect_payload)
            return value
        time.sleep(0.05)
    pytest.fail(str(state.campaign()))


def test_shipped_shape_native_execution_and_exact_retained_metrics(tmp_path):
    config, state, ledger = setup(tmp_path)
    envelope = worker.start(state, admit(config, state, ledger))
    assert wait(state, envelope.key.work_id)["status"] == "completed"
    result = workspace.collect_artifacts(state, envelope.key.work_id)
    assert result["collection"] == "complete"
    assert result["validation"] == "valid"
    retained = observation(state.db, result["observation_ids"][0])
    assert json.loads(retained["body"]) == {"score": 42}
    assert retained["metadata"]["metrics"] == {"score": 42}
    assert hashlib.sha256(retained["body"]).hexdigest() == retained["digest"]
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"


def test_snapshot_mutation_is_refused_before_execution(tmp_path):
    config, state, ledger = setup(tmp_path)
    envelope = admit(config, state, ledger)
    source = Path(envelope.metadata["execution"]["source_root"]) / "experiment.py"
    source.write_text("print('different')", encoding="utf-8")
    worker.start(state, envelope)
    result = wait(state, envelope.key.work_id)
    assert result["status"] == "not_started"
    assert "Source snapshot drift" in result["reason"]
    assert workspace.collect_artifacts(state, envelope.key.work_id)["collection"] == "not_performed"


def test_checked_input_mutation_at_execution_is_refused(tmp_path):
    data = tmp_path / "dataset.bin"
    data.write_bytes(b"original")
    config, state, ledger = setup(tmp_path, inputs={"data": {
        "path": str(data), "sha256": hashlib.sha256(b"original").hexdigest()}})
    envelope = admit(config, state, ledger)
    data.write_bytes(b"changed")
    worker.start(state, envelope)
    result = wait(state, envelope.key.work_id)
    assert result["status"] == "not_started"
    assert "Input revision drift" in result["reason"]


@pytest.mark.skipif(os.name != "nt", reason="Windows stable input read lease")
def test_prepared_small_input_is_separate_and_write_leased(tmp_path):
    data = tmp_path / "dataset.bin"
    data.write_bytes(b"original")
    config, _, _ = setup(tmp_path, inputs={"data": {"path": str(data), "assurance": "stable-consumption"}})
    spec = workspace.prepare_spec(config, request())
    prepared = Path(spec["inputs"]["data"]["access_path"])
    data.write_bytes(b"new original")
    assert prepared.read_bytes() == b"original"
    prepared.chmod(stat.S_IREAD | stat.S_IWRITE)
    with workspace.checked_inputs(spec["inputs"], "native"):
        with pytest.raises(PermissionError):
            prepared.write_bytes(b"changed")
    prepared.write_bytes(b"changed")
    with pytest.raises(ValueError, match="revision drift"):
        with workspace.checked_inputs(spec["inputs"], "native"):
            pass


def test_snapshot_cap_is_enforced_during_copy(tmp_path):
    config, _, _ = setup(tmp_path, storage={"snapshot_bytes": 8})
    with pytest.raises(SizeLimitError):
        workspace.prepare_spec(config, request())
    assert not list(config.state_dir.rglob("experiment.py"))


def test_large_inputs_are_not_hashed_without_a_declared_pin(tmp_path, monkeypatch):
    data = tmp_path / "large.bin"
    with data.open("wb") as stream:
        stream.truncate(2 * 1024 * 1024)
    config, _, _ = setup(tmp_path, inputs={"data": {"path": str(data)}})

    def forbidden(*args):
        raise AssertionError("Unrequested input hashing")

    monkeypatch.setattr(workspace, "hash_file", forbidden)
    spec = workspace.prepare_spec(config, request())
    assert spec["inputs"]["data"]["assurance"] == "declared"


def test_submission_idempotency_does_not_copy_again_and_conflicts_are_explicit(tmp_path):
    config, state, _ = setup(tmp_path)
    first = workspace.submit(state, config, request())
    second = workspace.submit(state, config, request())
    assert first["id"] == second["id"]
    assert len(list((state.root / "attempts").iterdir())) == 1
    with pytest.raises(ValueError, match="different request"):
        workspace.submit(state, config, request(seconds=3))


def test_existing_hypothesis_statement_is_frozen_on_admission(tmp_path):
    config, state, ledger = setup(tmp_path)
    state.hypothesis("h1", "The deterministic baseline emits score 42.")
    envelope = admit(config, state, ledger, request(hypothesis_id="h1"))
    finish(state, envelope)
    with pytest.raises(ValueError, match="immutable"):
        state.hypothesis("h1", "A different scientific claim.")
    spec = json.loads(state.attempt(envelope.key.work_id)["spec"])
    assert spec["hypothesis_description"] == "The deterministic baseline emits score 42."
    assert "hypothesis_description" not in spec["request"]


def test_recollection_creates_new_revision_and_old_download_is_unchanged(tmp_path):
    config, state, ledger = setup(tmp_path)
    envelope = finish(state, admit(config, state, ledger))
    output = Path(envelope.metadata["execution"]["output"]) / "metrics.json"
    output.write_bytes(b'{"score":42}')
    first = workspace.collect_artifacts(state, envelope.key.work_id)
    output.write_bytes(b'{"score":999}')
    assert workspace.collect_artifacts(state, envelope.key.work_id)["collection"] == "complete"
    second = workspace.collect_artifacts(state, envelope.key.work_id, recollect=True)
    assert first["observation_ids"] != second["observation_ids"]
    assert observation(state.db, first["observation_ids"][0])["body"] == b'{"score":42}'
    assert observation(state.db, second["observation_ids"][0])["body"] == b'{"score":999}'


def test_invalid_metrics_are_retained_and_missing_artifacts_stay_in_denominator(tmp_path):
    config, state, ledger = setup(tmp_path)
    envelope = finish(state, admit(config, state, ledger, request(artifacts=["metrics.json", "missing.bin"])))
    (Path(envelope.metadata["execution"]["output"]) / "metrics.json").write_bytes(b'{"score":NaN}')
    result = workspace.collect_artifacts(state, envelope.key.work_id)
    assert result["collection"] == "failed"
    assert result["validation"] == "invalid"
    assert "missing.bin" in result["reason"]
    assert "Nonfinite" in result["reason"]
    retained = observation(state.db, result["observation_ids"][0])
    assert retained["body"] == b'{"score":NaN}'
    assert "validation_error" in retained["metadata"]


def test_large_artifact_keeps_qualified_current_reference_without_copy(tmp_path):
    config, state, ledger = setup(tmp_path, storage={"capture_bytes": 16, "metrics_bytes": 16})
    envelope = finish(state, admit(config, state, ledger, request(artifacts=["weights.bin"])))
    output = Path(envelope.metadata["execution"]["output"]) / "weights.bin"
    output.write_bytes(b"x" * 1024)
    result = workspace.collect_artifacts(state, envelope.key.work_id)
    value = observation(state.db, result["observation_ids"][0])
    assert value["body"] is None and value["digest"] is None
    assert value["assurance"] == "unmanaged"
    assert "not retained" in value["metadata"]["limitation"]
    assert not (state.root / "evidence").exists()


def test_modified_retained_object_is_not_served_as_original(tmp_path):
    config, state, ledger = setup(tmp_path)
    envelope = finish(state, admit(config, state, ledger))
    (Path(envelope.metadata["execution"]["output"]) / "metrics.json").write_bytes(b'{"score":42}')
    result = workspace.collect_artifacts(state, envelope.key.work_id)
    original = observation(state.db, result["observation_ids"][0])
    Path(original["metadata"]["capture_path"]).write_bytes(b'{"score":99}')
    with pytest.raises(ValueError, match="modified"):
        observation(state.db, result["observation_ids"][0])


def test_collection_never_reopens_live_metrics_for_hashing_or_serving(tmp_path, monkeypatch):
    from labgoblin.evidence import Capture
    config, state, ledger = setup(tmp_path)
    envelope = finish(state, admit(config, state, ledger))
    metrics = Path(envelope.metadata["execution"]["output"]) / "metrics.json"
    metrics.write_bytes(b'{"score":42}')
    original = Capture.read

    def capture_then_mutate(path, limit):
        value = original(path, limit)
        if path == metrics:
            metrics.write_bytes(b'{"score":999}')
        return value

    monkeypatch.setattr(Capture, "read", capture_then_mutate)
    result = workspace.collect_artifacts(state, envelope.key.work_id)
    retained = observation(state.db, result["observation_ids"][0])
    assert retained["body"] == b'{"score":42}'
    assert retained["metadata"]["metrics"] == {"score": 42}
    assert retained["digest"] == hashlib.sha256(retained["body"]).hexdigest()
