import json

import pandas as pd
import streamlit as st

from agents import master
from core.schemas import HumanFeedback
from core.state import RunStatus
from ui import runtime

b = runtime.backend()
st.title(":material/rate_review: Review gate")

waiting = [r for r in b.checkpoints.list_runs(50) if r["status"] == RunStatus.AWAITING_HUMAN]
if not waiting:
    st.info("No runs are waiting for review.", icon=":material/done_all:")
    st.stop()
ids = [r["run_id"] for r in waiting]
cur = st.session_state.run_id if st.session_state.run_id in ids else ids[0]
rid = st.selectbox("Run awaiting review", ids, index=ids.index(cur),
                   format_func=lambda i: f"{i} - {next(r['slice_id'] for r in waiting if r['run_id'] == i)}")
st.session_state.run_id = rid
state = b.checkpoints.load(rid)
p = state.pending
stage, ev, art = p["stage"], p["eval"], p["artifact"]

with st.container(horizontal=True, vertical_alignment="center"):
    st.subheader(master.PHASE_LABELS[stage])
    color = {"pass": "green", "revise": "orange", "fail": "red"}[ev["verdict"]]
    st.badge(ev["verdict"].upper(), color=color)
    st.badge(f"confidence {ev['confidence']:.2f}", color="gray")
    st.badge(f"iteration {p['iteration']}", color="blue")
st.caption(ev.get("summary", ""))

# ------------------------------------------------------------------ artifact
st.markdown("#### Artifact")
with st.container(border=True):
    if stage == "discovery":
        st.markdown(art["markdown"])
        st.caption("To drop objects from scope, add lines like `exclude: DB.SCHEMA.OBJECT` to the notes below.")
    elif stage == "md_plan":
        tab_r, tab_s = st.tabs(["Rendered", "Markdown source"])
        tab_r.markdown(art)
        tab_s.code(art, language="markdown")
    elif stage in ("cd_codegen", "classifier"):
        files = art["files"]
        if stage == "classifier":
            st.markdown(art["markdown"])
            st.caption("To move an object to another layer, add `layer: DB.SCHEMA.OBJECT = gold` to the notes below.")
        if stage == "cd_codegen":
            m = p.get("meta", {})
            with st.container(horizontal=True):
                st.metric("MAPPED", m.get("origins", {}).get("MAPPED", 0))
                st.metric("HYBRID", m.get("origins", {}).get("HYBRID", 0))
                st.metric("AI", m.get("origins", {}).get("AI", 0))
                st.metric("AI slots filled", f"{m.get('slots_filled', 0)}/{m.get('slots_total', 0)}")
        names = sorted(files)
        name = st.selectbox("File", names, key=f"file_{rid}_{stage}_{p['iteration']}")
        st.code(files[name], language=runtime.lang_for(name), line_numbers=True)
    else:
        st.json(art)

# ------------------------------------------------------------------ feedback
st.markdown("#### Evaluator issues")
issues = ev["issues"]
if issues:
    df = pd.DataFrame(issues)[["id", "severity", "ref", "message", "suggested_fix"]]
    df.insert(0, "reject", False)
    df.insert(0, "accept", df["severity"].isin(["error", "warn"]))
    sev_order = {"error": 0, "warn": 1, "info": 2}
    df = df.sort_values("severity", key=lambda s: s.map(sev_order), kind="stable")
    edited = st.data_editor(
        df, hide_index=True, disabled=["id", "severity", "ref", "message", "suggested_fix"],
        column_config={"accept": st.column_config.CheckboxColumn("Fix", help="Ask the agent to fix this issue", width="small"),
                       "reject": st.column_config.CheckboxColumn("Ignore", help="Tell the agent not to change this", width="small"),
                       "message": st.column_config.TextColumn("Message", width="large")},
        key=f"issues_{rid}_{stage}_{p['iteration']}")
else:
    edited = pd.DataFrame(columns=["id", "accept", "reject"])
    st.success("No issues raised.", icon=":material/check:")

with st.form(f"feedback_{rid}_{stage}_{p['iteration']}"):
    notes = st.text_area("Notes for the agent", placeholder="Free-form guidance applied on the next iteration.")
    hard = st.text_area("Hard constraints (one per line)",
                        placeholder="must not contain: spark.sql(\"DROP\nmust contain: CLUSTER BY",
                        help="Placed above everything else in the prompt. 'must contain: X' and "
                             "'must not contain: X' are also checked automatically.")
    c1, c2, c3 = st.columns(3)
    approve = c1.form_submit_button("Approve and continue", type="primary", icon=":material/check_circle:")
    revise = c2.form_submit_button("Request revision", icon=":material/edit_note:")
    abort = c3.form_submit_button("Abort run", icon=":material/cancel:")

if approve or revise or abort:
    fb = HumanFeedback(
        accepted=bool(approve), abort=bool(abort),
        accepted_issue_ids=tuple(edited.loc[edited["accept"] & ~edited["reject"], "id"]) if len(edited) else (),
        rejected_issue_ids=tuple(edited.loc[edited["reject"], "id"]) if len(edited) else (),
        freeform=notes.strip(), hard_constraints=tuple(l.strip() for l in hard.splitlines() if l.strip()))
    if revise and not (fb.accepted_issue_ids or fb.freeform or fb.hard_constraints):
        st.warning("Select issues to fix or add notes before requesting a revision.", icon=":material/warning:")
        st.stop()
    state = runtime.drive(b, state, fb)
    if state.status == RunStatus.AWAITING_HUMAN:
        st.rerun()
    st.switch_page("app_pages/monitor.py")

with st.expander("Raw evaluation JSON", icon=":material/data_object:"):
    st.code(json.dumps(ev, indent=2), language="json")
