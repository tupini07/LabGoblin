from dataclasses import replace
import json
from pathlib import Path
import time

import pytest

from labgoblin import credits, provider, runtime
from labgoblin.cli import main
from labgoblin.config import Config
from labgoblin.storage import read_json
from tests.conftest import arm
from tests.test_closure_critic import closing
from tests.test_simple_provider import FakeSession, Value, exercise, model_info
from tests.test_simple_runtime import fixture_provider


OVERRIDES = {"coach_model": "coach-model", "closure_critic_model": "critic-model",
             "advice_model": "advice-model", "claim_review_model": "review-model"}
MODELS = ["research-model", *OVERRIDES.values()]


def configured(project, ledger, **changes):
    root, config, record = project
    values = {"model": "research-model", "reasoning_effort": "high", "coach": True, "consultant": True,
              "closure_critic": True, "max_ai_credits": 1000, **OVERRIDES, **changes}
    config = replace(config, **values)
    return arm((root, config, record), ledger, coach=config.coach, consultant=config.consultant,
               allowance=1000 * credits.NANO)


def scripts_with_models(scripts, models=None):
    catalog = [model_info(name) for name in MODELS] if models is None else models
    return [{"models": catalog, **script} for script in scripts]


@pytest.mark.parametrize("field", OVERRIDES)
@pytest.mark.parametrize("value", ["", " ", " model", "model ", False, 42, [], {}])
def test_model_overrides_reject_invalid_ids(field, value):
    with pytest.raises(ValueError, match=field):
        Config.parse({**Config().to_dict(), field: value})


def test_optional_models_roundtrip_without_enabling_roles(tmp_path):
    original = Config.parse({"version": 5, "model": "campaign"})
    assert all(getattr(original, field) is None for field in OVERRIDES)
    assert not set(OVERRIDES).intersection(original.to_dict())
    assert all(original.model_for(purpose) == "campaign" for purpose in
               ("researcher", "coach", "closure_critique", "advice", "claim_review"))
    config = replace(original, **OVERRIDES)
    (tmp_path / "labgoblin.toml").write_text(config.toml(), encoding="utf-8")
    assert Config.load(tmp_path) == config
    assert not config.coach and not config.consultant and not config.closure_critic
    assert config.model_for("inspector") == "campaign"
    with pytest.raises(ValueError, match="Unknown model purpose"):
        config.model_for("consult-0")


def test_init_exposes_all_role_models_without_provider_calls(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtime.provider, "capabilities", lambda: pytest.fail("Init must not contact Copilot"))
    arguments = ["init", str(tmp_path), "--model", "research-model"]
    for field, model in OVERRIDES.items():
        arguments.extend(["--" + field.replace("_", "-"), model])
    assert main(arguments) == 0
    capsys.readouterr()
    config = Config.load(tmp_path)
    assert {field: getattr(config, field) for field in OVERRIDES} == OVERRIDES
    assert not config.coach and not config.consultant


@pytest.mark.parametrize("unset", [None, "all", *OVERRIDES])
def test_frozen_purpose_models_route_callers_and_fresh_adjudicator(project, ledger, unset):
    changes = ({field: None for field in OVERRIDES} if unset == "all"
               else {unset: None} if unset is not None else {})
    envelope = configured(project, ledger, **changes)
    config = Config.parse(envelope.config)
    root = Path(envelope.project)
    (root / "report.md").write_text("A consequential claim needing review.")
    (root / "labgoblin.toml").write_text(
        replace(config, model="changed-after-admission", **{field: "changed" for field in OVERRIDES}).toml())
    advice = {"question": "Which assumption deserves scrutiny?"}
    claim = {"purpose": "claim_review", "question": "Does the claim follow?", "evidence_paths": ["report.md"]}
    code, clients, outcome = exercise(envelope, scripts_with_models([
        {"consult": [advice, claim]}, {}, {},
        {"decision": "continue", "consult": [advice, claim]}, {}, {}, *closing(),
    ]), seconds=1800)
    assert code == 0, outcome
    purposes = ["researcher", "advice", "claim_review", "coach", "advice", "claim_review",
                "coach", "closure_critique", "coach"]
    expected = [config.model_for(purpose) for purpose in purposes]
    assert [client.session.options["model"] for client in clients] == expected
    assert [client.session.options["reasoning_effort"] for client in clients] == ["high"] * 9
    assert [len(client.session.prompts) for client in clients] == [3, 1, 1, 1, 1, 1, 1, 1, 1]
    assert "available_tools" not in clients[0].session.options
    usage = runtime.usage_record(envelope, complete=True)
    assert usage["totalNanoAiu"] == 110 * credits.NANO
    assert [session["model"] for session in usage["sessions"]] == expected
    assert [session["purpose"] for session in usage["sessions"]] == purposes
    for session in usage["sessions"]:
        directory = Path(envelope.root) / "sessions" / session["role"]
        identity, selected = read_json(directory / "identity.json"), read_json(directory / "model.json")
        assert identity["requested_model"] == selected["applied"]["modelId"] == session["model"]
        assert selected["session_id"] == session["session_id"]
        assert selected["envelope_digest"] == envelope.digest
        assert selected["applied"]["reasoningEffort"] == "high"
        if session["role"].startswith("consult-"):
            response = read_json(directory / "response.json")
            assert response["model"] == session["model"]
            assert "Model: " + session["model"] in (root / response["artifact"]).read_text()
    assert all(client.stopped for client in clients)


