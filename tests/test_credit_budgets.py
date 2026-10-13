from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from labgoblin import agent, credits, worker
from labgoblin.campaign import Campaign
from labgoblin.config import initial_config, parse_config, restore_config
from labgoblin.dashboard_chat import worker_main
from labgoblin.evidence import atomic_json, read_json
from labgoblin.protocol import BudgetExhausted, LaunchReceipt, UncertainExecution, canonical
from labgoblin.scheduler import ResourceLedger
from labgoblin.state import State
from tests.test_controller import drain, fixture as controller_fixture
from tests.test_closure_v3 import CLOSER
from tests.test_observer_v3 import authorization
from tests.test_providers import HELP, fixture, wait


@pytest.mark.parametrize("value", [0, -1, 29.99, True, False, "100", "unknown", float("nan"), float("inf"), 30.0000000001])
def test_credit_limit_validation(value, tmp_path):
    for section in ("campaign", "chat"):
        raw = initial_config("credits", "copilot")
        target = raw["campaign"] if section == "campaign" else raw["dashboard"]["chat"]
        target["max_ai_credits"] = value
        with pytest.raises(ValueError):
            parse_config(raw, tmp_path / "labgoblin.toml")


def test_init_exposes_explicit_unlimited_and_claude_rejects_credit_cap(tmp_path):
    raw = initial_config("credits")
    assert raw["campaign"]["max_ai_credits"] == raw["dashboard"]["chat"]["max_ai_credits"] == "unlimited"
    assert parse_config(raw, tmp_path / "labgoblin.toml").campaign.max_ai_credits is None
    raw["campaign"]["max_ai_credits"] = 100
    with pytest.raises(ValueError, match="Copilot"):
        parse_config(raw, tmp_path / "labgoblin.toml")


def test_additive_optional_fields_restore_existing_schema_three_snapshot(tmp_path):
    config = parse_config(initial_config("credits"), tmp_path / "labgoblin.toml")
    snapshot = asdict(config)
    del snapshot["campaign"]["max_ai_credits"]
    del snapshot["chat"]["max_ai_credits"]
    assert restore_config(snapshot, revision=config.revision, path=config.config_path) == config


def test_existing_snapshot_can_activate_without_rewriting_its_identity(tmp_path):
    config, state, ledger, _ = controller_fixture(tmp_path)
    snapshot = asdict(config)
    del snapshot["campaign"]["max_ai_credits"]
    del snapshot["chat"]["max_ai_credits"]
    stored = canonical(snapshot).decode()
    with state.db.write() as conn:
        conn.execute("UPDATE configs SET content=? WHERE id=?", (stored, config.revision))
    with Campaign(config, state=state, ledger=ledger) as controller:
        assert not controller.step(no_agent=True, admit=False)["errors"]
    with state.db.read() as conn:
        assert conn.execute("SELECT content FROM configs WHERE id=?", (config.revision,)).fetchone()[0] == stored


@pytest.mark.parametrize("usage", [None, {}, {"cost": 100}, {"totalPremiumRequestCost": 1},
                                  {"totalNanoAiu": None}, {"totalNanoAiu": True},
                                  {"totalNanoAiu": -1}, {"totalNanoAiu": float("inf")}])
def test_unknown_usage_is_not_zero_or_premium_multipliers(usage):
    with pytest.raises(ValueError):
        credits.usage_nano(usage)


def test_recorded_cli_credit_shape_and_subunit_rounding():
    # Shape and aggregate from the prior authorized synthetic CLI acceptance.
    assert credits.usage_nano({"totalPremiumRequestCost": 1, "totalNanoAiu": 14_244_520_000,
                              "modelMetrics": {"model": {"totalNanoAiu": 14_244_520_000}}}) == 14_244_520_000
    assert credits.usage_nano({"totalNanoAiu": 0}) == 0
    assert credits.usage_nano({"totalNanoAiu": 1.2}) == 2
    assert credits.argument(14_244_520_001) == "14.244520001"


