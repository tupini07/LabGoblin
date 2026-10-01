from dataclasses import asdict
import json

import pytest

from tests.test_controller import drain, fixture
from tests.test_state import accept, allocate, envelope, spec, state
from tests.test_workspace import request
from labgoblin import briefing, reporting, workspace
from labgoblin.campaign import Campaign
from labgoblin.protocol import Handoff, LaunchEnvelope, LaunchKey, Resources, identifier


CLOSER = r'''
import json,pathlib,re,sys
if "--help" in sys.argv:
    print(HELP_TEXT)
    raise SystemExit(0)
prompt=sys.argv[sys.argv.index("-p")+1]
paths=[json.loads(item) for item in re.findall(r'"(?:\\.|[^"\\])*"',prompt)]
packet=json.loads(pathlib.Path(paths[0]).read_text(encoding="utf-8"))
result=dict(turn_id=packet["turn_id"],packet_id=packet["packet_id"],summary="Finite synthetic assessment",
            rationale="Baseline captured; retain the sealed cohort.",next_step="Do not submit new work.",
            disposition="finalize",reason="Synthetic stopping criterion evaluated.",evidence=[],
            stopping_criterion="Assess the admitted synthetic controls and their limitations.")
if packet["kind"] == "final_analysis":
    inventory=packet["inventory"]
    scores=sorted(o["metrics"]["score"] for a in inventory["attempts"] for o in a["observations"]
                  if "score" in o["metrics"])
    result["rationale"]="Captured scores: "+json.dumps(scores)
    result["assessment"]=dict(view_id=inventory["id"],inventory_digest=inventory["metadata"]["inventory_digest"],
                              assess_all=True,exclusions=[],limitations="Synthetic fixture; failed and unperformed work remains in the denominator.")
pathlib.Path(paths[1]).write_text(json.dumps(result),encoding="utf-8")
'''


def incomplete_attempts(state, count):
    for index in range(count):
        work = spec(state, f"unperformed-{index}")
        state.enqueue(work)
        state.fail_unlaunched_attempt(work["id"], "Deliberately not performed")
        state.record_collection(work["id"], [], "not_performed", "not_performed", "Deliberately not performed")
    state.source("goal", b"Assess this exact inventory, including unperformed work.", origin="operator", head="goal")
    state.close_admission("Sealed fixture")
    return reporting.seal_view(state, kind="closure")


def owned_final(state, packet):
    invocation = state.reserve_invocations(packet["turn_id"], "final_analysis", ("final_analysis",))[0]
    resources = Resources(1, 128)
    campaign = state.campaign()
    allocation = state.allocation(packet["turn_id"], "final_analysis", {"resources": asdict(resources)}, campaign["revision"])
    state.granted(allocation["token"])
    state.arm(LaunchEnvelope(
        LaunchKey(state.id, campaign["generation"], invocation, allocation["token"], identifier()),
        "final_analysis", ("provider-double",), str(state.root.parent), str(state.root / "turns" / packet["turn_id"]),
        str(state.path), str(state.ledger_identity()[0]), "machine", 10, resources, campaign["config_revision"]))
    view = packet["content"]["inventory"]
    return Handoff(packet["turn_id"], packet["id"], "Complete listed inventory.", "No unsupported measurements.",
                   "Retain limitations.", "finalize", "Exact cohort considered.", stopping_criterion="Inventory assessed.",
                   assessment={"view_id": view["id"], "inventory_digest": view["metadata"]["inventory_digest"],
                               "assess_all": True, "exclusions": [], "limitations": "The work was not performed."})


