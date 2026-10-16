# Agent 2 — standardization

Turns raw DDL into a normalized JSON object model. Deterministic; gate off by default.

- **gen**: `converters.ddl_parser.parse_ddl()` per statement. Unparseable statements are collected, not fatal.
- **eval**: duplicates (error), unqualified names (warn), required fields per type (error; see `REQUIRED`),
  skipped statements (warn), no objects at all (error). Empty procedure bodies get a hint about owner-only visibility.
- **finalize**: fills the default database/schema for unqualified pasted DDL; fails the stage if nothing parsed.

Object model (common keys): `id` (FQN), `type`, `database`, `schema`, `name`, `ddl`, `modifiers`, `comment`, `meta`.
Type-specific keys:

| Type | Keys |
|---|---|
| TABLE | `columns[{name, type, nullable, default, identity, comment, collation, primary_key}]`, `constraints`, `cluster_by`, `change_tracking`, `transient`, `query` (CTAS), `retention_days` |
| VIEW | `query`, `column_aliases`, `secure`, `materialized`, `recursive` |
| DYNAMIC_TABLE | `query`, `column_aliases`, `target_lag`, `warehouse`, `refresh_mode`, `initialize`, `cluster_by` |
| STREAM | `source`, `source_kind`, `append_only`, `insert_only`, `show_initial_rows` |
| TASK | `schedule`, `after[]`, `when`, `body`, `warehouse`, `serverless`, `finalizes`, `allow_overlapping` |
| PROCEDURE | `args[{name, type, default}]`, `returns`, `language`, `body`, `execute_as`, `handler`, `runtime_version`, `packages`, `signature` |

Artifact: `object_model.json`.
