import datetime as dt

import pandas as pd
import streamlit as st

from core import analytics
from ui import runtime, stats

b = runtime.backend()
st.title(":material/payments: Financial")
st.caption("Cortex AI cost of this app's LLM calls: estimated instantly from tokens, and billed credits matched "
           "from Snowflake's usage history.")

cfg = stats.settings(b)
rates = stats.rates(b)
price = cfg.get("usd_per_credit")

with st.expander("Settings: price and budget", icon=":material/settings:", expanded=not price):
    with st.form("fin_settings", border=False):
        c1, c2 = st.columns(2)
        p = c1.number_input("Price per AI credit (USD)", min_value=0.0, value=float(price or 0.0), step=0.1, format="%.4f",
                            help="From your Snowflake contract / invoice. 0 = show credits only.")
        bud = c2.number_input("Monthly budget (AI credits)", min_value=0.0, value=float(cfg.get("monthly_budget_credits") or 0.0),
                              step=1.0, help="0 = no budget tracking")
        if st.form_submit_button("Save", icon=":material/save:"):
            cfg.update(usd_per_credit=p or None, monthly_budget_credits=bud or None)
            analytics.save_settings(b.state_path, cfg)
            st.rerun()

calls = stats.calls_frame(b, rates)
tab_cost, tab_billing, tab_calls = st.tabs(["Cost overview", "Billing (Snowflake)", "All calls"])

# ------------------------------------------------------------------ overview
with tab_cost:
    if calls.empty:
        st.info("No LLM calls recorded yet. Runs with **Use Cortex** on will appear here.", icon=":material/info:")
    else:
        start, end = stats.date_filter("fin_dates", default_days=30)
        view = calls[(calls["day"] >= start) & (calls["day"] <= end)]
        ok = view[view["status"] == "ok"]
        c = analytics.cost_of(view.to_dict("records"))
        runs_n = view["run_id"].nunique()
        with st.container(horizontal=True):
            st.metric("LLM calls", c["calls"], delta=f"{c['errors']} errors" if c["errors"] else None, delta_color="inverse")
            st.metric("Tokens in / out", f"{c['tokens_in']:,} / {c['tokens_out']:,}")
            st.metric("Estimated", stats.money(c["est_credits"], price),
                      help="Tokens x rate from config/pricing.yaml (or calibrated overrides)")
            st.metric("Billed", stats.money(c["billed_credits"], price) if c["billed_calls"] else "not matched yet",
                      help=f"{c['billed_calls']} of {c['ok']} calls matched to Snowflake billing")
            st.metric("Per run (est.)", stats.money(c["est_credits"] / runs_n, price) if runs_n else "–")
        if c["est_unknown"]:
            st.warning(f"{c['est_unknown']} call(s) used a model without a rate in config/pricing.yaml; they are not "
                       "in the estimate. Use **Calibrate rates from billing** on the Billing tab.", icon=":material/warning:")

        budget = cfg.get("monthly_budget_credits")
        if budget:
            month0 = dt.date.today().replace(day=1)
            mcalls = calls[calls["day"] >= month0]
            spent = float(mcalls["billed_credits"].fillna(mcalls["est_credits"]).fillna(0).sum())
            st.progress(min(spent / budget, 1.0), text=f"This month: {spent:,.4f} of {budget:,.2f} credits "
                                                          f"({spent / budget:.0%}) · billed where matched, else estimated")

        g1, g2 = st.columns(2)
        with g1.container(border=True):
            st.markdown("**Daily cost (credits)**")
            daily = ok.groupby("day", as_index=False).agg(estimated=("est_credits", "sum"), billed=("billed_credits", "sum"))
            st.bar_chart(daily, x="day", y=["estimated", "billed"], stack=False, height=260)
        with g2.container(border=True):
            st.markdown("**By step and role**")
            by = ok.groupby(["stage", "role"], as_index=False).agg(credits=("est_credits", "sum"))
            by["step / role"] = by["stage"] + " / " + by["role"]
            st.bar_chart(by, x="step / role", y="credits", horizontal=True, height=260)

        t1, t2 = st.columns(2)
        with t1.container(border=True):
            st.markdown("**By model**")
            bm = ok.groupby("model", as_index=False).agg(calls=("id", "count"), tokens_in=("tokens_in", "sum"),
                                                         tokens_out=("tokens_out", "sum"), est_credits=("est_credits", "sum"),
                                                         billed_credits=("billed_credits", "sum"), avg_latency_ms=("latency_ms", "mean"))
            if price:
                bm["est_usd"] = bm["est_credits"] * price
            st.dataframe(bm, hide_index=True, column_config={
                "est_credits": st.column_config.NumberColumn("Est. credits", format="%.4f"),
                "billed_credits": st.column_config.NumberColumn("Billed credits", format="%.4f"),
                "est_usd": st.column_config.NumberColumn("Est. USD", format="$%.2f"),
                "avg_latency_ms": st.column_config.NumberColumn("Avg latency (ms)", format="%d")})
        with t2.container(border=True):
            st.markdown("**Most expensive runs (est.)**")
            br = ok.groupby("run_id", as_index=False).agg(calls=("id", "count"), est_credits=("est_credits", "sum"),
                                                          tokens=("tokens_in", "sum")).nlargest(10, "est_credits")
            st.dataframe(br, hide_index=True, column_config={
                "est_credits": st.column_config.NumberColumn("Est. credits", format="%.4f"),
                "tokens": st.column_config.NumberColumn("Tokens in", format="%d")})

        runs = stats.runs_frame(b, calls)
        if not runs.empty:
            r = runs[runs["run_id"].isin(view["run_id"].unique())]
            objs, slots = int(r["objects"].sum()), int(r["slots_filled"].sum())
            with st.container(horizontal=True):
                st.metric("Per object converted (est.)", stats.money(c["est_credits"] / objs, price) if objs else "–")
                st.metric("Per AI slot filled (est.)",
                          stats.money(float(ok[ok["stage"] == "cd_codegen"]["est_credits"].sum()) / slots, price) if slots else "–")
                st.metric("Plan writing + review (est.)", stats.money(float(ok[ok["stage"] == "md_plan"]["est_credits"].sum()), price))

