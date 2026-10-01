"""Strict local-only configuration. Loading never creates or migrates state."""

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import re
import sys
import tomllib

from labgoblin.protocol import (
    CONFIG_VERSION, Limit, Resources, argv, boolean, fingerprint, integer,
    number, require_version, strings, table, text,
)
from labgoblin.paths import configuration_path, state_directory


AGENT_COMMANDS = {
    "claude": ("claude", "--dangerously-skip-permissions"),
    "copilot": ("copilot", "--allow-all"),
}


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    research_goal: str = "research_goal.md"


@dataclass(frozen=True)
class Runner:
    kind: str
    python: str
    distro: str = ""
    image: str = ""
    context: str = ""
    network: bool = False


@dataclass(frozen=True)
class ExecutionConfig:
    default_runner: str
    source_files: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CampaignConfig:
    resources: Resources
    max_jobs: int
    max_gpu_hours: float
    max_seconds: Limit
    max_invocations: Limit


@dataclass(frozen=True)
class AgentConfig:
    provider: str
    command: tuple[str, ...]
    resources: Resources
    timeout_seconds: float = 600
    retries: int = 1
    model: str = ""
    reasoning_effort: str = ""
    sandbox: bool = False
    copilot_home: str = ""

    @property
    def invocation_bundle(self) -> int:
        return 2 if self.sandbox else 1


@dataclass(frozen=True)
class Watermark:
    path: str
    min_free_mb: int


@dataclass(frozen=True)
class StorageConfig:
    log_bytes: int = 16 * 1024 * 1024
    tail_bytes: int = 64 * 1024
    snapshot_bytes: int = 256 * 1024 * 1024
    capture_bytes: int = 1024 * 1024
    metrics_bytes: int = 256 * 1024
    volumes: dict[str, Watermark] = field(default_factory=dict)


@dataclass(frozen=True)
class InputConfig:
    path: str
    identity: str = ""
    sha256: str = ""
    wsl_path: str = ""
    prompt_access: bool = False
    assurance: str = "declared"
    verification_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class ChatSettings:
    enabled: bool = True
    model: str = "auto"
    reasoning_effort: str = ""
    timeout_seconds: float = 120
    cli_path: str = ""
    cpus: int = 1
    memory_mb: int = 2048
    max_invocations: int = 20


def parse_chat_settings(value: dict) -> ChatSettings:
    value = table(value, "dashboard.chat", set(ChatSettings.__dataclass_fields__))
    settings = ChatSettings(**value)
    boolean(settings.enabled, "dashboard.chat.enabled")
    for name in ("model", "reasoning_effort", "cli_path"):
        item = text(getattr(settings, name), f"dashboard.chat.{name}", empty=name != "model")
        if len(item) > 1024:
            raise ValueError(f"dashboard.chat.{name} exceeds 1024 characters")
    if settings.reasoning_effort not in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
        raise ValueError("Unsupported dashboard.chat.reasoning_effort")
    number(settings.timeout_seconds, "dashboard.chat.timeout_seconds")
    if not 5 <= settings.timeout_seconds <= 600:
        raise ValueError("dashboard.chat.timeout_seconds must be between 5 and 600")
    Resources(settings.cpus, settings.memory_mb)
    integer(settings.max_invocations, "dashboard.chat.max_invocations")
    return settings


@dataclass(frozen=True)
class LabGoblinConfig:
    project: ProjectConfig
    execution: ExecutionConfig
    runners: dict[str, Runner]
    campaign: CampaignConfig
    agent: AgentConfig
    storage: StorageConfig
    inputs: dict[str, InputConfig]
    config_path: str
    revision: str
    chat: ChatSettings = field(default_factory=ChatSettings)

    @property
    def root(self) -> Path:
        return Path(self.config_path).parent

    @property
    def state_dir(self) -> Path:
        return state_directory(self.config_path)


def environment(value, name: str) -> dict[str, str]:
    result = {}
    for key, item in table(value, name).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValueError(f"Invalid {name} variable: {key}")
        result[key] = text(item, f"{name}.{key}", empty=True)
    return result


def _runner(name: str, value: dict) -> Runner:
    prefix = f"runners.{name}"
    value = table(value, prefix, set(Runner.__dataclass_fields__))
    kind = text(value.get("kind"), f"{prefix}.kind")
    if kind not in ("native", "wsl", "docker"):
        raise ValueError(f"Unsupported runner kind: {kind}; use native, wsl or docker")
    python = text(value.get("python"), f"{prefix}.python")
    allowed = {"kind", "python"}
    if kind == "wsl":
        allowed.add("distro")
    if kind == "docker":
        allowed.update(("image", "context", "network"))
    if set(value) - allowed:
        raise ValueError(f"Settings do not apply to {kind} runner: {sorted(set(value) - allowed)}")
    distro = text(value.get("distro", ""), f"{prefix}.distro", empty=kind != "wsl")
    image = text(value.get("image", ""), f"{prefix}.image", empty=kind != "docker")
    context = text(value.get("context", ""), f"{prefix}.context", empty=kind != "docker")
    if distro.casefold() in ("docker-desktop", "docker-desktop-data"):
        raise ValueError("Use a development WSL distro, not Docker Desktop's internal distro")
    return Runner(kind, python, distro, image, context,
                  boolean(value.get("network", False), f"{prefix}.network"))


