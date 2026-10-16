# Conversion reference

What each Snowflake construct becomes, which rules apply, and what is not supported.
Sources: `agents/cd_codegen/mapping.py`, `converters/*.py`, `judges/rules.py`.

Every generated fragment starts with a node marker:
`-- NODE: <SNOWFLAKE_FQN> | <TYPE> | <ORIGIN>` (SQL) or `# NODE: ...` (Python); Job tasks carry it in `description`.

Origins: **MAPPED** fully deterministic · **HYBRID** deterministic scaffold + model-filled `__PENDING_n__` slots · **AI** whole body translated by the model.

## Names

`DB.SCHEMA.NAME` → `catalog.schema.name`, lower-cased. `catalog` = the run's *Target Unity Catalog*,
or `lower(DB)` if empty. Unquoted Snowflake identifiers resolve to upper case; quoted keep their case.
Column names keep their original spelling.

## Objects

| Snowflake | Databricks | File | Origin |
|---|---|---|---|
| TABLE | `CREATE TABLE IF NOT EXISTS ... USING DELTA` with mapped types, `NOT NULL`, `DEFAULT` (+ `allowColumnDefaults`), `PRIMARY KEY` constraint (only if all key columns are NOT NULL), `CLUSTER BY` (plain columns), `COMMENT`, `delta.enableChangeDataFeed` when a stream reads it or `CHANGE_TRACKING=TRUE` | `ddl/01_tables.sql` | MAPPED |
| TABLE ... AS SELECT (CTAS) | `CREATE TABLE IF NOT EXISTS ... AS <query>` | `ddl/01_tables.sql` | MAPPED / HYBRID |
| TABLE that is a CDC target | `dlt.create_streaming_table(name, schema, cluster_by, table_properties)` | pipeline layer file | MAPPED |
| VIEW / SECURE VIEW / MATERIALIZED VIEW | `CREATE OR REPLACE VIEW ... (cols) AS <query>` (secure → note: use UC grants/row filters; materialized → note: consider DLT MV) | `ddl/02_views.sql` | MAPPED / HYBRID |
| DYNAMIC TABLE | `@dlt.table(name=...)` returning `spark.sql("""<query>""")`, `.toDF(<column aliases>)`; `TARGET_LAG` → `jobs/pipeline_refresh.json` schedule (smallest lag) | pipeline layer file | MAPPED / HYBRID |
| STREAM (standalone) | `@dlt.view` with `spark.readStream.option("readChangeFeed","true").table(src)`; filters `update_preimage` (or keeps inserts only for APPEND_ONLY / INSERT_ONLY); `SHOW_INITIAL_ROWS` → `startingVersion 0` | pipeline layer file | MAPPED |
| STREAM + TASK + `MERGE INTO t USING stream` | one CDF view + `dlt.apply_changes(target, source, keys, sequence_by=_commit_version, apply_as_deletes if the MERGE deletes, SCD1)`; projection from the MERGE `INSERT` column list | pipeline layer file | MAPPED (HYBRID if keys/expressions need the model) |
| TASK (DAG) | one Job per DAG root; `task_key` = task name, `depends_on` from `AFTER`; root `SCHEDULE` → Quartz cron (`pause_status: PAUSED`) | `jobs/<schema>_<root>.json` | MAPPED |
| TASK body = `CALL proc(args)` | `notebook_task` → the procedure's notebook with `base_parameters` from the literal arguments | – | MAPPED |
| TASK body = SQL or a `BEGIN ... END` block (e.g. several `CALL`s) | SQL notebook `notebooks/task_<name>.sql` run on `${SQL_WAREHOUSE_ID}` | – | MAPPED / HYBRID |
| TASK collapsed into CDC | `pipeline_task` refreshing `${PIPELINE_ID}` | – | MAPPED |
| PROCEDURE, LANGUAGE SQL, plain statements | SQL notebook; each statement transpiled; `:PARAM` binds kept as named parameter markers; `RETURN` becomes a comment | `notebooks/proc_<name>.sql` | MAPPED / HYBRID |
| PROCEDURE with scripting (DECLARE, LET, IF, FOR, WHILE, LOOP, EXCEPTION, CURSOR, RESULTSET, EXECUTE IMMEDIATE, RETURN TABLE) or JAVASCRIPT / PYTHON / JAVA / SCALA | Python notebook: widgets for arguments (deterministic) + model-translated PySpark body; return value via `dbutils.notebook.exit` | `notebooks/proc_<name>.py` | AI |
| PROCEDURE with empty body in GET_DDL | stub notebook + `SRC001` error (body visible only to the owning role) | `notebooks/proc_<name>.sql` | – |

