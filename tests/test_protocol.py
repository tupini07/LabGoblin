from copy import deepcopy
from dataclasses import asdict
import json
import sys

import pytest

from xgenius.config import initial_config, load_config, parse_config
from xgenius.protocol import (
    HANDOFF_BYTES, Handoff, LaunchEnvelope, LaunchKey, Limit, Resources, argv, canonical,
)


def config(tmp_path, **changes):
    raw = initial_config("fixture", "copilot")
    for section, values in changes.items():
        raw[section].update(values)
    return parse_config(raw, tmp_path / "xgenius.toml")


def test_local_config_load_does_not_create_state(tmp_path):
    raw = initial_config("fixture")
    import tomli_w
    path = tmp_path / "xgenius.toml"
    path.write_text(tomli_w.dumps(raw), encoding="utf-8")
    value = load_config(path)
    assert value.agent.command == ("claude", "--dangerously-skip-permissions")
    assert value.runners["native"].kind == "native"
    assert value.agent.resources.memory_mb == 2048
    assert not value.state_dir.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["xgenius.toml"]


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_old_or_ambiguous_versions_rejected(tmp_path, version):
    raw = initial_config("fixture")
    raw["schema_version"] = version
    with pytest.raises(ValueError, match="Unsupported configuration"):
        parse_config(raw, tmp_path / "xgenius.toml")


@pytest.mark.parametrize("section,values", [
    ("campaign", {"max_seconds": -1}), ("campaign", {"max_invocations": True}),
    ("campaign", {"max_invocations": 1.5}), ("campaign", {"max_seconds": float("inf")}),
    ("campaign", {"cpus": 0}), ("campaign", {"memory_mb": 0}),
    ("campaign", {"max_jobs": 0}), ("agent", {"timeout_seconds": 0}),
    ("agent", {"max_turns": 10}), ("storage", {"log_bytes": 0}),
    ("storage", {"capture_bytes": 1}), ("agent", {"provider": "automatic"}),
    ("agent", {"resources": {"cpus": 10, "memory_mb": 10}}),
    ("agent", {"resources": {"cpus": 1, "memory_mb": 10, "gpus": ["gpu"]}}),
    ("execution", {"default_runner": "missing"}),
])
def test_invalid_config_fields(tmp_path, section, values):
    raw = initial_config("fixture")
    raw.setdefault(section, {}).update(values)
    with pytest.raises(ValueError):
        parse_config(raw, tmp_path / "xgenius.toml")


def test_explicit_unlimited_dimensions_and_zero_gpu(tmp_path):
    value = config(tmp_path, campaign={"max_seconds": 0, "max_invocations": 0, "max_gpu_hours": 0})
    assert value.campaign.max_seconds.allows(10**12, 10**12)
    assert not value.campaign.max_invocations.exhausted(10**12)
    assert value.campaign.max_invocations.view(50, 2) == {
        "configured": 0, "unlimited": True, "used": 50, "reserved": 2, "remaining": None,
    }
    assert value.campaign.max_gpu_hours == 0
    assert "Infinity" not in canonical(value.campaign.max_seconds.view(1)).decode()


def test_finite_budget_boundaries():
    limit = Limit(3)
    assert limit.allows(2, 1)
    assert not limit.allows(2, 2)
    assert limit.exhausted(3)
    assert limit.view(2, 1)["remaining"] == 0
    with pytest.raises(ValueError):
        limit.allows(-1)


def test_runner_fields_are_kind_specific_and_explicit(tmp_path):
    raw = initial_config("fixture")
    raw["runners"].update({
        "guest": {"kind": "wsl", "python": "python3", "distro": "Ubuntu"},
        "container": {"kind": "docker", "python": "python", "image": "prepared", "context": "local"},
    })
    assert len(parse_config(raw, tmp_path / "xgenius.toml").runners) == 3
    for name, field, value in [
        ("native", "kind", "local"), ("native", "python", ""), ("native", "distro", "Ubuntu"),
        ("guest", "distro", "docker-desktop"), ("container", "context", ""),
    ]:
        broken = deepcopy(raw)
        broken["runners"][name][field] = value
        with pytest.raises(ValueError):
            parse_config(broken, tmp_path / "xgenius.toml")