def test_credit_cap_requires_provider_support_and_blocks_command_override(tmp_path):
    config, _, _, _, _, _ = fixture(tmp_path, credit_limit=100)
    for missing in ("--max-ai-credits", "--usage-output-file"):
        with pytest.raises(ValueError, match="credit budgets"):
            agent.inspect_provider(config.agent, max_ai_credits=100,
                                   probe=lambda argv: HELP.replace(missing, ""))
    for flag in ("--max-ai-credits", "--max-ai-credits=1000"):
        with pytest.raises(ValueError, match="override"):
            agent.inspect_provider(replace(config.agent, command=(*config.agent.command, flag)),
                                   probe=lambda argv: HELP)


def test_frozen_allowance_reserves_final_analysis_and_is_rechecked(tmp_path):
    config, state, _, turn, ids, grant = fixture(tmp_path, credit_limit=100)
    envelope = agent.prepare(state, config, turn, ids[0], grant)
    assert envelope.metadata["credit_allowance_nano"] == 70 * credits.NANO
    assert envelope.argv[envelope.argv.index("--max-ai-credits") + 1] == "70"
    forged = replace(envelope, metadata={**envelope.metadata, "credit_allowance_nano": 100 * credits.NANO})
    with pytest.raises(ValueError, match="allowance changed"):
        state.arm(forged)
    with pytest.raises(ValueError, match="does not enforce"):
        state.arm(replace(envelope, argv=("provider", "-p", "test")))
    state.arm(envelope)
    budget = state.credit_budget()
    assert budget["used"] == 0 and budget["reserved"] == 70 and budget["remaining"] == 30
    assert budget["unknown"] == 0


def test_canary_and_final_bundle_minimums_are_preserved(tmp_path):
    _, state, _, _, ids, _ = fixture(tmp_path, sandbox=True, credit_limit=200)
    assert state.credit_allowance(ids[0]) == 30 * credits.NANO
    assert state.credit_allowance(ids[1]) == 110 * credits.NANO
    (tmp_path / "too-small").mkdir()
    with pytest.raises(BudgetExhausted, match="30-credit"):
        fixture(tmp_path / "too-small", sandbox=True, credit_limit=100)


def test_completed_credit_usage_is_durable_and_receipt_replay_is_idempotent(tmp_path):
    config, state, ledger, turn, ids, grant = fixture(tmp_path, credit_limit=100, usage_nano=14_244_520_000)
    envelope = worker.start(state, agent.prepare(state, config, turn, ids[0], grant))
    assert wait(state, ids[0]) == "completed"
    before = State.open(state.root).credit_budget()
    assert before["used_nano"] == 14_244_520_000
    assert before["remaining_nano"] == 85_755_480_000
    assert before["unknown"] == before["reserved"] == 0
    receipt = json.loads(state.launch(envelope.key.nonce)["receipt"])
    state.finish_launch(envelope.key.nonce, receipt)
    assert State.open(state.root).credit_budget() == before
    assert ledger.grant(grant["token"])["state"] == "released"
    state.finish_turn(turn, "Fixture complete")
    state.control("stop", "stop", state.campaign()["revision"])
    state.converge_stop()
    state.control("reopen", "reopen", state.campaign()["revision"])
    assert state.credit_budget() == before
    raw = initial_config("provider-fixture", "copilot")
    raw["agent"] = asdict(config.agent)
    raw["campaign"]["max_ai_credits"] = 150
    changed = parse_config(raw, config.config_path)
    with Campaign(changed, state=state, ledger=ledger) as controller:
        controller.step(no_agent=True, admit=False)
    assert state.credit_budget()["configured"] == 150
    assert state.credit_budget()["used_nano"] == before["used_nano"]


@pytest.mark.parametrize("spent", [None, 120_000_000_000])
def test_missing_usage_and_overshoot_block_further_spending(tmp_path, spent):
    config, state, ledger, turn, ids, grant = fixture(tmp_path, credit_limit=100, usage_nano=spent)
    worker.start(state, agent.prepare(state, config, turn, ids[0], grant))
    assert wait(state, ids[0]) == "completed"
    budget = state.credit_budget()
    with pytest.raises(UncertainExecution if spent is None else BudgetExhausted):
        credits.available(budget)
    if spent is None:
        assert budget["unknown"] == 1
        with Campaign(config, state=state, ledger=ledger) as controller:
            step = controller.step(no_agent=True, admit=False)
        assert any(error["category"] == "budget" for error in step["errors"])
    else:
        assert budget["used"] == 120 and budget["remaining"] == 0


