"""Shared value objects. No I/O on data."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    PASS = "pass"
    REVISE = "revise"
    FAIL = "fail"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class Origin(str, Enum):
    MAPPED = "MAPPED"  # fully deterministic
    HYBRID = "HYBRID"  # deterministic scaffold + AI slots
    AI = "AI"  # whole operation to the model


class ContractViolation(RuntimeError):
    """Fail loud on contract violations. Never swallow."""


def _check_ref(ref: str) -> None:
    # refs MUST be node/table/line addressable: node:<id>, table:<fqn>, line:<file>:<n>, file:<name>
    if not ref or ref.split(":", 1)[0] not in {"node", "table", "line", "file", "object"}:
        raise ContractViolation(f"Issue.ref must be addressable (node:/table:/line:/file:/object:), got {ref!r}")


@dataclass(frozen=True)
class Issue:
    id: str
    severity: Severity
    ref: str
    message: str
    suggested_fix: str = ""

    def __post_init__(self) -> None:
        _check_ref(self.ref)
        object.__setattr__(self, "severity", Severity(self.severity))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "Issue":
        return Issue(id=str(d["id"]), severity=Severity(d.get("severity", "warn")), ref=d["ref"],
                     message=d.get("message", ""), suggested_fix=d.get("suggested_fix", ""))


@dataclass(frozen=True)
class EvalResult:
    verdict: Verdict
    confidence: float
    issues: tuple[Issue, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", Verdict(self.verdict))
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ContractViolation(f"confidence must be in [0,1], got {self.confidence}")
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "confidence": float(self.confidence),
                "issues": [i.to_dict() for i in self.issues], "summary": self.summary}

    @staticmethod
    def from_dict(d: dict) -> "EvalResult":
        return EvalResult(verdict=Verdict(d["verdict"]), confidence=float(d.get("confidence", 0.5)),
                          issues=tuple(Issue.from_dict(i) for i in d.get("issues", [])),
                          summary=d.get("summary", ""))


@dataclass(frozen=True)
class HumanFeedback:
    accepted: bool = False  # True = approve artifact and advance
    accepted_issue_ids: tuple[str, ...] = ()
    rejected_issue_ids: tuple[str, ...] = ()
    freeform: str = ""
    hard_constraints: tuple[str, ...] = ()
    abort: bool = False  # human rejects the run outright
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("accepted_issue_ids", "rejected_issue_ids", "hard_constraints"):
            d[k] = list(d[k])
        return d

    @staticmethod
    def from_dict(d: dict) -> "HumanFeedback":
        return HumanFeedback(
            accepted=bool(d.get("accepted", False)),
            accepted_issue_ids=tuple(d.get("accepted_issue_ids", ())),
            rejected_issue_ids=tuple(d.get("rejected_issue_ids", ())),
            freeform=d.get("freeform", ""), hard_constraints=tuple(d.get("hard_constraints", ())),
            abort=bool(d.get("abort", False)), ts=float(d.get("ts", time.time())))


@dataclass(frozen=True)
class GenResult:
    artifact: Any  # str (markdown) or dict[filename, text]
    node_keys: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"artifact": self.artifact, "node_keys": list(self.node_keys), "meta": self.meta}


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    retries: int = 0
    parsed: Any = None


def dumps(o: Any) -> str:
    return json.dumps(o, default=lambda x: x.value if isinstance(x, Enum) else str(x))