def test_config_identity_changes_without_mutating_an_existing_config(tmp_path):
    raw = initial_config("fixture")
    before = parse_config(raw, tmp_path / "xgenius.toml")
    raw["agent"]["timeout_seconds"] = 123
    after = parse_config(raw, tmp_path / "xgenius.toml")
    assert before.agent.timeout_seconds == 600
    assert after.agent.timeout_seconds == 123
    assert before.revision != after.revision


def test_no_legacy_sections_or_unknown_input_fields(tmp_path):
    for extra in [{"clusters": {}}, {"watcher": {}}, {"safety": {}},
                  {"inputs": {"x": {"path": "input", "guess": True}}}]:
        raw = {**initial_config("fixture"), **extra}
        with pytest.raises(ValueError, match="Unknown"):
            parse_config(raw, tmp_path / "xgenius.toml")


def test_sandbox_requires_scoped_explicit_profile(tmp_path):
    with pytest.raises(ValueError, match="provisioned"):
        config(tmp_path, agent={"sandbox": True})
    with pytest.raises(ValueError, match="under this campaign"):
        config(tmp_path, agent={"sandbox": True, "copilot_home": str(tmp_path.parent)})
    value = config(tmp_path, agent={"sandbox": True, "copilot_home": ".xgenius\\copilot"})
    assert value.agent.invocation_bundle == 2
    assert not value.state_dir.exists()


def test_launch_envelope_roundtrip_and_incarnation(tmp_path):
    key = LaunchKey("campaign", 1, "work", "grant", "nonce")
    value = LaunchEnvelope(
        key, "research", (sys.executable, "with space", "", '"quoted"'),
        str(tmp_path), str(tmp_path), str(tmp_path / "campaign.db"),
        str(tmp_path / "machine.db"), "ledger", 10, Resources(1, 128), "config",
    )
    assert LaunchEnvelope.parse(json.loads(canonical(asdict(value)))) == value
    changed = asdict(value)
    changed["key"]["grant_id"] = "different"
    assert LaunchEnvelope.parse(changed).digest != value.digest
    changed["protocol"] = 2
    with pytest.raises(ValueError, match="Unsupported worker"):
        LaunchEnvelope.parse(changed)
    assert argv(["program", "", "a b"]) == ("program", "", "a b")
    with pytest.raises(ValueError):
        Resources(1, 128, ("gpu", "gpu"))


def handoff(**changes):
    return {"turn_id": "turn", "packet_id": "packet", "summary": "Prepared a control.",
            "rationale": "Need to compare the baseline.", "next_step": "Inspect the control.",
            "disposition": "wait", "reason": "The control is running.", **changes}


def test_owned_handoff_validation_and_deferral():
    value = Handoff.parse(handoff(evidence=[
        {"event_id": "event", "disposition": "deferred", "reason": "Running",
         "wake_condition": "Attempt completes"}]))
    assert value.evidence[0].wake_condition == "Attempt completes"
    for changes in [
        {"summary": ""}, {"journal": ".xgenius/journal.md"}, {"disposition": "complete"},
        {"disposition": "finalize"}, {"maintenance": ["shell"]},
        {"evidence": [{"event_id": "event", "disposition": "deferred", "reason": "Later"}]},
        {"summary": "x" * HANDOFF_BYTES},
    ]:
        with pytest.raises(ValueError):
            Handoff.parse(handoff(**changes))
    assert Handoff.parse(handoff(disposition="finalize", stopping_criterion="Control assessed."))


def test_json_contract_rejects_nonfinite_values():
    with pytest.raises(ValueError):
        canonical({"metric": float("nan")})
