"""Orchestration on LangGraph.

- One StateGraph per backend. Simple stages are one node; reviewable stages are three nodes
  (<stage>__gen -> <stage>__eval -> <stage>__review) so every step is its own checkpointed super-step.
- Nodes receive a COPY of the state and return a STATE DELTA (never mutate graph state).
- Human review = langgraph.types.interrupt(); resume = Command(resume=HumanFeedback dict).
- Checkpointer: SqliteSaver (local file, survives restarts) or InMemorySaver (tests).
- A failed node stays pending in the checkpoint; retry() re-invokes with None and LangGraph re-runs it.
- The run registry (status / error / slice for listing) lives beside the checkpointer.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from core.persistence import ArtifactStore, Checkpoints, Obs
from core.schemas import ContractViolation, HumanFeedback
from core.state import RunState, RunStatus


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


def _union(a: list | None, b: list | None) -> list:
    return list(dict.fromkeys((a or []) + (b or [])))


class GraphState(TypedDict, total=False):
    run_id: str
    slice: dict
    status: str
    current_stage: str
    completed: Annotated[list, _union]
    outputs: Annotated[dict, _merge]  # stage -> final output
    scratch: Annotated[dict, _merge]  # stage -> working memory (replaced per stage key)
    hitl: dict
    options: dict
    error: str
    created_at: float
    updated_at: float


_STATE_KEYS = set(GraphState.__annotations__)


class Aborted(Exception):
    pass


@dataclass
class RunContext:
    db: object | None
    store: ArtifactStore
    obs: Obs
    llm_enabled: bool = False
    run_id: str = ""
    state: RunState | None = None
    stage: str = ""
    progress: Callable[[str], None] = field(default=lambda msg: None)

    def interrupt(self, key: str, payload: dict) -> HumanFeedback:
        """Park the run for human review; returns the reviewer's feedback on resume."""
        return HumanFeedback.from_dict(interrupt({"stage": self.stage, "key": f"{self.stage}:{key}", **payload}))

    def put(self, name: str, content: str, iteration: int = 0) -> None:
        self.store.put(self.run_id, self.stage, iteration, name, content)


def to_graph_state(st: RunState) -> dict:
    d = json.loads(st.to_json())
    return {k: v for k, v in d.items() if k in _STATE_KEYS}


def view(values: dict) -> RunState:
    return RunState.from_json(copy.deepcopy(values))


# ------------------------------------------------------------------ run registry
class MemoryRegistry:
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def upsert(self, run_id: str, **kw) -> None:
        self._d.setdefault(run_id, {"run_id": run_id}).update(kw, updated_at=time.time())

    def get(self, run_id: str) -> dict | None:
        return self._d.get(run_id)

    def list(self, limit: int) -> list[dict]:
        return sorted(self._d.values(), key=lambda r: r["updated_at"], reverse=True)[:limit]


class SqliteRegistry:
    def __init__(self, path: str) -> None:
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._c.execute("CREATE TABLE IF NOT EXISTS run_registry (run_id TEXT PRIMARY KEY, slice_id TEXT, "
                            "status TEXT, stage TEXT, error TEXT, updated_at REAL)")
            self._c.commit()

    def upsert(self, run_id: str, **kw) -> None:
        cur = self.get(run_id) or {"run_id": run_id, "slice_id": "", "status": "", "stage": "", "error": ""}
        cur.update(kw, updated_at=time.time())
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO run_registry VALUES (?,?,?,?,?,?)",
                            (run_id, cur["slice_id"], cur["status"], cur["stage"], cur["error"], cur["updated_at"]))
            self._c.commit()

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            r = self._c.execute("SELECT run_id, slice_id, status, stage, error, updated_at FROM run_registry "
                                "WHERE run_id=?", (run_id,)).fetchone()
        return dict(zip(("run_id", "slice_id", "status", "stage", "error", "updated_at"), r)) if r else None

    def list(self, limit: int) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT run_id, slice_id, status, stage, error, updated_at FROM run_registry "
                                   "ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(zip(("run_id", "slice_id", "status", "stage", "error", "updated_at"), r)) for r in rows]


# ------------------------------------------------------------------ graph construction
SimpleNode = Callable[[RunContext, RunState], object]


