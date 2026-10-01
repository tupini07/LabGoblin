import csv
import io
from pathlib import Path
import sqlite3

import pytest

from labgoblin import results, workspace
from labgoblin.evidence import SizeLimitError
from tests.test_workspace import admit, finish, request, setup


def test_results_are_readonly_paginated_and_include_failed_collection(tmp_path):
    config, state, ledger = setup(tmp_path)
    first = finish(state, admit(config, state, ledger))
    (Path(first.metadata["execution"]["output"]) / "metrics.json").write_text('{"score":42}', encoding="utf-8")
    workspace.collect_artifacts(state, first.key.work_id)
    second = finish(state, admit(config, state, ledger, request(key="missing")))
    workspace.collect_artifacts(state, second.key.work_id)
    page = results.page(state.db, limit=1)
    assert page["total"] == 2 and page["has_more"]
    assert page["attempts"][0]["metrics"] == {"score": 42}
    assert page["attempts"][0]["scientific_acceptance"] == "not_inferred"
    last = results.page(state.db, offset=1, limit=1)
    assert not last["has_more"]
    assert last["attempts"][0]["collection"] == "failed"
    with state.db.read() as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE attempts SET status='queued'")


def test_latest_collection_does_not_inherit_old_successful_metrics(tmp_path):
    config, state, ledger = setup(tmp_path)
    envelope = finish(state, admit(config, state, ledger))
    path = Path(envelope.metadata["execution"]["output"]) / "metrics.json"
    path.write_text('{"score":42}', encoding="utf-8")
    first = workspace.collect_artifacts(state, envelope.key.work_id)
    path.unlink()
    latest = workspace.collect_artifacts(state, envelope.key.work_id, recollect=True)
    assert latest["event_id"] != first["event_id"]
    value = results.page(state.db)["attempts"][0]
    assert value["metrics"] == {}
    assert value["collection"] == "failed"
    with state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_csv_export_is_complete_bounded_and_never_replaces_prior_output(tmp_path):
    config, state, ledger = setup(tmp_path)
    for name in ("first", "second", "third"):
        envelope = finish(state, admit(config, state, ledger, request(key=name)))
        workspace.collect_artifacts(state, envelope.key.work_id)
    target = tmp_path / "results.csv"
    result = results.export(state.db, target)
    rows = list(csv.DictReader(io.StringIO(target.read_text(encoding="utf-8"))))
    assert result["attempts"] == len(rows) == 3
    assert {row["experiment_id"] for row in rows} == {"first", "second", "third"}
    assert all(row["collection"] == "failed" for row in rows)
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        results.export(state.db, target)
    assert target.read_bytes() == original
    limited = tmp_path / "limited.csv"
    with pytest.raises(SizeLimitError):
        results.export(state.db, limited, max_bytes=20)
    assert not limited.exists()
