"""Agent 5 - cd_codegen: object model -> MapperRegistry (MAPPED/HYBRID/AI) -> __PENDING_n__ slots
-> translator fan-out (merge-by-index) -> node-marked DLT Python / UC SQL / notebooks / Job JSON.
HITL per issue: accepted issues, freeform notes and hard constraints steer the translator.
"""
from __future__ import annotations

import hashlib
import json
import threading

from agents.cd_codegen.mapping import (PENDING_RX, REGISTRY, Fragment, MapCtx, SlotAllocator,
                                       build_jobs, map_cdc)
from agents.common import in_scope_objects, mapper_for, need
from converters.dialect import snowflake_residue
from converters.graph_model import topo_order
from core import llm
from core.concurrency import map_ordered
from core.graph import RunContext
from core.nodes import build_gen_eval_hitl, feedback_notes, issues_for
from core.prompts import PromptParts, assemble, post_check, strip_fences
from core.schemas import EvalResult, GenResult, Issue, Origin, Severity, Verdict
from core.state import RunState
from judges import rules

STAGE = "cd_codegen"

DLT_HEADER = "import dlt\nfrom pyspark.sql import functions as F\n"

SYSTEM = ("You are a senior data engineer migrating Snowflake workloads to Databricks (Unity Catalog, Delta, "
          "Lakeflow Declarative Pipelines / DLT, Jobs). Translate exactly what is asked, preserving semantics "
          "(NULL handling, time zones, integer vs decimal math, ordering). Use only functions that exist in "
          "Databricks SQL / PySpark - never PostgreSQL, SQL Server, MySQL or Oracle functions. Databricks SQL has "
          "no sleep/wait function: statements in a notebook already run sequentially, so drop SYSTEM$WAIT and "
          "similar waits and leave a SQL comment explaining why. Output ONLY the code for the requested target: "
          "no markdown fences, no explanations, no placeholders.")


def map_all(state: RunState) -> tuple[list[Fragment], SlotAllocator, MapCtx, dict[str, str], list[str]]:
    """Pure deterministic mapping (no model). Called twice for DET001."""
    objs = in_scope_objects(state)
    graph = need(state, "discovery")["graph"]
    m = mapper_for(state)
    by_id = {o["id"]: o for o in objs}
    cdc = {p["target"]: p for p in graph["patterns"] if p["stream"] in by_id and p["task"] in by_id}
    collapsed = {}
    for p in cdc.values():
        collapsed[p["stream"]] = p["id"]
        collapsed[p["task"]] = p["id"]
    cdf = {o["source"] for o in objs if o["type"] == "STREAM"}
    ctx = MapCtx(mapper=m, slots=SlotAllocator(), objects=by_id, graph=graph, cdc=cdc, collapsed=collapsed, cdf_sources=cdf)
    frags: list[Fragment] = []
    order = [i for i in topo_order(graph) if i in by_id]
    for oid in order:
        o = by_id[oid]
        frags += REGISTRY[o["type"]](o, ctx)
        if oid in cdc:
            frags += map_cdc(cdc[oid], ctx)
    for p in cdc.values():  # CDC whose target table is outside the slice
        if p["target"] not in by_id:
            frags += map_cdc(p, ctx)
    job_files, job_notes = build_jobs(by_id, [f for f in frags if f.obj_type == "TASK"], ctx)
    return frags, ctx.slots, ctx, job_files, job_notes


def _name_map(ctx: MapCtx) -> str:
    return "\n".join(f"{i} -> {ctx.mapper.target(i)}" for i in sorted(ctx.objects))


def _translate(rctx: RunContext, slot, name_map: str, notes: list[str], hard: list[str]) -> dict:
    task = (f"Translate the following {slot.source_lang} into {slot.target}.\n"
            f"Node: {slot.node_id}\n{slot.instructions}\n\nSOURCE:\n{slot.source}")
    msgs = assemble(PromptParts(system=SYSTEM, task=task, context=f"NAME MAP (Snowflake -> Databricks):\n{name_map}",
                                feedback=notes, hard_constraints=hard))
    res = llm.complete(rctx, "translator", msgs, stage=STAGE)
    code = strip_fences(res.text)
    if slot.kind in ("sql_query", "task_body"):  # the scaffold appends the terminator itself
        code = code.rstrip().rstrip(";").rstrip()
    return {"code": code, "model": res.model, "violations": post_check(code, hard),
            "residue": snowflake_residue(code)}


