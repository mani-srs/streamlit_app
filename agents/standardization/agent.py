"""Agent 2 - standardization: raw DDL -> normalized JSON object model + deterministic validator."""
from __future__ import annotations

import json

from agents.common import default_ns, need
from converters.ddl_parser import DdlParseError, parse_ddl
from core.graph import RunContext
from core.nodes import build_gen_eval_hitl, verdict_from
from core.schemas import GenResult, Issue, Severity
from core.state import RunState

STAGE = "standardization"

REQUIRED = {
    "TABLE": ["columns"], "VIEW": ["query"], "DYNAMIC_TABLE": ["query", "target_lag"],
    "STREAM": ["source"], "TASK": ["body"], "PROCEDURE": ["body", "language"],
}


def gen(ctx: RunContext, state: RunState, feedback) -> GenResult:
    raw = need(state, "connection")["raw"]
    db, sch = (state.slice.database.upper(), state.slice.schema.upper()) if state.slice.database else ("", "")
    objs, errors = [], []
    for i, r in enumerate(raw):
        try:
            o = parse_ddl(r["ddl"], db, sch)
            o["meta"] = r.get("meta", {})
            objs.append(o)
        except (DdlParseError, ValueError) as e:
            errors.append({"index": i, "error": str(e), "ddl_head": r["ddl"][:200]})
    return GenResult(artifact={"objects": objs, "parse_errors": errors}, node_keys=tuple(o["id"] for o in objs))


def evaluate(ctx: RunContext, state: RunState, g: GenResult):
    issues = []
    seen = set()
    for o in g.artifact["objects"]:
        if o["id"] in seen:
            issues.append(Issue(f"STD-DUP-{len(issues)}", Severity.ERROR, f"object:{o['id']}", "duplicate object"))
        seen.add(o["id"])
        if not o["database"] or not o["schema"]:
            issues.append(Issue(f"STD-NS-{len(issues)}", Severity.WARN, f"object:{o['id']}",
                                "unqualified name; defaulted namespace", "paste fully-qualified DDL or pick a schema"))
        for f in REQUIRED.get(o["type"], []):
            if not o.get(f):
                hint = ("GET_DDL shows procedure bodies only to the owning role"
                        if o["type"] == "PROCEDURE" and f == "body" else "")
                issues.append(Issue(f"STD-REQ-{len(issues)}", Severity.ERROR, f"object:{o['id']}",
                                    f"missing required field {f}", hint))
    for e in g.artifact["parse_errors"]:
        issues.append(Issue(f"STD-PARSE-{e['index']}", Severity.WARN, f"object:stmt#{e['index']}",
                            f"skipped unparseable statement: {e['error']}", e["ddl_head"]))
    if not g.artifact["objects"]:
        issues.append(Issue("STD-EMPTY", Severity.ERROR, "object:slice", "no supported objects found"))
    return verdict_from(issues, 1.0, f"{len(g.artifact['objects'])} objects normalized")


def save(ctx: RunContext, g: GenResult, it: int) -> None:
    ctx.put("object_model.json", json.dumps(g.artifact["objects"], indent=2, default=str), it)


def finalize(ctx, state: RunState, g: GenResult, e) -> dict:
    if not g.artifact["objects"]:
        raise RuntimeError("standardization produced no objects: " + "; ".join(i.message for i in e.issues))
    objs = g.artifact["objects"]
    db, sch = default_ns(state)
    for o in objs:  # unqualified pasted DDL: fill namespace
        o["database"] = o["database"] or db
        o["schema"] = o["schema"] or sch
        o["id"] = f"{o['database']}.{o['schema']}.{o['name']}"
    return {"objects": objs, "issues": [i.to_dict() for i in e.issues]}


node = build_gen_eval_hitl(STAGE, gen, evaluate, finalize, save, max_iters=1)
