"""Small typed execution records. There is no schema for scientific reasoning."""

from dataclasses import asdict, dataclass, field
import math
import re
import uuid

from labgoblin.storage import canonical, digest


CONFIG_VERSION = 5
LEDGER_VERSION = 3
WORKER_VERSION = 4
TERMINAL = {"completed", "checkpointed", "needs_operator", "review_unavailable", "budget_exhausted",
            "failed", "cancelled", "timed_out", "interrupted", "not_started"}


class PreExecutionError(RuntimeError):
    """Execution is proven not to have started."""


class UncertainExecution(RuntimeError):
    """Execution, spending or owned-tree quiescence is unresolved."""


class BudgetExhausted(ValueError):
    pass


def identifier():
    return uuid.uuid4().hex


def number(value, name, *, zero=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (value == 0 and not zero)):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'} and finite")
    return value


def integer(value, name, *, zero=False):
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return number(value, name, zero=zero)


def text(value, name, *, empty=False):
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a {'possibly empty' if empty else 'nonempty'} string without NUL")
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return value


def table(value, name, allowed=None):
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    if allowed is not None and set(value) - set(allowed):
        raise ValueError(f"Unknown {name} fields: {sorted(set(value) - set(allowed))}")
    return value


def argv(value):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("argv must be a nonempty string array")
    return tuple(text(item, "argv", empty=index > 0) for index, item in enumerate(value))


def require_version(value, expected, kind):
    if type(value) is not int or value != expected:
        raise ValueError(f"Unsupported {kind} version {value!r}; expected {expected}. "
                         "Use the preserved legacy runtime for old campaigns; no migration is performed.")


def fingerprint(value):
    return digest(value)


@dataclass(frozen=True)
class Resources:
    cpus: int
    memory_mb: int
    gpus: tuple[str, ...] = ()

    def __post_init__(self):
        integer(self.cpus, "cpus")
        integer(self.memory_mb, "memory_mb")
        if not isinstance(self.gpus, tuple) or len(set(self.gpus)) != len(self.gpus):
            raise ValueError("gpus must contain unique device identities")
        for gpu in self.gpus:
            text(gpu, "gpu")

    @classmethod
    def parse(cls, value):
        value = table(value, "resources", cls.__dataclass_fields__)
        gpus = value.get("gpus", [])
        if not isinstance(gpus, (list, tuple)):
            raise ValueError("gpus must be an array")
        return cls(value.get("cpus"), value.get("memory_mb"), tuple(gpus))


@dataclass(frozen=True)
class LaunchEnvelope:
    id: str
    project_id: str
    project: str
    root: str
    ledger_path: str
    ledger_id: str
    argv: tuple[str, ...]
    resources: Resources
    cpu_ids: tuple[int, ...]
    config: dict
    allowance_nano: int | None
    launcher: dict
    runtime: dict
    prompt_sha256: str
    protocol: int = WORKER_VERSION

    def __post_init__(self):
        require_version(self.protocol, WORKER_VERSION, "execution")
        for name in ("id", "project_id"):
            if not re.fullmatch(r"[a-f0-9]{32}", text(getattr(self, name), name)):
                raise ValueError(f"Invalid {name}")
        for name in ("project", "root", "ledger_path", "ledger_id", "prompt_sha256"):
            text(getattr(self, name), name)
        argv(self.argv)
        if (len(self.cpu_ids) != self.resources.cpus or len(set(self.cpu_ids)) != len(self.cpu_ids)
                or any(type(cpu) is not int or cpu < 0 for cpu in self.cpu_ids)):
            raise ValueError("CPU placement must name the exact granted CPU set")
        if self.allowance_nano is not None:
            integer(self.allowance_nano, "credit allowance")
        for name in ("config", "launcher", "runtime"):
            table(getattr(self, name), name)
        canonical(asdict(self))

    @property
    def digest(self):
        return digest(asdict(self))

    @classmethod
    def parse(cls, value):
        value = dict(table(value, "envelope", cls.__dataclass_fields__))
        require_version(value.get("protocol"), WORKER_VERSION, "execution")
        if set(cls.__dataclass_fields__) - set(value):
            raise ValueError("Execution envelope is incomplete")
        value["argv"] = argv(value["argv"])
        value["resources"] = Resources.parse(value["resources"])
        if not isinstance(value["cpu_ids"], list):
            raise ValueError("CPU placement must be an array")
        value["cpu_ids"] = tuple(value["cpu_ids"])
        return cls(**value)


@dataclass(frozen=True)
class LaunchReceipt:
    id: str
    envelope_digest: str
    status: str
    elapsed: float
    executed: bool
    returncode: int | None = None
    reason: str = ""
    usage: dict | None = None
    metadata: dict = field(default_factory=dict)
    quiescent: bool = True
    protocol: int = WORKER_VERSION

    def __post_init__(self):
        require_version(self.protocol, WORKER_VERSION, "receipt")
        text(self.id, "receipt id")
        text(self.envelope_digest, "envelope digest")
        number(self.elapsed, "elapsed", zero=True)
        boolean(self.executed, "executed")
        if self.quiescent is not True or self.status not in TERMINAL:
            raise ValueError("A terminal receipt requires proven owned-tree quiescence")
        if self.status == "not_started" and self.executed:
            raise ValueError("not_started cannot describe an executed payload")
        if self.returncode is not None and type(self.returncode) is not int:
            raise ValueError("returncode must be an integer or null")
        if self.status == "completed" and self.returncode != 0:
            raise ValueError("Completed execution requires returncode zero")
        text(self.reason, "receipt reason", empty=self.status == "completed")
        if self.usage is not None:
            table(self.usage, "usage")
        table(self.metadata, "receipt metadata")
        canonical(asdict(self))

    @classmethod
    def parse(cls, value):
        value = table(value, "receipt", cls.__dataclass_fields__)
        require_version(value.get("protocol"), WORKER_VERSION, "receipt")
        if set(cls.__dataclass_fields__) - set(value):
            raise ValueError("Execution receipt is incomplete")
        return cls(**value)