def gen(ctx: RunContext, state: RunState, feedback) -> GenResult:
    frags, slots, mctx, job_files, job_notes = map_all(state)
    notes, hard = feedback_notes(feedback, issues_for(state, STAGE))
    fills: list[dict | None] = [None] * len(slots.slots)
    if slots.slots and ctx.llm_enabled:
        ctx.progress(f"Cortex is translating {len(slots.slots)} AI slot(s) the rules could not convert")
        nm = _name_map(mctx)
        done, lock, total = [0], threading.Lock(), len(slots.slots)

        def one(s):
            r = _translate(ctx, s, nm, notes, hard)
            with lock:
                done[0] += 1
                ctx.progress(f"Cortex translated {done[0]}/{total}: {s.node_id} ({s.kind})")
            return r

        fills = map_ordered("translate", one, slots.slots,
                            on_error=lambda i, e: {"error": f"{type(e).__name__}: {e}"})
    elif slots.slots:
        ctx.progress(f"{len(slots.slots)} AI slot(s) left unfilled (Cortex is off)")
    slot_report = []
    for s, f in zip(slots.slots, fills):
        rep = {"idx": s.idx, "node_id": s.node_id, "kind": s.kind, "status": "pending"}
        if f and f.get("code"):
            rep.update(status="filled", model=f["model"], violations=f["violations"], residue=f["residue"])
        elif f and f.get("error"):
            rep.update(status="error", error=f["error"])
        slot_report.append(rep)

    def fill(text: str) -> str:
        return PENDING_RX.sub(lambda m: (fills[int(m.group(1))] or {}).get("code") or m.group(0), text)

    files: dict[str, str] = {}
    dlt = [f for f in frags if f.kind == "dlt"]
    if dlt:
        files["pipeline/_all_nodes.py"] = "# Databricks notebook source\n" + DLT_HEADER + "\n" + \
            "\n\n# COMMAND ----------\n\n".join(fill(f.code) for f in dlt) + "\n"
    for kind, name in (("ddl_table", "ddl/01_tables.sql"), ("ddl_view", "ddl/02_views.sql")):
        parts = [fill(f.code) for f in frags if f.kind == kind]
        if parts:
            files[name] = "\n\n".join(parts) + "\n"
    for f in frags:
        if f.kind == "notebook":
            files[f.file] = fill(f.code) + "\n"
    files.update(job_files)
    frag_dicts = []
    for f in frags:
        d = f.to_dict()
        d["code"] = fill(f.code)
        frag_dicts.append(d)
    if job_notes:
        frag_dicts.append(Fragment("jobs", "JOB", "job", Origin.MAPPED.value, "jobs", "", notes=job_notes).to_dict())
    return GenResult(artifact={"files": files, "fragments": frag_dicts, "slots": slot_report,
                               "targets": {i: mctx.mapper.target(i) for i in mctx.objects}},
                     node_keys=tuple(f.node_id for f in frags),
                     meta={"slots_total": len(slot_report), "slots_filled": sum(r["status"] == "filled" for r in slot_report),
                           "origins": {o: sum(f.origin == o for f in frags) for o in ("MAPPED", "HYBRID", "AI")}})


def det_diff(state: RunState) -> list[str]:
    """DET001: re-run the deterministic mapper twice; MAPPED fragments must hash identically."""
    a, *_ = map_all(state)
    b, *_ = map_all(state)
    h = lambda fs: {f.node_id + f.kind: hashlib.sha256(f.code.encode()).hexdigest() for f in fs if f.origin == "MAPPED"}
    ha, hb = h(a), h(b)
    return sorted({k for k in ha.keys() | hb.keys() if ha.get(k) != hb.get(k)})


def judge_bundle(state: RunState, art: dict) -> dict:
    return {"objects": in_scope_objects(state), "graph": need(state, "discovery")["graph"],
            "fragments": art["fragments"], "files": art["files"], "targets": art["targets"],
            "det_diff": det_diff(state)}


def evaluate(ctx: RunContext, state: RunState, g: GenResult) -> EvalResult:
    issues = list(rules.run_all(judge_bundle(state, g.artifact)))
    for s in g.artifact["slots"]:
        for v in s.get("violations", []):
            issues.append(Issue(f"HC-{s['idx']}", Severity.ERROR, f"node:{s['node_id']}", v))
        if s["status"] == "error":
            issues.append(Issue(f"SLOT-{s['idx']}", Severity.ERROR, f"node:{s['node_id']}", f"translator failed: {s['error']}"))
    fixable = {f["node_id"] for f in g.artifact["fragments"] if f["origin"] in ("HYBRID", "AI")}
    errs = [i for i in issues if i.severity == Severity.ERROR]
    if any(i.ref.split(":", 1)[1] in fixable or i.id.startswith(("STR002", "SLOT", "HC")) for i in errs) and ctx.llm_enabled:
        verdict = Verdict.REVISE  # regeneration can fix model-produced parts
    elif errs:
        verdict = Verdict.FAIL
    else:
        verdict = Verdict.PASS
    warn = sum(i.severity == Severity.WARN for i in issues)
    conf = max(0.1, 1.0 - 0.15 * len(errs) - 0.03 * warn)
    m = g.meta
    return EvalResult(verdict=verdict, confidence=round(conf, 2), issues=tuple(issues),
                      summary=f"{len(g.node_keys)} fragments ({m['origins']}), slots {m['slots_filled']}/{m['slots_total']} filled, "
                              f"{len(errs)} errors, {warn} warnings")


def save(ctx: RunContext, g: GenResult, it: int) -> None:
    for name, text in g.artifact["files"].items():
        ctx.put(name, text, it)
    ctx.put("slots.json", json.dumps(g.artifact["slots"], indent=2), it)


def finalize(ctx, state, g: GenResult, e: EvalResult) -> dict:
    return {**g.artifact, "eval": e.to_dict(), "meta": g.meta}


node = build_gen_eval_hitl(STAGE, gen, evaluate, finalize, save, max_iters=3)
