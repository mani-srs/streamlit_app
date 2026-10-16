import os
import re

import pandas as pd
import streamlit as st

from agents import master
from agents.connection.agent import list_objects
from core.state import SliceSpec
from ui import runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
b = runtime.backend()

st.title(":material/add_circle: New migration run")
st.caption("Convert a Snowflake schema slice (tables, views, dynamic tables, streams, tasks, procedures) "
           "into Databricks: UC DDL, DLT pipelines, notebooks and Jobs.")


# ctx_key (connection + role + secondary roles) must be a hashed argument: no leading underscore
@st.cache_data(ttl=600, show_spinner=False)
def _databases(ctx_key: str, usable_only: bool) -> list[str]:
    names = sorted(r["name"] for r in b.db.query("SHOW DATABASES"))
    return runtime.usable_databases(b.db, names) if usable_only else names


@st.cache_data(ttl=600, show_spinner=False)
def _schemas(ctx_key: str, database: str) -> list[str]:
    from core.db import qident
    return sorted(r["name"] for r in b.db.query(f"SHOW SCHEMAS IN DATABASE {qident(database)}")
                  if r["name"] != "INFORMATION_SCHEMA")


@st.cache_data(ttl=300, show_spinner="Listing objects...")
def _objects(ctx_key: str, database: str, schema: str) -> list[dict]:
    return list_objects(b.db, database, schema)


def _access_error(what: str, e: Exception) -> None:
    msg = str(e).splitlines()
    detail = next((l for l in msg if "privileges" in l.lower() or "not authorized" in l.lower()), msg[-1] if msg else str(e))
    role = runtime.session_info(b.db)["role"] if b.db is not None else "?"
    st.error(f"Cannot {what} with role **{role}**: {detail.strip()}", icon=":material/lock:")
    st.caption("Switch **Role** in the sidebar, turn on **Also use all my secondary roles**, or ask for USAGE on it.")


modes = ["Live Snowflake schema", "Paste DDL", "Sample slice"] if b.db is not None else ["Paste DDL", "Sample slice"]
mode = st.segmented_control("Source", modes, default=modes[0], key="source_mode")

