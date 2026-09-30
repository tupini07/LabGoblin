from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys
import time

import psutil
import pytest

from xgenius import backends, worker
from xgenius.config import initial_config, parse_config
from xgenius.evidence import publish_bytes, read_json, tail
from xgenius.protocol import LaunchEnvelope, LaunchKey, LaunchReceipt, Resources, canonical, identifier
from xgenius.scheduler import MachineSample, ResourceLedger
from xgenius.state import State


@pytest.fixture
def native_launch(tmp_path):
    cpu_ids = psutil.Process().cpu_affinity()[:2]
    config = parse_config(initial_config("native-fixture"), tmp_path / "xgenius.toml")
    ledger = ResourceLedger.create(
        tmp_path / "ledger.db",
        sampler=lambda gpus: MachineSample(tuple(cpu_ids), 8192, 8192),
        eligibility=lambda row: (True, ""))
    ledger.configure(2, 4096, (), 0)
    state = State.create(config, ledger.path)
    state.bind_ledger(ledger.path, ledger.id)
    work = identifier()
    root = state.root / "attempts" / work
    (root / "source").mkdir(parents=True)
    (root / "output").mkdir()
    state.enqueue({"id": work, "key": work, "request": {"key": work}, "experiment_id": work,
                   "cpus": 1, "memory_mb": 256, "gpus": [], "seconds": 10, "root": str(root)})
    resources = Resources(1, 256)
    allocation = state.allocation(work, "attempt", {"resources": asdict(resources)}, 0)
    ledger.request(allocation["token"], state.id, work, "attempt", resources,
                   {"kind": "campaign", "state_dir": str(state.root), "generation": 1, "revision": 0},
                   native=True)
    grant = ledger.reserve(allocation["token"])
    assert state.granted(allocation["token"])
    runner = backends.validate_runner({"kind": "native", "python": sys.executable})
    envelope = LaunchEnvelope(
        LaunchKey(state.id, 1, work, allocation["token"], identifier()), "attempt",
        (sys.executable, "-c", "print('frozen native worker')"),
        str(root / "source"), str(root), str(state.path), str(ledger.path), ledger.id,
        10, resources, config.revision,
        metadata={"runner": runner, "cpu_ids": json.loads(grant["native_cpus"]), "log_bytes": 1024,
                  "execution": {"source_root": str(root / "source")}})
    return state, ledger, envelope


def wait_for_outcome(state, ledger, envelope):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        row = state.attempt(envelope.key.work_id)
        if row["status"] not in ("queued", "starting", "running", "recovery_required"):
            worker.reconcile(state, backends.inspect_payload)
            assert ledger.grant(envelope.key.grant_id)["state"] == "released"
            return row
        time.sleep(0.05)
    root = worker.launch_directory(envelope)
    stderr = tail(root / "supervisor.stderr.log")["text"]
    pytest.fail(f"No durable outcome: {state.campaign()}; {stderr}")


def test_actual_frozen_native_worker_runs_and_releases(native_launch):
    state, ledger, envelope = native_launch
    frozen = worker.start(state, envelope)
    row = wait_for_outcome(state, ledger, frozen)
    assert row["status"] == "completed"
    assert tail(worker.launch_directory(frozen) / "main" / "stdout.log")["text"].strip() == "frozen native worker"
    receipt = read_json(worker.launch_directory(frozen) / "receipt.json")
    assert receipt["metadata"]["cpu_ids"] == envelope.metadata["cpu_ids"]
    with state.db.read() as conn:
        record = conn.execute("SELECT launcher,supervisor FROM launches").fetchone()
        launcher, supervisor = json.loads(record[0]), json.loads(record[1])
    assert launcher["token"] == supervisor["token"] == envelope.key.nonce
    until = time.monotonic() + 5
    while psutil.pid_exists(supervisor["pid"]) and time.monotonic() < until:
        time.sleep(0.02)
    assert not psutil.pid_exists(supervisor["pid"])


def test_actual_worker_deadline_is_independent_of_controller(native_launch):
    state, ledger, envelope = native_launch
    envelope = replace(envelope, argv=(sys.executable, "-c", "import time;time.sleep(60)"), timeout_seconds=0.3)
    frozen = worker.start(state, envelope)
    row = wait_for_outcome(state, ledger, frozen)
    assert row["status"] == "timed_out"
    handle = read_json(worker.launch_directory(frozen) / "main" / "process-handle.json")
    assert not psutil.pid_exists(handle["pid"])


def test_bounded_probe_refuses_truncated_output():
    with pytest.raises(ValueError, match="Probe output"):
        backends.command([sys.executable, "-c", "print('a'*100000)"], limit=1024)


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://localhost", "tcp://remote:2376"])
def test_docker_remote_endpoints_are_not_local_even_when_loopback(endpoint, monkeypatch):
    monkeypatch.setattr(backends, "command", lambda *args, **kwargs: endpoint)
    with pytest.raises(ValueError, match="remote"):
        backends.validate_docker_endpoint("fixture")


