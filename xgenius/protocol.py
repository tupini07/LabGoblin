"""Versioned local runtime records shared by controllers and owned workers."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
import uuid


CONFIG_VERSION = 3
DATABASE_VERSION = 3
LEDGER_VERSION = 3
WORKER_VERSION = 3
PACKET_BYTES = 64 * 1024
PACKET_EVENTS = 64
HANDOFF_BYTES = 16 * 1024
ACTIVE = ("queued", "starting", "running", "recovery_required")
TERMINAL = ("completed", "failed", "cancelled", "timed_out", "interrupted", "not_started")
PROVIDER_KINDS = ("research", "final_analysis", "report", "compact", "canary", "observer")


class PreExecutionError(RuntimeError):
    """The owned launch boundary proves that user/provider code did not start."""


class UncertainExecution(RuntimeError):
    """Execution or owned-tree quiescence cannot be established."""


class AdmissionWait(ValueError):
    """A valid request must wait for existing commitments to drain."""


class AdmissionClosed(ValueError):
    """Current control, generation, recovery or budget forbids admission."""


class BudgetExhausted(AdmissionClosed):
    def __init__(self, dimension: str, message: str):
        self.dimension = dimension
        super().__init__(message)


def identifier() -> str:
    return uuid.uuid4().hex


def number(value, name: str, *, zero: bool = False) -> int | float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (value == 0 and not zero)):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'} and finite")
    return value


def integer(value, name: str, *, zero: bool = False) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    number(value, name, zero=zero)
    return value


def text(value, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a {'possibly empty ' if empty else 'nonempty '}string without NUL")
    return value


def strings(value, name: str, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not empty and not value):
        raise ValueError(f"{name} must be {'an' if empty else 'a nonempty'} array of strings")
    return tuple(text(item, name) for item in value)


def argv(value, name: str = "argv") -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name} must be a nonempty argument array")
    return (text(value[0], name), *(text(item, name, empty=True) for item in value[1:]))


def table(value, name: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be an object/table")
    if allowed is not None and set(value) - allowed:
        raise ValueError(f"Unknown {name} fields: {sorted(set(value) - allowed)}")
    return value


def boolean(value, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return value


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def require_version(value, expected: int, kind: str):
    if type(value) is not int or value != expected:
        raise ValueError(f"Unsupported {kind} version {value!r}; expected {expected}. "
                         "Old formats are not migrated; initialize a fresh local campaign.")


@dataclass(frozen=True)
class Limit:
    value: int | float

    def __post_init__(self):
        number(self.value, "limit", zero=True)

    def allows(self, used: int | float, additional: int | float = 0) -> bool:
        number(used, "used", zero=True)
        number(additional, "additional", zero=True)
        return self.value == 0 or used + additional <= self.value

    def exhausted(self, used: int | float) -> bool:
        number(used, "used", zero=True)
        return self.value != 0 and used >= self.value

    def view(self, used: int | float, reserved: int | float = 0) -> dict:
        number(used, "used", zero=True)
        number(reserved, "reserved", zero=True)
        return {"configured": self.value, "unlimited": self.value == 0, "used": used,
                "reserved": reserved,
                "remaining": None if self.value == 0 else max(0, self.value - used - reserved)}


@dataclass(frozen=True)
class Resources:
    cpus: int
    memory_mb: int
    gpus: tuple[str, ...] = ()

    def __post_init__(self):
        integer(self.cpus, "resources.cpus")
        integer(self.memory_mb, "resources.memory_mb")
        strings(self.gpus, "resources.gpus", empty=True)
        if len(set(self.gpus)) != len(self.gpus):
            raise ValueError("resources.gpus must contain unique physical device identities")

    @classmethod
    def parse(cls, value: dict) -> "Resources":
        value = table(value, "resources", {"cpus", "memory_mb", "gpus"})
        return cls(integer(value.get("cpus"), "resources.cpus"),
                   integer(value.get("memory_mb"), "resources.memory_mb"),
                   strings(value.get("gpus", []), "resources.gpus", empty=True))


@dataclass(frozen=True)
class LaunchKey:
    campaign_id: str
    generation: int
    work_id: str
    grant_id: str
    nonce: str

    def __post_init__(self):
        integer(self.generation, "generation")
        for name in ("campaign_id", "work_id", "grant_id", "nonce"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", text(getattr(self, name), name)):
                raise ValueError(f"Invalid {name}")

    @classmethod
    def parse(cls, value: dict) -> "LaunchKey":
        value = table(value, "launch key", set(cls.__dataclass_fields__))
        missing = set(cls.__dataclass_fields__) - set(value)
        if missing:
            raise ValueError(f"Missing launch key fields: {sorted(missing)}")
        return cls(**value)


@dataclass(frozen=True)
class LaunchEnvelope:
    key: LaunchKey
    kind: str
    argv: tuple[str, ...]
    cwd: str
    root: str
    state_path: str
    ledger_path: str
    ledger_id: str
    timeout_seconds: float
    resources: Resources
    config_revision: str
    environment: dict[str, str] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    protocol: int = WORKER_VERSION

    def __post_init__(self):
        require_version(self.protocol, WORKER_VERSION, "worker")
        if self.kind not in ("attempt", "build", *PROVIDER_KINDS):
            raise ValueError(f"Unknown launch kind: {self.kind}")
        argv(self.argv)
        number(self.timeout_seconds, "timeout_seconds")
        for name in ("cwd", "root", "state_path", "ledger_path", "ledger_id", "config_revision"):
            text(getattr(self, name), name)
        for name, value in table(self.environment, "environment").items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                raise ValueError(f"Invalid environment variable name: {name}")
            text(value, f"environment.{name}", empty=True)
        table(self.metadata, "metadata")
        canonical(asdict(self))

    @property
    def digest(self) -> str:
        return fingerprint(asdict(self))

    @classmethod
    def parse(cls, value: dict) -> "LaunchEnvelope":
        value = dict(table(value, "launch envelope", set(cls.__dataclass_fields__)))
        require_version(value.get("protocol"), WORKER_VERSION, "worker")
        required = {"key", "kind", "argv", "cwd", "root", "state_path", "ledger_path",
                    "ledger_id", "timeout_seconds", "resources", "config_revision"}
        if required - set(value):
            raise ValueError(f"Missing launch envelope fields: {sorted(required - set(value))}")
        value["key"] = LaunchKey.parse(value["key"])
        value["resources"] = Resources.parse(value["resources"])
        value["argv"] = argv(value["argv"])
        return cls(**value)


@dataclass(frozen=True)
class LaunchReceipt:
    key: LaunchKey
    envelope_digest: str
    status: str
    quiescent: bool
    elapsed: float
    returncode: int | None = None
    reason: str = ""
    executed: bool = True
    usage: dict | None = None
    metadata: dict = field(default_factory=dict)
    protocol: int = WORKER_VERSION

    def __post_init__(self):
        require_version(self.protocol, WORKER_VERSION, "worker")
        text(self.envelope_digest, "envelope_digest")
        number(self.elapsed, "receipt elapsed", zero=True)
        if self.quiescent is not True or self.status not in TERMINAL:
            raise ValueError("A terminal receipt must establish owned-operation quiescence")
        if self.returncode is not None and type(self.returncode) is not int:
            raise ValueError("Receipt returncode must be an integer or null")
        if self.status == "completed" and self.returncode != 0:
            raise ValueError("A completed receipt requires returncode zero")
        boolean(self.executed, "executed")
        if self.status == "not_started" and self.executed:
            raise ValueError("not_started requires proven non-execution")
        text(self.reason, "receipt reason", empty=self.status == "completed")
        if self.usage is not None:
            table(self.usage, "provider usage")
        table(self.metadata, "receipt metadata")
        canonical(asdict(self))

    @classmethod
    def parse(cls, value: dict) -> "LaunchReceipt":
        value = dict(table(value, "launch receipt", set(cls.__dataclass_fields__)))
        require_version(value.get("protocol"), WORKER_VERSION, "worker")
        required = {"key", "envelope_digest", "status", "quiescent", "elapsed"}
        if required - set(value):
            raise ValueError(f"Missing receipt fields: {sorted(required - set(value))}")
        value["key"] = LaunchKey.parse(value["key"])
        return cls(**value)


@dataclass(frozen=True)
class EvidenceDisposition:
    event_id: str
    disposition: str
    reason: str
    references: tuple[str, ...] = ()
    wake_condition: str = ""

    def __post_init__(self):
        text(self.event_id, "event_id")
        text(self.reason, "evidence.reason")
        if self.disposition not in ("assessed", "excluded", "deferred"):
            raise ValueError("Evidence disposition must be assessed, excluded or deferred")
        strings(self.references, "references", empty=True)
        text(self.wake_condition, "wake_condition", empty=self.disposition != "deferred")

    @classmethod
    def parse(cls, value: dict) -> "EvidenceDisposition":
        value = dict(table(value, "evidence disposition", set(cls.__dataclass_fields__)))
        if {"event_id", "disposition", "reason"} - set(value):
            raise ValueError("Evidence requires event_id, disposition and reason")
        value["references"] = strings(value.get("references", []), "references", empty=True)
        return cls(**value)


@dataclass(frozen=True)
class Handoff:
    turn_id: str
    packet_id: str
    summary: str
    rationale: str
    next_step: str
    disposition: str
    reason: str
    evidence: tuple[EvidenceDisposition, ...] = ()
    stopping_criterion: str = ""
    maintenance: tuple[str, ...] = ()
    assessment: dict | None = None

    def __post_init__(self):
        for name in ("turn_id", "packet_id", "summary", "rationale", "next_step", "reason"):
            text(getattr(self, name), name)
        if self.disposition not in ("continue", "wait", "blocked", "finalize"):
            raise ValueError("Disposition must be continue, wait, blocked or finalize")
        text(self.stopping_criterion, "stopping_criterion", empty=self.disposition != "finalize")
        if len({e.event_id for e in self.evidence}) != len(self.evidence):
            raise ValueError("Evidence event IDs must be unique")
        if set(self.maintenance) - {"report", "compact"}:
            raise ValueError("Only report and compact maintenance may be requested")
        if self.assessment is not None:
            value = table(self.assessment, "closure assessment",
                          {"view_id", "inventory_digest", "assess_all", "exclusions", "limitations"})
            for name in ("view_id", "inventory_digest", "limitations"):
                text(value.get(name), f"assessment.{name}")
            if value.get("assess_all") is not True:
                raise ValueError("Assessment must explicitly cover every listed observation except named exclusions")
            exclusions = value.get("exclusions", [])
            if not isinstance(exclusions, list):
                raise ValueError("Assessment exclusions must be an array")
            seen = set()
            for item in exclusions:
                table(item, "assessment exclusion", {"attempt_id", "reason"})
                text(item.get("attempt_id"), "exclusion.attempt_id")
                text(item.get("reason"), "exclusion.reason")
                if item["attempt_id"] in seen:
                    raise ValueError("Duplicate assessment exclusion")
                seen.add(item["attempt_id"])
        if len(canonical(asdict(self))) > HANDOFF_BYTES:
            raise ValueError(f"Handoff exceeds {HANDOFF_BYTES} encoded bytes")

    @classmethod
    def parse(cls, value: dict) -> "Handoff":
        value = dict(table(value, "handoff", set(cls.__dataclass_fields__)))
        required = {"turn_id", "packet_id", "summary", "rationale", "next_step", "disposition", "reason"}
        if required - set(value):
            raise ValueError(f"Missing handoff fields: {sorted(required - set(value))}")
        evidence = value.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("evidence must be an array")
        value["evidence"] = tuple(EvidenceDisposition.parse(e) for e in evidence)
        value["maintenance"] = strings(value.get("maintenance", []), "maintenance", empty=True)
        return cls(**value)
