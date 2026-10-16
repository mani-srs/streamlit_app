# Agent 1 — connection

Extracts raw Snowflake DDL. No AI, no review gate. Implemented as a single LangGraph node.

**Live mode** (a database/schema and objects were picked):
- `list_objects(db, database, schema)` runs `SHOW TABLES | DYNAMIC TABLES | VIEWS | STREAMS | TASKS | USER PROCEDURES IN SCHEMA`
  (falls back to `SHOW PROCEDURES`), drops dynamic tables from the table list, and builds procedure
  signatures from the `arguments` column. Used by the New run page.
- For each selected object: `SELECT GET_DDL(<type>, <quoted fqn>, TRUE)` in the `introspect` pool (4 threads).
  Dynamic tables use type `TABLE`; procedures use `"DB"."SCH"."NAME"(ARG TYPES)`.
  Progress is reported per object (`GET_DDL 4/10: TABLE ORDERS`) for the UI progress panel.

**Paste mode**: the pasted script is split into statements with `converters.ddl_parser.split_statements`: on
top-level `;`, respecting quotes, `$$` bodies, comments, parentheses and Snowflake Scripting blocks, so a task
or procedure whose body is `BEGIN ... END` (or `DECLARE ... BEGIN ... END`) stays one statement.
`BEGIN TRANSACTION` is not treated as a block.

Everything runs with the role, secondary roles and warehouse chosen in the sidebar.

Output: `{"source": "live" | "pasted", "raw": [{"type", "name", "ddl", "meta": {"rows", "bytes"}}]}`.
Artifacts: `raw/<nnn>_<name>.sql`.

Permissions: `GET_DDL` shows procedure bodies only to the owning role (see SRC001 in validation).
