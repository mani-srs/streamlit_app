# Agent 6 — validation

Runs the deterministic judges on the final code and builds the parity manifest. No AI; gate off by default.

- **gen**:
  - `judges.rules.run_all()` over codegen output (same bundle as the codegen evaluator, including DET001 re-run).
  - `parity_queries()` per TABLE / VIEW / DYNAMIC TABLE: `COUNT(*)`, non-null count per column, and
    `MIN`/`MAX` (as strings) for numeric/date/timestamp columns, first 50 columns. Emitted as matching
    Snowflake and Databricks SQL.
  - If *Profile Snowflake tables now* was enabled (live mode), runs the Snowflake queries and stores results.
- **eval**: the judge issues plus failed profile queries (warn).

`DatabricksRunner` is a stub: executing the Databricks side needs network access from this machine/account
to a Databricks SQL warehouse, which is not implemented.

Output files: `validation/judges_report.json`, `validation/parity_manifest.json`,
`validation/parity_snowflake.sql`, `validation/parity_databricks.sql`.
Compare rule: row counts equal; `*__nn` equal; `*__min` / `*__max` equal as strings (time-zone normalized).