def guest_envelope(native_launch, kind):
    state, ledger, envelope = native_launch
    runner = ({"kind": "docker", "context": "fixture", "image": "mutable:tag", "network": False,
               "image_id": "sha256:" + "a" * 64, "resolved_python": "/usr/bin/python3",
               "endpoint": "npipe:////./pipe/fixture"} if kind == "docker" else
              {"kind": "wsl", "distro": "fixture", "resolved_python": "/usr/bin/python3"})
    envelope = replace(envelope, argv=("/usr/bin/python3", "script with spaces.py"),
                       metadata={**envelope.metadata, "runner": runner})
    return worker.prepare_runtime(state, envelope)


def test_docker_uses_frozen_image_readonly_source_and_no_implicit_pull(native_launch):
    envelope = guest_envelope(native_launch, "docker")
    before = envelope.digest
    spec, command = backends.prepare_guest(envelope)
    assert envelope.digest == before
    assert "--pull=never" in command
    assert envelope.metadata["runner"]["image_id"] in command
    assert "mutable:tag" not in command
    assert "--read-only" in command and "--cap-drop" in command
    assert any("target=/source,readonly" in value for value in command)
    assert any("target=/runtime,readonly" in value for value in command)
    assert spec["cpu_ids"] is None
    assert spec["envelope_digest"] == before
    assert spec["argv"] == list(envelope.argv)
    assert "/run/payload.json" in command


def test_wsl_mapping_preserves_host_envelope_and_full_argument_array(native_launch, monkeypatch):
    envelope = guest_envelope(native_launch, "wsl")
    path = Path(envelope.root) / "input.txt"
    path.write_text("fixture", encoding="utf-8")
    envelope = replace(envelope, metadata={**envelope.metadata, "execution": {
        **envelope.metadata["execution"], "inputs": {"data": {"access_path": str(path)}}}})
    before = envelope.digest
    monkeypatch.setattr(backends, "wsl_path", lambda runner, value: "/mapped/" + Path(value).name)
    spec, command = backends.prepare_guest(envelope)
    assert envelope.digest == before
    assert envelope.metadata["execution"]["inputs"]["data"]["access_path"] == str(path)
    assert spec["inputs"]["data"]["access_path"].startswith("/mapped/")
    assert spec["argv"][1] == "script with spaces.py"
    assert spec["cpu_ids"] is None
    assert "--payload" in command


def test_created_docker_container_is_not_proof_of_quiescence(native_launch, monkeypatch):
    envelope = guest_envelope(native_launch, "docker")
    monkeypatch.setattr(backends, "docker_state",
                        lambda value: {"state": {"Status": "created", "Running": False}})
    assert backends.inspect_payload(envelope) == "unknown"


def test_docker_payload_receipt_cannot_release_a_still_running_container(native_launch, monkeypatch):
    state, ledger, _ = native_launch
    envelope = guest_envelope(native_launch, "docker")
    state.arm(envelope)
    receipt = LaunchReceipt(envelope.key, envelope.digest, "completed", True, 1, returncode=0)
    publish_bytes(worker.launch_directory(envelope) / "backend-receipt.json", canonical(asdict(receipt)))
    monkeypatch.setattr(backends, "docker_state",
                        lambda value: {"state": {"Status": "running", "Running": True}})
    assert backends.inspect_payload(envelope) == "alive"
    worker.reconcile(state, backends.inspect_payload)
    assert ledger.grant(envelope.key.grant_id)["state"] == "granted"
    assert state.active_launches()


def test_mount_refuses_broad_home_and_socket_paths(tmp_path):
    with pytest.raises(ValueError, match="Refusing"):
        backends._mount(Path.home(), "/inputs")
    socket = tmp_path / "docker.sock"
    socket.touch()
    with pytest.raises(ValueError, match="Refusing"):
        backends._mount(socket, "/inputs")


@pytest.mark.parametrize("kind", ["wsl", "docker"])
@pytest.mark.parametrize("changed", [False, True])
def test_recovered_guest_receipt_checks_the_retained_derived_mapping(native_launch, monkeypatch, kind, changed):
    from xgenius.protocol import fingerprint
    state, ledger, _ = native_launch
    envelope = guest_envelope(native_launch, kind)
    monkeypatch.setattr(backends, "wsl_path", lambda runner, value: "/mapped/" + Path(value).name)
    spec, _ = backends.prepare_guest(envelope)
    state.arm(envelope)
    receipt = LaunchReceipt(envelope.key, envelope.digest, "completed", True, 1, returncode=0,
                            metadata={"payload_spec_digest": fingerprint(spec)})
    root = worker.launch_directory(envelope)
    publish_bytes(root / "backend-receipt.json", canonical(asdict(receipt)))
    if changed:
        spec["cwd"] = "/changed-after-publication"
        (root / "payload.json").write_bytes(canonical(spec))
    result = worker.reconcile(state, lambda envelope: "dead")
    assert bool(result["unresolved"]) == changed
    assert ledger.grant(envelope.key.grant_id)["state"] == ("granted" if changed else "released")
    assert bool(state.active_launches()) == changed
