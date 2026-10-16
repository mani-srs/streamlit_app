# agents/ — the seven migration agents

`master.py` defines the pipeline:

- `AGENT_ORDER` – connection → standardization → discovery → md_plan → cd_codegen → validation → classifier
- `HITL_DEFAULTS` – review gates on for discovery, md_plan, cd_codegen, classifier
- `PHASE_LABELS` – names shown in the UI
- `Backend` – Snowflake connection + artifact store + observability + the LangGraph `Runner`
- `new_run()`, `run()`, `resume()`, `retry()`, `mermaid()`

`common.py` has helpers every agent uses: `need(state, stage)` (read a prior output, fail loud if missing),
`mapper_for(state)` (Snowflake → UC name mapping), `in_scope_objects(state)` (objects minus reviewer exclusions).

| Agent | Folder | AI | Gate | Reads | Produces |
|---|---|---|---|---|---|
| 1 Connection | [connection/](connection/README.md) | – | – | slice | raw DDL |
| 2 Standardization | [standardization/](standardization/README.md) | – | off | raw DDL | object model |
| 3 Discovery | [discovery/](discovery/README.md) | – | on | object model | lineage graph, CDC patterns |
| 4 Migration plan | [md_plan/](md_plan/README.md) | planner, evaluator | on | model, graph | `plan.md` |
| 5 Codegen | [cd_codegen/](cd_codegen/README.md) | translator | on | model, graph | fragments, files |
| 6 Validation | [validation/](validation/README.md) | – | off | codegen output | judge report, parity SQL |
| 7 Classifier | [classifier/](classifier/README.md) | – | on | everything | layer files, final bundle |

Every agent module exposes `node`: either a plain function `(ctx, state) -> output` (connection) or a
`ReviewableStage` from `core.nodes.build_gen_eval_hitl(stage, gen, evaluate, finalize, save, max_iters)`.
