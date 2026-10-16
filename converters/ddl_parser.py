"""Snowflake DDL (GET_DDL output or hand-written) -> normalized JSON object model.

Supported: TABLE, VIEW (incl. SECURE / MATERIALIZED), DYNAMIC TABLE, STREAM, TASK, PROCEDURE.
Every object dict carries: id (FQN upper), type, database, schema, name, ddl, plus type fields.
"""
from __future__ import annotations

import re

from converters.sqlscan import (FQN, find_top_kw, matching_paren, split_fqn, split_top, sql_string,
                                strip_comments)

TYPES = ("TABLE", "VIEW", "DYNAMIC_TABLE", "STREAM", "TASK", "PROCEDURE")

_HEADER = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?P<mods>(?:(?:SECURE|TRANSIENT|TEMPORARY|TEMP|LOCAL|GLOBAL|VOLATILE|RECURSIVE|MATERIALIZED|HYBRID)\s+)*)"
    r"(?P<kind>DYNAMIC\s+TABLE|TABLE|VIEW|STREAM|TASK|PROCEDURE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>" + FQN + r")", re.I | re.S)


class DdlParseError(ValueError):
    pass


_WORD = re.compile(r"[A-Za-z_][\w$]*")
_END_QUAL = re.compile(r"\s+(IF|LOOP|FOR|WHILE|REPEAT|CASE)\b", re.I)
_NOT_BLOCK = re.compile(r"\s+(TRANSACTION|WORK)\b|\s*;", re.I)


def split_statements(text: str) -> list[str]:
    """Split a script on top-level ';'.

    Respects quotes, $$ bodies, comments and parentheses, and Snowflake Scripting blocks:
    ';' inside DECLARE ... BEGIN ... END (and CASE ... END) does not end a statement.
    """
    stmts, last = [], 0
    depth = 0  # BEGIN/CASE block depth
    declare_pending = False
    prev_word_end = -1
    for i, d in _scan_all(text):
        c = text[i]
        if d == 0 and (c.isalpha() or c == "_") and i > prev_word_end and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$")):
            m = _WORD.match(text, i)
            w, prev_word_end = m.group(0).upper(), m.end() - 1
            if w == "DECLARE" and depth == 0:
                declare_pending, depth = True, 1
            elif w == "BEGIN" and not _NOT_BLOCK.match(text, m.end()):
                if declare_pending:
                    declare_pending = False  # DECLARE already opened this block
                else:
                    depth += 1
            elif w == "CASE":
                depth += 1
            elif w == "END" and not _END_QUAL.match(text, m.end()):
                depth = max(0, depth - 1)
        elif c == ";" and d == 0 and depth == 0:
            stmts.append(text[last:i])
            last = i + 1
    stmts.append(text[last:])
    return [s.strip() for s in stmts if strip_comments(s).strip()]


def _scan_all(text):
    from converters.sqlscan import scan
    return scan(text)


def _prop(text: str, key: str) -> str | None:
    m = re.search(rf"\b{key}\s*=\s*('(?:[^']|'')*'|\$\$.*?\$\$|[^\s,)]+)", text, re.I | re.S)
    return sql_string(m.group(1)) if m else None


def _resolve(parts: list[str], db: str, schema: str) -> tuple[str, str, str]:
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return db, parts[0], parts[1]
    return db, schema, parts[0]


def parse_ddl(ddl: str, default_db: str = "", default_schema: str = "") -> dict:
    raw = ddl.strip().rstrip(";")
    text = strip_comments(raw).strip()
    m = _HEADER.match(text)
    if not m:
        raise DdlParseError(f"not a supported CREATE statement: {text[:80]!r}")
    kind = re.sub(r"\s+", "_", m.group("kind").upper())
    mods = m.group("mods").upper().split()
    if kind == "VIEW" and "MATERIALIZED" in mods:
        kind = "VIEW"  # materialized views convert like views; flagged below
    db, sch, name = _resolve(split_fqn(m.group("name")), default_db.upper(), default_schema.upper())
    obj = {"id": ".".join(p for p in (db, sch, name) if p), "type": kind, "database": db, "schema": sch,
           "name": name, "ddl": raw, "modifiers": mods, "comment": None}
    rest = text[m.end():]
    fn = {"TABLE": _table, "DYNAMIC_TABLE": _dynamic_table, "VIEW": _view, "STREAM": _stream,
          "TASK": _task, "PROCEDURE": _procedure}[kind]
    fn(obj, rest, raw)
    return obj


