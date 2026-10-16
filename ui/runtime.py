"""Shared UI state: Snowflake connection, persistence backend, and run helpers."""
from __future__ import annotations

import json
import os
import re
import threading

import streamlit as st

from agents import master
from core.concurrency import map_ordered
from core.db import connect, qident, resolve_connection_name
from core.state import RunState, RunStatus

STATUS_BADGE = {
    RunStatus.RUNNING: ("Running", "blue", ":material/progress_activity:"),
    RunStatus.AWAITING_HUMAN: ("Awaiting review", "orange", ":material/rate_review:"),
    RunStatus.DONE: ("Done", "green", ":material/check_circle:"),
    RunStatus.FAILED: ("Failed", "red", ":material/error:"),
    RunStatus.ABORTED: ("Aborted", "gray", ":material/cancel:"),
}


def init_state() -> None:
    ss = st.session_state
    ss.setdefault("run_id", None)
    ss.setdefault("app_schema", os.environ.get("MIGRATION_APP_SCHEMA", ""))
    ss.setdefault("memory_backend", None)
    ss.setdefault("db_error", None)


def connection_name() -> str | None:
    """The connection the UI uses: sidebar choice, else env var / config default / the only one defined."""
    return st.session_state.get("conn_name") or resolve_connection_name()


def get_db():
    """Snowflake connection for the chosen named connection (connections.toml).

    Cached per browser session; None (with db_error set) when no connection is available.
    """
    ss = st.session_state
    if "db" in ss and ss.db is not None:
        return ss.db
    try:
        ss.db, ss.db_error = connect(connection_name()), None
    except Exception as e:
        ss.db, ss.db_error = None, f"{type(e).__name__}: {e}"
    return ss.db


def reconnect() -> None:
    st.session_state.pop("db", None)
    st.session_state.pop("memory_backend", None)
    st.session_state.pop("session_info", None)


# ------------------------------------------------------------------ role / warehouse
def session_info(db) -> dict:
    """Current role, warehouse, secondary roles and roles the user may switch to (cached until changed)."""
    ss = st.session_state
    if ss.get("session_info") is None:
        r = db.query("SELECT CURRENT_ROLE() AS role, CURRENT_WAREHOUSE() AS wh, CURRENT_SECONDARY_ROLES() AS sec, "
                     "CURRENT_AVAILABLE_ROLES() AS avail")[0]
        sec = json.loads(r["sec"]) if r.get("sec") else {}
        ss.session_info = {"role": r["role"], "warehouse": r["wh"],
                           "secondary_all": str(sec.get("value", "")).upper() == "ALL",
                           "roles": sorted(json.loads(r["avail"] or "[]"))}
        try:
            ss.session_info["warehouses"] = sorted(w["name"] for w in db.query("SHOW WAREHOUSES"))
        except Exception:
            ss.session_info["warehouses"] = [r["wh"]] if r["wh"] else []
    return ss.session_info


def context_key(db) -> str:
    """Cache key for anything whose result depends on the current role (object lists, databases)."""
    i = session_info(db)
    return f"{id(db)}::{i['role']}::{i['secondary_all']}"


def apply_session(db, role: str | None = None, warehouse: str | None = None, secondary_all: bool | None = None) -> None:
    """USE ROLE / USE WAREHOUSE / USE SECONDARY ROLES on this browser session's connection."""
    ss = st.session_state
    try:
        if role:
            db.query(f"USE ROLE {qident(role)}")
        if secondary_all is not None:
            db.query("USE SECONDARY ROLES ALL" if secondary_all else "USE SECONDARY ROLES NONE")
        if warehouse:
            db.query(f"USE WAREHOUSE {qident(warehouse)}")
        ss.session_error = None
    except Exception as e:
        ss.session_error = str(e).splitlines()[0][:300]
        for k in ("ui_role", "ui_secondary", "ui_wh"):  # show what is really active, not the failed choice
            ss.pop(k, None)
    ss.session_info = None  # re-read what Snowflake actually applied
    for k in [k for k in ss if str(k).startswith(("sel::", "ver::", "flt::", "objs::"))]:
        del ss[k]  # object selections belong to the previous role


def usable_databases(db, names: list[str]) -> list[str]:
    """Databases the current role (+ secondary roles) can actually USE: probes each one in parallel."""
    def probe(name):
        try:
            db.query(f"SHOW SCHEMAS IN DATABASE {qident(name)} LIMIT 1")
            return name
        except Exception:
            return None
    return [n for n in map_ordered("probe", probe, names) if n]


_FQ = re.compile(r'^[A-Za-z_][\w$]*\.[A-Za-z_][\w$]*$')


