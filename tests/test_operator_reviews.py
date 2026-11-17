import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import time

import pytest

from labgoblin import operator_reviews as reviews
from labgoblin import payload
from labgoblin.processes import process_state
from labgoblin.storage import digest, hash_file, publish_json, read_json
from tests.test_inspector import captured, identities


@pytest.fixture
def ready(project, ledger, tmp_path):
    root, _, record = project
    (root / "report.md").write_text("Saved finding, not necessarily scientific success.")
    envelope = captured(project, ledger)
    media = tmp_path / "models"
    media.mkdir()
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        (media / name).write_bytes(b"Model-free fixture; never synthesize")
    return {
        "project": root, "directory": tmp_path / "operator", "run_id": envelope.id,
        "max_credits": 30, "media_python": sys.executable, "models": media,
        "copilot": sys.executable,
    }


def test_review_capture_and_completed_journal_never_mutate_research(ready, project, ledger, monkeypatch):
    root, _, record = project
    before, rows = identities(root), ledger.runs(record["id"])
    calls = []

    def fake(spec):
        calls.append(spec)
        request = read_json(spec["argv"][-2])
        assert digest(request) == spec["argv"][-1]
        assert spec["seconds"] == 5370
        assert request["max_ai_credits"] == 30
        manifest = reviews.capture(request)
        attempt = Path(request["directory"])
        assert (attempt / "sources" / "research" / "report.md").is_file()
        assert any(entry["path"] == ".labgoblin" for entry in manifest["omitted"])
        (attempt / "report.html").write_text("<h1>Qualified independent finding</h1>")
        (attempt / "review.mp4").write_bytes(b"Model-free renderer fixture")
        (attempt / "watch.html").write_text("<video controls src='review.mp4'></video>")
        publish_json(attempt / "media.json", {"decode_verified": True,
                                            "video_sha256": hash_file(attempt / "review.mp4")})
        publish_json(attempt / "usage.json", {"totalNanoAiu": 12_000_000_000})
        return {"status": "completed", "reason": "", "executed": True}

    monkeypatch.setattr(reviews.payload, "run_process", fake)
    result = reviews.review(**ready)
    assert result["status"] == "ready" and not result["research_resumed"]
    assert result["usage_status"] == "recorded"
    assert identities(root) == before and ledger.runs(record["id"]) == rows
    assert len(calls) == 1
    assert reviews.review(**ready)["status"] == "ready"
    assert len(calls) == 1
    entries = reviews.history(ready["directory"])
    assert len(entries) == 1 and entries[0]["run_id"] == ready["run_id"]
    index = (ready["directory"] / "reviews" / "index.html").read_text(encoding="utf-8")
    assert "report.html" in index and "watch.html" in index and "outcome.json" in index


@pytest.mark.parametrize("cap", ["unlimited", 0, 29, True, float("nan")])
def test_review_requires_explicit_finite_allowance(ready, cap):
    ready["max_credits"] = cap
    with pytest.raises(ValueError):
        reviews.review(**ready)
    assert not ready["directory"].exists()


@pytest.mark.parametrize("seconds", [0, -1, 5401, float("inf")])
def test_total_deadline_cannot_exceed_ninety_minutes(ready, seconds):
    with pytest.raises(ValueError):
        reviews.review(**ready, seconds=seconds)
    assert not ready["directory"].exists()


def test_watcher_starts_once_only_after_exact_settlement(ready, monkeypatch):
    original = reviews.settled
    observations = iter([None, None, original(ready["project"], ready["run_id"])])
    monkeypatch.setattr(reviews, "settled", lambda *_: next(observations))
    sleeps = []
    monkeypatch.setattr(reviews.time, "sleep", sleeps.append)
    calls = []
    def fail(spec):
        calls.append(spec)
        return {"status": "failed", "reason": "Fixture worker failed", "executed": True}
    monkeypatch.setattr(reviews.payload, "run_process", fail)
    result = reviews.review(**ready, watch=True)
    assert result["status"] == "failed" and len(calls) == 1 and sleeps == [2, 2]
    assert reviews.review(**ready, watch=True)["status"] == "failed"
    assert len(calls) == 1


