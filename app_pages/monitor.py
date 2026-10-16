import pandas as pd
import streamlit as st

from agents import master
from core.state import RunStatus
from ui import runtime

b = runtime.backend()
st.title(":material/monitoring: Run monitor")

runs = b.checkpoints.list_runs(50)
if not runs:
    st.info("No runs yet. Start one from **New run**.", icon=":material/info:")
    st.stop()

ids = [r["run_id"] for r in runs]
labels = {r["run_id"]: f"{r['run_id']} - {r['slice_id']} - {r['status']}" for r in runs}
cur = st.session_state.run_id if st.session_state.run_id in ids else ids[0]
rid = st.selectbox("Run", ids, index=ids.index(cur), format_func=labels.get)
st.session_state.run_id = rid
state = b.checkpoints.load(rid)

with st.container(horizontal=True, vertical_alignment="center"):
    runtime.status_badge(state)
    st.caption(f"Slice `{state.slice.slice_id}` | {len(state.slice.objects) or 'pasted'} objects | "
               f"LLM {'on' if state.options.get('use_llm') else 'off'}")
    if st.button("Refresh", icon=":material/refresh:"):
        st.rerun()

cols = st.columns(len(master.AGENT_ORDER))
for i, stage in enumerate(master.AGENT_ORDER):
    with cols[i].container(border=True):
        if stage in state.completed:
            icon, note = ":material/check_circle:", "done"
        elif stage == state.current_stage and state.status == RunStatus.AWAITING_HUMAN:
            icon, note = ":material/rate_review:", "awaiting review"
        elif stage == state.current_stage and state.status == RunStatus.FAILED:
            icon, note = ":material/error:", "failed"
        elif stage == state.current_stage and state.status == RunStatus.ABORTED:
            icon, note = ":material/cancel:", "aborted"
        else:
            icon, note = ":material/radio_button_unchecked:", "pending"
        st.markdown(f"{icon} **{master.PHASE_LABELS[stage]}**")
        gate = " | review gate" if state.hitl.get(stage) else ""
        iters = state.scratch.get(stage, {}).get("iteration")
        st.caption(note + gate + (f" | iteration {iters}" if iters else ""))

if state.status == RunStatus.AWAITING_HUMAN:
    st.warning(f"Waiting for your review at **{master.PHASE_LABELS[state.pending['stage']]}**.", icon=":material/rate_review:")
    if st.button("Open review gate", type="primary", icon=":material/arrow_forward:"):
        st.switch_page("app_pages/review.py")
elif state.status == RunStatus.FAILED:
    st.error(f"Failed at **{master.PHASE_LABELS.get(state.current_stage, state.current_stage)}**", icon=":material/error:")
    with st.expander("Error details", icon=":material/bug_report:"):
        st.code(state.error, language="text")
    if st.button("Retry stage", icon=":material/replay:"):
        state.error = ""
        runtime.drive(b, state)
        st.rerun()
elif state.status == RunStatus.ABORTED:
    st.info(f"Aborted: {state.error}", icon=":material/cancel:")
elif state.status == RunStatus.DONE:
    st.success("All stages complete.", icon=":material/check_circle:")
    if st.button("View outputs", type="primary", icon=":material/folder_zip:"):
        st.switch_page("app_pages/outputs.py")

disc = state.outputs.get("discovery")
if disc:
    st.subheader("Lineage")
    g = disc["graph"]
    shape = {"TABLE": "box", "VIEW": "ellipse", "DYNAMIC_TABLE": "box3d", "STREAM": "cds", "TASK": "hexagon",
             "PROCEDURE": "component", "EXTERNAL": "note"}
    q = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
    dot = ["digraph G { rankdir=LR; node [fontsize=10, fontname=Helvetica];"]
    for n in g["nodes"]:
        dot.append(f'"{q(n["id"])}" [label="{q(n["name"])}\\n{n["type"]}", shape={shape.get(n["type"], "box")}'
                   + (', style=dashed' if not n["in_scope"] else "") + "];")
    for e in g["edges"]:
        dot.append(f'"{q(e["src"])}" -> "{q(e["dst"])}" [label="{e["kind"]}", fontsize=8];')
    dot.append("}")
    st.graphviz_chart("\n".join(dot))

nodes = b.obs.nodes(rid)
if nodes:
    with st.expander("Stage timings", icon=":material/timer:"):
        df = pd.DataFrame(nodes)[["stage", "node", "status", "duration_ms"]]
        st.dataframe(df, hide_index=True)