@pytest.mark.parametrize("field", OVERRIDES)
def test_unavailable_enabled_role_fails_before_any_prompt(project, ledger, field):
    envelope = configured(project, ledger, **{field: "unavailable"})
    code, clients, outcome = exercise(envelope, scripts_with_models([{}]))
    assert code == 1 and "unavailable" in outcome["reason"]
    assert len(clients) == 1 and clients[0].session is None and clients[0].stopped
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 0


@pytest.mark.parametrize("change,message", [
    ({"policy": {"state": "disabled", "terms": ""}}, "not enabled"),
    ({"policy": {"state": "unconfigured", "terms": ""}}, "not enabled"),
    ({"supportedReasoningEfforts": ["low"]}, "reasoning_effort"),
    ({"supportedReasoningEfforts": None}, "reasoning_effort"),
    ({"capabilities": {"supports": {"vision": True, "reasoningEffort": False}}}, "reasoning_effort"),
    ({"capabilities": {"supports": {"vision": False, "reasoningEffort": True}}}, "image evidence"),
])
@pytest.mark.parametrize("name", ["coach-model", "critic-model"])
def test_incompatible_review_model_fails_preflight(project, ledger, change, message, name):
    models = [model_info(model, **change) if model == name else model_info(model) for model in MODELS]
    envelope = configured(project, ledger)
    code, clients, outcome = exercise(envelope, scripts_with_models([{}], models))
    assert code == 1 and message in outcome["reason"]
    assert clients[0].session is None and clients[0].stopped
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 0


@pytest.mark.parametrize("applied", [
    {"modelId": "wrong-model", "reasoningEffort": "high"},
    {"modelId": "research-model", "reasoningEffort": "low"},
    {"modelId": "research-model"}, {},
])
def test_provider_substitution_never_gets_a_prompt(project, ledger, applied):
    envelope = configured(project, ledger)
    code, clients, outcome = exercise(envelope, scripts_with_models([{"applied_model": applied}]))
    assert code == 1 and "model/effort" in outcome["reason"]
    assert clients[0].session.prompts == [] and clients[0].stopped
    record = read_json(Path(envelope.root) / "sessions" / "researcher" / "model.json")
    assert record["applied"] == applied
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 0


def test_disabled_roles_do_not_require_their_preset_models(project, ledger):
    envelope = configured(project, ledger, coach=False, consultant=False, closure_critic=False)
    code, clients, outcome = exercise(envelope, scripts_with_models([{}], [model_info("research-model")]))
    assert code == 0, outcome
    assert len(clients) == 1 and clients[0].session.options["model"] == "research-model"


def test_fresh_adjudicator_model_cannot_change_before_its_prompt(project, ledger, monkeypatch):
    original = FakeSession.current_model

    async def changed(session):
        if session.options["model"] == "coach-model" and "critique_response" in session.script:
            if getattr(session, "model_checked", False):
                return Value({"modelId": "different-coach", "reasoningEffort": "high"})
            session.model_checked = True
        return await original(session)

    monkeypatch.setattr(FakeSession, "current_model", changed)
    envelope = configured(project, ledger)
    code, clients, outcome = exercise(envelope, scripts_with_models([
        {}, {"decision": "continue"}, *closing(),
    ]), seconds=1800)
    assert code == 1 and outcome["status"] == "review_unavailable"
    assert "different-coach" in outcome["reason"]
    assert len(clients[2].session.prompts) == 1
    directory = Path(envelope.root) / "sessions" / "adjudicator-1"
    assert read_json(directory / "model.json")["applied"]["modelId"] == "coach-model"
    assert read_json(directory / "model-mismatch-0.json")["applied"]["modelId"] == "different-coach"
    assert not (Path(envelope.root) / "review-1.json").exists()
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 60 * credits.NANO


