"""Run statistics and Cortex cost analytics (used by the History and Financial pages).

Cost model
- ESTIMATED credits: tokens x per-model rate (config/pricing.yaml, overridable in .state/settings.json).
  Available immediately after a call.
- BILLED credits: exact, from SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY, matched by the
  Snowflake query id recorded for every call. That view lags by hours.
- Currency: optional USD-per-credit setting; everything else is reported in AI credits.
Excluded: warehouse compute for the calls, Cortex Code usage while building, Databricks costs.
"""
from __future__ import annotations

import json
import os
from collections import Counter

from core.db import APP_QUERY_TAG

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PRICING = os.path.join(ROOT, "config", "pricing.yaml")
USAGE_VIEW = "SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY"
_TOKENS = ("SUM(COALESCE(GET(FILTER(METRICS, x -> x:key:metric::STRING = '{m}' AND x:key:unit::STRING = 'tokens'), 0)"
           ":value::NUMBER, 0))")
_TOK_ROW = ("COALESCE(GET(FILTER(METRICS, x -> x:key:metric::STRING = '{m}' AND x:key:unit::STRING = 'tokens'), 0)"
            ":value::NUMBER, 0)")


# ------------------------------------------------------------------ settings & rates
def _settings_path(state_path: str | None) -> str:
    base = os.path.dirname(state_path) if state_path else os.path.join(ROOT, ".state")
    return os.path.join(base, "settings.json")


