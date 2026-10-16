import pandas as pd
import streamlit as st

from ui import runtime

b = runtime.backend()
st.title(":material/query_stats: Observability")

scope = st.segmented_control("Scope", ["Selected run", "All runs"], default="Selected run")
rid = st.session_state.run_id if scope == "Selected run" else None
if scope == "Selected run" and not rid:
    st.info("Select a run in **Run monitor** first.", icon=":material/info:")
    st.stop()

calls = pd.DataFrame(b.obs.llm_calls(rid))
nodes = pd.DataFrame(b.obs.nodes(rid))

st.subheader("Cortex LLM calls")
if calls.empty:
    st.caption("No model calls recorded.")
else:
    calls.columns = [c.lower() for c in calls.columns]
    for c in ("tokens_in", "tokens_out", "latency_ms", "retries"):
        calls[c] = pd.to_numeric(calls.get(c, 0), errors="coerce").fillna(0)
    ok = calls[calls["status"] == "ok"]
    with st.container(horizontal=True):
        st.metric("Calls", len(calls))
        st.metric("Errors", int((calls["status"] != "ok").sum()))
        st.metric("Tokens in", f"{int(ok['tokens_in'].sum()):,}")
        st.metric("Tokens out", f"{int(ok['tokens_out'].sum()):,}")
        st.metric("Median latency", f"{int(ok['latency_ms'].median()) if len(ok) else 0} ms")
    by = ok.groupby(["stage", "role", "model"], as_index=False)[["tokens_in", "tokens_out"]].sum()
    st.bar_chart(by, x="stage", y=["tokens_in", "tokens_out"], stack=True)
    st.dataframe(calls, hide_index=True)

st.subheader("Node durations")
if nodes.empty:
    st.caption("No node events recorded.")
else:
    nodes.columns = [c.lower() for c in nodes.columns]
    nodes["duration_ms"] = pd.to_numeric(nodes["duration_ms"], errors="coerce").fillna(0)
    st.bar_chart(nodes, x="node", y="duration_ms", color="status", horizontal=True)
    st.dataframe(nodes, hide_index=True)
