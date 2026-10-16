"""RunState: the single serializable state object checkpointed after every stage."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from core.schemas import ContractViolation


class RunStatus:
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class SliceSpec:
    """migration_slice_id = database.schema + the selected objects."""
    database: str = ""
    schema: str = ""
    objects: list[dict] = field(default_factory=list)  # [{"type": "TABLE", "name": "X"}]
    pasted_ddl: str = ""  # offline mode: raw DDL instead of live introspection
    target_catalog: str = ""  # UC catalog override; default = lower(database)

    @property
    def slice_id(self) -> str:
        return f"{self.database}.{self.schema}".upper() if self.database else "PASTED"


@dataclass
class RunState:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    slice: SliceSpec = field(default_factory=SliceSpec)
    status: str = RunStatus.RUNNING
    current_stage: str = ""
    completed: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)  # stage -> final output (JSON-able)
    scratch: dict[str, dict] = field(default_factory=dict)  # stage -> node working memory
    pending: dict | None = None  # interrupt payload while awaiting_human (derived from LangGraph on load)
    hitl: dict[str, bool] = field(default_factory=dict)  # stage -> HITL on/off
    options: dict = field(default_factory=dict)  # run-level knobs (use_llm, parity_live, ...)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def stage_scratch(self, stage: str) -> dict:
        """Read-only view of a stage's working memory (nodes return deltas; never write through this)."""
        return self.scratch.get(stage, {})

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @staticmethod
    def from_json(s: str | dict) -> "RunState":
        d = json.loads(s) if isinstance(s, str) else dict(s)
        d["slice"] = SliceSpec(**d.get("slice", {}))
        known = RunState.__dataclass_fields__.keys()
        return RunState(**{k: v for k, v in d.items() if k in known})


def merge_by_index(size: int, results: Iterable[tuple[int, Any]], fill: Callable[[int], Any] | None = None) -> list:
    """Reducer for fan-out: results arrive in any order, output is ordered by index.

    Duplicate or out-of-range indexes are contract violations (fail loud).
    """
    out: list[Any] = [None] * size
    seen: set[int] = set()
    for idx, val in results:
        if not 0 <= idx < size:
            raise ContractViolation(f"merge_by_index: index {idx} out of range 0..{size - 1}")
        if idx in seen:
            raise ContractViolation(f"merge_by_index: duplicate index {idx}")
        seen.add(idx)
        out[idx] = val
    missing = [i for i in range(size) if i not in seen]
    if missing:
        if fill is None:
            raise ContractViolation(f"merge_by_index: missing indexes {missing}")
        for i in missing:
            out[i] = fill(i)
    return out