def test_proven_nonstart_does_not_spend_or_poison_credit_accounting(tmp_path):
    config, state, _, turn, ids, grant = fixture(tmp_path, credit_limit=100)
    envelope = agent.prepare(state, config, turn, ids[0], grant)
    Path(envelope.metadata["packet_path"]).write_text("changed", encoding="utf-8")
    worker.start(state, envelope)
    assert wait(state, ids[0]) == "not_started"
    assert state.credit_budget()["remaining"] == 100
    assert state.credit_budget()["unknown"] == 0


def limited_observer(config, state, ledger, limit=100):
    envelope = authorization(config, state, ledger, campaign_id=state.id)
    allowance = credits.available(ledger.observer_credit_budget(state.id, limit))
    return replace(envelope, metadata={**envelope.metadata, "credit_campaign_id": state.id,
                   "max_ai_credits": limit, "credit_allowance_nano": allowance})


def test_observer_credit_pool_survives_dashboard_ids_and_does_not_write_research(tmp_path):
    config, state, ledger, _ = controller_fixture(tmp_path)
    with state.db.read() as conn:
        before = list(conn.iterdump())
    envelope = limited_observer(config, state, ledger)
    ledger.arm_consumer(envelope, max_invocations=20)
    receipt = LaunchReceipt(envelope.key, envelope.digest, "completed", True, 1,
                            returncode=0, usage={"totalNanoAiu": 14_244_520_000})
    ledger.finish_consumer(receipt)
    ledger.finish_consumer(receipt)
    reopened = ResourceLedger(ledger.path, expected_id=ledger.id)
    assert reopened.observer_credit_budget(state.id, 100)["remaining"] == 85.75548
    next_envelope = limited_observer(config, state, ledger)
    assert next_envelope.key.campaign_id != envelope.key.campaign_id
    assert next_envelope.metadata["credit_allowance_nano"] == 85_755_480_000
    ledger.release(next_envelope.key.grant_id, owner_id=next_envelope.key.campaign_id)
    with state.db.read() as conn:
        assert list(conn.iterdump()) == before


def test_concurrent_observers_cannot_both_reserve_remaining_credits(tmp_path):
    config, state, ledger, _ = controller_fixture(tmp_path)
    first = limited_observer(config, state, ledger)
    second = limited_observer(config, state, ledger)
    ledger.arm_consumer(first, max_invocations=20)
    with pytest.raises(BudgetExhausted):
        ledger.arm_consumer(second, max_invocations=20)
    assert ledger.observer_credit_budget(state.id, 100)["reserved"] == 100
    ledger.finish_consumer(LaunchReceipt(first.key, first.digest, "not_started", True, 0,
                                        executed=False, reason="Fixture cancelled before execution"))
    ledger.release(second.key.grant_id, owner_id=second.key.campaign_id)


def test_interrupted_observer_usage_stays_unknown_across_dashboard_restart(tmp_path):
    config, state, ledger, _ = controller_fixture(tmp_path)
    envelope = limited_observer(config, state, ledger)
    ledger.arm_consumer(envelope, max_invocations=20)
    ledger.finish_consumer(LaunchReceipt(envelope.key, envelope.digest, "failed", True, 1, reason="Interrupted"))
    budget = ledger.observer_credit_budget(state.id, 100)
    assert budget["unknown"] == 1
    with pytest.raises(UncertainExecution):
        credits.available(budget)


def test_observer_supervisor_publishes_usage_before_terminal_receipt(tmp_path, monkeypatch):
    config, state, ledger, _ = controller_fixture(tmp_path)
    envelope = limited_observer(config, state, ledger)
    root = Path(envelope.root)
    target = root / "envelope.json"
    atomic_json(target, asdict(envelope))
    ledger.arm_consumer(envelope, max_invocations=20)

    def execute(spec, *, publish_receipt):
        assert publish_receipt is False
        assert not (root / "backend-receipt.json").exists()
        atomic_json(root / "usage.json", {"totalNanoAiu": 14_244_520_000})
        return LaunchReceipt(envelope.key, envelope.digest, "completed", True, 1, returncode=0)

    monkeypatch.setattr("labgoblin.payload.execute_spec", execute)
    assert worker_main("--observer-supervisor", target) == 0
    receipt = read_json(root / "backend-receipt.json")
    assert json.loads(ledger.consumer_run(envelope.key.grant_id)["receipt"]) == receipt
    assert receipt["usage"]["totalNanoAiu"] == 14_244_520_000
    assert ledger.observer_credit_budget(state.id, 100)["used"] == 14.24452


