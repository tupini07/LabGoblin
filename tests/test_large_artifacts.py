import base64
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pytest

from labgoblin import cli, research_tools, workspace
from labgoblin.config import StorageConfig, Watermark, initial_config, parse_config, restore_config
from labgoblin.protocol import fingerprint
from labgoblin.evidence import (
    OBSERVATION_READ_BYTES, STREAM_BYTES, SizeLimitError, copy_bounded,
    export_observation, observation, observation_page, observation_record, require_space, verified_blocks,
)
from tests.test_dashboard import dump, get, serve
from tests.test_workspace import request, setup


def completed(config, state, files, *, old_policy=False):
    attempt = workspace.submit(state, config, request(artifacts=list(files)))
    spec = json.loads(attempt["spec"])
    for name, body in files.items():
        Path(spec["output"], name).write_bytes(body)
    if old_policy:
        spec.pop("capture_total_bytes")
    with state.db.write() as conn:
        conn.execute("UPDATE attempts SET status='completed',started=1,ended=2,exit_code=0,spec=? WHERE id=?",
                     (json.dumps(spec), attempt["id"]))
    result = workspace.collect_artifacts(state, attempt["id"])
    return result, [observation_record(state.db, oid) for oid in result["observation_ids"]]


def test_generous_defaults_and_existing_snapshot_limits_are_preserved(tmp_path):
    config, state, _ = setup(tmp_path)
    assert config.storage.capture_bytes == 16 * 1024**3
    result = workspace.validate_spec(config, request(), state=state)
    assert result["capture_limits"] == {
        "per_file_bytes": 16 * 1024**3, "per_attempt_bytes": 64 * 1024**3, "metrics_bytes": 256 * 1024,
    }
    snapshot = json.loads(json.dumps(asdict(config)))
    snapshot["storage"]["capture_bytes"] = 1024**2
    restored = restore_config(snapshot, revision=config.revision, path=config.config_path)
    assert restored.storage.capture_bytes == 1024**2
    assert snapshot["storage"]["capture_bytes"] == 1024**2


def test_new_capture_default_cannot_collide_with_an_existing_implicit_default(tmp_path):
    raw = initial_config("implicit-capture")
    config = parse_config(raw, tmp_path / "labgoblin.toml")
    original_revision = fingerprint(raw)
    assert config.revision != original_revision
    original = json.loads(json.dumps(asdict(config)))
    original["revision"] = original_revision
    original["storage"]["capture_bytes"] = 1024**2
    retained = restore_config(original, revision=original_revision, path=config.config_path)
    assert retained.revision == original_revision and retained.storage.capture_bytes == 1024**2
    raw.setdefault("storage", {})["capture_bytes"] = 16 * 1024**3
    assert parse_config(raw, config.config_path) == config


def test_large_capture_pages_and_export_use_bounded_reads(tmp_path, monkeypatch):
    config, state, _ = setup(tmp_path)
    data = bytes(range(256)) * 9000
    opened = Path.open
    read_sizes = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def seek(self, *args):
            return self.stream.seek(*args)

        def read(self, size=-1):
            assert 0 < size <= STREAM_BYTES
            read_sizes.append(size)
            return self.stream.read(size)

    def guarded(path, mode="r", *args, **kwargs):
        stream = opened(path, mode, *args, **kwargs)
        return Reader(stream) if mode == "rb" and path.stat().st_size > OBSERVATION_READ_BYTES else stream

    monkeypatch.setattr(Path, "open", guarded)
    result, records = completed(config, state, {"video.bin": data})
    assert result["collection"] == "complete"
    record = records[0]
    assert record["assurance"] == "captured"
    assert record["size"] == len(data) and record["digest"] == hashlib.sha256(data).hexdigest()
    with pytest.raises(SizeLimitError, match="byte pages"):
        observation(state.db, record["id"])
    page = observation_page(state.db, record["id"], offset=65530, limit=32)
    assert page["body"] == data[65530:65562] and page["has_more"]
    assert page["offset"] == 65530 and page["returned_bytes"] == 32
    assert observation_page(state.db, record["id"], offset=len(data), limit=1)["body"] == b""
    target = tmp_path / "exported.bin"
    exported = export_observation(state.db, record["id"], target)
    assert exported["sha256"] == record["digest"] and exported["bytes"] == len(data)
    assert read_sizes and max(read_sizes) == STREAM_BYTES
    with opened(target, "rb") as stream:
        assert stream.read() == data
    with pytest.raises(ValueError, match="1 MiB"):
        observation_page(state.db, record["id"], limit=OBSERVATION_READ_BYTES + 1)
    with pytest.raises(ValueError, match="offset"):
        observation_page(state.db, record["id"], offset=len(data) + 1)
    with pytest.raises(ValueError, match="campaign state"):
        export_observation(state.db, record["id"], state.root / "overwrite")