def _columns(body: str) -> tuple[list[dict], list[str]]:
    cols, constraints = [], []
    for part in split_top(body):
        head = part.split(None, 1)[0].upper() if part.split() else ""
        if head in ("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"):
            constraints.append(part)
            continue
        cm = re.match(r'\s*("(?:[^"]|"")+"|[\w$]+)\s+(.*)$', part, re.S)
        if not cm:
            continue
        cname = cm.group(1)
        rest = cm.group(2).strip()
        # data type: multi-word forms first, else one word with optional (args)
        tm = re.match(r"(?:DOUBLE\s+PRECISION|CHARACTER\s+VARYING|TIMESTAMP\s+WITH(?:OUT)?\s+(?:LOCAL\s+)?TIME\s+ZONE)"
                      r"(?:\s*\([^)]*\))?", rest, re.I) or re.match(r"[A-Za-z_]\w*(?:\s*\([^)]*\))?", rest)
        dtype = tm.group(0) if tm else rest.split()[0]
        attr = rest[len(dtype):]
        au = attr.upper()
        default = None
        dm = re.search(r"\bDEFAULT\s+(.+?)(?=\s+(?:NOT\s+NULL|NULL|COMMENT|COLLATE|PRIMARY|UNIQUE|REFERENCES|WITH)\b|$)", attr, re.I | re.S)
        if dm:
            default = dm.group(1).strip()
        ident = re.search(r"\b(AUTOINCREMENT|IDENTITY)\b", au) is not None
        com = re.search(r"\bCOMMENT\s+'((?:[^']|'')*)'", attr, re.I)
        coll = re.search(r"\bCOLLATE\s+'([^']*)'", attr, re.I)
        cols.append({
            "name": cname.strip('"').replace('""', '"') if cname.startswith('"') else cname.upper(),
            "type": re.sub(r"\s+", " ", dtype.strip()).upper(),
            "nullable": "NOT NULL" not in au,
            "default": default, "identity": ident,
            "comment": com.group(1).replace("''", "'") if com else None,
            "collation": coll.group(1) if coll else None,
            "primary_key": "PRIMARY KEY" in au,
        })
    return cols, constraints


def _table(obj: dict, rest: str, raw: str) -> None:
    obj.update(columns=[], constraints=[], cluster_by=[], change_tracking=False, query=None,
               transient="TRANSIENT" in obj["modifiers"])
    s = rest.lstrip()
    tail = s
    if s.startswith("("):
        close = matching_paren(s, 0)
        obj["columns"], obj["constraints"] = _columns(s[1:close])
        tail = s[close + 1:]
    as_i = find_top_kw(tail, "AS")
    if as_i >= 0:
        obj["query"] = tail[as_i + 2:].strip()
        tail = tail[:as_i]
    cm = re.search(r"\bCLUSTER\s+BY\s*(?:LINEAR\s*)?\(", tail, re.I)
    if cm:
        close = matching_paren(tail, cm.end() - 1)
        obj["cluster_by"] = split_top(tail[cm.end():close])
    obj["change_tracking"] = (_prop(tail, "CHANGE_TRACKING") or "").upper() == "TRUE"
    obj["comment"] = _prop(tail, "COMMENT")
    obj["retention_days"] = _prop(tail, "DATA_RETENTION_TIME_IN_DAYS")


def _split_header_query(rest: str) -> tuple[str, str]:
    i = find_top_kw(rest, "AS")
    if i < 0:
        raise DdlParseError("missing AS <query>")
    return rest[:i], rest[i + 2:].strip()


def _view(obj: dict, rest: str, raw: str) -> None:
    head, query = _split_header_query(rest)
    obj.update(query=query, secure="SECURE" in obj["modifiers"], materialized="MATERIALIZED" in obj["modifiers"],
               column_aliases=[], comment=_prop(head, "COMMENT"), recursive="RECURSIVE" in obj["modifiers"])
    h = head.lstrip()
    if h.startswith("("):
        close = matching_paren(h, 0)
        obj["column_aliases"] = [re.split(r"\s+", c.strip())[0].strip('"') for c in split_top(h[1:close])]


def _dynamic_table(obj: dict, rest: str, raw: str) -> None:
    head, query = _split_header_query(rest)
    cols = []
    h = head.lstrip()
    if h.startswith("("):
        close = matching_paren(h, 0)
        cols = [re.split(r"\s+", c.strip())[0].strip('"') for c in split_top(h[1:close])]
        head = h[close + 1:]
    cm = re.search(r"\bCLUSTER\s+BY\s*\(", head, re.I)
    cluster = []
    if cm:
        close = matching_paren(head, cm.end() - 1)
        cluster = split_top(head[cm.end():close])
    obj.update(query=query, column_aliases=cols, target_lag=_prop(head, "TARGET_LAG"),
               warehouse=_prop(head, "WAREHOUSE"), refresh_mode=(_prop(head, "REFRESH_MODE") or "AUTO").upper(),
               initialize=(_prop(head, "INITIALIZE") or "ON_CREATE").upper(), cluster_by=cluster,
               comment=_prop(head, "COMMENT"))


