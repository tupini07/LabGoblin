from dataclasses import asdict, replace
import json

import pytest

from labgoblin import credits, runtime
from labgoblin.processes import own_handle
from labgoblin.protocol import LaunchReceipt, UncertainExecution
from labgoblin.storage import publish_json
from tests.conftest import arm


def settle(envelope, ledger, *, usage=None, partial=None):
    supervisor = own_handle(envelope.id)
    assert ledger.claim_consumer(envelope, supervisor)
    receipt = LaunchReceipt(envelope.id, envelope.digest, "failed", 1, True, 1, "Synthetic fixture",
                            usage=usage, metadata={"supervisor": supervisor, "partial_usage": partial})
    ledger.finish_consumer(receipt)
    return receipt


def test_grants_are_one_use_and_receipts_are_identity_bound(project, ledger):
    envelope = arm(project, ledger)
    with pytest.raises(ValueError, match="already armed"):
        ledger.arm_consumer(envelope)
    with pytest.raises(ValueError, match="quiescent receipt"):
        ledger.release(envelope.id, owner_id=envelope.project_id)
    receipt = settle(envelope, ledger, usage={"totalNanoAiu": 10 * credits.NANO})
    ledger.finish_consumer(receipt)
    with pytest.raises(ValueError, match="matching armed grant"):
        ledger.claim_consumer(envelope, own_handle(envelope.id))
    assert ledger.grant(envelope.id)["state"] == "released"
    with pytest.raises(ValueError, match="different terminal receipt"):
        ledger.finish_consumer(replace(receipt, reason="changed"))


def test_unknown_usage_blocks_even_after_grant_is_released(project, ledger):
    envelope = arm(project, ledger)
    settle(envelope, ledger, partial={"totalNanoAiu": 12 * credits.NANO})
    budget = credits.summarize(ledger.runs(envelope.project_id), 300)
    assert budget["used"] == 12
    assert budget["uncovered_unknown"] == [envelope.id]
    assert budget["remaining"] is None
    with pytest.raises(UncertainExecution, match="Final credit usage"):
        credits.available(budget)
    assert ledger.grant(envelope.id)["state"] == "released"


def test_partial_usage_is_not_a_final_bill_and_estimate_stays_separate(project, ledger):
    root, config, record = project
    publish_json(root / ".labgoblin" / "ledger.json", {"path": str(ledger.path), "id": ledger.id})
    envelope = arm(project, ledger)
    receipt = settle(envelope, ledger, partial={"totalNanoAiu": 12 * credits.NANO})
    original = ledger.consumer_run(envelope.id)["receipt"]
    with pytest.raises(ValueError, match="confirmation"):
        runtime.record_usage(root, envelope.id, 20 * credits.NANO, "Operator estimate",
                             confirmation="wrong", estimated=True)
    with pytest.raises(ValueError, match="below"):
        runtime.record_usage(root, envelope.id, 10 * credits.NANO, "Too small",
                             confirmation=record["id"], estimated=True)
    runtime.record_usage(root, envelope.id, 20 * credits.NANO, "Explicit bounded estimate",
                         confirmation=record["id"], estimated=True)
    budget = runtime.status(root)["budget"]
    assert (budget["used"], budget["estimated"], budget["remaining"]) == (12, 8, 280)
    assert budget["unknown"] == [envelope.id]
    assert budget["uncovered_unknown"] == []
    runtime.record_usage(root, envelope.id, 18 * credits.NANO, "Recovered final provider usage",
                         confirmation=record["id"], estimated=False)
    budget = runtime.status(root)["budget"]
    assert (budget["used"], budget["estimated"], budget["remaining"]) == (18, 0, 282)
    assert ledger.consumer_run(envelope.id)["receipt"] == original
    assert len(list((root / ".labgoblin" / "accounting").glob("*.json"))) == 2


def test_cumulative_budget_and_reservations_survive_another_ledger_reader(project, ledger):
    envelope = arm(project, ledger)
    budget = credits.summarize(ledger.runs(envelope.project_id), 300)
    assert budget["reserved_nano"] == 300 * credits.NANO
    with pytest.raises(UncertainExecution, match="unsettled"):
        credits.available(budget)
    settle(envelope, ledger, usage={"totalNanoAiu": 100 * credits.NANO})
    from labgoblin.scheduler import ResourceLedger
    reopened = ResourceLedger(ledger.path)
    budget = credits.summarize(reopened.runs(envelope.project_id), 300)
    assert budget["remaining"] == 200
    with pytest.raises(ValueError, match="allowance changed"):
        arm(project, ledger, allowance=300 * credits.NANO)


@pytest.mark.parametrize("value", [{"cost": 1}, {"totalNanoAiu": True}, {"totalNanoAiu": -1},
                                  {"totalNanoAiu": float("nan")}, None])
def test_rejects_invalid_or_premium_only_usage(value):
    with pytest.raises(ValueError):
        credits.usage_nano(value)


def test_fractional_nano_units_round_conservatively():
    assert credits.usage_nano({"totalNanoAiu": 0.1}) == 1


def test_wrong_envelope_cannot_claim_or_settle(project, ledger):
    envelope = arm(project, ledger)
    wrong = replace(envelope, prompt_sha256="different")
    with pytest.raises(ValueError, match="matching armed grant"):
        ledger.claim_consumer(wrong, own_handle(wrong.id))
    supervisor = own_handle(envelope.id)
    assert ledger.claim_consumer(envelope, supervisor)
    receipt = LaunchReceipt(envelope.id, "wrong", "completed", 1, True, 0, "",
                            usage={"totalNanoAiu": 0}, metadata={"supervisor": supervisor})
    with pytest.raises(ValueError, match="ownership mismatch"):
        ledger.finish_consumer(receipt)