def backend() -> master.Backend:
    """LangGraph SQLite checkpoints under .state/ (runs survive restarts). With an app schema from setup.sql,
    artifacts, observability and a RunState audit mirror also go to Snowflake tables."""
    ss = st.session_state
    db = get_db()
    schema = (ss.app_schema or "").strip().upper()
    if db is not None and schema and _FQ.match(schema):
        key = f"sql_backend::{schema}"
        if key not in ss:
            try:
                db.query(f"SELECT 1 FROM {schema}.RUN_CHECKPOINTS LIMIT 1")
                ss[key] = master.Backend(db=db, app_schema=schema, state_path=master.DEFAULT_STATE_PATH)
            except Exception as e:
                ss[key] = None
                ss["app_schema_error"] = str(e).splitlines()[0]
        if ss[key] is not None:
            return ss[key]
    if ss.memory_backend is None:
        ss.memory_backend = master.Backend(db=db, state_path=ss.get("state_path", master.DEFAULT_STATE_PATH))
    return ss.memory_backend


def persistence_label(b: master.Backend) -> str:
    local = "LangGraph checkpoints in `.state/runs.sqlite`"
    return f"{local} + Snowflake tables in `{b.app_schema}`" if b.app_schema else local


def current_run(b: master.Backend) -> RunState | None:
    rid = st.session_state.run_id
    return b.checkpoints.load(rid) if rid else None


def status_badge(state: RunState) -> None:
    label, color, icon = STATUS_BADGE.get(state.status, (state.status, "gray", None))
    st.badge(label, color=color, icon=icon)


def drive(b: master.Backend, state: RunState, feedback=None) -> RunState:
    """Run or resume the graph with a progress bar, the current step in plain words, and a live activity log."""
    from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

    order = master.node_order()
    total = len(order)
    script_ctx = get_script_run_ctx()
    lock = threading.Lock()
    hitl = dict(state.hitl)

    bar = st.progress(0.0, text="Starting...")
    step_line = st.empty()
    detail_line = st.empty()
    box = st.status("Activity log", expanded=False)

    def ui(fn):  # progress calls may come from LangGraph / pool worker threads
        with lock:
            try:
                add_script_run_ctx(threading.current_thread(), script_ctx)
                fn()
            except Exception:
                pass  # display problems must never break a run

    def on_step(stage: str, node: str) -> None:
        i = order.index(node) if node in order else 0
        n_stage = master.AGENT_ORDER.index(stage) + 1 if stage in master.AGENT_ORDER else 0
        phase = master.PHASE_LABELS.get(stage, stage).split(". ", 1)[-1]
        text = master.step_text(node, hitl.get(stage, False))

        def show():
            bar.progress(min(i / total, 0.99), text=f"Step {n_stage} of {len(master.AGENT_ORDER)}: {phase}")
            step_line.markdown(f":material/progress_activity: **{text}**")
            detail_line.empty()
            box.write(f"**{phase}**: {text}")
        ui(show)

    def on_msg(msg: str) -> None:
        def show():
            detail_line.caption(msg)
            box.write(f"&nbsp;&nbsp;{msg}")
        ui(show)

    if feedback is not None:
        state = master.resume(b, state, feedback, on_msg, on_step)
    else:
        state = master.run(b, state, on_msg, on_step)

    stage_label = master.PHASE_LABELS.get(state.current_stage, state.current_stage or "")
    if state.status == RunStatus.DONE:
        bar.progress(1.0, text="All 7 steps complete")
        step_line.success("Conversion finished. Open **Outputs** to review and download the bundle.", icon=":material/check_circle:")
    elif state.status == RunStatus.AWAITING_HUMAN:
        done = order.index(f"{state.current_stage}__review") if f"{state.current_stage}__review" in order else 0
        bar.progress(done / total, text=f"Paused at {stage_label}")
        step_line.info(f"Waiting for your review at **{stage_label}**.", icon=":material/rate_review:")
    elif state.status == RunStatus.FAILED:
        step_line.error(f"Failed at **{stage_label}**. See Run monitor for details.", icon=":material/error:")
    elif state.status == RunStatus.ABORTED:
        step_line.warning("Run aborted.", icon=":material/cancel:")
    detail_line.empty()
    box.update(label="Activity log", state="error" if state.status == RunStatus.FAILED else "complete")
    st.session_state.run_id = state.run_id
    return state


def lang_for(name: str) -> str:
    return {".py": "python", ".sql": "sql", ".json": "json", ".md": "markdown"}.get(os.path.splitext(name)[1], "text")
