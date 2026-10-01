import base64
import json
from pathlib import Path

import pytest

from tests.test_controller import fixture
from tests.test_memory_v3 import COMPACTOR
from tests.test_workspace import admit, finish, request, setup
from labgoblin import reporting, workspace
from labgoblin.campaign import Campaign
from labgoblin.evidence import SizeLimitError, hash_file


REPORTER = COMPACTOR.replace(
    'if packet["kind"] == "compact":',
    '''if packet["kind"] == "report":
    view=packet["inventory"]
    observations=[o for a in view["attempts"] if a["selected"] for o in a["observations"] if "score" in o["metrics"]]
    claims=[dict(observation_id=o["id"],metric="score",value=o["metrics"]["score"]) for o in observations]
    result.update(view_id=view["id"],title="Synthetic result report",summary="The captured score is described by the checked claims.",
                  limitations="Synthetic controls only.",claims=claims,references=[o["id"] for o in observations])
elif packet["kind"] == "compact":''')


def measured(tmp_path, count=1):
    config, state, ledger = setup(tmp_path)
    goal = state.source("goal", b"Original finite scientific question.", origin="operator", head="goal")
    protocol = state.source("protocol", b"Original held-out comparison.", origin="operator", head="protocol")
    for index in range(count):
        launch = finish(state, admit(config, state, ledger, request(key=f"replication-{index}")))
        (Path(launch.metadata["execution"]["output"]) / "metrics.json").write_text(
            json.dumps({"score": 42 + index}), encoding="utf-8")
        workspace.collect_artifacts(state, launch.key.work_id)
    return config, state, ledger, goal, protocol


def test_selecting_one_replication_keeps_nine_and_the_original_sources(tmp_path):
    _, state, _, goal, protocol = measured(tmp_path, 9)
    selected = state.attempts()[0]["id"]
    view = reporting.seal_view(state, selected=[selected], selection_reason="Illustrate one prespecified replication.")
    state.source("goal", b"New, unrelated question.", origin="operator", head="goal", notify=True)
    state.source("protocol", b"New evaluation split.", origin="operator", head="protocol", notify=True)
    state.source("summary", b"Lossy current summary.", origin="operator", head="summary")
    report = reporting.publish_report(state, view["id"])
    inventory = reporting.page(state.db, view["id"])
    assert inventory["coverage"]["total"] == 9
    assert sum(row["selected"] for row in inventory["attempts"]) == 1
    html = Path(report["outputs"]["html"]["path"]).read_text(encoding="utf-8")
    assert "complete denominator: 9 attempts" in html and "<svg " in html
    assert "Original finite scientific question." in html and "Original held-out comparison." in html
    assert "New, unrelated question." not in html and "Lossy current summary." not in html
    records = [json.loads(line) for line in Path(report["outputs"]["manifest"]["path"]).read_text(encoding="utf-8").splitlines()]
    assert len([row for row in records if row["type"] == "attempt"]) == 9
    assert {goal, protocol}.issubset(row["source"]["id"] for row in records if row["type"] == "source")
    assert any(row["type"] == "observation" and row["observation"]["metadata"]["metrics"]["score"] == 42 for row in records)
    for output in report["outputs"].values():
        assert hash_file(Path(output["path"]), output["bytes"]) == output["sha256"]
    assert state.campaign()["invocations"] == 0


def test_recollection_never_changes_old_report_values_or_exact_observation(tmp_path):
    _, state, _, _, _ = measured(tmp_path)
    attempt = state.attempts()[0]
    view = reporting.seal_view(state)
    old = reporting.page(state.db, view["id"])["attempts"][0]["observations"][0]
    output = Path(json.loads(attempt["spec"])["root"]) / "output" / "metrics.json"
    output.write_text('{"score":999}', encoding="utf-8")
    workspace.collect_artifacts(state, attempt["id"], recollect=True)
    report = reporting.publish_report(state, view["id"])
    assert reporting.page(state.db, view["id"])["attempts"][0]["observations"][0]["metrics"] == {"score": 42}
    value = {"turn_id": "test", "packet_id": "test", "view_id": view["id"], "title": "Check", "summary": "Interpretation",
             "limitations": "Synthetic", "claims": [{"observation_id": old["id"], "metric": "score", "value": 999}],
             "references": [old["id"]]}
    with state.db.read() as conn, pytest.raises(ValueError, match="differs from captured"):
        reporting._validate_report(conn, value)
    assert "999" not in Path(report["outputs"]["html"]["path"]).read_text(encoding="utf-8").split("Retained source revisions")[0]


