"""Agent 3 - discovery: nodes/edges lineage graph + CDC patterns. HITL structural confirm.

Reviewer can prune scope with freeform lines:  exclude: DB.SCHEMA.OBJECT
"""
from __future__ import annotations

import json
import re

from agents.common import mapper_for, need
from converters.graph_model import build_graph, topo_order
from core.graph import RunContext
from core.nodes import build_gen_eval_hitl, verdict_from
from core.schemas import GenResult, Issue, Severity
from core.state import RunState

STAGE = "discovery"
_EXCL = re.compile(r"^\s*exclude\s*:\s*(\S+)\s*$", re.I | re.M)


def _excluded(feedback) -> list[str]:
    out = []
    for fb in feedback:
        for txt in [fb.freeform, *fb.hard_constraints]:
            out += [m.group(1).upper() for m in _EXCL.finditer(txt or "")]
    return list(dict.fromkeys(out))


def gen(ctx: RunContext, state: RunState, feedback) -> GenResult:
    objs = need(state, "standardization")["objects"]
    excl = _excluded(feedback)
    scope = [o for o in objs if o["id"] not in excl]
    g = build_graph(scope, mapper_for(state))
    g["order"] = topo_order(g)
    md = [f"### Discovery: {len(scope)} objects, {len(g['edges'])} edges, {len(g['patterns'])} CDC patterns", "",
          "| Object | Type | Upstream |", "|---|---|---|"]
    for nid in g["order"]:
        n = next(x for x in g["nodes"] if x["id"] == nid)
        ups = [f"{e['src'].split('.')[-1]} ({e['kind']})" for e in g["edges"] if e["dst"] == nid]
        md.append(f"| `{nid}` | {n['type']}{'' if n['in_scope'] else ' (outside slice)'} | {', '.join(ups) or '-'} |")
    for p in g["patterns"]:
        md.append(f"\n**CDC pattern** `{p['stream']}` + `{p['task']}` MERGE -> `{p['target']}` keys={p['keys']} "
                  f"-> one DLT `apply_changes`{'' if p['complete'] else ' (incomplete - needs AI slot)'}")
    if excl:
        md.append(f"\nExcluded by reviewer: {', '.join(excl)}")
    return GenResult(artifact={"graph": g, "markdown": "\n".join(md), "excluded": excl},
                     node_keys=tuple(n["id"] for n in g["nodes"]))


def evaluate(ctx: RunContext, state: RunState, gr: GenResult):
    g = gr.artifact["graph"]
    issues = []
    for n in g["nodes"]:
        if not n["in_scope"]:
            issues.append(Issue(f"DISC-EXT-{len(issues)}", Severity.WARN, f"node:{n['id']}",
                                "referenced object outside the slice; it must already exist in Databricks or be added",
                                "add it to the slice or map it to an existing UC table"))
    order_pos = {nid: i for i, nid in enumerate(g["order"])}
    for e in g["edges"]:
        if order_pos.get(e["src"], 0) > order_pos.get(e["dst"], 0):
            issues.append(Issue(f"DISC-CYC-{len(issues)}", Severity.ERROR, f"node:{e['dst']}", f"cycle via {e['src']}"))
    for p in g["patterns"]:
        if not p["complete"]:
            issues.append(Issue(f"DISC-CDC-{len(issues)}", Severity.WARN, f"node:{p['task']}",
                                "MERGE keys/columns not fully parsed; codegen will use AI slots"))
    collapsed = {p["stream"] for p in g["patterns"]}
    for n in g["nodes"]:
        if n["type"] == "STREAM" and n["id"] not in collapsed and not any(e["src"] == n["id"] for e in g["edges"]):
            issues.append(Issue(f"DISC-STRM-{len(issues)}", Severity.INFO, f"node:{n['id']}", "stream has no consumer in the slice"))
    return verdict_from(issues, 0.95, gr.artifact["markdown"].splitlines()[0])


def save(ctx, gr: GenResult, it: int) -> None:
    ctx.put("graph.json", json.dumps(gr.artifact["graph"], indent=2), it)
    ctx.put("discovery.md", gr.artifact["markdown"], it)


def finalize(ctx, state, gr: GenResult, e) -> dict:
    return {"graph": gr.artifact["graph"], "excluded": gr.artifact["excluded"], "markdown": gr.artifact["markdown"],
            "issues": [i.to_dict() for i in e.issues]}


node = build_gen_eval_hitl(STAGE, gen, evaluate, finalize, save, max_iters=1)
