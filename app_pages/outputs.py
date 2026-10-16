import pandas as pd
import streamlit as st

from agents.classifier.agent import bundle_zip
from core.state import RunStatus
from ui import runtime

b = runtime.backend()
st.title(":material/folder_zip: Outputs")

done = [r for r in b.checkpoints.list_runs(50) if r["status"] == RunStatus.DONE]
if not done:
    st.info("No completed runs yet.", icon=":material/hourglass_empty:")
    st.stop()
ids = [r["run_id"] for r in done]
cur = st.session_state.run_id if st.session_state.run_id in ids else ids[0]
rid = st.selectbox("Completed run", ids, index=ids.index(cur),
                   format_func=lambda i: f"{i} - {next(r['slice_id'] for r in done if r['run_id'] == i)}")
st.session_state.run_id = rid
state = b.checkpoints.load(rid)
files = state.outputs["classifier"]["files"]
report = state.outputs["validation"]["report"]
cg = state.outputs["cd_codegen"]

sev = pd.Series([i["severity"] for i in report["issues"]]).value_counts()
with st.container(horizontal=True):
    st.metric("Files", len(files))
    st.metric("Objects", len(cg["targets"]))
    st.metric("MAPPED / HYBRID / AI", " / ".join(str(cg["meta"]["origins"].get(o, 0)) for o in ("MAPPED", "HYBRID", "AI")))
    st.metric("Judge errors", int(sev.get("error", 0)))
    st.metric("Judge warnings", int(sev.get("warn", 0)))

st.download_button("Download bundle (.zip)", bundle_zip(files), file_name=f"{state.slice.slice_id.lower()}_{rid}.zip",
                   mime="application/zip", type="primary", icon=":material/download:")

tab_files, tab_plan, tab_layers, tab_judges = st.tabs(["Files", "Plan", "Layers", "Judges"])
with tab_files:
    groups = sorted({n.split("/")[0] if "/" in n else "(root)" for n in files})
    grp = st.segmented_control("Folder", groups, default=groups[0], key=f"grp_{rid}")
    names = sorted(n for n in files if (n.split("/")[0] if "/" in n else "(root)") == grp)
    if names:
        name = st.selectbox("File", names, key=f"out_file_{rid}_{grp}")
        st.code(files[name], language=runtime.lang_for(name), line_numbers=True)
with tab_plan:
    st.markdown(files.get("plan.md", "_no plan_"))
with tab_layers:
    st.markdown(state.outputs["classifier"]["markdown"])
with tab_judges:
    df = pd.DataFrame(report["issues"])
    if df.empty:
        st.success("All judges passed.", icon=":material/verified:")
    else:
        show = st.pills("Severity", ["error", "warn", "info"], selection_mode="multi", default=["error", "warn"])
        st.dataframe(df[df["severity"].isin(show or [])][["id", "severity", "ref", "message", "suggested_fix"]],
                     hide_index=True)
    st.table(pd.DataFrame([{"rule": k, "checks": v, "findings": report["counts"].get(k, 0)}
                           for k, v in report["rules"].items()]))
