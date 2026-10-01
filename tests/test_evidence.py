import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from labgoblin.config import StorageConfig, Watermark
from labgoblin.evidence import (
    Capture, SizeLimitError, atomic_json, contained, copy_bounded, hash_file,
    parse_json, publish_bytes, read_bytes, read_json, require_space, tail,
)
from labgoblin.processes import BoundedSpool, background_options


def test_capture_parse_digest_and_publication_use_same_bytes(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_bytes(b'{"score":42,"delta":-3}')
    captured = Capture.read(path, 256)
    path.write_bytes(b'{"score":999}')
    target = tmp_path / "retained.json"
    assert publish_bytes(target, captured.body) == captured.digest
    assert captured.metrics() == {"score": 42, "delta": -3}
    assert target.read_bytes() == captured.body
    assert hashlib.sha256(captured.body).hexdigest() == captured.digest
    assert publish_bytes(target, captured.body) == captured.digest
    with pytest.raises(ValueError, match="different bytes"):
        publish_bytes(target, path.read_bytes())
    assert target.read_bytes() == captured.body


@pytest.mark.parametrize("body", [
    b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":true}',
    b'{"a":null}', b'{"a":[]}', b'[]', b'{"":1}', b'{"a":1e999}',
])
def test_invalid_metrics_are_explicit(body, tmp_path):
    path = tmp_path / "metrics.json"
    path.write_bytes(body)
    with pytest.raises(ValueError):
        Capture.read(path, 1024).metrics()


def test_bounded_copy_hash_and_immutable_target(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"a" * 1025)
    with pytest.raises(SizeLimitError):
        copy_bounded(source, target, 1024)
    assert not target.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source"]
    result = copy_bounded(source, target, 1025)
    assert result == {"bytes": 1025, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    assert hash_file(target, 1025) == result["sha256"]
    with pytest.raises(SizeLimitError):
        hash_file(target, 1024)
    with pytest.raises(FileExistsError):
        copy_bounded(source, target, 1025)


def test_tail_reads_bounded_bytes_even_without_newlines(tmp_path, monkeypatch):
    path = tmp_path / "stdout.log"
    path.write_bytes(b"x" * 100_000 + b"\nlast\n")
    original = Path.open
    counts = []

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
            assert 0 <= size <= 32
            result = self.stream.read(size)
            counts.append(len(result))
            return result

    def opened(selected, *args, **kwargs):
        stream = original(selected, *args, **kwargs)
        return Reader(stream) if selected == path else stream

    monkeypatch.setattr(Path, "open", opened)
    value = tail(path, limit=32, lines=1)
    assert value["text"] == "last\n"
    assert value["bytes_read"] == sum(counts) == 32
    assert value["truncated"]


def test_multibyte_tail_and_rotated_order_are_bounded(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes("last \u03bb\n".encode())
    path.with_name("stdout.log.previous").write_bytes("older \u03bb\n".encode())
    result = tail(path, limit=64)
    assert result["text"] == "older \u03bb\nlast \u03bb\n"
    assert result["truncated"]
    assert tail(path, limit=3)["bytes_read"] == 3


def test_atomic_failure_preserves_previous_revision(tmp_path, monkeypatch):
    import labgoblin.evidence as implementation
    target = tmp_path / "receipt.json"
    atomic_json(target, {"before": True})

    def failed(*args):
        raise OSError("injected volume write failure")

    monkeypatch.setattr(implementation.os, "replace", failed)
    with pytest.raises(OSError, match="injected"):
        atomic_json(target, {"after": True})
    assert read_json(target) == {"before": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["receipt.json"]


def test_read_containment_and_explicit_volume_watermark(tmp_path, monkeypatch):
    import labgoblin.evidence as implementation
    path = tmp_path / "small.json"
    path.write_bytes(b'{"ok":true}')
    assert parse_json(read_bytes(path, 64)) == {"ok": True}
    with pytest.raises(SizeLimitError):
        read_json(path, 4)
    with pytest.raises(ValueError, match="escapes"):
        contained(tmp_path, "..\\not-owned")
    monkeypatch.setattr(implementation.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 100})())
    cfg = StorageConfig(volumes={"fixture": Watermark(str(tmp_path), 1)})
    with pytest.raises(OSError, match="Storage admission blocked"):
        require_space(cfg)


@pytest.mark.parametrize("limit", [1, 127, 128, 1024])
def test_real_child_output_keeps_draining_after_retention_limit(tmp_path, limit):
    spool = BoundedSpool(tmp_path / "stdout.log", limit)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*200000+b'END')"],
        stdout=spool.writer, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **background_options())
    spool.close_writer()
    try:
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    stats = spool.finish()
    assert stats["received_bytes"] == 200003
    assert stats["retained_bytes"] <= limit
    assert stats["truncated"]
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= limit
    assert tail(spool.path, limit=limit)["text"].endswith("END"[-min(3, limit):])


def test_spool_write_failure_does_not_deadlock_child_or_report_success(tmp_path):
    spool = BoundedSpool(tmp_path / "missing" / "stdout.log", 128)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*200000)"],
        stdout=spool.writer, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **background_options())
    spool.close_writer()
    try:
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    with pytest.raises(OSError, match="retain owned output"):
        spool.finish()
    assert spool.snapshot()["received_bytes"] == 200000
    assert spool.snapshot()["error"]


@pytest.mark.parametrize("code", [5, 32, 33])
def test_windows_file_replacement_retries_transient_reader_denials(tmp_path, monkeypatch, code):
    from labgoblin import processes
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    original = processes.os.replace
    calls = []

    def replace(*args):
        calls.append(True)
        if len(calls) == 1:
            error = PermissionError("reader temporarily prevents replacement")
            error.winerror = code
            raise error
        return original(*args)

    monkeypatch.setattr(processes.os, "replace", replace)
    processes.replace_file(source, target)
    assert target.read_bytes() == b"new"
    assert len(calls) == 2


def test_permanent_file_denial_is_bounded_and_preserves_error(tmp_path, monkeypatch):
    from labgoblin import processes
    calls = []

    def denied(*args):
        calls.append(True)
        error = PermissionError("persistent access denial")
        error.winerror = 5
        raise error

    monkeypatch.setattr(processes.os, "replace", denied)
    monkeypatch.setattr(processes.time, "sleep", lambda delay: None)
    with pytest.raises(PermissionError, match="persistent"):
        processes.replace_file(tmp_path / "new", tmp_path / "old")
    assert len(calls) == 10


def test_supervisor_diagnostics_have_a_finite_utf8_bound():
    import io
    from labgoblin.processes import BoundedDiagnostic
    stream = io.BytesIO()
    output = BoundedDiagnostic(stream, 256)
    output.write("\U0001f680" * 1000)
    output.write("never retained" * 1000)
    body = stream.getvalue()
    assert len(body) <= 256
    assert "truncated" in body.decode("utf-8")
    assert output.truncated
