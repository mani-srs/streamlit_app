"""Agent 1 - connection: SHOW + GET_DDL -> raw artifacts. No AI."""
from __future__ import annotations

import threading

from converters.ddl_parser import split_statements
from core.concurrency import map_ordered
from core.db import Db, qfqn
from core.graph import RunContext
from core.schemas import ContractViolation
from core.state import RunState

STAGE = "connection"

# UI type -> (SHOW noun, GET_DDL type)
KINDS = {
    "TABLE": ("TABLES", "TABLE"),
    "DYNAMIC_TABLE": ("DYNAMIC TABLES", "TABLE"),
    "VIEW": ("VIEWS", "VIEW"),
    "STREAM": ("STREAMS", "STREAM"),
    "TASK": ("TASKS", "TASK"),
    "PROCEDURE": ("USER PROCEDURES", "PROCEDURE"),
}


def list_objects(db: Db, database: str, schema: str) -> list[dict]:
    """Objects in one schema, for the New Run picker."""
    where = qfqn(database, schema)
    out = []
    for kind, (noun, _) in KINDS.items():
        try:
            rows = db.query(f"SHOW {noun} IN SCHEMA {where}")
        except Exception:
            if kind == "PROCEDURE":  # older accounts: SHOW PROCEDURES
                rows = [r for r in db.query(f"SHOW PROCEDURES IN SCHEMA {where}") if r.get("is_builtin") != "Y"]
            else:
                raise
        for r in rows:
            if kind == "TABLE" and str(r.get("is_dynamic", "N")).upper() == "Y":
                continue
            item = {"type": kind, "name": r["name"], "rows": r.get("rows"), "bytes": r.get("bytes"),
                    "comment": r.get("comment")}
            if kind == "PROCEDURE":
                # arguments look like "SP(NUMBER, VARCHAR) RETURN VARCHAR"
                sig = str(r.get("arguments", "")).split(" RETURN ")[0]
                item["signature"] = sig[sig.find("("):] if "(" in sig else "()"
                item["name"] = f"{r['name']}{item['signature']}"
            out.append(item)
    return out


def list_schemas(db: Db) -> list[tuple[str, str]]:
    rows = db.query("SHOW SCHEMAS IN ACCOUNT")
    return sorted((r["database_name"], r["name"]) for r in rows if r["name"] != "INFORMATION_SCHEMA")


def _get_ddl(db: Db, database: str, schema: str, item: dict) -> dict:
    kind = item["type"]
    ddl_type = KINDS[kind][1]
    name = item["name"]
    if kind == "PROCEDURE":
        base, sig = name.split("(", 1)
        fqn = f"{qfqn(database, schema, base)}({sig}"
    else:
        fqn = qfqn(database, schema, name)
    rows = db.query("SELECT GET_DDL(?, ?, TRUE) AS ddl", [ddl_type, fqn])
    return {"type": kind, "name": name, "ddl": rows[0]["ddl"], "meta": {k: item.get(k) for k in ("rows", "bytes")}}


def node(ctx: RunContext, state: RunState) -> dict:
    s = state.slice
    if s.pasted_ddl.strip():
        raw = [{"type": None, "name": None, "ddl": d, "meta": {}} for d in split_statements(s.pasted_ddl)]
        source = "pasted"
    else:
        if ctx.db is None:
            raise ContractViolation("no Snowflake connection and no pasted DDL")
        if not s.objects:
            raise ContractViolation("no objects selected")
        ctx.progress(f"GET_DDL for {len(s.objects)} objects in {s.database}.{s.schema}")
        done, lock, total = [0], threading.Lock(), len(s.objects)

        def fetch(it):
            r = _get_ddl(ctx.db, s.database, s.schema, it)
            with lock:
                done[0] += 1
                ctx.progress(f"GET_DDL {done[0]}/{total}: {it['type']} {it['name']}")
            return r

        raw = map_ordered("introspect", fetch, s.objects)
        source = "live"
    for i, r in enumerate(raw):
        ctx.put(f"raw/{i:03d}_{(r['name'] or 'stmt').split('(')[0]}.sql", r["ddl"])
    return {"source": source, "raw": raw}
