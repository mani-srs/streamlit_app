# core/ — shared contracts (frozen)

Everything agents build on. Changes here are contract changes: bump `CONTRACTS_VERSION` in
`__init__.py` (currently `1.1.0`) and update `tests/contracts/test_core.py`.

| File | Responsibility |
|---|---|
| `graph.py` | **LangGraph runner.** `GraphState` + reducers, `RunContext`, `Runner` (builds the `StateGraph`, SQLite/in-memory checkpointer, run registry, `start` / `resume` / `retry` / `load` / `list_runs`, `on_step` progress hook fired before every node) |
| `nodes.py` | `build_gen_eval_hitl()` → `ReviewableStage` with `gen_step`, `eval_step`, `review_step` (the review step calls `interrupt()`); `feedback_notes()`, `issues_for()`, `verdict_from()` |
| `state.py` | `RunState`, `SliceSpec`, `RunStatus`, `merge_by_index()` |
| `schemas.py` | `Verdict`, `Severity`, `Origin`, `Issue`, `EvalResult`, `HumanFeedback`, `GenResult`, `LlmResult`, `ContractViolation` |
| `llm.py` | The only model entry point: `complete(ctx, role, messages, json_mode=...)` → `SNOWFLAKE.CORTEX.COMPLETE`, retries, fallbacks, observability; `set_backend()` for tests |
| `models.py` | Role → model registry (defaults overridden by `config/models.yaml`) |
| `prompts.py` | `PromptParts`, `assemble()` (priority + context truncation), `post_check()` for hard constraints, `strip_fences()` |
| `concurrency.py` | `map_ordered(pool, fn, items)`: named pools (`translate` 4, `evaluate` 2, `introspect` 4, `probe` 12) under a global cap of 16 |
| `persistence.py` | Artifact store and observability sinks: `Memory*`, `Sql*` (Snowflake tables from `setup.sql`) and **`SqliteObs`** (durable call/node log in `.state/runs.sqlite`, with query ids, `unbilled_query_ids()`, `set_billed()`); `SqlCheckpoints` is used as an audit mirror |
| `analytics.py` | Run statistics and cost: `run_summary()`, `estimate_credits()`, `calibrate()`, `fetch_billed()`, `account_usage()`, `load_rates()` / `load_settings()` / `save_settings()` |
| `db.py` | `Db` protocol, `SnowparkDb`, `ConnectorDb` (qmark binding), `connect()`, `list_connections()` / `resolve_connection_name()` (reads `connections.toml` / `config.toml` under `$SNOWFLAKE_HOME` or `~/.snowflake`), `qident()` / `qfqn()` identifier quoting; `query_with_id()` returns the Snowflake query id; connections set `QUERY_TAG = APP_QUERY_TAG` |

## How a run flows through core

```
master.run()  →  Runner.start(state)  →  app.invoke(graph_state, thread_id=run_id)
   node wrapper: copy state → RunContext → agent function → delta → reducers → checkpoint
   review step: interrupt(payload)  → run parks (awaiting_human)
master.resume() →  Runner.resume(run_id, feedback) → app.invoke(Command(resume=feedback))
master.retry()  →  Runner.retry(run_id)            → app.invoke(None)  (re-runs the failed node)
```

See [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) for state keys, HITL semantics and persistence.
