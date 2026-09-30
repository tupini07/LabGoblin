from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import sys

import pytest

from tests.test_controller import fixture
from xgenius import backends, worker
from xgenius.evidence import publish_bytes
from xgenius.processes import own_handle
from xgenius.protocol import LaunchEnvelope, LaunchKey, LaunchReceipt, Resources, canonical, identifier


IMAGE = "sha256:" + "a" * 64


def local_image(name):
    assert name == "prepared:local"
    return {"id": IMAGE, "os": "linux", "onbuild": []}


def test_build_pins_local_bases_and_stage_copy_without_registry_references():
    body, bases = backends.build_dockerfile(
        b"FROM prepared:local AS source\nCOPY test.py /src/\n"
        b"FROM scratch\nCOPY --from=source /src/ /app/\n", local_image, "fixture")
    assert b"FROM " + b"a" * 64 + b" AS source" in body
    assert b"prepared:local" not in body
    assert body.count(b'LABEL xgenius.build="fixture"') == 2
    assert bases == {"prepared:local": IMAGE}


@pytest.mark.parametrize("body", [
    b"FROM ${IMAGE}\n", b"FROM --platform=linux/amd64 prepared:local\n",
    b"# syntax=docker/dockerfile:1\nFROM scratch\n",
    b"FROM scratch\nADD file.txt /file.txt\n",
    b"FROM scratch\nONBUILD RUN echo anything\n",
    b"FROM scratch\nCOPY --from=remote:image /bin /bin\n",
    b"FROM scratch\nCOPY --from=${IMAGE} /bin /bin\n",
    b"FROM scratch\nLABEL xgenius.build=other\n",
    b"FROM scratch\nLABEL ${KEY}=value\n",
    b"FROM prepared:local\nRUN <<EOF\nsomething\nEOF\n",
])
def test_build_rejects_hidden_fetches_or_uninspectable_dockerfile_features(body):
    with pytest.raises(ValueError):
        backends.build_dockerfile(body, local_image, "fixture")


def test_onbuild_base_is_refused_even_when_already_local():
    with pytest.raises(ValueError, match="without ONBUILD"):
        backends.build_dockerfile(b"FROM prepared:local\n",
            lambda _: {"id": IMAGE, "os": "linux", "onbuild": ["ADD https://invalid/file /file"]}, "fixture")


def test_context_uses_only_explicit_frozen_files_and_enforces_copy_bound(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "Dockerfile").write_bytes(b"FROM scratch\nCOPY result.txt /result.txt\n")
    (source / "result.txt").write_bytes(b"measured")
    (source / "not-included.txt").write_bytes(b"not part of the reviewed context")
    target = tmp_path / "build"
    result = backends.prepare_build_context(source, ["result.txt"], target, {}, "fixture", 512)
    assert set(result["files"]) == {"Dockerfile", "result.txt"}
    import tarfile
    with tarfile.open(result["tar"]) as archive:
        assert set(archive.getnames()) == {"Dockerfile", "result.txt"}
        assert archive.extractfile("result.txt").read() == b"measured"
    (source / "result.txt").write_bytes(b"changed")
    with tarfile.open(result["tar"]) as archive:
        assert archive.extractfile("result.txt").read() == b"measured"
    with pytest.raises(ValueError, match="snapshot allowance"):
        backends.prepare_build_context(source, ["result.txt"], tmp_path / "too-small", {}, "fixture", 8)
    with pytest.raises(ValueError, match="escapes"):
        backends.prepare_build_context(source, ["..\\external.txt"], tmp_path / "outside", {}, "fixture", 512)


class Response:
    headers = {"Transfer-Encoding": "chunked"}
    status_code = 200

    def __init__(self, chunks, failure=None):
        self.chunks, self.failure = chunks, failure

    def iter_content(self, chunk_size):
        assert chunk_size == 16384
        yield from self.chunks
        if self.failure:
            raise self.failure