def test_timeout_keeps_written_review_but_never_publishes_partial_video(ready, monkeypatch):
    def timeout(spec):
        request = read_json(spec["argv"][-2])
        root = Path(request["directory"])
        (root / "report.html").write_text("Completed written review")
        (root / "review.mp4").write_bytes(b"Worker claimed a video prematurely")
        publish_json(root / "provider-start.json", {"test": "Provider invocation was attempted"})
        return {"status": "timed_out", "reason": "Operation deadline exceeded", "executed": True}
    monkeypatch.setattr(reviews.payload, "run_process", timeout)
    result = reviews.review(**ready)
    assert result["status"] == "timed_out" and result["usage_status"] == "unknown"
    assert "report.html" in result["outputs"] and "review.mp4" not in result["outputs"]
    assert "review.mp4" not in (ready["directory"] / "reviews" / "index.html").read_text()


def test_interrupted_request_is_visible_and_cannot_rebuy_inference(ready, monkeypatch):
    def failed(spec):
        return {"status": "failed", "reason": "Fixture", "executed": True}
    monkeypatch.setattr(reviews.payload, "run_process", failed)
    result = reviews.review(**ready)
    directory = Path(result["directory"])
    (directory / "state.json").unlink()
    (directory / "outcome.json").unlink()
    monkeypatch.setattr(reviews.payload, "run_process", lambda _: pytest.fail("Unexpected replay"))
    assert reviews.review(**ready)["status"] == "incomplete"
    (directory / "request.json").unlink()
    with pytest.raises(ValueError, match="Incomplete review directory"):
        reviews.review(**ready)


def test_owned_deadline_stops_worker_and_descendant(ready, monkeypatch, tmp_path):
    original = payload.run_process
    child_path = tmp_path / "child.json"
    script = (
        "import subprocess,sys,time,json,psutil;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"open({str(child_path)!r},'w').write(json.dumps({{'pid':p.pid,'created':psutil.Process(p.pid).create_time()}}));"
        "time.sleep(60)"
    )
    def launch(spec):
        spec["argv"] = [sys.executable, "-c", script]
        return original(spec)
    monkeypatch.setattr(reviews.payload, "run_process", launch)
    began = time.monotonic()
    result = reviews.review(**ready, seconds=6)
    assert result["status"] == "timed_out", result
    assert time.monotonic() - began < 10
    assert process_state(read_json(child_path)) == "dead"
    request = read_json(Path(result["directory"]) / "request.json")
    assert payload.tree_state(Path(result["directory"]) / "process", request["token"]) == "dead"


def test_capture_discloses_omissions_and_preserves_input_boundary(ready, monkeypatch):
    root = ready["project"]
    (root / "inputs").mkdir(exist_ok=True)
    (root / "inputs" / "private.txt").write_text("Never capture")
    (root / "weights.bin").write_bytes(b"Not a review source")
    def collect(spec):
        request = read_json(spec["argv"][-2])
        manifest = reviews.capture(request)
        assert {item["path"] for item in manifest["omitted"]} >= {"inputs", "weights.bin"}
        assert not (Path(request["directory"]) / "sources" / "research" / "inputs").exists()
        return {"status": "failed", "reason": "Capture-only fixture", "executed": True}
    monkeypatch.setattr(reviews.payload, "run_process", collect)
    assert reviews.review(**ready)["status"] == "failed"