def _input(name: str, value: dict) -> InputConfig:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError("Input names must be valid environment variable identifiers")
    prefix = f"inputs.{name}"
    value = table(value, prefix, set(InputConfig.__dataclass_fields__))
    pin = text(value.get("sha256", ""), f"{prefix}.sha256", empty=True)
    if pin and not re.fullmatch("[0-9a-fA-F]{64}", pin):
        raise ValueError(f"{prefix}.sha256 must be a SHA-256 hex digest")
    assurance = value.get("assurance", "checked" if pin else "declared")
    if assurance not in ("declared", "checked", "stable-consumption", "unmanaged"):
        raise ValueError(f"Unknown {prefix}.assurance")
    if assurance == "checked" and not pin:
        raise ValueError(f"{prefix}: checked assurance requires sha256")
    return InputConfig(
        path=text(value.get("path"), f"{prefix}.path"),
        identity=text(value.get("identity", ""), f"{prefix}.identity", empty=True),
        sha256=pin.lower(),
        wsl_path=text(value.get("wsl_path", ""), f"{prefix}.wsl_path", empty=True),
        prompt_access=boolean(value.get("prompt_access", False), f"{prefix}.prompt_access"),
        assurance=assurance,
        verification_bytes=integer(value.get("verification_bytes", 64 * 1024 * 1024),
                                   f"{prefix}.verification_bytes"),
    )


def parse_config(raw: dict, path: str | Path) -> LabGoblinConfig:
    raw = table(raw, "configuration")
    require_version(raw.get("schema_version"), CONFIG_VERSION, "configuration")
    table(raw, "configuration", {"schema_version", "project", "execution", "runners",
                                "campaign", "agent", "storage", "inputs", "dashboard"})
    path = Path(path).resolve()
    project = table(raw.get("project", {}), "project", {"name", "research_goal"})
    goal = text(project.get("research_goal", "research_goal.md"), "project.research_goal")
    if not (path.parent / goal).resolve().is_relative_to(path.parent):
        raise ValueError("project.research_goal must be inside the project")
    project_config = ProjectConfig(text(project.get("name", path.parent.name), "project.name"), goal)
    execution = table(raw.get("execution", {}), "execution",
                      {"default_runner", "source_files", "environment"})
    runners = {text(name, "runner name"): _runner(name, value)
               for name, value in table(raw.get("runners", {}), "runners").items()}
    default = text(execution.get("default_runner"), "execution.default_runner")
    if default not in runners:
        raise ValueError(f"Default runner {default!r} is not defined")
    execution_config = ExecutionConfig(
        default, strings(execution.get("source_files", []), "execution.source_files", empty=True),
        environment(execution.get("environment", {}), "execution.environment"))
    campaign = table(raw.get("campaign", {}), "campaign",
                     {"cpus", "memory_mb", "gpus", "max_jobs", "max_gpu_hours",
                      "max_seconds", "max_invocations"})
    resources = Resources.parse({k: v for k, v in campaign.items() if k in ("cpus", "memory_mb", "gpus")})
    campaign_config = CampaignConfig(
        resources,
        integer(campaign.get("max_jobs"), "campaign.max_jobs"),
        number(campaign.get("max_gpu_hours"), "campaign.max_gpu_hours", zero=True),
        Limit(number(campaign.get("max_seconds"), "campaign.max_seconds", zero=True)),
        Limit(integer(campaign.get("max_invocations"), "campaign.max_invocations", zero=True)),
    )
    agent = table(raw.get("agent", {}), "agent",
                  {"provider", "command", "resources", "timeout_seconds", "retries",
                   "model", "reasoning_effort", "sandbox", "copilot_home"})
    provider = text(agent.get("provider", "claude"), "agent.provider")
    if provider not in AGENT_COMMANDS:
        raise ValueError("agent.provider must be claude or copilot")
    command = argv(agent.get("command", AGENT_COMMANDS[provider]), "agent.command")
    agent_resources = Resources.parse(agent.get("resources", {}))
    if agent_resources.gpus:
        raise ValueError("Managed provider resources do not support GPU allocation")
    if agent_resources.cpus > resources.cpus or agent_resources.memory_mb > resources.memory_mb:
        raise ValueError("Agent resource request exceeds the campaign envelope")
    sandbox = boolean(agent.get("sandbox", False), "agent.sandbox")
    home = text(agent.get("copilot_home", ""), "agent.copilot_home", empty=True)
    if sandbox:
        if provider != "copilot" or not home:
            raise ValueError("Sandbox requires Copilot and an explicitly provisioned copilot_home")
        sandbox_root = state_directory(path)
        if not (path.parent / home).resolve().is_relative_to(sandbox_root):
            raise ValueError(f"Sandbox profile must be under this campaign's {sandbox_root} directory")
    agent_config = AgentConfig(
        provider, command, agent_resources,
        number(agent.get("timeout_seconds", 600), "agent.timeout_seconds"),
        integer(agent.get("retries", 1), "agent.retries", zero=True),
        text(agent.get("model", ""), "agent.model", empty=True),
        text(agent.get("reasoning_effort", ""), "agent.reasoning_effort", empty=True),
        sandbox, home,
    )
    storage = table(raw.get("storage", {}), "storage", set(StorageConfig.__dataclass_fields__))
    defaults = StorageConfig()
    limits = {name: integer(storage.get(name, getattr(defaults, name)), f"storage.{name}")
              for name in ("log_bytes", "tail_bytes", "snapshot_bytes", "capture_bytes", "metrics_bytes")}
    if limits["metrics_bytes"] > limits["capture_bytes"]:
        raise ValueError("storage.metrics_bytes cannot exceed capture_bytes")
    volumes = {}
    for name, value in table(storage.get("volumes", {}), "storage.volumes").items():
        volume = table(value, f"storage.volumes.{name}", {"path", "min_free_mb"})
        volumes[text(name, "volume name")] = Watermark(
            str((path.parent / text(volume.get("path"), "volume.path")).resolve()),
            integer(volume.get("min_free_mb"), "volume.min_free_mb"))
    inputs = {name: _input(name, value)
              for name, value in table(raw.get("inputs", {}), "inputs").items()}
    dashboard = table(raw.get("dashboard", {}), "dashboard", {"chat"})
    return LabGoblinConfig(project_config, execution_config, runners, campaign_config,
                        agent_config, StorageConfig(**limits, volumes=volumes), inputs,
                        str(path), fingerprint(raw), parse_chat_settings(dashboard.get("chat", {})))