slice_spec = None
with st.container(border=True):
    if mode == "Live Snowflake schema":
        cache_key = runtime.context_key(b.db)
        usable_only = st.toggle("Only show databases my role can use", value=True, key="usable_only",
                                help="Checks each database with SHOW SCHEMAS (cached 10 min). Turn off to see every "
                                     "database SHOW DATABASES returns, including ones you can see but not use.")
        c1, c2 = st.columns(2)
        try:
            with st.spinner("Checking which databases your role can use...") if usable_only else st.empty():
                dbs = _databases(cache_key, usable_only)
        except Exception as e:
            _access_error("list databases", e)
            st.stop()
        role = runtime.session_info(b.db)["role"]
        database = c1.selectbox(f"Database ({len(dbs)} available to {role})", dbs, index=None,
                                placeholder="Choose a database", key=f"db::{cache_key}")
        schemas: list[str] = []
        if database:
            try:
                schemas = _schemas(cache_key, database)
            except Exception as e:
                _access_error(f"open database {database}", e)
        schema = c2.selectbox("Schema", schemas, index=None, placeholder="Choose a schema",
                              disabled=not schemas, key=f"sch::{cache_key}::{database}")
        if database and schema:
            try:
                objs = _objects(cache_key, database, schema)
            except Exception as e:
                _access_error(f"list objects in {database}.{schema}", e)
                objs = []
            if objs:
                sel_key, ver_key = f"sel::{database}.{schema}", f"ver::{database}.{schema}"
                if sel_key not in st.session_state:  # small lists start all-selected, large ones start empty
                    st.session_state[sel_key] = {f"{o['type']}|{o['name']}" for o in objs} if len(objs) <= 50 else set()
                    st.session_state[ver_key] = 0
                selected: set = st.session_state[sel_key]

                c1, c2 = st.columns([2, 3])
                types = c1.pills("Object types", sorted({o["type"] for o in objs}), selection_mode="multi",
                                 default=sorted({o["type"] for o in objs}))
                needle = c2.text_input("Filter by name", placeholder="e.g. ORDERS or ^DIM_", key=f"flt::{database}.{schema}",
                                       help="Case-insensitive; regular expressions allowed")
                try:
                    rx = re.compile(needle, re.I) if needle else None
                except re.error:
                    rx = re.compile(re.escape(needle), re.I)
                visible = [o for o in objs if o["type"] in (types or []) and (rx is None or rx.search(o["name"]))]
                vis_keys = [f"{o['type']}|{o['name']}" for o in visible]

                def _bulk(on: bool, keys=tuple(vis_keys), sk=sel_key, vk=ver_key):
                    s = st.session_state[sk]
                    s.update(keys) if on else s.difference_update(keys)
                    st.session_state[vk] += 1

                def _sync(ek, keys=tuple(vis_keys), sk=sel_key, vk=ver_key):
                    for idx, change in st.session_state[ek].get("edited_rows", {}).items():
                        if "migrate" in change:
                            k = keys[int(idx)]
                            st.session_state[sk].add(k) if change["migrate"] else st.session_state[sk].discard(k)
                    st.session_state[vk] += 1

                with st.container(horizontal=True, vertical_alignment="center"):
                    st.button(f"Select all ({len(visible)})", icon=":material/select_all:", on_click=_bulk, args=(True,),
                              disabled=not visible, key=f"all::{database}.{schema}")
                    st.button("Clear all", icon=":material/deselect:", on_click=_bulk, args=(False,),
                              disabled=not visible, key=f"none::{database}.{schema}")
                    st.caption(f"**{len(selected)}** of {len(objs)} objects selected"
                               + (f" · {len(visible)} shown" if len(visible) != len(objs) else ""))

                df = pd.DataFrame(visible, columns=["type", "name", "rows", "comment"]) if visible else \
                    pd.DataFrame(columns=["type", "name", "rows", "comment"])
                df.insert(0, "migrate", [k in selected for k in vis_keys])
                ek = f"objs::{database}.{schema}::{st.session_state[ver_key]}"
                st.data_editor(df, hide_index=True, disabled=["type", "name", "rows", "comment"], key=ek,
                               on_change=_sync, args=(ek,), height=min(38 + 35 * max(len(df), 1), 420),
                               column_config={"migrate": st.column_config.CheckboxColumn("Migrate", width="small")})
                chosen = [o for o in objs if f"{o['type']}|{o['name']}" in selected]
                if len(chosen) > 100:
                    st.warning(f"{len(chosen)} objects selected. Large runs take longer and make review gates long; "
                               "consider migrating in smaller slices.", icon=":material/warning:")
                if chosen:
                    slice_spec = SliceSpec(database=database, schema=schema, objects=chosen)
            elif database and schema:
                st.info("No supported objects in this schema.")
    elif mode == "Paste DDL":
        ddl = st.text_area("Snowflake DDL", height=320, placeholder="CREATE OR REPLACE TABLE ...;\nCREATE OR REPLACE VIEW ...;",
                           help="Output of GET_DDL or hand-written CREATE statements, separated by semicolons. "
                                "Unqualified names default to the database/schema below.")
        c1, c2 = st.columns(2)
        pdb = c1.text_input("Default database", value="MY_DB")
        psch = c2.text_input("Default schema", value="PUBLIC")
        if ddl.strip():
            slice_spec = SliceSpec(database=pdb.strip(), schema=psch.strip(), pasted_ddl=ddl)
    else:
        sample = open(os.path.join(ROOT, "samples", "sample_slice.sql"), encoding="utf-8").read()
        with st.expander("Sample DDL (SALES_DB.CORE)", icon=":material/description:"):
            st.code(sample, language="sql")
        slice_spec = SliceSpec(database="SALES_DB", schema="CORE", pasted_ddl=sample)

with st.form("run_options"):
    st.subheader("Options")
    c1, c2 = st.columns(2)
    catalog = c1.text_input("Target Unity Catalog", placeholder="default: lower-case Snowflake database name")
    use_llm = c2.toggle("Use Cortex for AI slots, plan writing and plan review", value=b.db is not None,
                        disabled=b.db is None)
    parity = c2.toggle("Profile Snowflake tables now (parity manifest)", value=False,
                       disabled=b.db is None or mode != "Live Snowflake schema",
                       help="Runs COUNT/MIN/MAX queries on the selected source objects.")
    st.caption("Review gates (human in the loop)")
    hcols = st.columns(len(master.AGENT_ORDER))
    hitl = {stage: hcols[i].checkbox(master.PHASE_LABELS[stage].split(". ", 1)[1], value=master.HITL_DEFAULTS[stage],
                                     key=f"hitl_{stage}")
            for i, stage in enumerate(master.AGENT_ORDER)}
    submitted = st.form_submit_button("Start run", type="primary", icon=":material/play_arrow:",
                                      disabled=slice_spec is None)

if submitted and slice_spec is not None:
    slice_spec.target_catalog = catalog.strip()
    state = master.new_run(slice_spec, hitl=hitl, use_llm=use_llm, parity_live=parity)
    state = runtime.drive(b, state)
    if state.status == "awaiting_human":
        st.switch_page("app_pages/review.py")
    st.switch_page("app_pages/monitor.py")