def test_frozen_total_allowance_retains_failures_and_allows_empty_file(tmp_path):
    config, state, _ = setup(tmp_path, storage={"capture_bytes": 16, "metrics_bytes": 16})
    files = {f"part-{i}": b"x" * 16 for i in range(4)}
    files.update({"over-total": b"x", "empty": b""})
    result, records = completed(config, state, files)
    assert result["collection"] == "failed"
    assert "per-attempt capture allowance" in result["reason"]
    assert len(records) == 6
    assert [record["assurance"] for record in records] == ["captured"] * 4 + ["unmanaged", "captured"]
    assert sum(record["size"] for record in records if record["assurance"] == "captured") == 64
    assert records[4]["digest"] is None
    assert observation(state.db, records[5]["id"])["body"] == b""


@pytest.mark.parametrize("old_policy", [False, True])
def test_oversized_output_respects_frozen_capture_policy(tmp_path, old_policy):
    config, state, _ = setup(tmp_path, storage={"capture_bytes": 16, "metrics_bytes": 16})
    result, records = completed(config, state, {"oversized": b"x" * 17}, old_policy=old_policy)
    assert result["collection"] == ("complete" if old_policy else "failed")
    assert records[0]["assurance"] == "unmanaged" and records[0]["digest"] is None
    assert "not retained or hashed" in records[0]["metadata"]["limitation"]
    assert observation(state.db, records[0]["id"])["body"] is None


