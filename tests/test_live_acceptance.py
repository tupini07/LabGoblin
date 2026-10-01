import sqlite3

import pytest

from tests.copilot_acceptance import reserve_call, run


def test_repeated_control_restart_cycles_keep_cumulative_accounting(tmp_path):
    from tests.test_controller import fixture
    from tests.test_workspace import request
    from labgoblin import workspace
    from labgoblin.campaign import Campaign
    from labgoblin.state import State
    config, state, ledger, _ = fixture(tmp_path)
    elapsed = 0
    for cycle in range(6):
        attempt = workspace.submit(state, config, request(key=f"cycle-{cycle}"))
        current = state.campaign()
        state.control("pause", f"pause-{cycle}", current["revision"])
        state = State.open(state.root)
        assert not Campaign(config, state=state, ledger=ledger).run(no_agent=True)["started"]
        state.control("resume", f"resume-{cycle}", state.campaign()["revision"])
        Campaign(config, state=state, ledger=ledger).run(no_agent=True)
        assert state.attempt(attempt["id"])["status"] == "completed"
        state.control("stop", f"stop-{cycle}", state.campaign()["revision"])
        Campaign(state=state, ledger=ledger).reconcile()
        assert state.campaign()["operator_mode"] == "stopped" and not ledger.rows()
        assert state.campaign()["elapsed"] >= elapsed
        elapsed = state.campaign()["elapsed"]
        state.control("reopen", f"reopen-{cycle}", state.campaign()["revision"])
        assert state.campaign()["generation"] == cycle + 2
        assert state.campaign()["invocations"] == 0


def test_exact_four_slot_scenario_with_provider_doubles(tmp_path):
    result = run(tmp_path / "scenario")
    assert not result["errors"], result["errors"]
    assert not result["remaining_grants"]
    assert [row[1] for row in result["calls"]] == ["research"] * 3 + ["observer"]
    assert result["research"]["scores"] == [42, 999]
    with pytest.raises(RuntimeError, match="allowance"):
        reserve_call(tmp_path / "scenario" / "validation-calls.db", "research", ["must not launch"])
    with sqlite3.connect(tmp_path / "scenario" / "validation-calls.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 4