def test_capture_prioritizes_summaries_over_bulk_and_preserves_control_evidence(ready, monkeypatch):
    root = ready["project"]
    bulk = root / "experiments" / "early-bulk"
    bulk.mkdir(parents=True)
    for index in range(25):
        (bulk / f"case-{index}.json").write_text(json.dumps({"input": "x" * 1000}))
    latest = root / "experiments" / "latest"
    latest.mkdir()
    (latest / "results.json").write_text('{"qualified_finding":42}')
    monkeypatch.setattr(reviews, "CAPTURE_BYTES", 20000)
    monkeypatch.setattr(reviews, "CAPTURE_FILES", 12)
    def collect(spec):
        request = read_json(spec["argv"][-2])
        manifest = reviews.capture(request)
        assert len(manifest["files"]) <= 12
        paths = {item["path"] for item in manifest["files"]}
        assert str(Path("sources") / "research" / "experiments" / "latest" / "results.json") in paths
        assert str(Path("sources") / "executions" / ready["run_id"] / "receipt.json") in paths
        assert any("case-" in item["path"] for item in manifest["omitted"])
        return {"status": "failed", "reason": "Capture-only fixture", "executed": True}
    monkeypatch.setattr(reviews.payload, "run_process", collect)
    assert reviews.review(**ready)["status"] == "failed"


@pytest.mark.parametrize("suffix", [".py", ".json", ".png", ".csv"])
def test_repeated_snapshots_cannot_crowd_out_results_and_visuals(ready, monkeypatch, suffix):
    root = ready["project"]
    for index in range(25):
        code = root / "experiments" / f"snapshot-{index}" / "code"
        code.mkdir(parents=True)
        (code / f"method{suffix}").write_text("Repeated frozen artifact")
    latest = root / "experiments" / "latest"
    latest.mkdir()
    (latest / "results.json").write_text('{"qualified_finding":42}')
    (latest / "comparison.png").write_bytes(b"Retained figure; no image decoding in capture")
    monkeypatch.setattr(reviews, "CAPTURE_FILES", 12)

    def collect(spec):
        request = read_json(spec["argv"][-2])
        manifest = reviews.capture(request)
        paths = {Path(item["path"]).name for item in manifest["files"]}
        assert {"results.json", "comparison.png"} <= paths
        assert len(manifest["files"]) <= 12
        return {"status": "failed", "reason": "Capture-only fixture", "executed": True}

    monkeypatch.setattr(reviews.payload, "run_process", collect)
    assert reviews.review(**ready)["status"] == "failed"


def test_review_worker_disables_remote_export_without_another_invocation(ready, monkeypatch):
    skill = ready["directory"] / ".github" / "skills" / "labgoblin-review"
    reviews.shutil.copytree(reviews.ASSETS, skill)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--prompt" in command:
            (Path(command[command.index("--usage-output-file") + 1]).parent / "report.html").write_text(
                "Model-free review fixture")

    def launch(spec):
        reviews.worker(spec["argv"][-2], spec["argv"][-1])
        return {"status": "failed", "reason": "Model-free worker fixture", "executed": True}

    monkeypatch.setattr(reviews.subprocess, "run", run)
    monkeypatch.setattr(reviews.payload, "run_process", launch)
    reviews.review(**ready)
    invocations = [command for command in calls if "--prompt" in command]
    assert len(invocations) == 1
    assert "--sync-session=off" in invocations[0]
    assert "--no-remote-export" in invocations[0]
    attempt = Path(invocations[0][invocations[0].index("--usage-output-file") + 1]).parent
    captured_playbook = attempt / "tooling" / "references" / "review-playbook.md"
    assert str(captured_playbook) in invocations[0][invocations[0].index("--prompt") + 1]
    assert captured_playbook.read_bytes() == (reviews.ASSETS / "references" / "review-playbook.md").read_bytes()
    assert {"path": str(captured_playbook.relative_to(attempt)), "sha256": hash_file(captured_playbook)} in read_json(
        attempt / "tooling.json")
    assert len(calls) == 3