def test_model_attribution_survives_event_truncation(project, ledger, monkeypatch):
    monkeypatch.setattr(provider, "MAX_EVENTS_BYTES", 0)
    envelope = configured(project, ledger, coach=False, consultant=False, closure_critic=False)
    code, _, outcome = exercise(envelope, scripts_with_models([{}]))
    assert code == 0, outcome
    session = runtime.usage_record(envelope, complete=True)["sessions"][0]
    assert session["events_truncated"] and session["model"] == "research-model"
    directory = Path(envelope.root) / "sessions" / "researcher"
    assert (directory / "events.jsonl").stat().st_size == 0
    assert read_json(directory / "model.json")["applied"]["modelId"] == "research-model"


def test_catalog_failure_is_explicit_and_spends_nothing(project, ledger):
    envelope = configured(project, ledger)
    code, clients, outcome = exercise(envelope, [{"catalog_error": True}])
    assert code == 1 and "Model catalog unavailable" in outcome["reason"]
    assert clients[0].stopped and clients[0].session is None
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 0


@pytest.mark.parametrize("boundary", ["cancelled", "timed_out"])
def test_model_verification_cannot_cross_prompt_admission_boundary(project, ledger, monkeypatch, boundary):
    original = provider.Session.verify_model

    async def interrupted(session):
        checked = bool(session.model_snapshot)
        await original(session)
        if checked:
            if boundary == "cancelled":
                (Path(session.envelope.root) / "cancel").write_text("Operator cancellation")
            else:
                session.deadline = time.monotonic() + provider.GRACE_SECONDS - 1

    monkeypatch.setattr(provider.Session, "verify_model", interrupted)
    envelope = configured(project, ledger, coach=False, consultant=False, closure_critic=False)
    code, clients, outcome = exercise(envelope, scripts_with_models([{}]))
    assert code == (130 if boundary == "cancelled" else 124) and outcome["status"] == boundary
    assert clients[0].session.prompts == []
    assert runtime.usage_record(envelope, complete=True)["totalNanoAiu"] == 0


def test_native_sdk_role_models_are_frozen_and_attributed(project, ledger, monkeypatch):
    root, config, _ = project
    config = replace(config, model="research-model", reasoning_effort="high", coach=True, consultant=True,
                     closure_critic=True, timeout_seconds=1800, max_ai_credits=1000, **OVERRIDES)
    (root / "labgoblin.toml").write_text(config.toml())
    fixture_provider(root, monkeypatch, "closure_critic")
    fixture = {**read_json(root / "fixture-provider.json"), "models": MODELS}
    (root / "fixture-provider.json").write_text(json.dumps(fixture))
    result = runtime.run(root)
    assert result["status"] == "completed", result
    assert result["usage"]["totalNanoAiu"] == 70 * credits.NANO
    sessions = result["usage"]["sessions"]
    assert [item["model"] for item in sessions] == [
        "research-model", "coach-model", "coach-model", "critic-model", "coach-model"]
    assert [item["prompts"] for item in sessions] == [3, 1, 1, 1, 1]
    for item in sessions:
        options = read_json(root / f"fixture-session-{item['session_id']}.json")
        assert options["model"] == item["model"]
        assert options["reasoningEffort"] == item["reasoning_effort"] == "high"
    (root / "labgoblin.toml").write_text(replace(config, coach_model="candidate-only").toml())
    status = runtime.status(root)
    assert status["runs"][-1]["config"]["coach_model"] == "coach-model"
    assert status["candidate_config"]["coach_model"] == "candidate-only"
    assert not ledger.rows()


def test_native_unavailable_coach_stops_before_research_and_releases_ownership(project, ledger, monkeypatch):
    root, config, _ = project
    (root / "labgoblin.toml").write_text(
        replace(config, model="model-free", coach=True, coach_model="unavailable").toml())
    fixture_provider(root, monkeypatch)
    result = runtime.run(root)
    assert result["status"] == "failed" and "unavailable" in result["reason"]
    assert result["usage"]["totalNanoAiu"] == 0
    assert [session["prompts"] for session in result["usage"]["sessions"]] == [0]
    assert not (root / "report.md").exists()
    calls = [json.loads(line)["method"] for line in (root / "fixture-rpc.jsonl").read_text().splitlines()]
    assert "models.list" in calls and "session.create" not in calls and "session.send" not in calls
    assert runtime.status(root)["budget"]["remaining"] == 300
    assert not ledger.rows()
