"""Agent 7 - classifier: bronze/silver/gold from config/medallion_rules.yaml; splits the DLT code into
exactly 3 pipeline files and assembles the final deliverable. HITL.

Reviewer overrides (freeform lines):  layer: DB.SCHEMA.OBJECT = gold
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile

from agents.cd_codegen.agent import DLT_HEADER
from agents.common import in_scope_objects, need
from converters.graph_model import downstream, upstream
from core.graph import RunContext
from core.nodes import build_gen_eval_hitl, verdict_from
from core.schemas import GenResult, Issue, Severity
from core.state import RunState

STAGE = "classifier"
LAYERS = ("bronze", "silver", "gold")
_CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config", "medallion_rules.yaml")
_OVR = re.compile(r"^\s*layer\s*:\s*(\S+)\s*=\s*(bronze|silver|gold)\s*$", re.I | re.M)

DEFAULT_RULES = {
    "layers": {
        "bronze": {"name_patterns": ["^RAW_", "^STG_", "^LND_", "_RAW$", "^SRC_"], "schema_patterns": ["RAW", "LANDING", "BRONZE", "STAGING"]},
        "silver": {"name_patterns": ["^INT_", "^CLEAN_", "_ENRICHED$", "^DT_"], "schema_patterns": ["SILVER", "INTEGRATION", "CORE"]},
        "gold": {"name_patterns": ["^DIM_", "^FCT_", "^FACT_", "^AGG_", "^RPT_", "_MART$", "^MART_"], "schema_patterns": ["GOLD", "MART", "REPORTING", "PRESENTATION"]},
    },
    "precedence": ["name", "graph", "schema"],
    "default": "silver",
}


def load_rules() -> dict:
    try:
        import yaml

        with open(_CFG, encoding="utf-8") as f:
            return yaml.safe_load(f) or DEFAULT_RULES
    except (FileNotFoundError, ImportError):  # no config or no PyYAML in the runtime: defaults
        return DEFAULT_RULES


def _match(patterns, value) -> bool:
    return any(re.search(p, value or "", re.I) for p in patterns or [])


def classify(objs: list[dict], graph: dict, rules: dict, overrides: dict[str, str]) -> dict[str, dict]:
    ids = {o["id"] for o in objs}
    out = {}
    for o in objs:
        if o["id"] in overrides:
            out[o["id"]] = {"layer": overrides[o["id"]], "reason": "reviewer override"}
            continue
        decided = None
        for step in rules.get("precedence", ["name", "graph", "schema"]):
            if step == "name":
                for layer in LAYERS:
                    if _match(rules["layers"][layer].get("name_patterns"), o["name"]):
                        decided = (layer, f"name matches {layer} pattern")
                        break
            elif step == "schema":
                for layer in LAYERS:
                    if _match(rules["layers"][layer].get("schema_patterns"), o["schema"]):
                        decided = (layer, f"schema matches {layer} pattern")
                        break
            elif step == "graph":
                ups = [u for u in upstream(graph, o["id"]) if u in ids]
                downs = [d for d in downstream(graph, o["id"]) if d in ids]
                if o["type"] in ("TABLE", "STREAM") and not ups and o["type"] != "TASK":
                    decided = ("bronze", "source table (no in-slice upstream)")
                elif o["type"] in ("VIEW", "DYNAMIC_TABLE") and not downs:
                    decided = ("gold", "terminal consumer (no in-slice downstream)")
            if decided:
                break
        layer, reason = decided or (rules.get("default", "silver"), "default")
        out[o["id"]] = {"layer": layer, "reason": reason}
    return out


def _overrides(feedback) -> dict[str, str]:
    ovr = {}
    for fb in feedback:
        for txt in [fb.freeform, *fb.hard_constraints]:
            for m in _OVR.finditer(txt or ""):
                ovr[m.group(1).upper()] = m.group(2).lower()
    return ovr


def _min_lag_minutes(objs) -> int | None:
    best = None
    for o in objs:
        m = re.match(r"^\s*(\d+)\s*(second|minute|hour|day)s?\s*$", str(o.get("target_lag") or ""), re.I)
        if m:
            n = int(m.group(1)) * {"second": 1 / 60, "minute": 1, "hour": 60, "day": 1440}[m.group(2).lower()]
            best = n if best is None else min(best, n)
    return None if best is None else max(1, int(best))


def gen(ctx: RunContext, state: RunState, feedback) -> GenResult:
    objs = in_scope_objects(state)
    graph = need(state, "discovery")["graph"]
    cg = need(state, "cd_codegen")
    val = need(state, "validation")
    ovr = _overrides(feedback)
    assign = classify(objs, graph, load_rules(), ovr)
    layer_of = lambda nid: assign.get(nid, {}).get("layer") or "silver"
    cells = {l: [] for l in LAYERS}
    for f in cg["fragments"]:
        if f["kind"] == "dlt":
            nid = f["node_id"][4:] if f["node_id"].startswith("cdc:") else f["node_id"]
            cells[layer_of(nid)].append(f["code"])
        elif f["kind"] in ("ddl_table", "ddl_view"):
            cells[layer_of(f["node_id"])].append(f"# NODE-REF: {f['node_id']} | {f['obj_type']} -> {f['file']} (UC DDL, run outside the pipeline)")
    s = state.slice
    files = {k: v for k, v in cg["files"].items() if k != "pipeline/_all_nodes.py"}
    for l in LAYERS:
        body = "\n\n# COMMAND ----------\n\n".join(cells[l]) if cells[l] else "# (no datasets in this layer)"
        files[f"pipeline/{l}.py"] = (f"# Databricks notebook source\n# Layer: {l} | slice {s.slice_id} | run {state.run_id}\n"
                                     f"{DLT_HEADER}\n# COMMAND ----------\n\n{body}\n")
    targets = cg["targets"]
    cat = (s.target_catalog or (objs[0]["database"] if objs else "main")).lower()
    sch = (objs[0]["schema"] if objs else "default").lower()
    pipeline = {"name": f"{sch}_migrated_pipeline", "catalog": cat, "schema": sch, "serverless": True,
                "continuous": False, "channel": "CURRENT", "development": True,
                "libraries": [{"notebook": {"path": f"${{NOTEBOOK_ROOT}}/pipeline/{l}"}} for l in LAYERS],
                "configuration": {"spark.sql.session.timeZone": "UTC"}}
    files["pipeline/pipeline_settings.json"] = json.dumps(pipeline, indent=2)
    lag = _min_lag_minutes(objs)
    if lag:
        q = f"0 0/{lag} * * * ?" if lag < 60 and 60 % lag == 0 else "0 0 * * * ?"
        files["jobs/pipeline_refresh.json"] = json.dumps({
            "name": f"{sch}_pipeline_refresh", "description": f"Replaces dynamic table TARGET_LAG (min {lag} min)",
            "schedule": {"quartz_cron_expression": q, "timezone_id": "UTC", "pause_status": "PAUSED"},
            "tasks": [{"task_key": "refresh", "pipeline_task": {"pipeline_id": "${PIPELINE_ID}"}}]}, indent=2)
    files.update(val["files"])
    files["plan.md"] = need(state, "md_plan")["plan_md"]
    files["README.md"] = _readme(s, assign, targets, files)
    md = ["| Object | Layer | Reason |", "|---|---|---|"] + [f"| `{k}` | **{v['layer']}** | {v['reason']} |" for k, v in assign.items()]
    return GenResult(artifact={"files": files, "assignments": assign, "markdown": "\n".join(md)},
                     node_keys=tuple(assign))


def _readme(s, assign, targets, files) -> str:
    lines = [f"# {s.slice_id} -> Databricks", "", "Generated by the Snowflake -> Databricks migration app.", "",
             "## Deploy order", "1. `ddl/01_tables.sql` on a SQL warehouse", "2. Import `pipeline/*.py`, create the pipeline from `pipeline/pipeline_settings.json`",
             "3. `ddl/02_views.sql`", "4. Create jobs from `jobs/*.json` (created PAUSED)", "5. Run `validation/parity_*.sql` and compare", "",
             "## Placeholders", "- `${NOTEBOOK_ROOT}`: workspace folder where this bundle is imported",
             "- `${PIPELINE_ID}`: id of the created pipeline", "- `${SQL_WAREHOUSE_ID}`: SQL warehouse for SQL notebooks", "",
             "## Objects", "| Snowflake | Databricks | Layer |", "|---|---|---|"]
    lines += [f"| `{k}` | `{targets.get(k, '')}` | {v['layer']} |" for k, v in assign.items()]
    lines += ["", "## Files"] + [f"- `{n}`" for n in sorted(files)]
    return "\n".join(lines) + "\n"


def evaluate(ctx, state, g: GenResult):
    issues = []
    files = g.artifact["files"]
    for l in LAYERS:
        if f"pipeline/{l}.py" not in files:
            issues.append(Issue(f"CLS-{l}", Severity.ERROR, f"file:pipeline/{l}.py", "layer file missing"))
    for k, v in g.artifact["assignments"].items():
        if v["reason"] == "default":
            issues.append(Issue(f"CLS-DEF-{len(issues)}", Severity.INFO, f"node:{k}", f"no rule matched; defaulted to {v['layer']}",
                                f"override with: layer: {k} = gold"))
    return verdict_from(issues, 0.9, "3 layer files assembled")


def save(ctx, g: GenResult, it: int) -> None:
    for n, t in g.artifact["files"].items():
        if n.startswith("pipeline/") or n == "README.md":
            ctx.put(n, t, it)


def finalize(ctx, state, g: GenResult, e) -> dict:
    return {"files": g.artifact["files"], "assignments": g.artifact["assignments"], "markdown": g.artifact["markdown"]}


def bundle_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(files):
            z.writestr(n, files[n])
    return buf.getvalue()


node = build_gen_eval_hitl(STAGE, gen, evaluate, finalize, save, max_iters=1)