def load_settings(state_path: str | None) -> dict:
    try:
        with open(_settings_path(state_path), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(state_path: str | None, settings: dict) -> None:
    p = _settings_path(state_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def load_rates(settings: dict | None = None) -> dict[str, dict]:
    """model -> {input_per_m, output_per_m, source}; settings overrides beat config/pricing.yaml."""
    rates: dict[str, dict] = {}
    try:
        import yaml

        with open(_PRICING, encoding="utf-8") as f:
            rates = {k: dict(v or {}) for k, v in ((yaml.safe_load(f) or {}).get("models") or {}).items()}
    except (FileNotFoundError, ImportError):
        rates = {"claude-4-sonnet": {"input_per_m": 1.8, "output_per_m": 9.0, "source": "built-in default"}}
    for model, r in ((settings or {}).get("rates_override") or {}).items():
        rates.setdefault(model, {}).update(r)
    return rates


def estimate_credits(model: str, tokens_in: int, tokens_out: int, rates: dict) -> float | None:
    r = rates.get(model) or {}
    if r.get("input_per_m") is None or r.get("output_per_m") is None:
        return None
    return (int(tokens_in or 0) * float(r["input_per_m"]) + int(tokens_out or 0) * float(r["output_per_m"])) / 1e6


def calibrate(rows: list[dict]) -> dict[str, dict]:
    """Least-squares fit (no intercept) of credits = a*input + b*output per model, in credits per 1M tokens.

    rows: [{"model", "tokens_in", "tokens_out", "credits"}]. Needs >= 2 rows with different in/out mixes.
    """
    import numpy as np

    out = {}
    by_model: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("credits") is not None and (r.get("tokens_in") or r.get("tokens_out")):
            by_model.setdefault(r["model"], []).append(r)
    for model, rs in by_model.items():
        X = np.array([[float(r["tokens_in"]), float(r["tokens_out"])] for r in rs]) / 1e6
        y = np.array([float(r["credits"]) for r in rs])
        if len(rs) < 2 or np.linalg.matrix_rank(X) < 2:
            continue
        (a, b), *_ = np.linalg.lstsq(X, y, rcond=None)
        err = float(np.max(np.abs(X @ np.array([a, b]) - y))) if len(y) else 0.0
        out[model] = {"input_per_m": round(float(a), 4), "output_per_m": round(float(b), 4), "n": len(rs),
                      "max_abs_error": err}
    return out


# ------------------------------------------------------------------ Snowflake billing (read-only)
def fetch_billed(db, query_ids: list[str], days: int = 30) -> dict[str, float]:
    """query_id -> billed credits for completed calls (chunks of 200, bounded window)."""
    billed: dict[str, float] = {}
    for i in range(0, len(query_ids), 200):
        chunk = query_ids[i:i + 200]
        rows = db.query(
            f"SELECT QUERY_ID AS qid, SUM(COALESCE(CREDITS, 0)) AS credits FROM {USAGE_VIEW} "
            f"WHERE START_TIME >= DATEADD('day', -{int(days)}, CURRENT_TIMESTAMP()) "
            f"AND QUERY_ID IN ({', '.join('?' * len(chunk))}) GROUP BY QUERY_ID", chunk)
        billed.update({r["qid"]: float(r["credits"]) for r in rows})
    return billed


def account_usage(db, days: int = 30, scope: str = "app") -> list[dict]:
    """Billed Cortex AI function usage per day and model. scope 'app' = this app's query tag,
    'user' = all Cortex AI functions called by the current user (includes calls made before tagging)."""
    where = "QUERY_TAG = ?" if scope == "app" else \
        "USER_ID = (SELECT USER_ID FROM SNOWFLAKE.ACCOUNT_USAGE.USERS WHERE NAME = CURRENT_USER() AND DELETED_ON IS NULL LIMIT 1)"
    params = [APP_QUERY_TAG] if scope == "app" else []
    return db.query(
        f"SELECT TO_VARCHAR(DATE(START_TIME)) AS day, MODEL_NAME AS model, FUNCTION_NAME AS function_name, "
        f"COUNT(DISTINCT QUERY_ID) AS calls, ROUND(SUM(COALESCE(CREDITS, 0)), 6) AS credits, "
        f"{_TOKENS.format(m='input')} AS tokens_in, {_TOKENS.format(m='output')} AS tokens_out "
        f"FROM {USAGE_VIEW} WHERE START_TIME >= DATEADD('day', -{int(days)}, CURRENT_TIMESTAMP()) AND {where} "
        f"GROUP BY 1, 2, 3 ORDER BY 1, 2", params)


def calibration_rows(db, days: int = 30) -> list[dict]:
    """Per-query billed rows for the current user, for calibrate()."""
    rows = db.query(
        f"SELECT MODEL_NAME AS model, {_TOK_ROW.format(m='input')} AS tokens_in, {_TOK_ROW.format(m='output')} AS tokens_out, "
        f"CREDITS AS credits FROM {USAGE_VIEW} WHERE START_TIME >= DATEADD('day', -{int(days)}, CURRENT_TIMESTAMP()) "
        "AND CREDITS IS NOT NULL AND USER_ID = (SELECT USER_ID FROM SNOWFLAKE.ACCOUNT_USAGE.USERS "
        "WHERE NAME = CURRENT_USER() AND DELETED_ON IS NULL LIMIT 1) LIMIT 5000")
    return [{"model": r["model"], "tokens_in": int(r["tokens_in"] or 0), "tokens_out": int(r["tokens_out"] or 0),
             "credits": float(r["credits"])} for r in rows]


# ------------------------------------------------------------------ calls & runs
def enrich_calls(calls: list[dict], rates: dict) -> list[dict]:
    """Adds est_credits to each call (None when the model has no rate)."""
    out = []
    for c in calls:
        d = dict(c)
        d["est_credits"] = estimate_credits(d.get("model") or "", d.get("tokens_in") or 0, d.get("tokens_out") or 0,
                                            rates) if d.get("status") == "ok" else 0.0
        out.append(d)
    return out


def cost_of(calls: list[dict]) -> dict:
    ok = [c for c in calls if c.get("status") == "ok"]
    est = [c.get("est_credits") for c in ok]
    billed = [c["billed_credits"] for c in ok if c.get("billed_credits") is not None]
    return {"calls": len(calls), "ok": len(ok), "errors": len(calls) - len(ok),
            "tokens_in": sum(int(c.get("tokens_in") or 0) for c in ok),
            "tokens_out": sum(int(c.get("tokens_out") or 0) for c in ok),
            "est_credits": sum(e for e in est if e is not None),
            "est_unknown": sum(e is None for e in est),
            "billed_credits": sum(billed), "billed_calls": len(billed)}


def run_summary(state, calls: list[dict]) -> dict:
    """One History row per run, from its RunState and its (enriched) LLM calls."""
    o = state.outputs or {}
    objs = (o.get("standardization") or {}).get("objects") or []
    types = Counter(x["type"] for x in objs) if objs else Counter(x.get("type") for x in state.slice.objects)
    cg = o.get("cd_codegen") or {}
    meta = cg.get("meta") or ((state.scratch.get("cd_codegen") or {}).get("gen") or {}).get("meta") or {}
    origins = meta.get("origins") or {}
    report = (o.get("validation") or {}).get("report")
    issues = report["issues"] if report else ((cg.get("eval") or {}).get("issues") or [])
    sev = Counter(i.get("severity") for i in issues)
    rules = Counter(i["id"].split("-")[0] for i in issues if i.get("severity") in ("error", "warn"))
    revisions = {s: sc.get("iteration", 0) for s, sc in (state.scratch or {}).items() if sc.get("iteration")}
    cost = cost_of(calls)
    n_obj = sum(types.values())
    return {
        "run_id": state.run_id, "slice": state.slice.slice_id,
        "source": "pasted" if state.slice.pasted_ddl else "live",
        "status": state.status, "stage": state.current_stage,
        "created": state.created_at, "updated": state.updated_at,
        "duration_min": round(max(0.0, (state.updated_at or 0) - (state.created_at or 0)) / 60, 1),
        "objects": n_obj, "types": dict(types), "use_llm": bool((state.options or {}).get("use_llm")),
        "mapped": origins.get("MAPPED", 0), "hybrid": origins.get("HYBRID", 0), "ai": origins.get("AI", 0),
        "slots_total": meta.get("slots_total", 0), "slots_filled": meta.get("slots_filled", 0),
        "errors": sev.get("error", 0), "warnings": sev.get("warn", 0), "top_rules": dict(rules.most_common(5)),
        "revisions": sum(revisions.values()), "revisions_by_stage": revisions,
        "files": len((o.get("classifier") or {}).get("files") or {}),
        **{f"llm_{k}": v for k, v in cost.items()},
        "credits_per_object": (cost["est_credits"] / n_obj) if n_obj else None,
    }
