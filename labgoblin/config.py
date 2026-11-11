"""A small restart-only configuration for an ordinary local researcher."""

import ast
from dataclasses import asdict, dataclass
import math
import operator
from pathlib import Path
import tomllib

from labgoblin import credits
from labgoblin.paths import machine_ledger_path
from labgoblin.protocol import CONFIG_VERSION, Resources, boolean, integer, number, require_version, table, text
from labgoblin.session_log import DEFAULT_LOG_BYTES, MAX_LOG_BYTES
from labgoblin.storage import read_text
from labgoblin.supervisor import Schedule


ROLE_MODELS = ("coach_model", "closure_critic_model", "advice_model", "claim_review_model")
TIMEOUT_FIELDS = ("timeout_seconds", "consultation_timeout_seconds", "goal_review_timeout_seconds")


def timeout_expression(value, field="timeout_seconds"):
    if not isinstance(value, str):
        return value
    if not value.strip() or len(value) > 256:
        raise ValueError(f"{field} expression must contain 1 to 256 characters")
    operations = {ast.Add: operator.add, ast.Sub: operator.sub,
                  ast.Mult: operator.mul, ast.Div: operator.truediv}

    def calculate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            result = operations[type(node.op)](calculate(node.left), calculate(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = calculate(node.operand)
            if isinstance(node.op, ast.USub):
                result = -result
        else:
            raise ValueError(f"{field} allows only numbers, parentheses and +, -, *, /")
        if not math.isfinite(result):
            raise ValueError(f"{field} arithmetic must remain finite")
        return result

    try:
        tree = ast.parse(value.strip(), mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 64:
            raise ValueError(f"{field} expression exceeds 64 syntax nodes")
        return calculate(tree.body)
    except (SyntaxError, ZeroDivisionError, OverflowError) as error:
        raise ValueError(f"Invalid {field} arithmetic: {error}") from error


@dataclass(frozen=True)
class Config:
    version: int = CONFIG_VERSION
    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "high"
    max_ai_credits: int | float | str = 1000
    timeout_seconds: int | float = 5400
    cpus: int | str = "unlimited"
    memory_mb: int | str = "unlimited"
    gpus: tuple[str, ...] = ()
    coach: bool = True
    goal_review_interval_seconds: int = 3 * 60 * 60
    goal_review_timeout_seconds: int | float = 30 * 60
    max_stalled_reviews: int = 10
    consultant: bool = True
    consultation_timeout_seconds: int | float = 60 * 60
    closure_critic: bool | None = None
    coach_model: str | None = "gpt-6-astra"
    closure_critic_model: str | None = "gpt-6-astra"
    advice_model: str | None = "gpt-6-astra"
    claim_review_model: str | None = "gpt-6-astra"
    ledger: str = ""
    max_snapshot_bytes: int = 64 * 1024 * 1024 * 1024
    archive_workspace: bool = False
    max_event_log_bytes: int = DEFAULT_LOG_BYTES
    evaluation_spec: str | None = None
    supervisor_review: Schedule | None = None

    def __post_init__(self):
        require_version(self.version, CONFIG_VERSION, "configuration")
        text(self.model, "model")
        for field in ROLE_MODELS:
            value = getattr(self, field)
            if value == "inherit":
                object.__setattr__(self, field, None)
                value = None
            if value is not None:
                text(value, field)
                if value != value.strip():
                    raise ValueError(f"{field} must be a nonempty model ID without surrounding whitespace")
        if self.reasoning_effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("Unsupported reasoning_effort")
        credits.parse_limit(self.max_ai_credits)
        for field in TIMEOUT_FIELDS:
            value = getattr(self, field)
            number(value, field)
            if value < 60:
                raise ValueError(f"{field} must allow at least 60 seconds including shutdown")
        Resources(1 if self.cpus == "unlimited" else self.cpus,
                  1 if self.memory_mb == "unlimited" else self.memory_mb, self.gpus)
        boolean(self.coach, "coach")
        if self.supervisor_review is not None and not isinstance(self.supervisor_review, Schedule):
            raise ValueError("supervisor_review must be a validated calendar schedule")
        integer(self.goal_review_interval_seconds, "goal_review_interval_seconds", zero=True)
        integer(self.max_stalled_reviews, "max_stalled_reviews")
        boolean(self.consultant, "consultant")
        if self.closure_critic is None:
            object.__setattr__(self, "closure_critic", self.coach and self.consultant)
        boolean(self.closure_critic, "closure_critic")
        if self.closure_critic and not (self.coach and self.consultant):
            raise ValueError("closure_critic requires both coach and consultant")
        if self.coach and self.max_ai_credits != "unlimited" and self.max_ai_credits < 60:
            raise ValueError("Coaching requires at least 60 credits for a researcher and stopping review")
        if self.consultant and self.max_ai_credits != "unlimited" and self.max_ai_credits < (90 if self.coach else 60):
            raise ValueError("Consultations require at least 60 credits, or 90 with stopping review")
        text(self.ledger, "ledger", empty=True)
        integer(self.max_snapshot_bytes, "max_snapshot_bytes")
        boolean(self.archive_workspace, "archive_workspace")
        integer(self.max_event_log_bytes, "max_event_log_bytes")
        if self.max_event_log_bytes > MAX_LOG_BYTES:
            raise ValueError(f"max_event_log_bytes must be at most {MAX_LOG_BYTES} bytes (64 GiB)")
        if self.evaluation_spec is not None:
            text(self.evaluation_spec, "evaluation_spec")
            if not Path(self.evaluation_spec).is_absolute():
                raise ValueError("evaluation_spec must be an absolute path to a reviewed external kit")

    @property
    def credit_limit(self):
        return credits.parse_limit(self.max_ai_credits)

    @property
    def ledger_path(self):
        return Path(self.ledger).resolve() if self.ledger else machine_ledger_path()

    def to_dict(self):
        values = {**asdict(self), "gpus": list(self.gpus)}
        # TOML has no null; explicit inheritance must survive default-on presets.
        values.update({field: "inherit" for field in ROLE_MODELS if values[field] is None})
        return {key: value for key, value in values.items()
                if value is not None}

    def model_for(self, purpose):
        overrides = {"researcher": None, "inspector": None, "coach": self.coach_model,
                     "closure_critique": self.closure_critic_model, "advice": self.advice_model,
                     "claim_review": self.claim_review_model}
        if purpose not in overrides:
            raise ValueError(f"Unknown model purpose: {purpose}")
        return overrides[purpose] or self.model

    @classmethod
    def parse(cls, value, *, frozen=False):
        value = dict(table(value, "configuration", cls.__dataclass_fields__))
        require_version(value.get("version"), CONFIG_VERSION, "configuration")
        if "closure_critic" in value:
            boolean(value["closure_critic"], "closure_critic")
        if frozen:
            # Missing options in admitted records retain their historical policy.
            value.setdefault("model", "auto")
            value.setdefault("reasoning_effort", "medium")
            for field in ("coach", "consultant", "closure_critic"):
                value.setdefault(field, False)
            for field in ROLE_MODELS:
                value.setdefault(field, None)
            value.setdefault("cpus", 2)
            value.setdefault("memory_mb", 4096)
            value.setdefault("archive_workspace", True)
            value.setdefault("max_event_log_bytes", 1024 * 1024 * 1024)
            value.setdefault("goal_review_interval_seconds", 0)
            value.setdefault("goal_review_timeout_seconds", 15 * 60)
            value.setdefault("consultation_timeout_seconds", 30 * 60)
        for field in TIMEOUT_FIELDS:
            if field in value:
                value[field] = timeout_expression(value[field], field)
        if "supervisor_review" in value:
            value["supervisor_review"] = Schedule.parse(value["supervisor_review"])
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
