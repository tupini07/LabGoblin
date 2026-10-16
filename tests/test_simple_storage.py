from pathlib import Path
import os

import pytest

from labgoblin.storage import inside, publish_json, read_json, run_lock, snapshot


def test_snapshot_preserves_sources_outputs_and_explicit_exclusions(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "candidate.py").write_text("print('candidate')", encoding="utf-8")
    (root / "artifacts").mkdir()
    content = b"dataset" * 300000
    (root / "artifacts" / "dataset.bin").write_bytes(content)
    (root / "inputs").mkdir()
    (root / "inputs" / "external.bin").write_bytes(b"input is not a captured result")
    state = root / ".labgoblin"
    state.mkdir()
    result = snapshot(root, state / "snapshot", 4 * 1024 * 1024)
    assert (state / "snapshot" / "artifacts" / "dataset.bin").read_bytes() == content
    assert result["files"] == 2
    assert result["excluded"] == [".labgoblin", "inputs"]


def test_snapshot_overflow_is_explicit_not_truncated_success(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "large").write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="exceeds"):
        snapshot(root, tmp_path / "capture", 99)
    assert not (tmp_path / "snapshot.json").exists()


def test_snapshot_excludes_credential_and_state_names_case_insensitively(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in (".ENV", ".env.production", ".npmrc", ".git-credentials"):
        (root / name).write_text("synthetic private fixture", encoding="utf-8")
    for name in (".SSH", ".AWS", ".Copilot", ".GIT", ".VENV"):
        (root / name).mkdir()
        (root / name / "private").write_text("synthetic private fixture", encoding="utf-8")
    (root / "report.md").write_text("Retain the research report.", encoding="utf-8")
    result = snapshot(root, tmp_path / "capture", 4096)
    assert result["files"] == 1
    assert set(result["excluded"]) == {
        ".ENV", ".env.production", ".npmrc", ".git-credentials", ".SSH", ".AWS", ".Copilot", ".GIT", ".VENV"}
    assert sorted(path.name for path in (tmp_path / "capture").iterdir()) == ["report.md"]


def test_snapshot_file_count_is_bounded_even_for_empty_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for index in range(4):
        (root / str(index)).touch()
    with pytest.raises(ValueError, match="filesystem entries"):
        snapshot(root, tmp_path / "capture", 100, max_entries=3)


@pytest.mark.skipif(os.name == "nt", reason="POSIX special file")
def test_snapshot_never_blocks_reading_a_fifo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    os.mkfifo(root / "pipe")
    result = snapshot(root, tmp_path / "capture", 100)
    assert result["excluded"] == ["pipe"] and result["files"] == 0


def test_immutable_record_rejects_overwrite_and_nonfinite_values(tmp_path):
    target = tmp_path / "record.json"
    publish_json(target, {"value": 1})
    with pytest.raises(FileExistsError):
        publish_json(target, {"value": 2})
    assert read_json(target) == {"value": 1}
    with pytest.raises(ValueError):
        publish_json(tmp_path / "nonfinite.json", {"value": float("nan")})
    (tmp_path / "bad.json").write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_json(tmp_path / "bad.json")


def test_control_reads_are_bounded_and_paths_cannot_escape(tmp_path):
    target = tmp_path / "record.json"
    target.write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="exceeds"):
        read_json(target, 99)
    with pytest.raises(ValueError, match="escapes"):
        inside(tmp_path, "..")


def test_controller_lock_is_exclusive(tmp_path):
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    with run_lock(path):
        with pytest.raises(ValueError, match="Another controller"):
            with run_lock(path):
                pass
    with run_lock(path):
        pass
