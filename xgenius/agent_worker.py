"""Read only the result belonging to a successfully owned provider invocation."""

import json
from pathlib import Path

from xgenius.evidence import Capture, contained, parse_json
from xgenius.protocol import HANDOFF_BYTES, Handoff


def read_result(state, turn_id: str) -> dict:
    with state.db.read() as conn:
        turn = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        row = conn.execute("""SELECT i.id,l.envelope,l.receipt FROM invocations i
            JOIN launches l ON l.nonce=i.nonce WHERE i.turn_id=? AND i.kind!='canary'
            AND i.state='completed' AND l.phase='quiescent' ORDER BY i.bundle_position DESC LIMIT 1""",
                           (turn_id,)).fetchone()
    if not turn or not row:
        raise ValueError("A result requires a successfully completed owned provider invocation")
    envelope = json.loads(row["envelope"])
    receipt = json.loads(row["receipt"])
    owned = receipt.get("metadata", {}).get("result_capture", {})
    if owned.get("error"):
        raise ValueError(f"Owned provider result is invalid: {owned['error']}")
    path = contained(state.root, envelope["metadata"]["result_path"])
    capture = Capture.read(path, HANDOFF_BYTES)
    if capture.digest != owned.get("digest") or len(capture.body) != owned.get("bytes"):
        raise ValueError("Provider result changed after its owned completion")
    result = parse_json(capture.body)
    if not isinstance(result, dict) or result.get("turn_id") != turn_id or result.get("packet_id") != turn["packet_id"]:
        raise ValueError("Provider result does not belong to its exact turn and packet")
    if turn["kind"] in ("research", "final_analysis"):
        Handoff.parse(result)
    return {"content": result, "bytes": capture.body, "digest": capture.digest,
            "invocation_id": row["id"], "provider_receipt": receipt}


def accept_result(state, turn_id: str) -> dict:
    value = read_result(state, turn_id)
    with state.db.read() as conn:
        kind = conn.execute("SELECT kind FROM turns WHERE id=?", (turn_id,)).fetchone()[0]
    if kind == "compact":
        state.accept_compaction(value["content"], invocation_id=value["invocation_id"])
    elif kind == "report":
        from xgenius.reporting import publish_report
        publish_report(state, value["content"].get("view_id"), value=value["content"], invocation_id=value["invocation_id"])
    else:
        state.accept_handoff(Handoff.parse(value["content"]), invocation_id=value["invocation_id"])
    return value["content"]