def load_config(path: str | Path = ".") -> LabGoblinConfig:
    path = configuration_path(path)
    from labgoblin.evidence import read_bytes
    return parse_config(tomllib.loads(read_bytes(path, 64 * 1024).decode("utf-8")), path)


def restore_config(snapshot: dict, *, revision: str, path: str | Path) -> LabGoblinConfig:
    """Validate the existing persisted dataclass representation without reading TOML."""
    snapshot = table(snapshot, "configuration snapshot", set(LabGoblinConfig.__dataclass_fields__))
    if set(snapshot) != set(LabGoblinConfig.__dataclass_fields__):
        raise ValueError("Configuration snapshot is incomplete")
    if (snapshot["revision"] != revision or not re.fullmatch("[0-9a-f]{64}", revision)
            or snapshot["config_path"] != str(Path(path).resolve())):
        raise ValueError("Configuration snapshot identity/path does not match its campaign")
    campaign = table(snapshot["campaign"], "snapshot.campaign", set(CampaignConfig.__dataclass_fields__))
    limits = {}
    for name in ("max_seconds", "max_invocations"):
        value = table(campaign.get(name), name, {"value"})
        limits[name] = value.get("value")
    runners = {}
    for name, value in table(snapshot["runners"], "snapshot.runners").items():
        value = table(value, "snapshot.runner", set(Runner.__dataclass_fields__))
        fields = {"kind", "python"}
        if value.get("kind") == "wsl":
            fields.add("distro")
        elif value.get("kind") == "docker":
            fields.update(("image", "context", "network"))
        runners[name] = {key: item for key, item in value.items() if key in fields}
    raw = {"schema_version": CONFIG_VERSION, "project": snapshot["project"],
           "execution": snapshot["execution"], "runners": runners,
           "campaign": {**table(campaign.get("resources"), "snapshot.resources"), **limits,
                        "max_jobs": campaign.get("max_jobs"), "max_gpu_hours": campaign.get("max_gpu_hours")},
           "agent": snapshot["agent"], "storage": snapshot["storage"], "inputs": snapshot["inputs"],
           "dashboard": {"chat": snapshot["chat"]}}
    config = replace(parse_config(raw, path), revision=revision)
    if fingerprint(asdict(config)) != fingerprint(snapshot):
        raise ValueError("Configuration snapshot does not round-trip through the strict schema")
    return config


def initial_config(name: str, provider: str = "claude", python: str | None = None) -> dict:
    if provider not in AGENT_COMMANDS:
        raise ValueError("Provider must be claude or copilot")
    return {
        "schema_version": CONFIG_VERSION,
        "project": {"name": name, "research_goal": "research_goal.md"},
        "execution": {"default_runner": "native", "source_files": []},
        "runners": {"native": {"kind": "native", "python": python or sys.executable}},
        "campaign": {"cpus": 2, "memory_mb": 4096, "gpus": [], "max_jobs": 1,
                     "max_gpu_hours": 0, "max_seconds": 3600, "max_invocations": 10},
        "agent": {"provider": provider, "command": list(AGENT_COMMANDS[provider]),
                  "model": "", "reasoning_effort": "",
                  "resources": {"cpus": 1, "memory_mb": 2048},
                  "timeout_seconds": 600, "retries": 1, "sandbox": False},
    }


def get_project_dir(config: LabGoblinConfig) -> str:
    return str(config.root)


def get_labgoblin_dir(config: LabGoblinConfig) -> str:
    return str(config.state_dir)