## SQL conversion

`converters/dialect.transpile()` parses with sqlglot (Snowflake dialect), renames tables, applies the
rewrite registry, and generates Databricks SQL. It **refuses** (raises `TranspileError`, which creates an AI
slot) when parsing fails, generation is unsupported, or Snowflake-only constructs remain.

Verified function conversions (sqlglot built-ins plus the registry in `converters/dialect.REWRITES`,
which only applies to functions sqlglot leaves unparsed):

| Snowflake | Databricks output | By |
|---|---|---|
| `IFF(a, 1, 2)` | `IF(a, 1, 2)` | sqlglot |
| `NVL(a, 0)` | `COALESCE(a, 0)` | sqlglot |
| `TO_VARCHAR(d, 'YYYY')` | `TO_CHAR(d, 'YYYY')` | sqlglot |
| `DIV0(a, b)` | `IF(b = 0 AND NOT a IS NULL, 0, a / b)` | sqlglot |
| `DIV0NULL(a, b)` | `CASE WHEN b = 0 OR b IS NULL THEN 0 ELSE a / b END` | registry |
| `ZEROIFNULL(x)` | `IF(x IS NULL, 0, x)` | sqlglot |
| `NULLIFZERO(x)` | `IF(x = 0, NULL, x)` | sqlglot |
| `EQUAL_NULL(a, b)` | `a IS NOT DISTINCT FROM b` | sqlglot |
| `SYSDATE()` | `CURRENT_TIMESTAMP()` | registry |
| `BOOLAND(a, b)` / `BOOLOR(a, b)` | `a AND b` / `a OR b` | registry |
| `QUALIFY ...` | kept (`NULLS FIRST` made explicit in `ORDER BY ... DESC`) | sqlglot |

Snowflake-only residue that blocks deterministic output (and fails DET002 if it reaches a file):
`IFF(`, `TO_VARCHAR(`, `ARRAY_CONSTRUCT(`, `OBJECT_CONSTRUCT(`, `LATERAL FLATTEN`, `TABLE(FLATTEN`, `SYSTEM$…`,
`METADATA$…`, `DIV0(`, `ZEROIFNULL(`, `SYSDATE(`, `TRY_TO_NUMBER(`, `ARRAY_SIZE(`, `STRTOK(`, `REGEXP_SUBSTR(`,
`OBJECT_KEYS(`, `CURRENT_WAREHOUSE(`, `RESULT_SCAN(`, `GET_DDL(`.

## Types (`converters/types.py`)

| Snowflake | Databricks | Risk note |
|---|---|---|
| NUMBER/NUMERIC/DECIMAL(p,s) | DECIMAL(min(p,38),s) | |
| INT, INTEGER, BIGINT, SMALLINT, TINYINT, BYTEINT | DECIMAL(38,0) | (Snowflake integers are NUMBER(38,0)) |
| FLOAT, FLOAT4/8, DOUBLE, REAL | DOUBLE | |
| VARCHAR/STRING/TEXT/NCHAR/NVARCHAR(n) | STRING | |
| CHAR/CHARACTER | STRING | padding semantics differ |
| BOOLEAN, DATE, BINARY/VARBINARY | same | |
| TIMESTAMP, TIMESTAMP_NTZ, DATETIME | TIMESTAMP_NTZ | precision > 6 truncated |
| TIMESTAMP_LTZ | TIMESTAMP | uses Spark session time zone |
| TIMESTAMP_TZ | TIMESTAMP | offset not stored; normalized to UTC |
| TIME | STRING | no TIME type |
| VARIANT | VARIANT | needs DBR 15.3+ |
| OBJECT, ARRAY, structured ARRAY/OBJECT/MAP | VARIANT | consider STRUCT/MAP/ARRAY<T> |
| GEOGRAPHY, GEOMETRY | STRING | spatial functions need rework |
| VECTOR | ARRAY<FLOAT> | |
| anything else | STRING | flagged as unknown |
| AUTOINCREMENT / IDENTITY | BIGINT GENERATED BY DEFAULT AS IDENTITY | gaps/ordering differ |
| COLLATE | dropped | flagged |

