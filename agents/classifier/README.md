# Agent 7 — classifier

Assigns bronze / silver / gold, splits the DLT code into exactly three pipeline files, and assembles
the final bundle. Deterministic; **review gate on**.

## Layer rules

Loaded from `config/medallion_rules.yaml` (built-in `DEFAULT_RULES` if the file or PyYAML is missing).
Evaluated in `precedence` order, first match wins:

1. Reviewer override (`layer: DB.SCHEMA.OBJECT = gold` in review notes)
2. `name` – regex on the object name (e.g. `^RAW_` → bronze, `^DIM_|^FCT_` → gold)
3. `graph` – tables/streams with no in-slice upstream → bronze; views/dynamic tables with no in-slice downstream → gold
4. `schema` – regex on the schema name
5. `default` (silver)

CDC nodes (`cdc:<target>`) follow their target table's layer. UC DDL objects get a `# NODE-REF` line in their
layer file pointing at `ddl/*.sql`.

## Bundle assembly

- `pipeline/bronze.py`, `silver.py`, `gold.py` – Databricks notebook source, `import dlt`, one cell per dataset
- `pipeline/pipeline_settings.json` – serverless triggered pipeline over the three files, `spark.sql.session.timeZone=UTC`
- `jobs/pipeline_refresh.json` – schedule from the smallest dynamic-table `TARGET_LAG`
- all codegen files except `pipeline/_all_nodes.py`, validation files, `plan.md`, and a generated `README.md`

`bundle_zip(files)` builds the downloadable zip. Output: `{"files", "assignments", "markdown"}`.