def test_report_claims_cannot_refer_to_another_view_or_unselected_measurement(tmp_path):
    _, state, _, _, _ = measured(tmp_path, 2)
    attempts = state.attempts()
    view = reporting.seal_view(state, selected=[attempts[0]["id"]], selection_reason="Preselected control.")
    page = reporting.page(state.db, view["id"])
    excluded = next(row for row in page["attempts"] if not row["selected"])["observations"][0]
    value = {"turn_id": "t", "packet_id": "p", "view_id": view["id"], "title": "Bounded scope", "summary": "Interpretation",
             "limitations": "Partial selection", "references": [],
             "claims": [{"observation_id": excluded["id"], "metric": "score", "value": excluded["metrics"]["score"]}]}
    with state.db.read() as conn, pytest.raises(ValueError, match="selected scope"):
        reporting._validate_report(conn, value)
    value.update(claims=[], references=["not-a-retained-reference"])
    with state.db.read() as conn, pytest.raises(ValueError, match="outside"):
        reporting._validate_report(conn, value)


def test_tiny_report_publication_limit_never_registers_success(tmp_path):
    _, state, _, _, _ = measured(tmp_path)
    view = reporting.seal_view(state)
    with pytest.raises(SizeLimitError):
        reporting.publish_report(state, view["id"], max_bytes=50)
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0


def test_owned_report_request_executes_once_without_changing_research_progress(tmp_path):
    config, state, ledger, _ = fixture(tmp_path, program=REPORTER)
    workspace.submit(state, config, request())
    Campaign(config, state=state, ledger=ledger).run()
    before = state.campaign()
    requested = state.request_maintenance("report")
    Campaign(config, state=state, ledger=ledger).run()
    current = state.campaign()
    assert current["progress"] == before["progress"] == "wait"
    assert current["invocations"] == before["invocations"] + 1
    with state.db.read() as conn:
        report = conn.execute("SELECT outputs FROM reports").fetchone()
        assert conn.execute("SELECT state FROM maintenance WHERE id=?", (requested["id"],)).fetchone()[0] == "completed"
    assert Path(json.loads(report["outputs"])["html"]["path"]).exists()
    assert state.request_maintenance("report")["id"] == requested["id"]
    assert not ledger.rows()


def test_report_publication_recovers_without_reinvoking_provider(tmp_path, monkeypatch):
    config, state, ledger, _ = fixture(tmp_path, program=REPORTER)
    Campaign(config, state=state, ledger=ledger).run()
    state.request_maintenance("report")
    publisher = reporting.publish_stream
    seen = []

    def cut(path, chunks, **kwargs):
        result = publisher(path, chunks, **kwargs)
        if path.name == "report.html" and not seen:
            seen.append(path)
            raise OSError("Injected publication cut after immutable HTML")
        return result

    monkeypatch.setattr(reporting, "publish_stream", cut)
    result = Campaign(config, state=state, ledger=ledger).run()
    assert any(error["category"] == "report_publication" for error in result["errors"])
    invocations = state.campaign()["invocations"]
    existing = seen[0].read_bytes()
    Campaign(config, state=state, ledger=ledger).run()
    assert state.campaign()["invocations"] == invocations
    assert seen[0].read_bytes() == existing
    assert not state.campaign()["blockers"] and not ledger.rows()
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1


def test_report_embeds_only_bounded_captured_local_figures(tmp_path):
    config, state, ledger = setup(tmp_path)
    state.source("goal", b"Show the exact synthetic figure.", origin="operator", head="goal")
    launch = finish(state, admit(config, state, ledger, request(artifacts=["figure.png"])))
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    (Path(launch.metadata["execution"]["output"]) / "figure.png").write_bytes(image)
    workspace.collect_artifacts(state, launch.key.work_id)
    view = reporting.seal_view(state)
    report = reporting.publish_report(state, view["id"])
    content = Path(report["outputs"]["html"]["path"]).read_text(encoding="utf-8")
    assert "data:image/png;base64," + base64.b64encode(image).decode() in content
    assert '<script' not in content and 'src="http' not in content


def test_different_explicit_selections_do_not_alias_one_maintenance_request(tmp_path):
    _, state, _, _, _ = measured(tmp_path, 2)
    rows = state.attempts()
    a = state.request_maintenance("report", options={"selected": [rows[0]["id"]], "selection_reason": "First control"})
    b = state.request_maintenance("report", options={"selected": [rows[1]["id"]], "selection_reason": "Second control"})
    assert a["id"] != b["id"]
    assert state.request_maintenance("report", options=json.loads(a["options"]))["id"] == a["id"]