class Runner:
    """Builds and drives the LangGraph for one backend. Exposes load()/list_runs() for the UI."""

    def __init__(self, stages: list[tuple[str, object]], ctx_factory: Callable[[RunState, str], RunContext], obs: Obs,
                 state_path: str | None = None, audit: Checkpoints | None = None) -> None:
        names = [n for n, _ in stages]
        if len(set(names)) != len(names):
            raise ContractViolation(f"duplicate stage names: {names}")
        self.stages, self.ctx_factory, self.obs, self.audit = stages, ctx_factory, obs, audit
        self.on_step: Callable[[str, str], None] = lambda stage, node: None  # UI progress hook
        if state_path:
            self.saver = SqliteSaver(sqlite3.connect(state_path, check_same_thread=False))
            self.registry = SqliteRegistry(state_path)
        else:
            self.saver, self.registry = InMemorySaver(), MemoryRegistry()
        self.app = self._build().compile(checkpointer=self.saver)

    # -- nodes
    def _ctx(self, values: dict, stage: str) -> tuple[RunContext, RunState]:
        st = view(values)
        return self.ctx_factory(st, stage), st

    def _harness(self, stage: str, node: str, fn: Callable[[], dict]) -> dict:
        t0 = time.time()
        ctx_obs = self.obs
        rid = self._current_run
        try:
            self.on_step(stage, node)
        except Exception:
            pass  # progress display must never break a run
        try:
            delta = fn()
        except Exception as e:
            if type(e).__name__ in ("GraphInterrupt", "Interrupt"):
                ctx_obs.node({"run_id": rid, "stage": stage, "node": node, "status": "awaiting_human",
                              "duration_ms": int((time.time() - t0) * 1000)})
            else:
                ctx_obs.node({"run_id": rid, "stage": stage, "node": node, "status": "failed",
                              "duration_ms": int((time.time() - t0) * 1000), "detail": {"error": str(e)[:500]}})
            raise
        ctx_obs.node({"run_id": rid, "stage": stage, "node": node, "status": "done",
                      "duration_ms": int((time.time() - t0) * 1000)})
        return {**delta, "current_stage": stage, "updated_at": time.time()}

    def _simple(self, stage: str, fn: SimpleNode):
        def node(values: dict) -> dict:
            def run():
                ctx, st = self._ctx(values, stage)
                return {"outputs": {stage: fn(ctx, st)}, "completed": [stage]}
            return self._harness(stage, stage, run)
        return node

    def _build(self) -> StateGraph:
        g = StateGraph(GraphState)
        entries, exits = [], []  # (first node, last node, router-needed?)
        for stage, spec in self.stages:
            if callable(spec):
                g.add_node(stage, self._simple(stage, spec))
                entries.append(stage)
                exits.append((stage, None))
            else:  # ReviewableStage from core.nodes
                gen_n, eval_n, rev_n = f"{stage}__gen", f"{stage}__eval", f"{stage}__review"
                g.add_node(gen_n, self._step(stage, gen_n, spec.gen_step))
                g.add_node(eval_n, self._step(stage, eval_n, spec.eval_step))
                g.add_node(rev_n, self._step(stage, rev_n, spec.review_step))
                g.add_edge(gen_n, eval_n)
                g.add_edge(eval_n, rev_n)
                entries.append(gen_n)
                exits.append((rev_n, gen_n))
        g.add_edge(START, entries[0])
        for i, (last, regen) in enumerate(exits):
            nxt = entries[i + 1] if i + 1 < len(entries) else END
            stage = self.stages[i][0]
            if regen is None:
                g.add_conditional_edges(last, _router(stage, nxt, None), [nxt, END] if nxt != END else [END])
            else:
                g.add_conditional_edges(last, _router(stage, nxt, regen), list(dict.fromkeys([nxt, regen, END])))
        return g

    def _step(self, stage: str, node: str, fn):
        def step(values: dict) -> dict:
            def run():
                ctx, st = self._ctx(values, stage)
                return fn(ctx, st)
            return self._harness(stage, node, run)
        return step

    # -- driving
    _current_run = ""

    def _cfg(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 200}

    def _invoke(self, run_id: str, inp: Any) -> RunState:
        self._current_run = run_id
        try:
            self.app.invoke(inp, self._cfg(run_id))
            self.registry.upsert(run_id, error="")
        except Exception as e:
            self.registry.upsert(run_id, error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}")
        st = self.load(run_id)
        self.registry.upsert(run_id, slice_id=st.slice.slice_id, status=st.status, stage=st.current_stage)
        if self.audit is not None:
            try:
                self.audit.save(st)
            except Exception:
                pass  # audit mirror must never break a run
        return st

    def start(self, st: RunState) -> RunState:
        if self.app.get_state(self._cfg(st.run_id)).values:
            return self.retry(st.run_id)
        self.registry.upsert(st.run_id, slice_id=st.slice.slice_id, status=RunStatus.RUNNING, stage="")
        return self._invoke(st.run_id, to_graph_state(st))

    def resume(self, run_id: str, feedback: HumanFeedback) -> RunState:
        st = self.load(run_id)
        if st is None or st.status != RunStatus.AWAITING_HUMAN:
            raise ContractViolation(f"run {run_id} is not awaiting human input (status={st and st.status})")
        return self._invoke(run_id, Command(resume=feedback.to_dict()))

    def retry(self, run_id: str) -> RunState:
        return self._invoke(run_id, None)

    # -- read side (Checkpoints-compatible)
    def load(self, run_id: str) -> RunState | None:
        snap = self.app.get_state(self._cfg(run_id))
        if not snap.values:
            return None
        st = view(snap.values)
        reg = self.registry.get(run_id) or {}
        if snap.interrupts:
            val = snap.interrupts[0].value
            st.status, st.pending, st.current_stage = RunStatus.AWAITING_HUMAN, dict(val), val.get("stage", st.current_stage)
        elif st.status == RunStatus.ABORTED:
            st.pending = None
        elif snap.next:
            st.current_stage = snap.next[0].split("__")[0]
            st.status = RunStatus.FAILED if reg.get("error") else RunStatus.RUNNING
            st.error = reg.get("error", "")
        else:
            st.status, st.current_stage, st.pending = RunStatus.DONE, "", None
        return st

    def list_runs(self, limit: int = 50) -> list[dict]:
        return self.registry.list(limit)

    def save(self, state: RunState) -> None:  # Checkpoints protocol: LangGraph owns persistence
        self.registry.upsert(state.run_id, slice_id=state.slice.slice_id, status=state.status, stage=state.current_stage)


def _router(stage: str, nxt: str, regen: str | None):
    def route(values: dict) -> str:
        if values.get("status") == RunStatus.ABORTED:
            return END
        if stage in (values.get("completed") or []):
            return nxt
        if regen is None:
            raise ContractViolation(f"stage {stage} finished without output")
        return regen
    return route