def _stream(obj: dict, rest: str, raw: str) -> None:
    m = re.search(r"\bON\s+(TABLE|VIEW|EXTERNAL\s+TABLE|DYNAMIC\s+TABLE|STAGE)\s+(" + FQN + ")", rest, re.I | re.S)
    if not m:
        raise DdlParseError("stream without ON TABLE/VIEW")
    db, sch, name = _resolve(split_fqn(m.group(2)), obj["database"], obj["schema"])
    obj.update(source_kind=re.sub(r"\s+", "_", m.group(1).upper()), source=".".join(p for p in (db, sch, name) if p),
               append_only=(_prop(rest, "APPEND_ONLY") or "").upper() == "TRUE",
               insert_only=(_prop(rest, "INSERT_ONLY") or "").upper() == "TRUE",
               show_initial_rows=(_prop(rest, "SHOW_INITIAL_ROWS") or "").upper() == "TRUE",
               comment=_prop(rest, "COMMENT"))


def _task(obj: dict, rest: str, raw: str) -> None:
    as_i = find_top_kw(rest, "AS")
    if as_i < 0:
        raise DdlParseError("task without AS <body>")
    head, body = rest[:as_i], rest[as_i + 2:].strip()
    after = []
    am = re.search(r"\bAFTER\s+(.+?)(?=\s+(?:WHEN|AS|COMMENT|WAREHOUSE|SCHEDULE|USER_TASK|ALLOW_OVERLAPPING|SUSPEND_TASK|ERROR_INTEGRATION|CONFIG|FINALIZE|TASK_AUTO_RETRY)\b|\s*$)",
                   head, re.I | re.S)
    if am:
        for p in split_top(am.group(1)):
            db, sch, name = _resolve(split_fqn(p), obj["database"], obj["schema"])
            after.append(".".join(x for x in (db, sch, name) if x))
    when = None
    wm = re.search(r"\bWHEN\s+(.+)$", head, re.I | re.S)
    if wm:
        when = wm.group(1).strip()
    fin = re.search(r"\bFINALIZE\s*=\s*(" + FQN + ")", head, re.I)
    obj.update(warehouse=_prop(head, "WAREHOUSE"), schedule=_prop(head, "SCHEDULE"), after=after, when=when,
               body=body, comment=_prop(head, "COMMENT"),
               serverless=_prop(head, "WAREHOUSE") is None,
               finalizes=fin.group(1) if fin else None,
               allow_overlapping=(_prop(head, "ALLOW_OVERLAPPING_EXECUTION") or "").upper() == "TRUE")


def _procedure(obj: dict, rest: str, raw: str) -> None:
    s = rest.lstrip()
    args = []
    if s.startswith("("):
        close = matching_paren(s, 0)
        for a in split_top(s[1:close]):
            am = re.match(r'\s*("(?:[^"]|"")+"|[\w$]+)\s+(.+?)(?:\s+DEFAULT\s+(.+))?$', a, re.I | re.S)
            if am:
                args.append({"name": am.group(1).strip('"'), "type": am.group(2).strip().upper(),
                             "default": am.group(3)})
        s = s[close + 1:]
    # body: final top-level AS '...' | AS $$...$$
    bm = None
    for mm in re.finditer(r"\bAS\s+('(?:[^'\\]|''|\\.)*'|\$\$.*?\$\$)\s*$", s, re.I | re.S):
        bm = mm
    if not bm:
        raise DdlParseError("procedure without AS '<body>' / $$<body>$$")
    head, body = s[:bm.start()], sql_string(bm.group(1))
    rm = re.search(r"\bRETURNS\s+(.+?)(?=\s+(?:LANGUAGE|NOT\s+NULL|NULL|STRICT|CALLED|RETURNS\s+NULL|VOLATILE|IMMUTABLE|COMMENT|EXECUTE|RUNTIME_VERSION|PACKAGES|HANDLER|IMPORTS)\b|$)",
                   head, re.I | re.S)
    lang = (re.search(r"\bLANGUAGE\s+(\w+)", head, re.I) or [None, "SQL"])[1].upper()
    em = re.search(r"\bEXECUTE\s+AS\s+(OWNER|CALLER|RESTRICTED\s+CALLER)", head, re.I)
    pk = re.search(r"\bPACKAGES\s*=\s*\(([^)]*)\)", head, re.I)
    obj.update(args=args, returns=rm.group(1).strip().upper() if rm else None, language=lang, body=body,
               execute_as=(em.group(1).upper() if em else "OWNER"),
               handler=_prop(head, "HANDLER"), runtime_version=_prop(head, "RUNTIME_VERSION"),
               packages=[p.strip().strip("'") for p in pk.group(1).split(",")] if pk else [],
               comment=_prop(head, "COMMENT"))
    obj["signature"] = f"{obj['name']}({', '.join(a['type'] for a in args)})"
