# Agent 5 — cd_codegen

Generates Databricks code. **Review gate on**; up to 3 automatic iterations when the gate is off.

| File | Role |
|---|---|
| `mapping.py` | **MapperRegistry** (`REGISTRY`): one `map_<type>(obj, ctx) -> list[Fragment]` per object type, `map_cdc()` for stream+task+MERGE, `build_jobs()` for task DAGs, `SlotAllocator` for `__PENDING_n__` slots |
| `agent.py` | `map_all()` (pure deterministic mapping), slot translation fan-out, file assembly, `det_diff()` (DET001), evaluation, the stage node |

## Flow

```
objects (topological order) ─► REGISTRY mappers ─► fragments + slots
                                  │ CDC targets → dlt.create_streaming_table + map_cdc() apply_changes
                                  │ tasks → build_jobs() → jobs/*.json
slots ─► map_ordered("translate") ─► core.llm.complete(role="translator") ─► merged by index ─► placeholders replaced
fragments ─► files: pipeline/_all_nodes.py (DLT), ddl/01_tables.sql, ddl/02_views.sql, notebooks/*, jobs/*
```

A `Fragment` carries: `node_id`, `obj_type`, `kind` (`dlt` | `ddl_table` | `ddl_view` | `notebook` | `job_task`),
`origin` (MAPPED | HYBRID | AI), `file`, `code`, `target`, `collapses` (ids folded into a CDC node),
`notes` (`"RULE|message"` findings surfaced by the judges) and `job_task` (Job task JSON).

A `Slot` carries: `idx`, `node_id`, `kind` (`sql_query` | `sql_statement` | `procedure` | `task_body` | `projection`),
`source`, `source_lang`, `target` (what to emit) and `instructions`. The translator prompt includes the full
Snowflake → Databricks name map, reviewer notes and hard constraints, and restricts output to Databricks SQL /
PySpark functions (never PostgreSQL / SQL Server / MySQL / Oracle ones; `SYSTEM$WAIT` is dropped with a comment
because Databricks has no sleep and notebook statements run in order). Output is fence-stripped, loses a trailing
`;` for SQL slots (the scaffold adds it), is post-checked against hard constraints and scanned for Snowflake residue.
The UI progress panel shows `Cortex translated n/N: <node> (<kind>)` as each slot finishes.

## Evaluation and verdict

`judges.rules.run_all()` on the fragments and files, plus slot errors (`SLOT-*`) and hard-constraint
violations (`HC-*`). Verdict: **revise** if an error touches a HYBRID/AI node or a slot and Cortex is on
(regeneration can fix it); **fail** for other errors; **pass** otherwise. Confidence drops 0.15 per error and 0.03 per warning.

On review, accepted issues, notes and hard constraints reach the translator on the next iteration;
deterministic (MAPPED) output is unchanged by design.

Output: `{"files", "fragments", "slots", "targets", "eval", "meta": {"origins", "slots_total", "slots_filled"}}`.
See [docs/CONVERSION_REFERENCE.md](../../docs/CONVERSION_REFERENCE.md) for every mapping.