def test_late_contradictory_admitted_result_is_in_one_final_owned_assessment(tmp_path):
    config, state, ledger, _ = fixture(tmp_path, max_invocations=2, program=CLOSER)
    barrier = tmp_path / "release-replication"
    (config.root / "replication.py").write_text(
        "import os,pathlib,time\n"
        f"while not pathlib.Path({str(barrier)!r}).exists(): time.sleep(0.02)\n"
        "pathlib.Path(os.environ['LABGOBLIN_OUTPUT_DIR'],'metrics.json').write_text('{\"score\":999}')\n",
        encoding="utf-8")
    baseline = workspace.submit(state, config, request())
    replication = workspace.submit(state, config, request(key="replication", argv=["python", "replication.py"],
                                                          source_files=["replication.py"], seconds=20))
    with Campaign(config, state=state, ledger=ledger) as controller:
        try:
            drain(controller, lambda _: state.collection(baseline["id"])["collection"] == "complete"
                  and state.attempt(replication["id"])["status"] in ("starting", "running"))
            drain(controller, lambda _: state.campaign()["generation_state"] == "sealed", no_agent=False)
            assert state.attempt(replication["id"])["status"] in ("starting", "running")
            assert state.campaign()["invocations"] == 1
        finally:
            barrier.write_text("release", encoding="utf-8")
        drain(controller, lambda _: state.campaign()["generation_state"] == "closed", no_agent=False)
        for _ in range(3):
            assert not controller.step()["started"]
    current = state.campaign()
    assert current["research_outcome"] == "assessed" and current["invocations"] == 2
    view = reporting.page(state.db, current["closure"]["view_id"])
    assert view["coverage"]["total"] == 2
    with state.db.read() as conn:
        final = json.loads(conn.execute("SELECT result FROM turns WHERE kind='final_analysis'").fetchone()[0])
    assert final["rationale"] == "Captured scores: [42, 999]"
    assert not ledger.rows()


@pytest.mark.parametrize("more_work", [False, True])
def test_final_failure_or_more_work_never_retries_or_reopens(tmp_path, more_work):
    if more_work:
        program = CLOSER.replace('result["rationale"]="Captured scores: "+json.dumps(scores)',
                                 'result["disposition"]="continue"\n    result["rationale"]="More research required."')
    else:
        program = CLOSER.replace('inventory=packet["inventory"]', 'raise RuntimeError("Deliberate final-analysis failure")')
    config, state, ledger, _ = fixture(tmp_path, retries=5, program=program)
    workspace.submit(state, config, request())
    Campaign(config, state=state, ledger=ledger).run(no_agent=True)
    Campaign(config, state=state, ledger=ledger).run()
    current = state.campaign()
    assert current["generation_state"] == "closed" and current["invocations"] == 2
    assert current["research_outcome"] == ("needs_more_work" if more_work else "unassessed")
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE kind='final_analysis'").fetchone()[0] == 1
    assert not ledger.rows()


def test_pausing_prepared_final_turn_does_not_spend_the_one_actual_analysis(tmp_path):
    config, state, ledger, _ = fixture(tmp_path, program=CLOSER)
    workspace.submit(state, config, request())
    state.close_admission("Finalize before queued work is performed")
    state.converge_stop()
    for attempt in state.attempts():
        workspace.collect_artifacts(state, attempt["id"])
    view = reporting.seal_view(state, kind="closure")
    old = briefing.prepare(state, "final_analysis", view_id=view["id"])
    state.control("pause", "pause", 0)
    with Campaign(config, state=state, ledger=ledger) as controller:
        controller.step()
        with state.db.read() as conn:
            assert conn.execute("SELECT final_turn FROM generations").fetchone()[0] is None
            assert conn.execute("SELECT state FROM turns WHERE id=?", (old["turn_id"],)).fetchone()[0] == "cancelled"
        state.control("resume", "resume", 1)
        drain(controller, lambda _: state.campaign()["generation_state"] == "closed", no_agent=False)
    assert state.campaign()["research_outcome"] == "assessed"
    assert state.campaign()["invocations"] == 1 and not ledger.rows()


def test_inventory_claim_rejects_undelivered_pages(state):
    view = incomplete_attempts(state, 30)
    packet = briefing.prepare(state, "final_analysis", view_id=view["id"])
    assert packet["content"]["inventory"]["coverage"]["has_more"]
    value = owned_final(state, packet)
    with pytest.raises(ValueError, match="every page"):
        accept(state, value)
    assert state.campaign()["generation_state"] == "sealed"


