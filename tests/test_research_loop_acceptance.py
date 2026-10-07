import json
from pathlib import Path

import pytest

from tests import research_loop_acceptance as acceptance
from tests.test_controller import fixture


def arguments(tmp_path, ledger):
    return ["--output", str(tmp_path / "evaluation"), "--ledger", str(ledger.path),
            "--variant", "workspace", "--variant", "flat", "--scenario", "optional-branch",
            "--scenario", "blocked", "--seed", "1", "--credits", "600", "--model", "synthetic-model"]


def test_prepare_only_creates_every_selected_case_without_admitting_work(tmp_path, monkeypatch):
    _, _, ledger, _ = fixture(tmp_path)
    original = acceptance.command

    def guarded(python, source, root, args):
        assert args[0] in ("init", "validate"), "Prepare-only attempted inference or execution"
        return original(python, source, root, args)

    monkeypatch.setattr(acceptance, "command", guarded)
    assert acceptance.main(arguments(tmp_path, ledger)) == 0
    rows = json.loads((tmp_path / "evaluation" / "results.json").read_bytes())
    assert len(rows) == 4
    assert all(row["measurements"]["provider_roles"] == [] and row["measurements"]["attempts"] == 0 for row in rows)
    assert all(row["validation"]["returncode"] == 0 for row in rows)
    assert not ledger.rows()
    assert "not run" in (tmp_path / "evaluation" / "report.html").read_text()
    assert len(json.loads((tmp_path / "evaluation" / "human-grading.json").read_bytes())) == 4


def test_live_requires_exact_total_approval_before_creating_output(tmp_path):
    _, _, ledger, _ = fixture(tmp_path)
    with pytest.raises(SystemExit) as failure:
        acceptance.main([*arguments(tmp_path, ledger), "--live", "--approve-total-credits", "600"])
    assert failure.value.code == 2
    assert not (tmp_path / "evaluation").exists()


def test_failure_is_retained_without_skipping_other_selected_cases(tmp_path, monkeypatch):
    _, _, ledger, _ = fixture(tmp_path)
    original = acceptance.command
    failed = []

    def one_failure(python, source, root, args):
        if args[0] == "init" and not failed:
            failed.append(str(root))
            return {"returncode": 7, "stdout": "", "stderr": "Synthetic initialization error"}
        return original(python, source, root, args)

    monkeypatch.setattr(acceptance, "command", one_failure)
    assert acceptance.main(arguments(tmp_path, ledger)) == 1
    rows = json.loads((tmp_path / "evaluation" / "results.json").read_bytes())
    assert len(rows) == 4 and rows[0]["initialization"]["stderr"] == "Synthetic initialization error"
    assert all(row["validation"]["returncode"] == 0 for row in rows[1:])


@pytest.mark.parametrize("scenario", acceptance.CASES)
def test_synthetic_output_scoring_is_independently_checkable(tmp_path, scenario):
    _, targets = acceptance.case_data(scenario, 42)
    path = tmp_path / "answer.json"
    path.write_text(json.dumps({"status": "blocked" if scenario == "blocked" else "solved",
                                "predictions": targets, "limitations": "Synthetic-domain only"}))
    assert acceptance.score(tmp_path, scenario, 42)["correct"]
    path.write_text(json.dumps({"status": "solved", "predictions": [], "limitations": "Wrong answer"}))
    assert not acceptance.score(tmp_path, scenario, 42)["correct"]