def test_build_requires_complete_http_stream_not_cli_exit_or_success_text(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    value = backends._build_response(Response([b'{"stream":"Successfully built something\\n"}\n']))
    assert value["status"] == "completed"
    with pytest.raises(OSError, match="lost"):
        backends._build_response(Response([b'{"stream":"Successfully built something\\n"}\n'], OSError("lost")))
    value = backends._build_response(Response([b'{"errorDetail":{"message":"RUN failed"}}\n']))
    assert value["status"] == "failed" and "RUN failed" in value["reason"]
    response = Response([])
    response.headers = {}
    with pytest.raises(RuntimeError, match="boundary"):
        backends._build_response(response)


def build_authorization(tmp_path):
    config, state, ledger, _ = fixture(tmp_path)
    consumer, token = identifier(), identifier()
    resources = Resources(2, 768)
    ledger.request(token, consumer, token, "build", resources,
        {"kind": "build", "handle": own_handle(consumer)}, native=False)
    assert ledger.reserve(token)["state"] == "granted"
    root = ledger.path.parent / "builds" / consumer / token
    root.mkdir(parents=True)
    path = root / "context.tar"
    path.write_bytes(b"frozen fixture")
    from xgenius.evidence import hash_file
    envelope = LaunchEnvelope(
        LaunchKey(consumer, 1, token, token, token), "build", (sys.executable, "-c", "pass"),
        str(root), str(root), str(state.path), str(ledger.path), ledger.id, 10, resources, config.revision,
        metadata={"cpu_ids": [], "log_bytes": 1024, "build": {
            "runner": {"kind": "docker", "context": "fixture", "endpoint": "unix:///fixture", "image": "test:local"},
            "snapshot": {"tar": str(path), "tar_limit": 1024, "tar_sha256": hash_file(path, 1024)},
            "temporary_tag": "xgenius-build:" + token, "network": "none", "cpus": 1, "memory_mb": 512}})
    envelope = worker.prepare_runtime(state, envelope, root=root.parent)
    ledger.arm_consumer(envelope)
    path = root / "envelope.json"
    publish_bytes(path, canonical(asdict(envelope)))
    return state, ledger, envelope, path


def test_unknown_daemon_build_retains_resources_despite_dead_client(tmp_path, monkeypatch):
    state, ledger, envelope, path = build_authorization(tmp_path)
    monkeypatch.setattr(backends, "validate_docker_endpoint", lambda _: "unix:///fixture")
    monkeypatch.setattr(backends.payload, "run_process", lambda _: {
        "executed": True, "status": "timed_out", "reason": "client deadline", "returncode": None})
    assert backends.build_main("--build-supervisor", path) == 1
    assert ledger.consumer_run(envelope.key.grant_id)["phase"] == "executing"
    with pytest.raises(ValueError, match="quiescent"):
        ledger.release(envelope.key.grant_id, owner_id=envelope.key.campaign_id)
    assert ledger.recover_consumers()[0]["state"] == "awaiting_owned_receipt"
    assert state.campaign()["invocations"] == 0


def test_build_failure_response_releases_exact_consumer_and_can_recover_receipt(tmp_path, monkeypatch):
    state, ledger, envelope, path = build_authorization(tmp_path)
    monkeypatch.setattr(backends, "validate_docker_endpoint", lambda _: "unix:///fixture")
    monkeypatch.setattr(backends, "command", lambda *args, **kwargs: "")
    monkeypatch.setattr(backends.payload, "run_process", lambda _: {
        "executed": True, "status": "failed", "reason": "engine rejected RUN", "returncode": 1})
    publish_bytes(Path(envelope.root) / "build-result.json", canonical({
        "envelope_digest": envelope.digest, "status": "failed", "executed": True, "reason": "RUN failed"}))
    assert backends.build_main("--build-supervisor", path) == 1
    receipt = LaunchReceipt.parse(json.loads(ledger.consumer_run(envelope.key.grant_id)["receipt"]))
    assert receipt.quiescent and receipt.status == "failed"
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"
    assert ledger.observer_usage(envelope.key.campaign_id)["committed"] == 0


def test_build_context_mutation_is_pre_execution_failure(tmp_path, monkeypatch):
    _, ledger, envelope, path = build_authorization(tmp_path)
    monkeypatch.setattr(backends, "validate_docker_endpoint", lambda _: "unix:///fixture")
    Path(envelope.metadata["build"]["snapshot"]["tar"]).write_bytes(b"mutated")
    monkeypatch.setattr(backends.payload, "run_process", lambda _: pytest.fail("changed context executed"))
    assert backends.build_main("--build-supervisor", path) == 1
    receipt = LaunchReceipt.parse(json.loads(ledger.consumer_run(envelope.key.grant_id)["receipt"]))
    assert receipt.status == "not_started" and not receipt.executed
    assert ledger.grant(envelope.key.grant_id)["state"] == "released"


def test_only_matching_complete_build_receipts_can_retire_uncertain_consumers(tmp_path):
    _, ledger, envelope, _ = build_authorization(tmp_path)
    assert ledger.claim_consumer(envelope, own_handle(envelope.key.nonce))
    assert not ledger.claim_consumer(envelope, own_handle(envelope.key.nonce))
    receipt = LaunchReceipt(envelope.key, envelope.digest, "failed", True, 1, returncode=1, reason="engine failure")
    with pytest.raises(ValueError, match="ownership"):
        ledger.finish_consumer(replace(receipt, envelope_digest="different"))
    publish_bytes(Path(envelope.root) / "backend-receipt.json", canonical(asdict(receipt)))
    assert ledger.recover_consumers()[0]["state"] == "released"


@pytest.mark.skipif(os.environ.get("XGENIUS_DOCKER_BUILD_TESTS") != "1",
                    reason="Opt in to an already prepared local Docker engine and Python image")
@pytest.mark.parametrize("fails", [False, True])
def test_prepared_docker_build_has_real_engine_receipt_and_releases(tmp_path, fails):
    from xgenius.config import parse_config
    config, state, ledger, raw = fixture(tmp_path)
    tag = "xgenius-validation:" + identifier()
    raw["runners"]["container"] = {
        "kind": "docker", "context": os.environ.get("XGENIUS_DOCKER_CONTEXT", "default"),
        "python": "python", "image": tag, "network": False}
    config = parse_config(raw, config.config_path)
    (config.root / "fixture.txt").write_bytes(b"synthetic build marker")
    (config.root / "Dockerfile").write_text(
        "FROM python:3.11-slim\nCOPY fixture.txt /fixture.txt\n"
        + ("RUN python -c \"raise SystemExit(7)\"\n" if fails else
           "RUN python -c \"from pathlib import Path; assert Path('/fixture.txt').read_text() == 'synthetic build marker'\"\n"),
        encoding="utf-8")
    prefix = backends.docker_prefix(raw["runners"]["container"])
    try:
        result = backends.build(state, config, "container", config.root, ["fixture.txt"],
                                cpus=1, memory_mb=256, timeout=45)
        assert result["status"] == ("failed" if fails else "completed"), result
        assert result["failed"] == int(fails)
        record = ledger.consumer_run(result["grant"])
        receipt = LaunchReceipt.parse(json.loads(record["receipt"]))
        assert receipt.quiescent and receipt.executed
        assert ledger.grant(result["grant"])["state"] == "released"
        assert result["snapshot"]["bases"]["python:3.11-slim"].startswith("sha256:")
        assert result["resources"] == {"cpus": 2, "memory_mb": 512, "gpus": ()}
        assert not ledger.rows() and state.campaign()["invocations"] == 0
    finally:
        for grant in ledger.rows(history=True):
            if grant["kind"] != "build":
                continue
            record = ledger.consumer_run(grant["token"])
            if record is None or record["phase"] != "quiescent":
                continue
            envelope = LaunchEnvelope.parse(json.loads(record["envelope"]))
            assert not backends.command([*prefix, "ps", "--filter",
                "label=xgenius.build=" + envelope.key.nonce, "--format", "{{.ID}}"])
            owned = backends.command([*prefix, "image", "ls", "--filter",
                "label=xgenius.build=" + envelope.key.nonce, "--format", "{{.Repository}}:{{.Tag}}"])
            permitted = {tag, envelope.metadata["build"]["temporary_tag"]}
            for name in set(owned.splitlines()) & permitted:
                backends.command([*prefix, "image", "rm", "--no-prune", name])
            remaining = backends.command([*prefix, "image", "ls", "--all", "--no-trunc", "--filter",
                "label=xgenius.build=" + envelope.key.nonce, "--format", "{{.ID}}"])
            for image in dict.fromkeys(remaining.splitlines()):
                identity = json.loads(backends.command([*prefix, "image", "inspect", image, "--format",
                    '{"owner":{{json (index .Config.Labels "xgenius.build")}},"tags":{{json .RepoTags}}}']))
                assert identity["owner"] == envelope.key.nonce and not identity["tags"]
                backends.command([*prefix, "image", "rm", "--no-prune", image])
