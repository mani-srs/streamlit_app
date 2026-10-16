import pandas as pd
import streamlit as st

from agents import master
from ui import runtime, stats

b = runtime.backend()
st.title(":material/history: History")
st.caption("Every migration run on this machine: what was converted, how, with which quality results and at what AI cost.")

rates = stats.rates(b)
calls = stats.calls_frame(b, rates)
runs = stats.runs_frame(b, calls)
if runs.empty:
    st.info("No runs yet. Start one from **New run**.", icon=":material/info:")
    st.stop()

# ------------------------------------------------------------------ filters
with st.container(horizontal=True, vertical_alignment="bottom"):
    start, end = stats.date_filter("hist_dates", default_days=30)
    statuses = sorted(runs["status"].unique())
    pick = st.multiselect("Status", statuses, default=statuses, key="hist_status")
    needle = st.text_input("Slice contains", key="hist_slice", placeholder="e.g. SALES_DB")
view = runs[(runs["day"] >= start) & (runs["day"] <= end) & runs["status"].isin(pick)]
if needle:
    view = view[view["slice"].str.contains(needle, case=False, na=False)]
if view.empty:
    st.warning("No runs match the filters.", icon=":material/filter_alt_off:")
    st.stop()

# ------------------------------------------------------------------ summary
done = view[view["status"] == "done"]
objs = int(view["objects"].sum())
conv = int(view[["mapped", "hybrid", "ai"]].sum().sum())
rule_share = (view["mapped"].sum() / conv) if conv else None
price = stats.settings(b).get("usd_per_credit")
with st.container(horizontal=True):
    st.metric("Runs", len(view), help="Runs started in the selected range")
    st.metric("Completed", f"{len(done)} ({len(done) / len(view):.0%})")
    st.metric("Objects", f"{objs:,}")
    st.metric("Converted by rules", f"{rule_share:.0%}" if rule_share is not None else "–",
              help="Share of generated fragments that needed no AI (MAPPED)")
    st.metric("AI slots filled", f"{int(view['slots_filled'].sum())}/{int(view['slots_total'].sum())}")
    st.metric("Check errors", int(view["errors"].sum()))
    st.metric("Revisions", int(view["revisions"].sum()), help="Review-gate or automatic regenerations")
    st.metric("AI cost (est.)", stats.money(float(view["llm_est_credits"].sum()), price))

# ------------------------------------------------------------------ charts
c1, c2 = st.columns(2)
with c1.container(border=True):
    st.markdown("**Runs per day**")
    per_day = view.groupby(["day", "status"]).size().reset_index(name="runs")
    st.bar_chart(per_day, x="day", y="runs", color="status", stack=True, height=260)
with c2.container(border=True):
    st.markdown("**How objects were converted**")
    split = pd.DataFrame({"origin": ["Rules (MAPPED)", "Rules + AI (HYBRID)", "AI"],
                          "fragments": [int(view["mapped"].sum()), int(view["hybrid"].sum()), int(view["ai"].sum())]})
    st.bar_chart(split, x="origin", y="fragments", horizontal=True, height=260)

c3, c4 = st.columns(2)
with c3.container(border=True):
    st.markdown("**Objects by type**")
    types = pd.DataFrame([(t, n) for d in view["types"] for t, n in d.items()], columns=["type", "objects"])
    if types.empty:
        st.caption("No object details recorded yet.")
    else:
        st.bar_chart(types.groupby("type", as_index=False)["objects"].sum(), x="type", y="objects", height=260)
with c4.container(border=True):
    st.markdown("**Most frequent check findings** (errors + warnings)")
    rules = pd.DataFrame([(r, n) for d in view["top_rules"] for r, n in d.items()], columns=["rule", "findings"])
    if rules.empty:
        st.caption("No findings.")
    else:
        st.bar_chart(rules.groupby("rule", as_index=False)["findings"].sum().nlargest(10, "findings"),
                     x="rule", y="findings", horizontal=True, height=260)

# ------------------------------------------------------------------ run table
st.subheader("Runs")
table = view.sort_values("created", ascending=False)[[
    "run_id", "created_at", "slice", "source", "status", "stage", "objects", "mapped", "hybrid", "ai",
    "slots_filled", "slots_total", "errors", "warnings", "revisions", "files", "duration_min",
    "llm_calls", "llm_tokens_in", "llm_tokens_out", "llm_est_credits", "llm_billed_credits"]].reset_index(drop=True)
event = st.dataframe(
    table, hide_index=True, on_select="rerun", selection_mode="single-row", key="hist_table",
    column_config={
        "created_at": st.column_config.DatetimeColumn("Started", format="YYYY-MM-DD HH:mm"),
        "duration_min": st.column_config.NumberColumn("Minutes", format="%.1f"),
        "llm_est_credits": st.column_config.NumberColumn("Est. credits", format="%.4f"),
        "llm_billed_credits": st.column_config.NumberColumn("Billed credits", format="%.4f",
                                                            help="Filled in after 'Refresh billed credits' on the Financial page"),
        "llm_calls": st.column_config.NumberColumn("LLM calls"),
        "llm_tokens_in": st.column_config.NumberColumn("Tokens in", format="%d"),
        "llm_tokens_out": st.column_config.NumberColumn("Tokens out", format="%d"),
    })
st.download_button("Download as CSV", table.to_csv(index=False).encode("utf-8"), file_name="migration_runs.csv",
                   mime="text/csv", icon=":material/download:")

rows = event.selection.rows if event and event.selection else []
if rows:
    sel = table.iloc[rows[0]]
    rid = sel["run_id"]
    full = view[view["run_id"] == rid].iloc[0]
    with st.container(border=True):
        st.markdown(f"**Run `{rid}`** · {sel['slice']} · {master.PHASE_LABELS.get(full['stage'], full['status'])}")
        st.caption(f"Objects by type: {full['types'] or '–'} · Revisions by step: {full['revisions_by_stage'] or 'none'}"
                   f" · Top findings: {full['top_rules'] or 'none'}")
        with st.container(horizontal=True):
            for label, page, icon in (("Run monitor", "app_pages/monitor.py", ":material/monitoring:"),
                                      ("Review gate", "app_pages/review.py", ":material/rate_review:"),
                                      ("Outputs", "app_pages/outputs.py", ":material/folder_zip:"),
                                      ("Financial", "app_pages/financial.py", ":material/payments:")):
                if st.button(f"Open in {label}", icon=icon, key=f"open_{page}",
                             disabled=(page.endswith("review.py") and full["status"] != "awaiting_human")
                             or (page.endswith("outputs.py") and full["status"] != "done")):
                    st.session_state.run_id = rid
                    st.switch_page(page)
else:
    st.caption("Select a row to see details and open the run in another page.")