@pytest.mark.parametrize("overshoot,expected_calls,outcome", [(0, 2, "assessed"), (31, 1, "unassessed")])
def test_credit_exhaustion_closes_without_renewing_retry_allowance(tmp_path, overshoot, expected_calls, outcome):
    prefix = r'''
import json,pathlib,re,sys
if "--help" in sys.argv:
    print(HELP_TEXT)
    raise SystemExit(0)
prompt=sys.argv[sys.argv.index("-p")+1]
paths=[json.loads(item) for item in re.findall(r'"(?:\\.|[^"\\])*"',prompt)]
packet=json.loads(pathlib.Path(paths[0]).read_text(encoding="utf-8"))
usage=pathlib.Path(sys.argv[sys.argv.index("--usage-output-file")+1])
cap=float(sys.argv[sys.argv.index("--max-ai-credits")+1])
if packet["kind"]=="research":
    assert cap==70
    usage.write_text(json.dumps({"totalNanoAiu":int((cap+OVERSHOOT)*1e9)}),encoding="utf-8")
    print("AI credit session limit reached")
    raise SystemExit(1)
assert packet["kind"]=="final_analysis" and cap==30
'''
    suffix = '\nusage.write_text(json.dumps({"totalNanoAiu":10_000_000_000}),encoding="utf-8")\n'
    config, state, ledger, raw = controller_fixture(
        tmp_path, max_invocations=0, retries=3, program=f"OVERSHOOT={overshoot}\n" + prefix + CLOSER + suffix)
    raw["campaign"]["max_ai_credits"] = 100
    config = parse_config(raw, config.config_path)
    with Campaign(config, state=state, ledger=ledger) as controller:
        drain(controller, lambda _: state.campaign()["generation_state"] == "closed", no_agent=False)
        for _ in range(3):
            assert not controller.step(no_agent=False)["started"]
    assert state.campaign()["invocations"] == expected_calls
    assert state.campaign()["research_outcome"] == outcome
    assert state.credit_budget()["used"] == (80 if overshoot == 0 else 101)
    assert state.credit_budget()["unknown"] == 0
    assert not ledger.rows()


def test_dashboard_reports_credit_limit_without_mutating_state(tmp_path):
    import tomli_w
    from tests.test_dashboard import get, serve

    config, state, ledger, raw = controller_fixture(tmp_path)
    raw["campaign"]["max_ai_credits"] = 100
    config = parse_config(raw, config.config_path)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    with Campaign(config, state=state, ledger=ledger) as controller:
        controller.step(no_agent=True, admit=False)
    with state.db.read() as conn:
        before = list(conn.iterdump())
    with serve(config.config_path) as base:
        status, _, body = get(base, "/resources")
        assert status == 200
        assert b"AI credits (soft cap)" in body
        assert b"0.000 reported / 100.000" in body
    with state.db.read() as conn:
        assert list(conn.iterdump()) == before


def interrupted_credit(tmp_path, *, partial=7):
    config, state, ledger, turn, ids, grant = fixture(tmp_path, credit_limit=100)
    envelope = agent.prepare(state, config, turn, ids[0], grant)
    state.arm(envelope)
    state.finish_launch(envelope.key.nonce, asdict(LaunchReceipt(
        envelope.key, envelope.digest, "timed_out", True, 1, reason="Operation deadline exceeded",
        metadata={"partial_usage": {"totalNanoAiu": partial * credits.NANO}} if partial is not None else {})))
    return config, state, ledger, turn, envelope


