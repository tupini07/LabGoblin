"""Owned native execution and complete CLI handoff flow, without model requests."""

import json
from pathlib import Path

import pytest

from labgoblin import workspace
from labgoblin.campaign import Campaign
from labgoblin.config import parse_config
from tests.test_closure_v3 import CLOSER
from tests.test_controller import fixture
from tests.test_workspace import request


def test_native_controller_carries_goal_gaps_and_accepts_typed_cli_completion(tmp_path):
    ending = r'''
if packet["kind"] == "research":
    carried = packet.get("goal_progress", {})
    if not carried.get("preview_included"):
        result.update(disposition="continue", reason="Assess the synthetic result in the next fixture turn.",
                      stopping_criterion="", stop_basis="")
        result["goal_progress"] = {
            "coverage": "Model-free fixture, not an actual research-quality judgment.",
            "items": [{"deliverable": "Assess retained score and limitations", "status": "unresolved",
                       "assessment": "The next turn must receive this exact accepted assessment.", "references": []}]}
        result["commitment"] = {
            "branch": "native-fixture", "boundary": "branch", "goal_connection": "Assess the retained control.",
            "decision_value": "Complete the end-to-end acceptance contract.", "revisit_when": "The control is assessed."}
    else:
        assert carried["applicability"] == "current"
        assert carried["assessment"]["items"][0]["status"] == "unresolved"
        assert carried["source_id"] in packet["references"]
draft = pathlib.Path(paths[1]).with_suffix(".draft.json")
result.pop("turn_id")
result.pop("packet_id")
draft.write_text(json.dumps(result), encoding="utf-8")
from labgoblin.cli import main
assert main(["--project", packet["project"], "research", "complete", "--file", str(draft), "--json"]) == 0
pathlib.Path(sys.argv[sys.argv.index("--usage-output-file")+1]).write_text(
    json.dumps({"totalNanoAiu": 10_000_000_000}), encoding="utf-8")
'''
    program = (f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
               + CLOSER.replace('pathlib.Path(paths[1]).write_text(json.dumps(result),encoding="utf-8")', ending))
    config, state, ledger, raw = fixture(tmp_path, program=program)
    raw["campaign"].update(max_ai_credits=1000)
    raw["research"]["max_parallel_agents"] = 3
    raw["agent"]["timeout_seconds"] = 30
    config = parse_config(raw, config.config_path)
    attempt = workspace.submit(state, config, request())
    assert not Campaign(config, state=state, ledger=ledger).run(no_agent=True)["errors"]
    result = Campaign(config, state=state, ledger=ledger).run()
    assert not result["errors"]
    assert state.attempt(attempt["id"])["validation"] == "valid"
    assert state.campaign()["research_outcome"] == "assessed"
    assert state.credit_budget()["used"] == 30
    assert state.campaign()["controller"] is None
    assert not ledger.rows()
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE state='accepted'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE kind='final_analysis'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM launches WHERE phase!='quiescent'").fetchone()[0] == 0
        packets = [json.loads(row[0]) for row in conn.execute("SELECT content FROM packets ORDER BY created")]
        assert packets[1]["goal_progress"]["preview_included"]
        assert packets[2]["goal_progress"]["applicability"] == "sealed"


@pytest.mark.parametrize("max_invocations", [2, 4])
def test_late_invocation_seal_runs_the_reserved_final_analysis(tmp_path, max_invocations):
    program = CLOSER.replace(
        'if packet["kind"] == "research":',
        'if packet["kind"] == "research" and packet["events"]:').replace(
        'pathlib.Path(paths[1]).write_text(json.dumps(result),encoding="utf-8")',
        '''if packet["kind"] == "research":
    result.update(disposition="continue", reason="Further ordinary work requested.",
                  stopping_criterion="", stop_basis="")
    result["evidence"] = [
        dict(event_id=event["id"], disposition="assessed", reason="Synthetic result assessed.",
             references=[packet["sources"]["goal"]["id"]]) for event in packet["events"]]
pathlib.Path(paths[1]).write_text(json.dumps(result),encoding="utf-8")
''')
    config, state, ledger, raw = fixture(tmp_path, max_invocations=max_invocations, program=program)
    raw["agent"]["timeout_seconds"] = 30
    config = parse_config(raw, config.config_path)
    workspace.submit(state, config, request())
    assert not Campaign(config, state=state, ledger=ledger).run(no_agent=True)["errors"]
    result = Campaign(config, state=state, ledger=ledger).run()
    assert not result["errors"]
    current = state.campaign()
    assert current["generation_state"] == "closed"
    assert current["research_outcome"] == "assessed"
    assert current["invocations"] == max_invocations
    assert current["controller"] is None and not ledger.rows()
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE kind='research' AND state='accepted'").fetchone()[0] == max_invocations - 1
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE kind='final_analysis'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM launches WHERE phase!='quiescent'").fetchone()[0] == 0