## Schedules (`converters/cron.py`)

| Snowflake `SCHEDULE` | Quartz |
|---|---|
| `USING CRON m h dom mon dow TZ` | `0 m h dom mon ?` or `0 m h ? mon DOW` (day names), `timezone_id = TZ` |
| `N MINUTE` (N divides 60) | `0 0/N * * * ?` |
| `N HOUR` (N divides 24) / `60 MINUTE` | `0 0 0/N * * ?` |
| `N SECOND` | rounded to minutes (flagged) |
| other intervals | hourly default + SNF003 warning |

## Quality gates (`judges/rules.py`)

| Rule | Severity | Checks |
|---|---|---|
| STR001 | error | every in-scope object has a node marker |
| STR002 | error | no unresolved `__PENDING_n__` slot in any file |
| STR003 | error / warn | Python compiles, JSON parses (error); SQL parses as Databricks SQL (warn) |
| DET001 | error | MAPPED output identical on two re-runs of the mapper |
| DET002 | error | no Snowflake-only residue in code lines |
| DET003 | error | no functions from other databases that Databricks lacks (e.g. `pg_sleep`, `GETDATE`, `WAITFOR DELAY`, `SELECT TOP n`) |
| SEM001 | error / warn | every source column / view alias appears in the generated code; dropped defaults/keys |
| SEM004 | warn | lossy type conversions (from the type map) |
| SEM005 | warn | session/time-zone sensitive functions in the source |
| LIN001 | warn | consumers reference their in-scope upstreams |
| LIN002 | error | no task/procedure writes into a DLT-managed table |
| LIN003 | error | every procedure the source `CALL`s is still invoked in the output |
| POL001 | error | no `USE ROLE`, ACCOUNTADMIN/SECURITYADMIN, `DROP TABLE/SCHEMA/...`, `GRANT`, hard-coded credentials, Databricks tokens, Snowflake warehouse names |
| SNF001 | info | TARGET_LAG mapped to a refresh schedule |
| SNF002 | warn | task `WHEN` needs review (info when collapsed into CDC) |
| SNF003 | warn | schedule conversion caveats |
| SRC001 | error | source definition visible (e.g. procedure body not hidden) |

## Risk register (md_plan)

Detected from DDL: VARIANT/semi-structured, FLATTEN, QUALIFY, time zones, COLLATE, identity/sequence,
TARGET_LAG, non-SQL procedures, secure views/policies, `SYSTEM$STREAM_HAS_DATA`, `EXECUTE AS CALLER`.

## Not supported

PIPE / COPY INTO (Auto Loader), UDFs/UDTFs, sequences, stages, file formats, external and Iceberg
tables, masking/row-access policies, tags, grants, alerts, data movement/backfill, and executing the
generated code on Databricks. Unsupported objects are skipped at parse time with a warning.

## Test status

| Object | Sample slice (`samples/sample_slice.sql`) | Live Snowflake extract |
|---|---|---|
| TABLE, VIEW, DYNAMIC TABLE, SQL PROCEDURE | passes | passes (`DATAWAREHOUSE_DEV.HLSG_DW_CONSUMPTION`, 10 objects) |
| STREAM, TASK DAG, STREAM+TASK+MERGE, JAVASCRIPT PROCEDURE | passes | not yet (no such objects in the tested schema) |
| TASK with a `BEGIN ... END` body of `CALL`s and `SYSTEM$WAIT` (`samples/task_refresh_dpc_uat.sql`) | passes (with Cortex: all 3 `CALL`s kept, waits dropped with a comment; cron 02:00 America/Chicago) | pasted DDL only |
| CTAS, MATERIALIZED VIEW, PYTHON/JAVA PROCEDURE | code path exists, not tested | not tested |

"Passes" = parsed, converted and cleared the static gates. No generated code has been executed on Databricks.

Bugs found by these tests and fixed (each has a regression test):
- scripting blocks were split at every `;`, silently dropping all but the first statement of a `BEGIN ... END` body;
- the translator emitted PostgreSQL `pg_sleep()` for `SYSTEM$WAIT` (now prompted against, and caught by DET003);
- `SYSTEM$WAIT` was counted as a procedure dependency; a doubled `;;` ended AI-filled SQL;
- LIN003 missed calls routed through a procedure's notebook (`proc_<name>`).