def test_cited_paths_handles_report_links_without_expanding_scope(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        '![Figure](experiments/partial/figure.png "Observed result")\n'
        '[Table](<experiments/partial/case%20results.csv> "Case results")\n'
        '<img src="experiments/partial/input&amp;output.png#detail">\n'
        '`experiments\\partial\\results.json` [Empty]()\n'
        '[Outside](../outside.png) [Remote](https://example.com/image.png)\n'
        '`C:\\private\\image.png` `/absolute/image.png`\n'
    )
    assert reviews.cited_paths(report) == {
        "experiments/partial/figure.png", "experiments/partial/case results.csv",
        "experiments/partial/input&output.png", "experiments/partial/results.json",
    }


def test_capture_keeps_cited_figures_and_case_results_ahead_of_newer_snapshots(ready, monkeypatch):
    root = ready["project"]
    study = root / "experiments" / "cited"
    (study / "figures").mkdir(parents=True)
    (study / "figures" / "comparison.png").write_bytes(b"Original figure")
    (study / "results.json").write_text('{"cases":[{"score":0.5}]}')
    (root / "report.md").write_text(
        "![Observed result](experiments/cited/figures/comparison.png)\n"
        "`inputs/private.png` ![External](../outside.png)\n"
    )
    (root / "inputs").mkdir(exist_ok=True)
    (root / "inputs" / "private.png").write_bytes(b"Excluded even when cited")
    for index in range(25):
        bulk = root / "experiments" / f"snapshot-{index}"
        bulk.mkdir()
        for name in ("results.json", "comparison.png", "notes.md", "method.py"):
            (bulk / name).write_text("Newer bulk snapshot")
    monkeypatch.setattr(reviews, "CAPTURE_FILES", 10)

    def collect(spec):
        request = read_json(spec["argv"][-2])
        manifest = reviews.capture(request)
        paths = {Path(item["path"]).as_posix() for item in manifest["files"]}
        assert "sources/research/experiments/cited/figures/comparison.png" in paths
        assert "sources/research/experiments/cited/results.json" in paths
        assert not any("inputs" in Path(path).parts for path in paths)
        assert any("snapshot-" in item["path"] for item in manifest["omitted"])
        return {"status": "failed", "reason": "Capture-only fixture", "executed": True}

    monkeypatch.setattr(reviews.payload, "run_process", collect)
    reviews.review(**ready)


def test_journal_directory_alias_cannot_write_into_research(ready):
    operator = ready["directory"]
    operator.mkdir()
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(ready["project"]), str(operator / "reviews"))
    else:
        (operator / "reviews").symlink_to(ready["project"], target_is_directory=True)
    before = identities(ready["project"])
    with pytest.raises(ValueError, match="aliases"):
        reviews.review(**ready)
    assert identities(ready["project"]) == before


