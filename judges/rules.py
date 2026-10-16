"""Deterministic quality gates. NEVER call a model here.

Input bundle:
  objects   list[dict]      normalized Snowflake objects in scope
  graph     dict            discovery nodes/edges/patterns
  fragments list[dict]      codegen fragments (Fragment.to_dict())
  files     dict[str,str]   emitted files
  targets   dict[id,str]    Snowflake id -> Databricks name
  det_diff  list[str]       node ids whose MAPPED output changed on re-run (DET001)
"""
from __future__ import annotations

import ast
import json
import re

from converters.dialect import snowflake_residue, validate_databricks
from converters.ddl_parser import split_statements
from core.schemas import Issue, Severity

PENDING_RX = re.compile(r"__PENDING_\d+__")

RULES = {
    "STR001": "every in-scope object has a node marker in the output",
    "STR002": "no unresolved __PENDING_n__ slots",
    "STR003": "emitted Python / JSON / SQL is syntactically valid",
    "DET001": "MAPPED output is reproducible (same input -> identical output)",
    "DET002": "no Snowflake-only functions or metadata columns remain",
    "DET003": "no functions from other databases (PostgreSQL, SQL Server, MySQL, Oracle) that Databricks lacks",
    "SEM001": "column sets are preserved",
    "SEM004": "lossy type conversions are declared",
    "SEM005": "time-zone / session-sensitive functions are declared",
    "LIN001": "in-scope lineage edges are preserved in generated code",
    "LIN002": "no external writes into DLT-managed (apply_changes / pipeline) tables",
    "LIN003": "every procedure CALLed in the source is still invoked in the output",
    "POL001": "no role switches, destructive DDL, credentials or Snowflake warehouse names",
    "SNF001": "dynamic table TARGET_LAG mapped to a pipeline trigger",
    "SNF002": "task WHEN conditions reviewed",
    "SNF003": "task schedules converted to Quartz cron",
    "SRC001": "source definition is visible and complete",
}
_ERROR_HINTS = {"SRC001"}

_TZ_RX = re.compile(r"\b(CURRENT_TIMESTAMP|SYSDATE|LOCALTIMESTAMP|CURRENT_TIME|CONVERT_TIMEZONE|GETDATE|TIMESTAMP_LTZ)\b", re.I)
# Common non-Databricks functions models tend to emit (denylist, not exhaustive). Only constructs that do not
# exist in Databricks SQL; valid Databricks functions such as LEN, DATEPART, CHARINDEX, DECODE are not listed.
_FOREIGN_RX = re.compile(
    r"\b((?:PG_SLEEP(?:_FOR|_UNTIL)?|DBMS_LOCK\.SLEEP|DBMS_SESSION\.SLEEP|GETDATE|GETUTCDATE|SYSDATETIME|"
    r"STRING_TO_ARRAY|GENERATE_SERIES|TO_TSVECTOR|JSONB_\w+)\s*\(|WAITFOR\s+DELAY|ISNULL\s*\([^,()]+,|"
    r"SELECT\s+TOP\s+\d+|\bROWNUM\b)", re.I)
_POL = [
    (re.compile(r"\bUSE\s+(?:SECONDARY\s+)?ROLES?\b", re.I), "role switch"),
    (re.compile(r"\bACCOUNTADMIN\b|\bSECURITYADMIN\b", re.I), "privileged role reference"),
    (re.compile(r"\bDROP\s+(?:TABLE|SCHEMA|DATABASE|CATALOG|VIEW)\b", re.I), "destructive DDL"),
    (re.compile(r"\bGRANT\s+.+\s+TO\b", re.I), "grant statement"),
    (re.compile(r"\b(?:password|secret|token)\s*[=:]\s*['\"][^'\"]{4,}['\"]", re.I), "hard-coded credential"),
    (re.compile(r"\bdapi[0-9a-f]{32}\b"), "Databricks token"),
]


