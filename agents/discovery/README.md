# Agent 3 — discovery

Builds the dependency graph and detects CDC patterns. Deterministic; **review gate on** (structural confirm).

- **gen**: `converters.graph_model.build_graph()` + `topo_order()` over in-scope objects.
  - Edge kinds: `reads` (views, dynamic tables, CTAS, task/procedure bodies), `writes` (INSERT/MERGE/UPDATE/DELETE/TRUNCATE/COPY targets),
    `cdc_of` (stream → source table), `precedes` (task `AFTER`), `calls` (`CALL proc`).
  - Referenced objects outside the slice become `EXTERNAL` nodes (`in_scope: false`).
  - CDC pattern: a task whose body is `MERGE INTO <target> USING <stream in slice>`; keys from the `ON` equalities,
    columns/values from the `INSERT` clause, `has_delete` if a `THEN DELETE` branch exists.
- **eval**: external references (warn), cycles (error), incomplete CDC patterns (warn), streams with no consumer (info).
- **review**: add `exclude: DB.SCHEMA.OBJECT` lines to the notes to drop objects; the next iteration rebuilds the graph.

Output: `{"graph": {"nodes", "edges", "patterns", "order"}, "excluded", "markdown", "issues"}`.
Artifacts: `graph.json`, `discovery.md`. The Run monitor renders the graph.
