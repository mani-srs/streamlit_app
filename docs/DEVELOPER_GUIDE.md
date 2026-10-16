# Developer guide

How the code is organised, the contracts every change must respect, and recipes for common extensions.

## Layout and ownership

```
core/         FROZEN contracts. Change only via a reviewed change + CONTRACTS_VERSION bump.
agents/       One folder per agent; implements against core. master.py wires them.
converters/   Deterministic Snowflake parsing/conversion. No I/O, no model calls.
judges/       Deterministic quality gates. Never call a model.
app_pages/, ui/  Streamlit UI. Business logic stays out of pages.
config/       Model registry, medallion rules and Cortex pricing (YAML).
tests/        contracts/ (mocked LLM) and ui/ (headless AppTest).
```

## Golden rules

1. Call models **only** through `core.llm.complete(ctx, role, messages)`. Ask for a role, not a model.
2. Build prompts with `core.prompts.assemble(PromptParts(...))`; never hand-format a final prompt.
3. Nodes return a **state delta**; never mutate the `RunState` you receive (it is a copy, but treat it as read-only).
4. Nodes must be safe to re-run: artifacts are keyed by `(run_id, stage, iteration, name)` and overwritten, never appended.
5. Fan-out results merge by index (`core.concurrency.map_ordered`, `core.state.merge_by_index`); acquire a named pool.
6. Every `Issue.ref` must be addressable: `node:`, `table:`, `line:<file>:<n>`, `file:` or `object:`.
7. Judges are hard-coded rules. If a check needs a model, it belongs in an evaluator, not in `judges/`.
8. Converters parse (sqlglot / the quote-aware scanner) rather than regex-replace SQL text.
9. Any interface change in `core/` bumps `CONTRACTS_VERSION` and its contract test.

## Contracts (`core/`)

| Contract | Where | Shape |
|---|---|---|
| `Verdict`, `Severity`, `Origin` | `schemas.py` | enums |
| `Issue` | `schemas.py` | id, severity, ref (addressable), message, suggested_fix |
| `EvalResult` | `schemas.py` | verdict, confidence ∈ [0,1], issues, summary |
| `HumanFeedback` | `schemas.py` | accepted, accepted/rejected issue ids, freeform, hard_constraints, abort |
| `GenResult` | `schemas.py` | artifact (str or dict), node_keys, meta |
| `RunState`, `SliceSpec` | `state.py` | see docs/ARCHITECTURE.md §4 |
| Simple stage | `graph.Runner` | `fn(ctx: RunContext, state: RunState) -> output` |
| Reviewable stage | `nodes.build_gen_eval_hitl` | `gen(ctx, state, feedback) -> GenResult`, `evaluate(ctx, state, gen) -> EvalResult`, `finalize(ctx, state, gen, eval) -> output`, optional `save(ctx, gen, iteration)` |

`RunContext` gives a node: `db`, `store`, `obs`, `llm_enabled`, `run_id`, `stage`, `progress(msg)`,
`put(name, content, iteration)` and `interrupt(key, payload)` (used only by the review step).

**Progress:** call `ctx.progress("...")` for user-visible detail inside long work (it may be called from pool
worker threads; the UI handles that). The runner reports each node automatically via `Runner.on_step`; if you add
a node, add its plain-language text to `STEP_TEXT` in `agents/master.py`.

**UI caches:** anything whose result depends on the Snowflake role must be cached with `runtime.context_key(db)`
as a normal argument (not `_key`: Streamlit does not hash underscore-prefixed arguments).

## Recipes

### Support a new Snowflake object type

1. **Parse** it in `converters/ddl_parser.py`: add the keyword to `_HEADER`, a `_<type>(obj, rest, raw)` filler, and the type to `TYPES`.
2. **Validate** required fields in `agents/standardization/agent.py` (`REQUIRED`).
3. **Lineage**: add edges in `converters/graph_model.build_graph`.
4. **Map** it in `agents/cd_codegen/mapping.py`: write `map_<type>(o, ctx) -> list[Fragment]`, emit a node marker, register it in `REGISTRY`. Use `ctx.slots.new(...)` for parts that need the model.
5. **Assemble**: fragments with `kind="dlt"` go to the layer files; `ddl_table`/`ddl_view`/`notebook` go to their `file`; `job_task` goes into Job JSON.
6. **Plan**: add a target description to `TARGETS` in `agents/md_plan/agent.py` (and a `RISKS` entry if relevant).
7. **Test**: add DDL to `samples/sample_slice.sql` or a focused test in `tests/contracts/test_pipeline.py`.

### Add a function rewrite

Add an entry to `REWRITES` in `converters/dialect.py` (`"FUNC": lambda args: <sqlglot expression>`). It only
fires for functions sqlglot leaves as `Anonymous`. If the function must never reach output, add a pattern
to `SNOWFLAKE_ONLY` (enforced by DET002). Verify with a one-line `transpile("SELECT FUNC(a)", ...)` check.

### Add a judge rule

Add the id and description to `RULES` in `judges/rules.py`, implement the check in `run_all()` returning
`Issue`s with addressable refs, and add a failing-input test in `tests/contracts/test_pipeline.py::test_judges_catch_violations`.
Mappers can also declare findings by appending `"RULE|message"` to `Fragment.notes`.

### Add or change an agent

Implement `agents/<name>/agent.py` exposing `node` (a callable or `build_gen_eval_hitl(...)`), then add it to
`AGENT_ORDER`, `HITL_DEFAULTS`, `PHASE_LABELS` and `_NODES` in `agents/master.py`. Read prior outputs with
`agents.common.need(state, "<stage>")`.

### Change models

Edit `config/models.yaml` (`model`, `fallback`, `max_tokens`, `temperature` per role). No code change.

## Testing

```powershell
.\.venv\Scripts\python -m pytest tests -q                  # 35 tests: contracts + UI flow
.\.venv\Scripts\python scripts\local_dryrun.py             # full pipeline, no model calls
.\.venv\Scripts\python scripts\live_smoke.py --connection <name> --database <DB> --schema <SCH>
```

The contract tests mock `complete()` with `llm.set_backend(fn)`; see `tests/contracts/conftest.py`.
The UI test uses `streamlit.testing.v1.AppTest` with no Snowflake connection and a temp checkpoint file.

## Dependencies

Pinned in `requirements.txt`: `langgraph`, `langgraph-checkpoint-sqlite`, `sqlglot`, `streamlit`,
`snowflake-connector-python`, `pandas`, `pyyaml`, `pytest`. LangChain is not used (LangGraph installs
`langchain-core` as a dependency only).
