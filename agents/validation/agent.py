"""Agent 6 - validation: deterministic judges on the final code + Snowflake-side parity manifest.

Running the Databricks side is out of scope for the MVP (needs an EAI to Databricks); the manifest
ships the matching Databricks SQL so the comparison can be run there. A Runner interface is stubbed.
"""
from __future__ import annotations

import json

from agents.cd_codegen.agent import judge_bundle
from agents.common import in_scope_objects, need
from converters.types import map_type
from core.db import qfqn
from core.graph import RunContext
from core.nodes import build_gen_eval_hitl, verdict_from
from core.schemas import GenResult, Issue, Severity
from core.state import RunState
from judges import rules

STAGE = "validation"
_NUMERIC_OR_TIME = ("DECIMAL", "DOUBLE", "DATE", "TIMESTAMP")


class DatabricksRunner:
    """Stub: execute parity SQL on a Databricks SQL warehouse. Requires an EAI (not in MVP)."""

    def run(self, sql: str) -> list[dict]:
        raise NotImplementedError("Databricks execution requires an External Access Integration")


def parity_queries(objs: list[dict], targets: dict) -> list[dict]:
    out = []
    for o in objs:
        if o["type"] not in ("TABLE", "VIEW", "DYNAMIC_TABLE"):
            continue
        sf = qfqn(o["database"], o["schema"], o["name"])
        dbx = targets[o["id"]]
        prof_sf, prof_dbx = ["COUNT(*) AS row_count"], ["COUNT(*) AS row_count"]
        for c in o.get("columns", [])[:50]:
            n = c["name"]
            a = n.lower()
            prof_sf.append(f'COUNT("{n}") AS "{a}__nn"')
            prof_dbx.append(f"COUNT(`{n}`) AS `{a}__nn`")
            if map_type(c["type"])[0].startswith(_NUMERIC_OR_TIME):
                prof_sf += [f'MIN("{n}")::VARCHAR AS "{a}__min"', f'MAX("{n}")::VARCHAR AS "{a}__max"']
                prof_dbx += [f"CAST(MIN(`{n}`) AS STRING) AS `{a}__min`", f"CAST(MAX(`{n}`) AS STRING) AS `{a}__max`"]
        out.append({"node": o["id"], "snowflake_sql": f"SELECT {', '.join(prof_sf)} FROM {sf}",
                    "databricks_sql": f"SELECT {', '.join(prof_dbx)} FROM {dbx}",
                    "columns": [c["name"] for c in o.get("columns", [])]})
    return out


def gen(ctx: RunContext, state: RunState, feedback) -> GenResult:
    cg = need(state, "cd_codegen")
    objs = in_scope_objects(state)
    issues = rules.run_all(judge_bundle(state, cg))
    queries = parity_queries(objs, cg["targets"])
    results = {}
    if state.options.get("parity_live") and ctx.db is not None and not state.slice.pasted_ddl:
        for q in queries:
            try:
                ctx.progress(f"parity profile {q['node']}")
                results[q["node"]] = ctx.db.query(q["snowflake_sql"])[0]
            except Exception as e:
                results[q["node"]] = {"error": str(e)[:500]}
    manifest = {"slice": state.slice.slice_id, "run_id": state.run_id, "queries": queries,
                "snowflake_results": results, "databricks_results": None,
                "compare": "row_count equal; *__nn equal; *__min/*__max equal as strings (TZ-normalized)"}
    by_rule: dict[str, int] = {}
    for i in issues:
        k = i.id.split("-")[0]
        by_rule[k] = by_rule.get(k, 0) + 1
    report = {"rules": rules.RULES, "counts": by_rule, "issues": [i.to_dict() for i in issues]}
    return GenResult(artifact={"manifest": manifest, "report": report,
                               "files": {"validation/parity_manifest.json": json.dumps(manifest, indent=2, default=str),
                                         "validation/parity_snowflake.sql": ";\n\n".join(q["snowflake_sql"] for q in queries) + ";\n",
                                         "validation/parity_databricks.sql": ";\n\n".join(q["databricks_sql"] for q in queries) + ";\n",
                                         "validation/judges_report.json": json.dumps(report, indent=2)}},
                     meta={"issues": [i.to_dict() for i in issues]})


def evaluate(ctx, state, g: GenResult):
    issues = [Issue.from_dict(d) for d in g.meta["issues"]]
    for node_id, r in g.artifact["manifest"]["snowflake_results"].items():
        if "error" in r:
            issues.append(Issue(f"PAR-{len(issues)}", Severity.WARN, f"node:{node_id}", f"parity profile failed: {r['error']}"))
    e = verdict_from(issues, 0.9, f"{sum(i.severity == Severity.ERROR for i in issues)} errors, "
                                  f"{sum(i.severity == Severity.WARN for i in issues)} warnings")
    return e


def save(ctx, g: GenResult, it: int) -> None:
    for n, t in g.artifact["files"].items():
        ctx.put(n, t, it)


def finalize(ctx, state, g: GenResult, e) -> dict:
    return {**g.artifact, "eval": e.to_dict()}


node = build_gen_eval_hitl(STAGE, gen, evaluate, finalize, save, max_iters=1)
