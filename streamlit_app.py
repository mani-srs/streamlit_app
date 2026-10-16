"""Snowflake -> Databricks migration app (Streamlit in Snowflake)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st  # noqa: E402

from core.db import list_connections  # noqa: E402
from ui import runtime  # noqa: E402

st.set_page_config(page_title="Snowflake to Databricks", page_icon=":material/swap_horiz:", layout="wide")
runtime.init_state()

pages = st.navigation([
    st.Page("app_pages/new_run.py", title="New run", icon=":material/add_circle:", default=True),
    st.Page("app_pages/monitor.py", title="Run monitor", icon=":material/monitoring:"),
    st.Page("app_pages/review.py", title="Review gate", icon=":material/rate_review:"),
    st.Page("app_pages/outputs.py", title="Outputs", icon=":material/folder_zip:"),
    st.Page("app_pages/history.py", title="History", icon=":material/history:"),
    st.Page("app_pages/financial.py", title="Financial", icon=":material/payments:"),
    st.Page("app_pages/observability.py", title="Observability", icon=":material/query_stats:"),
])

with st.sidebar:
    st.caption("Snowflake to Databricks migration")
    known = list_connections()
    if known:
        default = runtime.connection_name()
        st.selectbox("Snowflake connection", known, index=known.index(default) if default in known else 0,
                     key="conn_name", on_change=runtime.reconnect,
                     help="Named connections from ~/.snowflake/connections.toml")
    st.text_input("App state schema", key="app_schema", placeholder="APP_DB.APP_SCHEMA",
                  help="Optional: schema where setup.sql was run, for artifacts, observability and an audit copy of "
                       "run state in Snowflake. Runs are always checkpointed locally by LangGraph.")
    b = runtime.backend()
    st.caption(f"Runs stored in: {runtime.persistence_label(b)}")
    if st.session_state.get("app_schema_error") and not b.app_schema and st.session_state.app_schema:
        st.warning(f"App schema not usable: {st.session_state.app_schema_error}", icon=":material/warning:")
    if b.db is None:
        st.warning("No Snowflake connection: paste-DDL mode only, no Cortex.", icon=":material/cloud_off:")
        if st.session_state.get("db_error"):
            with st.expander("Connection error", icon=":material/bug_report:"):
                st.code(st.session_state.db_error, language="text", wrap_lines=True)
                st.caption(f"Connection name: `{runtime.connection_name() or '(none found)'}`")
        if st.button("Retry connection", icon=":material/refresh:"):
            runtime.reconnect()
            st.rerun()
    else:
        st.caption(f":material/cloud_done: Connected: `{runtime.connection_name()}`")
        try:
            info = runtime.session_info(b.db)
            roles = info["roles"] or [info["role"]]
            st.selectbox("Role", roles, index=roles.index(info["role"]) if info["role"] in roles else 0,
                         key="ui_role", help="Roles granted to your user. Switching runs USE ROLE for this session.",
                         on_change=lambda: runtime.apply_session(b.db, role=st.session_state.ui_role))
            st.toggle("Also use all my secondary roles", value=info["secondary_all"], key="ui_secondary",
                      help="USE SECONDARY ROLES ALL: privileges from every role granted to you apply at once.",
                      on_change=lambda: runtime.apply_session(b.db, secondary_all=st.session_state.ui_secondary))
            whs = info["warehouses"]
            if whs:
                st.selectbox("Warehouse", whs, index=whs.index(info["warehouse"]) if info["warehouse"] in whs else None,
                             placeholder="Choose a warehouse", key="ui_wh",
                             help="Used for metadata queries, Cortex calls and parity profiling.",
                             on_change=lambda: runtime.apply_session(b.db, warehouse=st.session_state.ui_wh))
            if not info["warehouse"]:
                st.warning("No warehouse is active for this role: pick one (needed for Cortex).", icon=":material/warning:")
            if st.session_state.get("session_error"):
                st.error(st.session_state.session_error, icon=":material/error:")
        except Exception as e:
            st.error(f"Could not read session info: {str(e).splitlines()[0][:200]}", icon=":material/error:")

pages.run()
