"""A small restart-only configuration for an ordinary local researcher."""

from dataclasses import asdict, dataclass
from pathlib import Path
import tomllib

from labgoblin import credits
from labgoblin.paths import machine_ledger_path
from labgoblin.protocol import CONFIG_VERSION, Resources, boolean, integer, number, require_version, table, text
from labgoblin.storage import read_text


@dataclass(frozen=True)
class Config:
    version: int = CONFIG_VERSION
    model: str = "auto"
    reasoning_effort: str = "medium"
    max_ai_credits: int | float | str = 1000
    timeout_seconds: int | float = 5400
    cpus: int = 2
    memory_mb: int = 4096
    gpus: tuple[str, ...] = ()
    coach: bool = False
    max_stalled_reviews: int = 10
    consultant: bool = False
    closure_critic: bool = False
    coach_model: str | None = None
    closure_critic_model: str | None = None
    advice_model: str | None = None
    claim_review_model: str | None = None
    ledger: str = ""
    max_snapshot_bytes: int = 64 * 1024 * 1024 * 1024

    def __post_init__(self):
        require_version(self.version, CONFIG_VERSION, "configuration")
        text(self.model, "model")
        for field in ("coach_model", "closure_critic_model", "advice_model", "claim_review_model"):
            value = getattr(self, field)
            if value is not None:
                text(value, field)
                if value != value.strip():
                    raise ValueError(f"{field} must be a nonempty model ID without surrounding whitespace")
        if self.reasoning_effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("Unsupported reasoning_effort")
        credits.parse_limit(self.max_ai_credits)
        number(self.timeout_seconds, "timeout_seconds")
        if self.timeout_seconds < 60:
            raise ValueError("timeout_seconds must allow at least 60 seconds including shutdown")
        Resources(self.cpus, self.memory_mb, self.gpus)
        boolean(self.coach, "coach")
        integer(self.max_stalled_reviews, "max_stalled_reviews")
        boolean(self.consultant, "consultant")
        boolean(self.closure_critic, "closure_critic")
        if self.closure_critic and not (self.coach and self.consultant):
            raise ValueError("closure_critic requires both coach and consultant")
        if self.coach and self.max_ai_credits != "unlimited" and self.max_ai_credits < 60:
            raise ValueError("Coaching requires at least 60 credits for a researcher and stopping review")
        if self.consultant and self.max_ai_credits != "unlimited" and self.max_ai_credits < (90 if self.coach else 60):
            raise ValueError("Consultations require at least 60 credits, or 90 with stopping review")
        text(self.ledger, "ledger", empty=True)
        integer(self.max_snapshot_bytes, "max_snapshot_bytes")

    @property
    def resources(self):
        return Resources(self.cpus, self.memory_mb, self.gpus)

    @property
    def credit_limit(self):
        return credits.parse_limit(self.max_ai_credits)

    @property
    def ledger_path(self):
        return Path(self.ledger).resolve() if self.ledger else machine_ledger_path()

    def to_dict(self):
        return {key: value for key, value in {**asdict(self), "gpus": list(self.gpus)}.items()
                if value is not None}

    def model_for(self, purpose):
        overrides = {"researcher": None, "inspector": None, "coach": self.coach_model,
                     "closure_critique": self.closure_critic_model, "advice": self.advice_model,
                     "claim_review": self.claim_review_model}
        if purpose not in overrides:
            raise ValueError(f"Unknown model purpose: {purpose}")
        return overrides[purpose] or self.model

    @classmethod
    def parse(cls, value):
        value = dict(table(value, "configuration", cls.__dataclass_fields__))
        require_version(value.get("version"), CONFIG_VERSION, "configuration")
        if "gpus" in value:
            if not isinstance(value["gpus"], list):
                raise ValueError("gpus must be an array")
            value["gpus"] = tuple(value["gpus"])
        return cls(**value)

    @classmethod
    def load(cls, project):
        return cls.parse(tomllib.loads(read_text(Path(project) / "labgoblin.toml")))

    def toml(self):
        import tomli_w
        return tomli_w.dumps(self.to_dict())