def test_retrieved_final_pages_enable_bounded_complete_inventory_claim(state):
    view = incomplete_attempts(state, 30)
    packet = briefing.prepare(state, "final_analysis", view_id=view["id"])
    value = owned_final(state, packet)
    cursor = packet["content"]["inventory"]["coverage"]["end"]
    while cursor < 30:
        page = reporting.page(state.db, view["id"], offset=cursor)
        state.retrieved_view(value.turn_id, page)
        cursor = page["coverage"]["end"]
    accept(state, value)
    result = state.close_generation(view["id"], "assessed", "Owned full-scope assessment", assessed_turn=value.turn_id)
    assert result["operational_quiescent"] and result["attempts"] == 30


def test_unknown_owned_work_keeps_its_grant_and_an_incomplete_inventory(state):
    state.source("goal", b"Inspect uncertainty, not infer completion.", origin="operator", head="goal")
    work = spec(state)
    state.enqueue(work)
    allocation = allocate(state, work)
    launch = envelope(state, work, allocation)
    state.arm(launch)
    state.close_admission("Finite assessment requested")
    state.launch_problem(launch.key.nonce, "Owned payload cannot be inspected")
    controller = Campaign(state=state)
    errors = []
    controller._advance_closure(errors, no_agent=True, admit=False)
    current = state.campaign()
    assert not errors and current["research_outcome"] == "incomplete"
    assert current["generation_state"] == "sealed" and not current["closure"]["operational_quiescent"]
    with state.db.read() as conn:
        assert conn.execute("SELECT state FROM allocations WHERE token=?", (allocation["token"],)).fetchone()[0] == "attached"
    assert reporting.page(state.db, current["closure"]["view_id"])["coverage"]["total"] == 1


def test_no_agent_closure_is_deterministic_and_includes_unperformed_work(tmp_path):
    config, state, ledger, _ = fixture(tmp_path, program=CLOSER)
    attempt = workspace.submit(state, config, request())
    state.close_admission("Finite scope without inference")
    Campaign(config, state=state, ledger=ledger).run(no_agent=True)
    current = state.campaign()
    assert current["generation_state"] == "closed" and current["research_outcome"] == "unassessed"
    assert current["invocations"] == 0
    row = reporting.member(state.db, current["closure"]["view_id"], attempt["id"])["content"]
    assert row["status"] == "cancelled" and not row["admitted"]
    assert row["collection"] == "not_performed"


def test_empty_fully_assessed_finalize_avoids_a_redundant_final_call(tmp_path):
    config, state, ledger, _ = fixture(tmp_path, program=CLOSER)
    Campaign(config, state=state, ledger=ledger).run()
    assert state.campaign()["research_outcome"] == "assessed" and state.campaign()["invocations"] == 1
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE kind='final_analysis'").fetchone()[0] == 0


def test_final_preparation_failure_with_pending_release_does_not_start_another_cycle(tmp_path, monkeypatch):
    config, state, ledger, _ = fixture(tmp_path, program=CLOSER)
    workspace.submit(state, config, request())
    state.close_admission("Assess unperformed scope")
    launches = []
    with Campaign(config, state=state, ledger=ledger) as controller:
        def fail_after_grant(turn):
            launches.append(turn["id"])
            controller._grant(turn["id"], "final_analysis", config.agent.resources, native=True)
            raise ValueError("Deliberate pre-invocation preparation failure")
        monkeypatch.setattr(controller, "_launch_turn", fail_after_grant)
        drain(controller, lambda _: state.campaign()["generation_state"] == "closed", no_agent=False)
        for _ in range(3):
            controller.step()
    assert len(launches) == 1
    assert state.campaign()["research_outcome"] == "unassessed" and state.campaign()["invocations"] == 0
    assert not ledger.rows()


def test_later_constraints_are_staleness_facts_not_another_analysis(tmp_path):
    config, state, ledger, _ = fixture(tmp_path, program=CLOSER)
    Campaign(config, state=state, ledger=ledger).run()
    previous = state.campaign()["closure"]
    state.directive("Any future experiments must use a new evaluation split.")
    Campaign(config, state=state, ledger=ledger).run()
    current = state.campaign()
    assert current["closure"] == previous and current["assessment_scope_stale"]
    assert current["invocations"] == 1 and current["generation_state"] == "closed"