class _Ids:
    def __init__(self) -> None:
        self.n: dict[str, int] = {}

    def __call__(self, rule: str) -> str:
        self.n[rule] = self.n.get(rule, 0) + 1
        return f"{rule}-{self.n[rule]}"


def _code_lines(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not re.match(r"^\s*(--|#)", l))


def run_all(b: dict) -> list[Issue]:
    ids = _Ids()
    out: list[Issue] = []
    frags = b.get("fragments", [])
    files = b.get("files", {})
    all_code = "\n".join(files.values()) + "\n" + "\n".join(f.get("code", "") for f in frags) + \
        "\n" + "\n".join(json.dumps(f.get("job_task") or {}) for f in frags)
    by_node: dict[str, list[dict]] = {}
    for f in frags:
        by_node.setdefault(f["node_id"], []).append(f)
        for c in f.get("collapses", []):
            by_node.setdefault(c, []).append(f)

    # STR001
    for o in b.get("objects", []):
        if f"NODE: {o['id']} |" not in all_code:
            out.append(Issue(ids("STR001"), Severity.ERROR, f"node:{o['id']}", f"no node marker for {o['type']} {o['id']}",
                             "regenerate; mapper must emit a marker for every object"))
    # STR002
    for name, text in files.items():
        for m in PENDING_RX.finditer(text):
            out.append(Issue(ids("STR002"), Severity.ERROR, f"file:{name}", f"unresolved slot {m.group(0)}",
                             "regenerate the slot with the translator model or fill it manually"))
    # STR003
    for name, text in files.items():
        if name.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError as e:
                out.append(Issue(ids("STR003"), Severity.ERROR, f"line:{name}:{e.lineno or 0}", f"Python syntax error: {e.msg}"))
        elif name.endswith(".json"):
            try:
                json.loads(text)
            except ValueError as e:
                out.append(Issue(ids("STR003"), Severity.ERROR, f"file:{name}", f"invalid JSON: {e}"))
        elif name.endswith(".sql") and not PENDING_RX.search(text):
            for stmt in split_statements(text):
                code = _code_lines(stmt).strip()
                if not code:
                    continue
                err = validate_databricks(code)
                if err:
                    out.append(Issue(ids("STR003"), Severity.WARN, f"file:{name}", f"Databricks SQL parse warning: {err}",
                                     "verify statement on a Databricks SQL warehouse"))
    # DET001
    for nid in b.get("det_diff", []):
        out.append(Issue(ids("DET001"), Severity.ERROR, f"node:{nid}", "MAPPED output differs between two identical runs"))
    # DET002
    for name, text in files.items():
        for r in snowflake_residue(_code_lines(text)):
            out.append(Issue(ids("DET002"), Severity.ERROR, f"file:{name}", f"Snowflake-only construct remains: {r}",
                             "rewrite with the Databricks equivalent"))
    # DET003: functions from other databases (typical model mistakes)
    for name, text in files.items():
        if not name.endswith((".sql", ".py")):
            continue
        for m in _FOREIGN_RX.finditer(_code_lines(text)):
            out.append(Issue(ids("DET003"), Severity.ERROR, f"file:{name}",
                             f"not a Databricks function: {m.group(1).strip()}",
                             "use the Databricks SQL / PySpark equivalent (or drop waits; statements run sequentially)"))
    # SEM001: columns preserved
    for o in b.get("objects", []):
        code = "\n".join(f.get("code", "") for f in by_node.get(o["id"], []))
        if not code:
            continue
        names = [c["name"] for c in o.get("columns", [])] if o["type"] == "TABLE" else o.get("column_aliases", [])
        for c in names:
            if not re.search(rf"(?<![\w$]){re.escape(c)}(?![\w$])", code, re.I):
                out.append(Issue(ids("SEM001"), Severity.ERROR, f"node:{o['id']}", f"column {c} missing from generated code"))
    # SEM005
    for o in b.get("objects", []):
        src = o.get("query") or o.get("body") or ""
        hits = sorted({m.group(1).upper() for m in _TZ_RX.finditer(src)})
        if hits:
            out.append(Issue(ids("SEM005"), Severity.WARN, f"node:{o['id']}",
                             f"session/time-zone sensitive: {', '.join(hits)}; Snowflake session TZ vs spark.sql.session.timeZone",
                             "set spark.sql.session.timeZone to the Snowflake account TIMEZONE"))
    # LIN001: upstream in-scope references appear in the consumer's code
    targets = b.get("targets", {})
    in_scope = {o["id"] for o in b.get("objects", [])}
    for e in b.get("graph", {}).get("edges", []):
        if e["kind"] not in ("reads", "cdc_of") or e["src"] not in in_scope or e["dst"] not in in_scope:
            continue
        code = "\n".join(f.get("code", "") for f in by_node.get(e["dst"], []))
        tgt = targets.get(e["src"], "")
        if code and tgt and tgt.lower() not in code.lower() and not PENDING_RX.search(code):
            out.append(Issue(ids("LIN001"), Severity.WARN, f"node:{e['dst']}",
                             f"upstream {e['src']} ({tgt}) not referenced in generated code"))
    # LIN002: DLT owns CDC targets and dynamic tables; outside writers break the pipeline
    dlt_owned = {f["node_id"] for f in frags if f.get("kind") == "dlt" and f.get("obj_type") in ("TABLE", "DYNAMIC_TABLE")}
    collapsed = {c for f in frags for c in f.get("collapses", [])}
    for e in b.get("graph", {}).get("edges", []):
        if e["kind"] == "writes" and e["dst"] in dlt_owned and e["src"] not in collapsed:
            out.append(Issue(ids("LIN002"), Severity.ERROR, f"node:{e['src']}",
                             f"writes to {e['dst']}, which is a DLT-managed table; DML from outside the pipeline fails",
                             "move this logic into the pipeline (e.g. an append flow) or target a separate table"))
    # LIN003: every procedure the source CALLs is still invoked in the generated code / job
    for e in b.get("graph", {}).get("edges", []):
        if e["kind"] != "calls" or e["src"] not in in_scope:
            continue
        frs = by_node.get(e["src"], [])
        code = "\n".join(f.get("code", "") + json.dumps(f.get("job_task") or {}) for f in frs)
        callee = e["dst"].split(".")[-1]
        # "_" may precede the name: converted procedures are invoked as notebooks named proc_<name>
        if frs and not PENDING_RX.search(code) and not re.search(rf"(?<![A-Za-z0-9$]){re.escape(callee)}(?![\w$])", code, re.I):
            out.append(Issue(ids("LIN003"), Severity.ERROR, f"node:{e['src']}",
                             f"source calls {e['dst']} but the generated code never invokes it",
                             "regenerate, or add the call manually"))
    # POL001
    wh = {o.get("warehouse") for o in b.get("objects", []) if o.get("warehouse")}
    for name, text in files.items():
        code = _code_lines(text)
        for rx, what in _POL:
            if rx.search(code):
                out.append(Issue(ids("POL001"), Severity.ERROR, f"file:{name}", f"policy violation: {what}"))
        for w in wh:
            if re.search(rf"\b{re.escape(w)}\b", code, re.I):
                out.append(Issue(ids("POL001"), Severity.ERROR, f"file:{name}", f"Snowflake warehouse {w} referenced"))
    # mapper-declared notes (SEM001/SEM004/SNF*/LIN001/INFO)
    for f in frags:
        for n in f.get("notes", []):
            hint, _, msg = n.partition("|")
            sev = Severity.INFO if hint in ("INFO", "SNF001") else Severity.ERROR if hint in _ERROR_HINTS else Severity.WARN
            rule = hint if hint in RULES else "INFO"
            out.append(Issue(ids(rule), sev, f"node:{f['node_id']}", msg))
    return out
