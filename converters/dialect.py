"""SQL transpilation Snowflake -> Databricks: sqlglot + rewrite registry + UC name mapping."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from converters.sqlscan import split_fqn

logging.getLogger("sqlglot").setLevel(logging.ERROR)


@dataclass
class NameMapper:
    """Snowflake DB.SCHEMA.NAME -> UC catalog.schema.name (lower case)."""
    default_db: str
    default_schema: str
    catalog_override: str = ""
    overrides: dict[str, str] = field(default_factory=dict)  # FQN upper -> target

    def resolve(self, parts: list[str]) -> str:
        if len(parts) == 1:
            parts = [self.default_db, self.default_schema, parts[0]]
        elif len(parts) == 2:
            parts = [self.default_db, *parts]
        return ".".join(parts)

    def target(self, fqn: str) -> str:
        if fqn in self.overrides:
            return self.overrides[fqn]
        db, sch, name = fqn.split(".") if fqn.count(".") == 2 else (self.default_db, self.default_schema, fqn)
        cat = self.catalog_override or db.lower()
        return f"{cat}.{sch.lower()}.{name.lower()}"


# ---- rewrite registry: Snowflake function (upper) -> callable(args) -> exp
def _div0(args, null=False):
    a, b = args
    cond = exp.EQ(this=b.copy(), expression=exp.Literal.number(0))
    if null:
        cond = exp.Or(this=cond, expression=exp.Is(this=b.copy(), expression=exp.Null()))
    return exp.Case(ifs=[exp.If(this=cond, true=exp.Literal.number(0))], default=exp.Div(this=a, expression=b))


REWRITES = {
    "DIV0": lambda a: _div0(a),
    "DIV0NULL": lambda a: _div0(a, null=True),
    "ZEROIFNULL": lambda a: exp.Coalesce(this=a[0], expressions=[exp.Literal.number(0)]),
    "NULLIFZERO": lambda a: exp.Nullif(this=a[0], expression=exp.Literal.number(0)),
    "EQUAL_NULL": lambda a: exp.NullSafeEQ(this=a[0], expression=a[1]),
    "SYSDATE": lambda a: exp.CurrentTimestamp(),
    "BOOLAND": lambda a: exp.And(this=a[0], expression=a[1]),
    "BOOLOR": lambda a: exp.Or(this=a[0], expression=a[1]),
}

# Snowflake-only constructs that must not survive into Databricks output (used by DET002)
SNOWFLAKE_ONLY = [
    r"\bIFF\s*\(", r"\bTO_VARCHAR\s*\(", r"\bARRAY_CONSTRUCT\s*\(", r"\bOBJECT_CONSTRUCT\s*\(",
    r"\bLATERAL\s+FLATTEN\b", r"\bTABLE\s*\(\s*FLATTEN\b", r"\bSYSTEM\$\w+", r"\bMETADATA\$\w+",
    r"\bDIV0\s*\(", r"\bZEROIFNULL\s*\(", r"\bSYSDATE\s*\(", r"\bTRY_TO_NUMBER\s*\(", r"\bARRAY_SIZE\s*\(",
    r"\bSTRTOK\s*\(", r"\bREGEXP_SUBSTR\s*\(", r"\bOBJECT_KEYS\s*\(", r"\bCURRENT_WAREHOUSE\s*\(",
    r"\bRESULT_SCAN\s*\(", r"\bGET_DDL\s*\(",
]
_SF_ONLY_RX = [re.compile(p, re.I) for p in SNOWFLAKE_ONLY]


def snowflake_residue(sql: str) -> list[str]:
    return sorted({m.group(0).strip() for rx in _SF_ONLY_RX for m in rx.finditer(sql)})


class TranspileError(Exception):
    pass


@dataclass
class Transpiled:
    sql: str
    tables: list[str]  # source FQNs referenced (upper)
    warnings: list[str]


def _apply_rewrites(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Anonymous):
        fn = REWRITES.get(str(node.this).upper())
        if fn:
            try:
                return fn(list(node.expressions))
            except Exception:
                return node
    return node


def _parts(tb: exp.Table) -> list[str]:
    out = []
    for k in ("catalog", "db", "this"):
        i = tb.args.get(k)
        if isinstance(i, exp.Identifier):
            out.append(i.name if i.quoted else i.name.upper())
    return out


def referenced_tables(sql: str, mapper: NameMapper) -> list[str]:
    try:
        trees = sqlglot.parse(sql, read="snowflake")
    except Exception:
        return regex_tables(sql, mapper)
    ctes = set()
    out = []
    for t in trees:
        if t is None:
            continue
        ctes |= {c.alias_or_name.upper() for c in t.find_all(exp.CTE)}
        for tb in t.find_all(exp.Table):
            if not tb.name or (not tb.db and tb.name.upper() in ctes):
                continue
            out.append(mapper.resolve(_parts(tb)))
    return list(dict.fromkeys(out))


_TBL_RX = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE|USING|TABLE)\s+(?!SET\b)((?:"[^"]+"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:"[^"]+"|[A-Za-z_][\w$]*)){0,2})', re.I)
_NOT_TABLE = {"SELECT", "TABLE", "LATERAL", "FLATTEN", "VALUES", "DUAL", "SET"}


def regex_tables(sql: str, mapper: NameMapper) -> list[str]:
    out = []
    for m in _TBL_RX.finditer(sql):
        parts = split_fqn(m.group(1))
        if parts and parts[-1].upper() not in _NOT_TABLE:
            out.append(mapper.resolve(parts))
    return list(dict.fromkeys(out))


def transpile(sql: str, mapper: NameMapper, pretty: bool = True) -> Transpiled:
    """Deterministic Snowflake -> Databricks. Raises TranspileError when not safely convertible."""
    try:
        trees = sqlglot.parse(sql, read="snowflake", error_level=ErrorLevel.RAISE)
    except Exception as e:
        raise TranspileError(f"parse: {e}") from e
    outs, tables, warns = [], [], []
    for tree in trees:
        if tree is None:
            continue
        ctes = {c.alias_or_name.upper() for c in tree.find_all(exp.CTE)}
        for tb in list(tree.find_all(exp.Table)):
            if not tb.name or (not tb.db and tb.name.upper() in ctes):
                continue
            if isinstance(tb.this, exp.Func) or not isinstance(tb.this, exp.Identifier):
                continue  # table functions (FLATTEN etc.)
            fqn = mapper.resolve(_parts(tb))
            tables.append(fqn)
            cat, sch, nm = mapper.target(fqn).split(".")
            tb.set("catalog", exp.to_identifier(cat))
            tb.set("db", exp.to_identifier(sch))
            tb.set("this", exp.to_identifier(nm))
        tree = tree.transform(_apply_rewrites)
        try:
            out = tree.sql(dialect="databricks", pretty=pretty, unsupported_level=ErrorLevel.RAISE)
        except Exception as e:
            raise TranspileError(f"generate: {e}") from e
        outs.append(out)
    sql_out = ";\n".join(outs)
    residue = snowflake_residue(sql_out)
    if residue:
        raise TranspileError(f"Snowflake-only constructs remain: {', '.join(residue)}")
    return Transpiled(sql=sql_out, tables=list(dict.fromkeys(tables)), warnings=warns)


def validate_databricks(sql: str) -> str | None:
    try:
        sqlglot.parse(sql, read="databricks", error_level=ErrorLevel.RAISE)
        return None
    except Exception as e:
        return str(e).splitlines()[0][:300]