@pytest.mark.parametrize("partial", [None, 7])
def test_dashboard_brief_keeps_unknown_credit_balance_explicit(tmp_path, partial):
    import tomli_w
    from tests.test_dashboard import get, serve

    config, state, _, _, _ = interrupted_credit(tmp_path, partial=partial)
    raw = initial_config("provider-fixture", "copilot")
    raw["agent"] = asdict(config.agent)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    state.blocker("credit-accounting", "budget", "AI credit usage is unknown; further admission is blocked")
    assert state.credit_budget()["remaining"] is None
    with state.db.read() as conn:
        before = list(conn.iterdump())
    with serve(config.config_path, chat=False) as base:
        status, _, body = get(base)
        assert status == 200
        assert b"<dt>Unreserved credits</dt><dd>Unknown (billing incomplete)</dd>" in body
        assert b"AI credit usage is unknown; further admission is blocked" in body
        assert b"1 unknown usage record(s)" in body
        for route in ("/monitor", "/memos", "/resources"):
            assert get(base, route)[0] == 200
    with state.db.read() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("partial", [None, 7])
def test_estimate_is_durable_distinct_idempotent_and_restores_capped_admission(tmp_path, partial):
    from labgoblin.state import _source_revision
    config, state, ledger, turn, envelope = interrupted_credit(tmp_path, partial=partial)
    receipt = state.launch(envelope.key.nonce)["receipt"]
    with state.db.read() as conn:
        revision = _source_revision(conn)
    state.blocker("credit-accounting", "budget", "Usage unknown")
    result = state.estimate_credit(envelope.key.work_id, 20, confirm=state.id, reason="Operator approved estimate")
    assert not result["replayed"]
    assert state.estimate_credit(envelope.key.work_id, 20, confirm=state.id, reason="Replay") == {
        **result, "replayed": True}
    with pytest.raises(ValueError, match="Conflicting"):
        state.estimate_credit(envelope.key.work_id, 21, confirm=state.id, reason="Not the approved amount")
    budget = state.credit_budget()
    assert budget["unknown"] == budget["estimated"] == 1
    assert budget["uncovered_unknown"] == 0 and budget["remaining_is_estimated"]
    assert budget["used"] == (partial or 0) and budget["estimated_used"] == 20 - (partial or 0)
    assert budget["accounted_used"] == 20 and budget["remaining"] == 80
    assert credits.available(budget, reserve=30 * credits.NANO) == 50 * credits.NANO
    assert State.open(state.root).credit_budget() == budget
    assert state.launch(envelope.key.nonce)["receipt"] == receipt
    with state.db.read() as conn:
        assert _source_revision(conn) == revision
        row = conn.execute("SELECT body,metadata FROM sources WHERE kind='credit_estimate'").fetchone()
        record = json.loads(row["body"])
        assert record["estimatedNanoAiu"] == 20 * credits.NANO and "totalNanoAiu" not in record
        assert record["_labgoblin"]["complete"] is False
        assert json.loads(row["metadata"])["reason"] == "Operator approved estimate"
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0
    assert not state.campaign()["blockers"]
    state.finish_turn(turn, "Timed out")
    with Campaign(config, state=state, ledger=ledger) as controller:
        step = controller.step(no_agent=True, admit=False)
    assert not step["errors"]
    state.control("stop", "stop", state.campaign()["revision"])
    state.converge_stop()
    state.control("reopen", "reopen", state.campaign()["revision"])
    assert state.credit_budget() == budget


@pytest.mark.parametrize("amount", [-1, True, float("nan"), float("inf"), 0.0000000001, 6])
def test_estimate_rejects_invalid_values_and_below_partial_totals(tmp_path, amount):
    _, state, _, _, envelope = interrupted_credit(tmp_path)
    with pytest.raises(ValueError):
        state.estimate_credit(envelope.key.work_id, amount, confirm=state.id, reason="Invalid estimate")
    assert state.credit_budget()["uncovered_unknown"] == 1


@pytest.mark.parametrize("case", ["active", "known", "nonstart"])
def test_estimate_requires_a_quiescent_unknown_executed_invocation(tmp_path, case):
    config, state, _, turn, ids, grant = fixture(tmp_path, credit_limit=100)
    envelope = agent.prepare(state, config, turn, ids[0], grant)
    state.arm(envelope)
    if case != "active":
        receipt = LaunchReceipt(
            envelope.key, envelope.digest, "failed" if case == "known" else "not_started",
            True, 1, reason="Fixture outcome", executed=case == "known",
            usage={"totalNanoAiu": credits.NANO} if case == "known" else None)
        state.finish_launch(envelope.key.nonce, asdict(receipt))
    with pytest.raises(ValueError, match="quiescent|authoritative|non-start"):
        state.estimate_credit(envelope.key.work_id, 20, confirm=state.id, reason="Not eligible")


