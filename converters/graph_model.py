"""Object model -> dependency graph (nodes/edges) + stream/task/MERGE CDC pattern detection."""
from __future__ import annotations

import re

from converters.dialect import NameMapper, referenced_tables, regex_tables
from converters.sqlscan import FQN, split_fqn, split_top

_WRITE_RX = re.compile(r"\b(?:INSERT\s+(?:OVERWRITE\s+)?INTO|MERGE\s+INTO|UPDATE(?!\s+SET\b)|DELETE\s+FROM|TRUNCATE\s+(?:TABLE\s+)?(?:IF\s+EXISTS\s+)?|COPY\s+INTO)\s+(" + FQN + ")", re.I)
_CALL_RX = re.compile(r"\bCALL\s+(" + FQN + r")\s*\(", re.I)


def writes_of(sql: str, m: NameMapper) -> list[str]:
    return list(dict.fromkeys(m.resolve(split_fqn(x.group(1))) for x in _WRITE_RX.finditer(sql)))


def calls_of(sql: str, m: NameMapper) -> list[str]:
    """Procedures invoked with CALL; Snowflake system functions (SYSTEM$...) are not dependencies."""
    return list(dict.fromkeys(m.resolve(split_fqn(x.group(1))) for x in _CALL_RX.finditer(sql)
                              if not split_fqn(x.group(1))[-1].upper().startswith("SYSTEM$")))


def build_graph(objects: list[dict], mapper: NameMapper) -> dict:
    by_id = {o["id"]: o for o in objects}
    edges: list[dict] = []

    def add(src, dst, kind):
        if src != dst and not any(e["src"] == src and e["dst"] == dst and e["kind"] == kind for e in edges):
            edges.append({"src": src, "dst": dst, "kind": kind})

    for o in objects:
        t, oid = o["type"], o["id"]
        if t in ("VIEW", "DYNAMIC_TABLE") or (t == "TABLE" and o.get("query")):
            for r in referenced_tables(o["query"], mapper):
                add(r, oid, "reads")
        elif t == "STREAM":
            add(o["source"], oid, "cdc_of")
        elif t == "TASK":
            for p in o.get("after", []):
                add(p, oid, "precedes")
            body = o.get("body", "")
            writes = set(writes_of(body, mapper))
            for c in calls_of(body, mapper):
                add(oid, c, "calls")
            for r in regex_tables(body, mapper):
                if r not in writes:
                    add(r, oid, "reads")
            for w in writes:
                add(oid, w, "writes")
        elif t == "PROCEDURE":
            body = o.get("body", "")
            if o.get("language") == "SQL":
                writes = set(writes_of(body, mapper))
                for r in regex_tables(body, mapper):
                    if r not in writes:
                        add(r, oid, "reads")
                for w in writes:
                    add(oid, w, "writes")
                for c in calls_of(body, mapper):
                    add(oid, c, "calls")
    nodes = [{"id": o["id"], "type": o["type"], "name": o["name"], "in_scope": True} for o in objects]
    known = set(by_id)
    for e in edges:
        for end in (e["src"], e["dst"]):
            if end not in known:
                nodes.append({"id": end, "type": "EXTERNAL", "name": end.split(".")[-1], "in_scope": False})
                known.add(end)
    return {"nodes": nodes, "edges": edges, "patterns": detect_cdc(objects, mapper)}


_MERGE_RX = re.compile(r"^\s*MERGE\s+INTO\s+(?P<tgt>" + FQN + r")(?:\s+(?:AS\s+)?(?P<ta>[A-Za-z_]\w*))?\s+USING\s+(?P<src>" + FQN +
                       r")(?:\s+(?:AS\s+)?(?P<sa>[A-Za-z_]\w*))?\s+ON\s+(?P<on>.+?)\s+WHEN\b", re.I | re.S)
_INSERT_RX = re.compile(r"\bTHEN\s+INSERT\s*\((?P<cols>[^)]*)\)\s*VALUES\s*\((?P<vals>.*)\)\s*;?\s*$", re.I | re.S)


def detect_cdc(objects: list[dict], mapper: NameMapper) -> list[dict]:
    """TASK body = MERGE INTO <target> USING <stream> ... -> one DLT apply_changes node."""
    streams = {o["id"]: o for o in objects if o["type"] == "STREAM"}
    out = []
    for o in objects:
        if o["type"] != "TASK":
            continue
        m = _MERGE_RX.match(o.get("body", ""))
        if not m:
            continue
        src = mapper.resolve(split_fqn(m.group("src")))
        if src not in streams:
            continue
        tgt = mapper.resolve(split_fqn(m.group("tgt")))
        ta, sa = (m.group("ta") or "").upper(), (m.group("sa") or "").upper()
        keys = []
        for cond in re.split(r"\bAND\b", m.group("on"), flags=re.I):
            eq = re.match(r"\s*(?:(\w+)\.)?(\w+)\s*=\s*(?:(\w+)\.)?(\w+)\s*$", cond.strip())
            if not eq:
                keys = []
                break
            a_al, a_col, b_al, b_col = [(x or "").upper() for x in eq.groups()]
            keys.append(a_col if a_al == ta or b_al == sa else b_col)
        body = o["body"]
        ins = _INSERT_RX.search(body)
        cols, vals = [], []
        if ins:
            cols = [c.strip().strip('"') for c in split_top(ins.group("cols"))]
            vals = [re.sub(rf"\b{re.escape(sa or 'S')}\.", "", v.strip(), flags=re.I) for v in split_top(ins.group("vals"))]
        out.append({
            "id": f"cdc:{tgt}", "task": o["id"], "stream": src, "source": streams[src]["source"],
            "target": tgt, "keys": keys, "columns": cols, "values": vals,
            "has_delete": re.search(r"\bTHEN\s+DELETE\b", body, re.I) is not None,
            "append_only": streams[src].get("append_only", False),
            "complete": bool(keys) and bool(cols) and len(cols) == len(vals),
        })
    return out


def upstream(graph: dict, node_id: str) -> list[str]:
    return [e["src"] for e in graph["edges"] if e["dst"] == node_id]


def downstream(graph: dict, node_id: str) -> list[str]:
    return [e["dst"] for e in graph["edges"] if e["src"] == node_id]


def topo_order(graph: dict) -> list[str]:
    ids = [n["id"] for n in graph["nodes"]]
    indeg = {i: 0 for i in ids}
    for e in graph["edges"]:
        indeg[e["dst"]] = indeg.get(e["dst"], 0) + 1
    ready = sorted(i for i, d in indeg.items() if d == 0)
    out = []
    while ready:
        n = ready.pop(0)
        out.append(n)
        for e in graph["edges"]:
            if e["src"] == n:
                indeg[e["dst"]] -= 1
                if indeg[e["dst"]] == 0:
                    ready.append(e["dst"])
                    ready.sort()
    rest = [i for i in ids if i not in out]  # cycles: append deterministically
    return out + sorted(rest)
