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


def timeout_expression(value):
    if not isinstance(value, str):
        return value
    if not value.strip() or len(value) > 256:
        raise ValueError("timeout_seconds expression must contain 1 to 256 characters")
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
            raise ValueError("timeout_seconds allows only numbers, parentheses and +, -, *, /")
        if not math.isfinite(result):
            raise ValueError("timeout_seconds arithmetic must remain finite")
        return result

    try:
        tree = ast.parse(value.strip(), mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 64:
            raise ValueError("timeout_seconds expression exceeds 64 syntax nodes")
        return calculate(tree.body)
    except (SyntaxError, ZeroDivisionError, OverflowError) as error:
        raise ValueError(f"Invalid timeout_seconds arithmetic: {error}") from error


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
    archive_workspace: bool = False
    max_event_log_bytes: int = DEFAULT_LOG_BYTES
    evaluation_spec: str | None = None

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
        boolean(self.archive_workspace, "archive_workspace")
        integer(self.max_event_log_bytes, "max_event_log_bytes")
        if self.max_event_log_bytes > MAX_LOG_BYTES:
            raise ValueError(f"max_event_log_bytes must be at most {MAX_LOG_BYTES} bytes (64 GiB)")
        if self.evaluation_spec is not None:
            text(self.evaluation_spec, "evaluation_spec")
            if not Path(self.evaluation_spec).is_absolute():
                raise ValueError("evaluation_spec must be an absolute path to a reviewed external kit")

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
    def parse(cls, value, *, frozen=False):
        value = dict(table(value, "configuration", cls.__dataclass_fields__))
        require_version(value.get("version"), CONFIG_VERSION, "configuration")
        if frozen:
            # Missing options in admitted records retain their historical policy.
            value.setdefault("archive_workspace", True)
            value.setdefault("max_event_log_bytes", 1024 * 1024 * 1024)
        if "timeout_seconds" in value:
            value["timeout_seconds"] = timeout_expression(value["timeout_seconds"])
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