@pytest.fixture
def renderer():
    path = reviews.ASSETS / "scripts" / "render.py"
    spec = importlib.util.spec_from_file_location("review_renderer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storyboard_requires_local_cited_visuals_and_accepts_animation(renderer, tmp_path):
    (tmp_path / "one.png").write_bytes(b"Image decoding is done by the media environment")
    (tmp_path / "two.png").write_bytes(b"Second animation frame")
    (tmp_path / "result.json").write_text("{}")
    scene = {"title": "Finding", "takeaway": "Qualified finding", "narration": "Describe the evidence.",
             "frames": ["one.png", "two.png"], "sources": ["result.json"]}
    value = {"version": 1, "scenes": [scene] * 3}
    (tmp_path / "storyboard.json").write_text(json.dumps(value))
    assert renderer.load_storyboard(tmp_path) == value
    for invalid in ("../escape.png", str(tmp_path / "one.png"), "https://example.com/image.png"):
        scene["frames"] = ["one.png", invalid]
        (tmp_path / "storyboard.json").write_text(json.dumps(value))
        with pytest.raises(ValueError):
            renderer.load_storyboard(tmp_path)


def test_visual_reveals_follow_sentence_starts_including_pauses(renderer):
    timing = {"start": 10, "end": 45, "sentence_starts": [10, 30, 34]}
    scene = {"frame_sentences": [0, 1, 2]}
    assert [renderer.visual_index(scene, timing, now, 3) for now in (10, 29.99, 30, 33.99, 34, 45)] == [
        0, 0, 1, 1, 2, 2]
    assert renderer.visual_index({}, timing, 29, 3) == 1


@pytest.mark.parametrize("starts", [[1, 2], [0, 0], [0, -1], [0, 3], [0], [False, 1], "0,1"])
def test_storyboard_rejects_invalid_sentence_reveals(renderer, tmp_path, starts):
    (tmp_path / "figure.png").write_bytes(b"Fixture")
    scene = {"title": "Finding", "takeaway": "Qualified finding",
             "narration": "First sentence. Longer second sentence. Last sentence.",
             "frames": ["figure.png", "figure.png"], "frame_sentences": starts,
             "sources": ["figure.png"]}
    (tmp_path / "storyboard.json").write_text(json.dumps({"version": 1, "scenes": [scene] * 3}))
    with pytest.raises(ValueError, match="frame_sentences"):
        renderer.load_storyboard(tmp_path)


@pytest.mark.skipif(not os.environ.get("LABGOBLIN_REVIEW_MEDIA_PYTHON"),
                    reason="Explicitly prepared local media environment/models required; never downloads")
def test_real_local_narration_animation_and_decode(tmp_path):
    media = os.environ["LABGOBLIN_REVIEW_MEDIA_PYTHON"]
    models = os.environ["LABGOBLIN_REVIEW_MODELS"]
    code = (
        "from PIL import Image,ImageDraw;from pathlib import Path;import sys;"
        "p=Path(sys.argv[1]);"
        "a=Image.new('RGB',(960,440),'#173548');d=ImageDraw.Draw(a);"
        "d.rectangle((100,100,300,300),fill='#66d9c6');a.save(p/'one.png');"
        "d.rectangle((500,100,700,300),fill='#e8b64e');a.save(p/'two.png')"
    )
    subprocess.run([media, "-I", "-B", "-c", code, str(tmp_path)], check=True)
    (tmp_path / "source.json").write_text('{"kind":"synthetic renderer test, not research evidence"}')
    narration = (
        "This is a synthetic presentation test, not a scientific result. "
        "The first frame contains one coloured square, and the next frame adds another. "
        "The animation makes that change visible without suggesting that a new experiment was performed. "
        "The full written review would explain the actual evidence and its limitations."
    )
    scenes = [{"title": f"Local rendering check {i + 1}", "takeaway": "Synthetic media check, not research evidence",
               "narration": narration, "frames": ["one.png", "two.png"], "frame_sentences": [0, 2],
               "sources": ["source.json"],
               "highlights": [{"box": [.1, .2, .3, .5], "start": .2, "end": .8}]} for i in range(3)]
    (tmp_path / "storyboard.json").write_text(json.dumps({"version": 1, "title": "Media readiness", "scenes": scenes}))
    (tmp_path / "report.html").write_text("Synthetic media check")
    subprocess.run([media, "-I", "-B", str(reviews.ASSETS / "scripts" / "render.py"), str(tmp_path),
                    "--models", models, "--theme", str(reviews.ASSETS / "assets" / "theme.json")],
                   check=True, timeout=300)
    result = read_json(tmp_path / "media.json")
    assert result["decode_verified"] and result["duration_seconds"] > 30
    assert result["video_sha256"] == hash_file(tmp_path / "review.mp4")
    assert (tmp_path / "transcript.vtt").read_text().startswith("WEBVTT")
    assert "currentTime" in (tmp_path / "watch.html").read_text()
    assert not (tmp_path / "review.partial.mp4").exists()
    timing = read_json(tmp_path / "timing.json")
    for scene in timing["scenes"]:
        cues = [cue for cue in timing["captions"] if scene["start"] <= cue["start"] < scene["end"]]
        assert scene["sentence_starts"] == [cue["start"] for cue in cues]
