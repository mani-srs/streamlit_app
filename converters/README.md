# converters/ — deterministic Snowflake conversion

Pure functions: no Snowflake I/O, no model calls. Used by standardization, discovery, md_plan and codegen.

| File | What it does | Main entry points |
|---|---|---|
| `sqlscan.py` | Quote-, `$$`-, comment- and parenthesis-aware scanning of Snowflake text | `scan`, `split_top`, `find_top_kw`, `matching_paren`, `strip_comments`, `split_fqn`, `unquote`, `sql_string` |
| `ddl_parser.py` | Script → statements (top-level `;`, aware of `BEGIN … END` / `DECLARE` / `CASE … END` blocks), and `CREATE` statement → object dict (TABLE, VIEW, DYNAMIC TABLE, STREAM, TASK, PROCEDURE) | `split_statements`, `parse_ddl`, `DdlParseError` |
| `dialect.py` | Snowflake SQL → Databricks SQL via sqlglot: table renaming, rewrite registry, residue guard | `NameMapper`, `transpile`, `TranspileError`, `referenced_tables`, `regex_tables`, `snowflake_residue`, `validate_databricks`, `REWRITES`, `SNOWFLAKE_ONLY` |
| `types.py` | Snowflake type → Databricks type + risk note | `map_type` |
| `cron.py` | Task `SCHEDULE` → Quartz cron + timezone | `to_quartz` |
| `graph_model.py` | Object list → nodes/edges; stream+task+MERGE CDC detection; ordering. `CALL SYSTEM$...` (e.g. `SYSTEM$WAIT`) is not counted as a procedure dependency | `build_graph`, `detect_cdc`, `topo_order`, `upstream`, `downstream`, `writes_of`, `calls_of` |

Example:

```python
from converters.dialect import NameMapper, transpile
m = NameMapper("SALES_DB", "CORE", catalog_override="main")
transpile("select iff(a>1,'x','y') from orders qualify row_number() over (order by a)=1", m).sql
# SELECT IF(a > 1, 'x', 'y') FROM main.core.orders QUALIFY ROW_NUMBER() OVER (ORDER BY a NULLS LAST) = 1
```

Design notes:
- `transpile()` raises instead of emitting doubtful SQL; the caller turns that into an AI slot.
- Identifier case follows Snowflake: unquoted → upper case, quoted → exact.
- Regex is used only for DDL header properties and body lineage where sqlglot cannot parse (procedure/task bodies,
  MERGE shape); SQL text is never regex-rewritten.

Full mapping tables: [docs/CONVERSION_REFERENCE.md](../docs/CONVERSION_REFERENCE.md).
