"""The ONLY path to a model. Every call is logged to OBS_LLM_CALLS.

Backend is SNOWFLAKE.CORTEX.COMPLETE (messages + options form, returns usage).
Tests swap the backend with set_backend().
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

from core import models
from core.schemas import ContractViolation, LlmResult

# backend(db, model, messages, options) -> (text, tokens_in, tokens_out[, snowflake_query_id])
Backend = Callable[[object, str, list, dict], tuple]


def _cortex_backend(db, model: str, messages: list, options: dict) -> tuple[str, int, int, str | None]:
    sql = "SELECT SNOWFLAKE.CORTEX.COMPLETE(?, PARSE_JSON(?)::ARRAY, PARSE_JSON(?)::OBJECT) AS r"
    params = [model, json.dumps(messages), json.dumps(options)]
    if hasattr(db, "query_with_id"):
        rows, qid = db.query_with_id(sql, params)
    else:
        rows, qid = db.query(sql, params), None
    raw = rows[0]["r"]
    d = json.loads(raw) if isinstance(raw, str) else raw
    choice = d["choices"][0]
    text = choice.get("messages") or choice.get("message", {}).get("content", "")
    usage = d.get("usage", {})
    return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), qid


_backend: Backend = _cortex_backend


def set_backend(fn: Backend | None) -> None:
    global _backend
    _backend = fn or _cortex_backend


class LlmUnavailable(RuntimeError):
    pass


def complete(ctx, role: str, messages: list[dict], *, stage: str = "", json_mode: bool = False,
             max_tokens: int | None = None, attempts: int = 2) -> LlmResult:
    """ctx needs .db, .obs, .run_id, .llm_enabled."""
    if not getattr(ctx, "llm_enabled", False):
        raise LlmUnavailable("LLM disabled for this run")
    spec = models.for_role(role)
    options = {"temperature": spec.get("temperature", 0), "max_tokens": int(max_tokens or spec.get("max_tokens", 4000))}
    chain = [spec["model"], *spec.get("fallback", [])]
    last_err = None
    retries = 0
    for model in chain:
        for _ in range(attempts):
            t0 = time.time()
            try:
                out = _backend(ctx.db, model, messages, options)
                text, tin, tout = out[:3]
                qid = out[3] if len(out) > 3 else None
                parsed = parse_json(text) if json_mode else None
                ms = int((time.time() - t0) * 1000)
                ctx.obs.llm_call({"run_id": ctx.run_id, "stage": stage, "role": role, "model": model,
                                  "tokens_in": tin, "tokens_out": tout, "latency_ms": ms,
                                  "retries": retries, "status": "ok", "query_id": qid})
                return LlmResult(text=text, model=model, tokens_in=tin, tokens_out=tout,
                                 latency_ms=ms, retries=retries, parsed=parsed)
            except Exception as e:  # retry, then fall back to the next model
                last_err = e
                retries += 1
                ctx.obs.llm_call({"run_id": ctx.run_id, "stage": stage, "role": role, "model": model,
                                  "latency_ms": int((time.time() - t0) * 1000), "retries": retries,
                                  "status": "error", "error": f"{type(e).__name__}: {e}"})
    raise LlmUnavailable(f"all models failed for role {role}: {last_err}")


def parse_json(text: str):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", t, re.S)
    if m:
        t = m.group(1)
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ContractViolation("model did not return JSON")
    end = max(t.rfind("}"), t.rfind("]"))
    return json.loads(t[start:end + 1])