def test_capture_growth_is_rejected_without_publishing_partial_object(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "capture"
    source.write_bytes(b"x" * (STREAM_BYTES * 2))
    opened = Path.open

    class ChangingReader:
        def __init__(self, stream):
            self.stream, self.changed = stream, False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            body = self.stream.read(size)
            if not self.changed:
                self.changed = True
                with opened(source, "ab") as output:
                    output.write(b"changed")
            return body

    def changing(path, mode="r", *args, **kwargs):
        stream = opened(path, mode, *args, **kwargs)
        return ChangingReader(stream) if path == source and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", changing)
    with pytest.raises(ValueError, match="changed during"):
        copy_bounded(source, target, STREAM_BYTES * 3)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_corruption_outside_page_is_rejected_by_reads_export_and_download(tmp_path):
    config, state, _ = setup(tmp_path)
    _, records = completed(config, state, {"data": b"x" * (OBSERVATION_READ_BYTES + 50)})
    record = records[0]
    retained = Path(record["metadata"]["capture_path"])
    with retained.open("r+b") as stream:
        stream.seek(OBSERVATION_READ_BYTES)
        stream.write(b"y")
    with pytest.raises(ValueError, match="modified"):
        observation_page(state.db, record["id"], limit=8)
    target = tmp_path / "export"
    with pytest.raises(ValueError, match="modified"):
        export_observation(state.db, record["id"], target)
    assert not target.exists()
    assert not list(tmp_path.glob(".export.*.tmp"))
    with serve(config.config_path, chat=False) as base:
        assert get(base, "/artifact?id=" + record["id"])[0] == 503


def test_large_dashboard_download_is_exact_and_readonly(tmp_path):
    config, state, _ = setup(tmp_path)
    data = b"binary\0" * 200000
    _, records = completed(config, state, {"dataset": data})
    before = dump(state)
    with serve(config.config_path, chat=False) as base:
        code, headers, body = get(base, "/artifact?id=" + records[0]["id"])
        assert code == 200 and body == data
        assert int(headers["Content-Length"]) == len(data)
        assert headers["X-Content-SHA256"] == hashlib.sha256(data).hexdigest()
        assert b"Captured download" in get(base, "/observation?id=" + records[0]["id"])[2]
    assert dump(state) == before


def test_cli_and_sdk_pages_and_cli_export_do_not_put_whole_artifact_in_context(tmp_path, capsys):
    config, state, _ = setup(tmp_path)
    data = bytes(range(256)) * 5000
    _, records = completed(config, state, {"dataset": data})
    oid = records[0]["id"]
    before = dump(state)
    assert cli.main(["--project", str(config.root), "evidence", "observation", "--id", oid,
                     "--offset", "257", "--bytes", "19", "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert base64.b64decode(response["base64"]) == data[257:276] and response["has_more"]
    target = tmp_path / "export"
    assert cli.main(["--project", str(config.root), "evidence", "observation", "--id", oid,
                     "--export", str(target), "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert "base64" not in response and response["bytes"] == len(data)
    assert target.read_bytes() == data
    response = research_tools.read(state, {"operation": "artifact", "id": oid, "offset": 31, "limit": 17})
    assert base64.b64decode(response["base64"]) == data[31:48] and response["has_more"]
    assert len(json.dumps(response).encode()) < 48000
    with pytest.raises(ValueError, match="16 KiB"):
        research_tools.read(state, {"operation": "artifact", "id": oid, "limit": 16385})
    assert dump(state) == before
    target.write_bytes(b"existing unrelated contents")
    with pytest.raises(ValueError, match="different bytes"):
        export_observation(state.db, oid, target)
    assert target.read_bytes() == b"existing unrelated contents"


def test_capture_space_accounts_for_the_pending_copy(tmp_path, monkeypatch):
    import labgoblin.evidence as evidence
    storage = StorageConfig(volumes={"project": Watermark(str(tmp_path), 1)})
    monkeypatch.setattr(evidence.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 2 * 1024**2})())
    require_space(storage, reserve_bytes=1024**2, destination=tmp_path)
    with pytest.raises(OSError, match="plus 1048577 bytes"):
        require_space(storage, reserve_bytes=1024**2 + 1, destination=tmp_path)


def test_changed_download_never_emits_a_complete_success_length(tmp_path):
    path = tmp_path / "captured"
    original = b"x" * (STREAM_BYTES * 3)
    path.write_bytes(original)
    with path.open("rb") as stream:
        blocks = verified_blocks(stream, len(original), hashlib.sha256(original).hexdigest())
        received = len(next(blocks))
        with path.open("r+b") as writer:
            writer.seek(STREAM_BYTES * 2)
            writer.write(b"y")
        with pytest.raises(ValueError, match="modified"):
            for block in blocks:
                received += len(block)
    assert received < len(original)


def test_stable_inputs_stream_instead_of_using_whole_object_capture(tmp_path, monkeypatch):
    from labgoblin.evidence import Capture
    data = tmp_path / "input.bin"
    body = b"dataset" * 200000
    data.write_bytes(body)
    config, state, _ = setup(tmp_path, inputs={"DATA": {
        "path": str(data), "assurance": "stable-consumption", "sha256": hashlib.sha256(body).hexdigest()}})
    if __import__("os").name != "nt":
        pytest.skip("Native stable-consumption input leases require Windows")

    def not_in_memory(*args):
        pytest.fail("Stable inputs must stream, not use Capture.read")

    monkeypatch.setattr(Capture, "read", not_in_memory)
    attempt = workspace.submit(state, config, request())
    prepared = json.loads(attempt["spec"])["inputs"]["DATA"]
    assert prepared["bytes"] == len(body)
    assert prepared["sha256"] == hashlib.sha256(body).hexdigest()
