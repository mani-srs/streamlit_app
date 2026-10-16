"""AGENT_ORDER, HITL defaults, phase labels, and the runtime facade used by the UI and scripts."""
from __future__ import annotations

import os

from agents.cd_codegen import agent as cd_codegen
from agents.classifier import agent as classifier
from agents.connection import agent as connection
from agents.discovery import agent as discovery
from agents.md_plan import agent as md_plan
from agents.standardization import agent as standardization
from agents.validation import agent as validation
from core.db import Db
from core.graph import RunContext, Runner
from core.persistence import MemoryObs, MemoryStore, SqlCheckpoints, SqliteObs, SqlObs, SqlStore
from core.schemas import HumanFeedback
from core.state import RunState, SliceSpec

AGENT_ORDER = ["connection", "standardization", "discovery", "md_plan", "cd_codegen", "validation", "classifier"]

HITL_DEFAULTS = {"connection": False, "standardization": False, "discovery": True, "md_plan": True,
                 "cd_codegen": True, "validation": False, "classifier": True}

PHASE_LABELS = {
    "connection": "1. Connect & extract DDL",
    "standardization": "2. Normalize object model",
    "discovery": "3. Discover lineage",
    "md_plan": "4. Migration plan",
    "cd_codegen": "5. Generate Databricks code",
    "validation": "6. Validate",
    "classifier": "7. Medallion layers & bundle",
}

# simple callables run as one LangGraph node; ReviewableStage expands to gen -> eval -> review nodes
_NODES = {"connection": connection.node, "standardization": standardization.node, "discovery": discovery.node,
          "md_plan": md_plan.node, "cd_codegen": cd_codegen.node, "validation": validation.node,
          "classifier": classifier.node}

DEFAULT_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".state", "runs.sqlite")


class Backend:
    """Connection + persistence.

    state_path: LangGraph SqliteSaver file (runs survive restarts); None -> in-memory (tests).
    app_schema: optional Snowflake schema from setup.sql for artifacts, observability and a RunState audit mirror.
    """

    def __init__(self, db: Db | None, app_schema: str | None = None, state_path: str | None = None) -> None:
        self.db, self.app_schema = db, app_schema
        if state_path:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
        if app_schema and db is not None:
            self.store, audit = SqlStore(db, app_schema), SqlCheckpoints(db, app_schema)
            mirror = SqlObs(db, app_schema)
        else:
            self.store, audit, mirror = MemoryStore(), None, None
        # the local state file is the authoritative, restart-proof log; Snowflake tables get a copy
        self.obs = SqliteObs(state_path, mirror=mirror) if state_path else (mirror or MemoryObs())
        self.state_path = state_path
        self._progress = lambda m: None
        self.runner = Runner([(n, _NODES[n]) for n in AGENT_ORDER], self._context, self.obs,
                             state_path=state_path, audit=audit)
        self.checkpoints = self.runner  # load() / list_runs() for the UI

    def _context(self, state: RunState, stage: str) -> RunContext:
        return RunContext(db=self.db, store=self.store, obs=self.obs,
                          llm_enabled=bool(state.options.get("use_llm")) and self.db is not None,
                          run_id=state.run_id, state=state, stage=stage, progress=self._progress)

    def with_progress(self, progress, on_step=None) -> "Backend":
        self._progress = progress or (lambda m: None)
        self.runner.on_step = on_step or (lambda stage, node: None)
        return self


# Plain-language description of every LangGraph node, for progress display
STEP_TEXT = {
    "connection": "Reading object definitions from Snowflake (SHOW / GET_DDL)",
    "standardization__gen": "Parsing the DDL into a structured object model",
    "standardization__eval": "Validating the object model",
    "discovery__gen": "Building the lineage graph and detecting stream + task CDC patterns",
    "discovery__eval": "Checking the lineage for cycles and missing objects",
    "md_plan__gen": "Drafting the migration plan and risk register",
    "md_plan__eval": "Reviewing the migration plan",
    "cd_codegen__gen": "Converting objects to Databricks code (rules first, Cortex for AI slots)",
    "cd_codegen__eval": "Running quality checks on the generated code",
    "validation__gen": "Running final checks and building the parity SQL",
    "validation__eval": "Summarizing validation findings",
    "classifier__gen": "Assigning bronze / silver / gold layers and assembling the bundle",
    "classifier__eval": "Checking the final bundle",
}


def node_order() -> list[str]:
    """LangGraph nodes in execution order (used to compute progress)."""
    out = []
    for name in AGENT_ORDER:
        out += [name] if callable(_NODES[name]) else [f"{name}__gen", f"{name}__eval", f"{name}__review"]
    return out


def step_text(node: str, hitl: bool = False) -> str:
    if node.endswith("__review"):
        return "Waiting for your review" if hitl else "Finalizing the step"
    return STEP_TEXT.get(node, node)


def new_run(slice_spec: SliceSpec, hitl: dict | None = None, **options) -> RunState:
    st = RunState(slice=slice_spec)
    st.hitl = {**HITL_DEFAULTS, **(hitl or {})}
    st.options = {"use_llm": True, "parity_live": False, **options}
    return st


def run(backend: Backend, state: RunState, progress=None, on_step=None) -> RunState:
    return backend.with_progress(progress, on_step).runner.start(state)


def resume(backend: Backend, state: RunState, feedback: HumanFeedback, progress=None, on_step=None) -> RunState:
    return backend.with_progress(progress, on_step).runner.resume(state.run_id, feedback)


def retry(backend: Backend, state: RunState, progress=None, on_step=None) -> RunState:
    return backend.with_progress(progress, on_step).runner.retry(state.run_id)


def mermaid() -> str:
    """The compiled LangGraph as a Mermaid diagram (for docs / the UI)."""
    return Backend(db=None).runner.app.get_graph().draw_mermaid()