# ------------------------------------------------------------------ billing
with tab_billing:
    st.markdown("Snowflake records the exact credits of every Cortex call in "
                f"`{analytics.USAGE_VIEW}`, usually a few hours after the call. Needs `SNOWFLAKE.USAGE_VIEWER` "
                "(or IMPORTED PRIVILEGES on the SNOWFLAKE database).")
    if b.db is None:
        st.warning("No Snowflake connection.", icon=":material/cloud_off:")
    else:
        with st.container(horizontal=True):
            if st.button("Refresh billed credits", icon=":material/sync:", type="primary",
                         help="Match this app's recorded calls (by query id) with billed credits"):
                if not hasattr(b.obs, "unbilled_query_ids"):
                    st.error("Billed-credit matching needs the local call log (.state).")
                else:
                    ids = b.obs.unbilled_query_ids()
                    try:
                        with st.spinner(f"Looking up {len(ids)} call(s) in Snowflake billing..."):
                            n = b.obs.set_billed(analytics.fetch_billed(b.db, ids)) if ids else 0
                        st.success(f"Matched {n} of {len(ids)} unmatched call(s). The rest are not in billing yet "
                                   "(it lags by hours) or are older than 30 days.", icon=":material/check:")
                    except Exception as e:
                        st.error(f"Could not read billing: {str(e).splitlines()[0][:250]}", icon=":material/lock:")
            if st.button("Calibrate rates from billing", icon=":material/tune:",
                         help="Fit credits per 1M input/output tokens per model from your billed calls (last 30 days)"):
                try:
                    with st.spinner("Fitting rates from billed calls..."):
                        fit = analytics.calibrate(analytics.calibration_rows(b.db, 30))
                    if fit:
                        cfg["rates_override"] = {m: {"input_per_m": v["input_per_m"], "output_per_m": v["output_per_m"],
                                                     "source": f"calibrated {dt.date.today()} from {v['n']} billed calls"}
                                                 for m, v in fit.items()}
                        analytics.save_settings(b.state_path, cfg)
                        st.success("Rates updated: " + ", ".join(f"{m}: {v['input_per_m']} in / {v['output_per_m']} out"
                                                                   for m, v in fit.items()), icon=":material/check:")
                    else:
                        st.info("Not enough billed calls to fit rates yet.")
                except Exception as e:
                    st.error(f"Could not read billing: {str(e).splitlines()[0][:250]}", icon=":material/lock:")

        st.markdown("**Rates used for estimates** (AI credits per 1M tokens)")
        st.dataframe(pd.DataFrame([{"model": m, "input": r.get("input_per_m"), "output": r.get("output_per_m"),
                                    "source": r.get("source", "")} for m, r in stats.rates(b).items()]), hide_index=True)

        scope = st.segmented_control("Snowflake billing view", ["This app", "All my Cortex AI functions"],
                                     default="This app", key="fin_scope",
                                     help="'This app' uses the app's query tag (calls from now on). "
                                          "'All my…' includes calls made before tagging and any other AI function use.")
        try:
            usage = pd.DataFrame(analytics.account_usage(b.db, 30, "app" if scope == "This app" else "user"))
        except Exception as e:
            usage = pd.DataFrame()
            st.error(f"Could not read billing: {str(e).splitlines()[0][:250]}", icon=":material/lock:")
        if usage.empty:
            st.caption("No billed usage in the last 30 days for this scope (recent calls can take a few hours to appear).")
        else:
            for col in ("credits", "tokens_in", "tokens_out", "calls"):
                usage[col] = pd.to_numeric(usage[col], errors="coerce")
            with st.container(horizontal=True):
                st.metric("Billed calls (30 days)", int(usage["calls"].sum()))
                st.metric("Billed", stats.money(float(usage["credits"].sum()), price))
                st.metric("Tokens in / out", f"{int(usage['tokens_in'].sum()):,} / {int(usage['tokens_out'].sum()):,}")
            st.bar_chart(usage, x="day", y="credits", color="model", height=240)
            st.dataframe(usage, hide_index=True)

    with st.expander("What is and isn't included", icon=":material/info:"):
        st.markdown(
            "- **Included:** Cortex `COMPLETE` credits for this app's calls (planner, evaluator, translator).\n"
            "- **Not included:** warehouse compute used while those queries run; metadata queries (SHOW, GET_DDL) and "
            "parity profiling; Cortex Code usage while building the app; any Databricks cost.\n"
            "- **Estimated** = tokens × rate (instant). **Billed** = Snowflake's recorded credits (exact, delayed).\n"
            "- USD figures use the price per credit you enter above; Snowflake bills Cortex in AI credits.")

# ------------------------------------------------------------------ raw calls
with tab_calls:
    if calls.empty:
        st.caption("No calls recorded.")
    else:
        show = calls.sort_values("time", ascending=False)[["time", "run_id", "stage", "role", "model", "status", "tokens_in",
                                                          "tokens_out", "latency_ms", "retries", "est_credits",
                                                          "billed_credits", "query_id", "error"]]
        st.dataframe(show, hide_index=True, column_config={
            "time": st.column_config.DatetimeColumn("Time", format="YYYY-MM-DD HH:mm:ss"),
            "est_credits": st.column_config.NumberColumn("Est. credits", format="%.6f"),
            "billed_credits": st.column_config.NumberColumn("Billed credits", format="%.6f")})
        st.download_button("Download calls as CSV", show.to_csv(index=False).encode("utf-8"), file_name="llm_calls.csv",
                           mime="text/csv", icon=":material/download:")
