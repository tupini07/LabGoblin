import json

import pytest

from labgoblin import session_log
from labgoblin.storage import canonical, read_json


def event(number, *, kind="tool.execution_complete", content="result"):
    return {"type": kind, "timestamp": f"2026-10-27T00:00:{number:02}Z",
            "data": {"toolCallId": f"call-{number}", "toolName": "python", "content": content}}


def test_segmented_original_events_filter_and_page_without_losing_late_work(tmp_path):
    log = session_log.EventLog(tmp_path, event_bytes=40000, total_bytes=1000000, segment_bytes=1000)
    originals = [event(i, content="x" * 150) for i in range(8)]
    for index, value in enumerate(originals):
        log.append(value, prompt=1 if index < 4 else 2)
    large = event(8, content="\u0394" * 10000)
    originals.append(large)
    log.append(large, prompt=2)
    log.close()
    reference = log.checkpoint()
    manifest = read_json(tmp_path / "events-manifest.json")
    assert len(manifest["segments"]) > 1 and not manifest["omitted"]
    retained = b"".join((tmp_path / segment["events"]).read_bytes() for segment in manifest["segments"])
    assert retained == b"".join(canonical(value) + b"\n" for value in originals)
    found = session_log.page(tmp_path, {"prompt": 2, "tool_call_id": "call-8"}, terminal_log=reference)
    assert [row["sequence"] for row in found["events"]] == [8]
    assert found["events"][0]["truncated"]
    first = session_log.page(tmp_path, {"event": 8}, terminal_log=reference)
    second = session_log.page(tmp_path, {"event": 8, "offset": first["next_offset"]}, terminal_log=reference)
    assert json.loads(first["text"] + second["text"]) == large
    assert second["next_offset"] is None


def test_oversized_event_is_explicit_and_does_not_hide_later_events(tmp_path):
    log = session_log.EventLog(tmp_path, event_bytes=300, total_bytes=10000)
    log.append(event(0, content="x" * 500), prompt=1)
    log.append(event(1), prompt=2)
    log.close()
    result = session_log.page(tmp_path, {}, terminal_log=log.checkpoint())
    assert result["retention_truncated"] and result["omitted"] == {"event_size": 1}
    assert result["events"][0]["omitted"] == "event_size"
    assert result["events"][1]["sequence"] == 1
    assert result["events"][1]["prompt"] == 2


def test_storage_limit_retains_explicit_missing_tail(tmp_path):
    log = session_log.EventLog(tmp_path, event_bytes=1000, total_bytes=800)
    for index in range(8):
        log.append(event(index), prompt=1)
    log.close()
    manifest = read_json(tmp_path / "events-manifest.json")
    assert manifest["bytes"] <= 800
    assert 0 < manifest["retained"] < 8
    assert manifest["retained"] + manifest["omitted"]["storage_limit"] == 8
    result = session_log.page(tmp_path, {}, terminal_log=log.checkpoint())
    assert result["observed_events"] == 8 and result["retention_truncated"]
    assert result["unindexed_range"] == [manifest["retained"], 8]


@pytest.mark.parametrize("target", ["manifest", "index", "event"])
def test_terminal_log_rejects_changed_captured_evidence(tmp_path, target):
    log = session_log.EventLog(tmp_path, event_bytes=1000, total_bytes=10000)
    log.append(event(0), prompt=1)
    log.close()
    reference = log.checkpoint()
    names = {"manifest": "events-manifest.json", "index": "events-index-000000.jsonl", "event": "events.jsonl"}
    path = tmp_path / names[target]
    if target == "manifest":
        value = read_json(path)
        value["seen"] += 1
        path.write_bytes(canonical(value))
    else:
        path.write_bytes(path.read_bytes().replace(b"python", b"foobar"))
    with pytest.raises(ValueError, match="differs"):
        session_log.page(tmp_path, {}, terminal_log=reference)


def test_live_reader_can_follow_index_after_last_manifest_checkpoint(tmp_path):
    log = session_log.EventLog(tmp_path, event_bytes=1000, total_bytes=10000)
    try:
        log.append(event(0), prompt=1)
        found = session_log.page(tmp_path, {})
        assert [row["sequence"] for row in found["events"]] == [0]
        assert not found["manifest_closed"]
        assert found["observed_events"] == 1
    finally:
        log.close()


def test_index_page_continuation_and_incomplete_live_tail(tmp_path):
    log = session_log.EventLog(tmp_path, event_bytes=1000, total_bytes=100000)
    try:
        for index in range(35):
            log.append(event(index), prompt=1)
        first = session_log.page(tmp_path, {"kind": "tool.execution_complete"})
        second = session_log.page(tmp_path, {"offset": first["next_offset"]})
        assert [row["sequence"] for row in first["events"] + second["events"]] == list(range(35))
        with (tmp_path / "events-index-000000.jsonl").open("ab") as stream:
            stream.write(b'{"sequence":')
        assert session_log.page(tmp_path, {"offset": 35})["incomplete_tail"]
    finally:
        log.close()


def test_long_metadata_keeps_original_bytes_and_rejects_unsearchable_filter(tmp_path):
    log = session_log.EventLog(tmp_path, event_bytes=10000, total_bytes=30000)
    original = event(0)
    original["data"]["toolCallId"] = "call-" + "x" * 2000
    log.append(original, prompt=1)
    log.close()
    assert session_log.page(tmp_path, {})["events"][0]["metadata_truncated"]
    assert json.loads(session_log.page(tmp_path, {"event": 0})["text"]) == original
    with pytest.raises(ValueError, match="128 characters"):
        session_log.page(tmp_path, {"tool_call_id": original["data"]["toolCallId"]})
