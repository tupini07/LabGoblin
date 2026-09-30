import base64
import hashlib
import json
from pathlib import Path
import time

import pytest

from tests.test_state import accept, handoff, state, turn
from xgenius import briefing, journal
from xgenius.evidence import SizeLimitError
from xgenius.protocol import PACKET_BYTES, canonical, identifier


def goal(state, value=b"Determine whether the synthetic control is reproducible."):
    return state.source("goal", value, origin="operator", head="goal")


def test_packet_freezes_cutoff_and_replay_does_not_read_new_context(state):
    original = goal(state)
    packet = briefing.prepare(state)
    path = Path(packet["path"])
    body = path.read_bytes()
    later = state.event("evidence", {"new": True})
    goal(state, b"Changed goal.")
    replay = briefing.prepare(state, turn_id=packet["turn_id"])
    assert replay["digest"] == packet["digest"]
    assert path.read_bytes() == body
    assert packet["content"]["sources"]["goal"]["id"] == original
    assert later not in {e["id"] for e in replay["content"]["events"]}
    assert len(body) <= PACKET_BYTES
    assert hashlib.sha256(body).hexdigest() == packet["digest"]


@pytest.mark.parametrize("after_write", [False, True])
def test_packet_publication_cutpoints_recover_exact_committed_bytes(state, monkeypatch, after_write):
    goal(state)
    original = briefing.publish_bytes

    def interrupted(path, body):
        if after_write:
            original(path, body)
        raise OSError("injected publication cutpoint")

    monkeypatch.setattr(briefing, "publish_bytes", interrupted)
    with pytest.raises(OSError, match="cutpoint"):
        briefing.prepare(state)
    with state.db.read() as conn:
        packet = dict(conn.execute("SELECT * FROM packets").fetchone())
        assert packet["ready"] == 0
    state.event("evidence", {"later": True})
    monkeypatch.setattr(briefing, "publish_bytes", original)
    recovered = briefing.publish(state, packet["id"])
    assert recovered["digest"] == packet["digest"]
    assert Path(recovered["path"]).read_bytes() == packet["content"].encode("utf-8")


def test_corrupt_published_packet_is_never_replaced_or_accepted(state):
    goal(state)
    packet = briefing.prepare(state)
    path = Path(packet["path"])
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="different bytes"):
        briefing.publish(state, packet["id"])
    assert path.read_bytes() == b"corrupt"


def test_large_event_has_bounded_header_and_exact_paged_payload(state):
    goal(state)
    event_id = state.event("evidence", {"value": "evidence " * 20000})
    packet = briefing.prepare(state)
    header = next(event for event in packet["content"]["events"] if event["id"] == event_id)
    assert header["truncated"] and len(header["payload_preview"].encode()) <= 512
    body = bytearray()
    offset = 0
    while True:
        page = briefing.event(state.db, event_id, offset=offset, limit=8000)
        body.extend(base64.b64decode(page["base64"]))
        offset += page["returned_bytes"]
        if not page["has_more"]:
            break
    assert hashlib.sha256(body).hexdigest() == header["payload_digest"]
    assert json.loads(body) == {"value": "evidence " * 20000}


def test_old_evidence_keeps_delivery_space_with_large_steering_backlog(state):
    goal(state)
    for number in range(100):
        state.event("directive", {"text": f"Operator note {number}"})
    old_evidence = state.event("evidence", {"score": 42})
    packet = briefing.prepare(state)
    assert packet["content"]["events"][0]["id"] == old_evidence
    assert len(packet["content"]["events"]) <= 64
    assert packet["content"]["coverage"]["has_more"]
    assert packet["content"]["coverage"]["pending_at_cutoff"] == 102


def test_mandatory_context_is_not_silently_clipped(state):
    goal(state, b"x" * PACKET_BYTES)
    with pytest.raises(SizeLimitError, match="Mandatory"):
        briefing.prepare(state)
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_current_rationale_is_exact_and_journal_is_the_owned_projection(state):
    goal(state)
    current = turn(state)
    accepted = handoff(current, rationale="Negative result: the control was contaminated; repeat independently.")
    accept(state, accepted)
    packet = briefing.prepare(state)
    rationale = packet["content"]["sources"]["rationale"]
    projected = journal.entry(state.db, rationale["id"])
    assert accepted.rationale in projected["markdown"]
    assert projected["handoff"]["turn_id"] == current.turn_id
    assert json.loads(rationale["text"]) == projected["handoff"]
    assert journal.page(state.db)["total"] == 1


def test_sealed_analysis_pins_constraints_and_goal(state):
    original = goal(state)
    directive_id = identifier()
    source_id = state.source("directive", b"Keep the original held-out split.", origin="operator")
    with state.db.write() as conn:
        conn.execute("""INSERT INTO directives(id,source_id,origin,scope,created)
            VALUES(?,?,'operator','campaign',?)""", (directive_id, source_id, time.time()))
    current = turn(state)
    accept(state, handoff(current, disposition="finalize", stopping_criterion="Controls fully assessed."))
    goal(state, b"New goal, outside the sealed assessment.")
    with state.db.write() as conn:
        conn.execute("UPDATE directives SET active=0")
    packet = briefing.prepare(state, "final_analysis")
    assert packet["content"]["sources"]["goal"]["id"] == original
    assert packet["content"]["directives"][0]["source_id"] == source_id
    with pytest.raises(ValueError, match="one final analysis"):
        briefing.prepare(state, "final_analysis")


def test_journal_edits_alone_do_not_advance_research(state):
    goal(state)
    packet = briefing.prepare(state)
    (state.root / "journal.md").write_text("Research complete.", encoding="utf-8")
    assert journal.page(state.db)["total"] == 0
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM turns WHERE id=?", (packet["turn_id"],)).fetchone()[0] == "prepared"
