"""Data loading shared by the History and Financial pages."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from core import analytics


def settings(b) -> dict:
    return analytics.load_settings(getattr(b, "state_path", None))


def rates(b) -> dict:
    return analytics.load_rates(settings(b))


def calls_frame(b, rates_: dict, run_id: str | None = None) -> pd.DataFrame:
    calls = analytics.enrich_calls(b.obs.llm_calls(run_id), rates_)
    df = pd.DataFrame(calls)
    if df.empty:
        return df
    df.columns = [c.lower() for c in df.columns]
    for c in ("tokens_in", "tokens_out", "latency_ms", "retries"):
        df[c] = pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0).astype(int)
    for c in ("est_credits", "billed_credits"):
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else float("nan")
    ts = df["ts"]
    df["time"] = pd.to_datetime(ts, unit="s") if pd.api.types.is_numeric_dtype(ts) else pd.to_datetime(ts)
    df["day"] = df["time"].dt.date
    return df


def runs_frame(b, calls_df: pd.DataFrame, limit: int = 1000) -> pd.DataFrame:
    by_run = {rid: g.to_dict("records") for rid, g in calls_df.groupby("run_id")} if not calls_df.empty else {}
    rows = []
    for r in b.checkpoints.list_runs(limit):
        try:
            st_ = b.checkpoints.load(r["run_id"])
        except Exception:
            st_ = None
        if st_ is None:
            continue
        rows.append(analytics.run_summary(st_, by_run.get(st_.run_id, [])))
    df = pd.DataFrame(rows)
    if not df.empty:
        df["created_at"] = pd.to_datetime(df["created"], unit="s")
        df["day"] = df["created_at"].dt.date
    return df


def date_filter(key: str, default_days: int = 30) -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    val = st.date_input("Date range", (today - dt.timedelta(days=default_days), today), key=key,
                        max_value=today, format="YYYY-MM-DD")
    if isinstance(val, (list, tuple)) and len(val) == 2:
        return val[0], val[1]
    d = val[0] if isinstance(val, (list, tuple)) else val
    return d, d


def money(credits: float | None, price: float | None) -> str:
    if credits is None:
        return "–"
    return f"{credits:,.4f} cr" + (f" (${credits * price:,.2f})" if price else "")