@pytest.mark.parametrize("final", [12, 95])
def test_final_reconciliation_replaces_estimate_without_double_charging(tmp_path, final):
    from labgoblin.copilot_worker import usage_record
    _, state, _, _, envelope = interrupted_credit(tmp_path)
    state.estimate_credit(envelope.key.work_id, 20, confirm=state.id, reason="Operator approved estimate")
    record = usage_record(envelope, "provider-session", {"totalNanoAiu": final * credits.NANO}, complete=True)
    state.reconcile_credit(envelope.key.work_id, canonical(record), confirm=state.id, reason="Final metrics recovered")
    budget = state.credit_budget()
    assert budget["used"] == budget["accounted_used"] == final
    assert budget["unknown"] == budget["estimated"] == budget["estimated_used"] == 0
    assert budget["remaining"] == 100 - final and not budget["remaining_is_estimated"]
    with pytest.raises(ValueError, match="authoritative reconciled"):
        state.estimate_credit(envelope.key.work_id, 20, confirm=state.id, reason="Obsolete estimate")
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind='credit_estimate'").fetchone()[0] == 1


def test_estimate_does_not_cover_other_unknown_launches_or_erase_budget_exhaustion(tmp_path):
    _, state, _, _, envelope = interrupted_credit(tmp_path)
    state.estimate_credit(envelope.key.work_id, 120, confirm=state.id, reason="Approved conservative estimate")
    with pytest.raises(BudgetExhausted):
        credits.available(state.credit_budget())
    row = state.launch(envelope.key.nonce)
    with state.db.read() as conn:
        estimate = json.loads(conn.execute("SELECT body FROM sources WHERE kind='credit_estimate'").fetchone()[0])
    other = json.loads(row["envelope"])
    other["key"].update(nonce="other-nonce", work_id="other-invocation")
    result = credits.summarize(
        [row, {**row, "envelope": canonical(other)}], 1000,
        estimates={envelope.key.nonce: estimate})
    assert result["unknown"] == 2 and result["estimated"] == result["uncovered_unknown"] == 1
    assert result["remaining"] is None
    with pytest.raises(UncertainExecution):
        credits.available(result)


def test_estimate_cli_requires_operator_and_campaign_confirmation(tmp_path, monkeypatch):
    from labgoblin import cli
    _, state, _, _, envelope = interrupted_credit(tmp_path)
    args = cli.parser().parse_args([
        "--project", str(tmp_path), "estimate-credits", "--id", envelope.key.work_id,
        "--credits", "20", "--confirm", "wrong", "--reason", "Operator approval",
    ])
    with pytest.raises(ValueError, match="exact campaign ID"):
        cli.execute(args)
    args.confirm = state.id
    monkeypatch.setenv("LABGOBLIN_TURN_ID", "research")
    with pytest.raises(ValueError, match="operator operation"):
        cli.execute(args)
    monkeypatch.delenv("LABGOBLIN_TURN_ID")
    assert cli.execute(args)["ai_credits"]["accounted_used"] == 20


def test_dashboard_distinguishes_estimates_from_reported_usage(tmp_path):
    import tomli_w
    from tests.test_dashboard import get, serve
    config, state, _, _, envelope = interrupted_credit(tmp_path)
    raw = initial_config("provider-fixture", "copilot")
    raw["agent"] = asdict(config.agent)
    Path(config.config_path).write_text(tomli_w.dumps(raw), encoding="utf-8")
    state.estimate_credit(envelope.key.work_id, 20, confirm=state.id, reason="Operator approved estimate")
    with state.db.read() as conn:
        before = list(conn.iterdump())
    with serve(config.config_path) as base:
        status, _, body = get(base, "/monitor")
        assert status == 200
        assert b"7.000 reported / 100.000" in body
        assert b"13.000 additional operator-estimated credits" in body
        assert b"remaining allowance is approximate" in body
        assert b"80.000 remaining" in body and b"1 unknown usage record" in body
        status, _, brief = get(base)
        assert status == 200
        assert b"<dt>Unreserved credits</dt><dd>80.00</dd>" in brief
        assert b"13.00 additional operator-estimated credits" in brief
    with state.db.read() as conn:
        assert list(conn.iterdump()) == before
