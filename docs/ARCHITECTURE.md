# Architecture

This document describes how the app is built: the agent pipeline, the LangGraph graph, run state,
human-in-the-loop (HITL), LLM usage, persistence and observability. It ends with a comparison to the
DOMO → Databricks blueprint this app was modelled on.

## 1. Big picture

```
┌──────────────────────────────── Streamlit UI (app_pages/, ui/runtime.py) ────────────────────────────────┐
│ New run │ Run monitor │ Review gate │ Outputs │ Observability                                             │
└────┬────────────────────────────────────────────────────────────────────────────────────────────────────┘
     │ master.run / resume / retry
┌────▼──────────────────── agents/master.py : Backend ────────────────────┐
│  Runner (core/graph.py) = compiled LangGraph StateGraph + SqliteSaver   │
│  RunContext per node: db, store, obs, llm_enabled, progress             │
└────┬───────────────┬───────────────────────────┬────────────────────────┘
     │               │                           │
 converters/      judges/                  core/llm.py ──► SNOWFLAKE.CORTEX.COMPLETE
 (deterministic)  (deterministic gates)    (only model entry point)
     │
 core/db.py ──► Snowflake (SHOW, GET_DDL, optional parity profiling, optional app tables)
```

## 2. The seven agents

| # | Agent | Kind | AI | Default gate | Output |
|---|---|---|---|---|---|
| 1 | `connection` | simple node | no | – | raw DDL per object (live `SHOW` + `GET_DDL`, or pasted) |
| 2 | `standardization` | gen → eval → review | no | off | normalized JSON object model |
| 3 | `discovery` | gen → eval → review | no | **on** | lineage graph (nodes/edges), CDC patterns, topological order |
| 4 | `md_plan` | gen → eval → review | planner + evaluator | **on** | `plan.md` with risk register |
| 5 | `cd_codegen` | gen → eval → review | translator (slots only) | **on** | node-marked fragments + files |
| 6 | `validation` | gen → eval → review | no | off | judge report + parity manifest/SQL |
| 7 | `classifier` | gen → eval → review | no | **on** | bronze/silver/gold pipeline files, final bundle |

Order, defaults and labels live in `agents/master.py` (`AGENT_ORDER`, `HITL_DEFAULTS`, `PHASE_LABELS`).
Details per agent: `agents/<name>/README.md`.

## 3. The LangGraph graph

`core/graph.py` builds one `StateGraph` from the stage list:

- A plain callable stage (e.g. `connection`) becomes **one node**.
- A `ReviewableStage` (from `core/nodes.py`) becomes **three nodes**: `<stage>__gen → <stage>__eval → <stage>__review`.
- After each stage's last node a conditional router decides:
  - stage in `completed` → next stage's first node
  - review asked for a revision → back to `<stage>__gen`
  - `status == aborted` → `END`

```
START → connection → standardization__gen → __eval → __review ─┬→ discovery__gen → __eval → __review ─┬→ ...
                                             ▲                  │               ▲                    │
                                             └─── revise ───────┘               └──── revise ────────┘
... classifier__review → END
```

Print the real compiled graph with `python -c "from agents import master; print(master.mermaid())"` (20 nodes).

Why three nodes instead of one: each node is its own checkpointed super-step. When a run resumes after
review, LangGraph re-runs only the `__review` node; generation and evaluation are not repeated (no
duplicate LLM calls).

## 4. State

`GraphState` (TypedDict in `core/graph.py`) holds JSON-able values; agents see it as a `RunState`
dataclass (`core/state.py`).

| Key | Reducer | Meaning |
|---|---|---|
| `run_id` | overwrite | LangGraph `thread_id` and checkpoint key |
| `slice` | overwrite | `SliceSpec`: database, schema, selected objects or pasted DDL, target catalog |
| `status`, `current_stage`, `error` | overwrite | run status (`running`, `awaiting_human`, `done`, `failed`, `aborted`) |
| `completed` | ordered union | stages that finished |
| `outputs` | dict merge | stage → final output |
| `scratch` | dict merge (per stage key) | stage working memory: `iteration`, `gen`, `eval`, `feedback`, `issues_by_iter` |
| `hitl`, `options` | overwrite | per-stage gate switches; `use_llm`, `parity_live` |

Rules:
- Nodes receive a **copy** and return a **delta** (`{"scratch": {stage: ...}}`, `{"outputs": {...}, "completed": [...]}`); they never mutate graph state.
- Iterations are versioned: artifacts are stored per `(run_id, stage, iteration, name)`; regeneration never overwrites an approved prior.
- Fan-out results (AI slot translations) are merged by index (`core/state.merge_by_index`), never last-write-wins.

## 5. Human in the loop

1. `<stage>__review` calls `ctx.interrupt(...)` → `langgraph.types.interrupt(payload)`. The payload carries the
   artifact, the evaluation (verdict, confidence, issues) and metadata.
2. The run parks. `Runner.load()` reports `status=awaiting_human` and `pending=payload` (read from
   `get_state().interrupts`).
3. The UI (Review gate) sends a `HumanFeedback`: accepted/rejected issue ids, free-form notes, hard
   constraints, `accepted` or `abort`.
4. `Runner.resume()` invokes `Command(resume=feedback)`. The review node re-runs; `interrupt()` returns the feedback:
   - `accepted` → `finalize()` → stage output, `completed += [stage]`
   - `abort` → `status=aborted` → `END`
   - otherwise → feedback appended, `iteration + 1` → router sends it back to `__gen`

With a gate switched off the review node auto-revises: if the evaluator says `revise` and iterations
remain (`max_iters`), evaluator issues are fed back as feedback; otherwise it finalizes.

Stage-specific reviewer directives (parsed from notes):
- discovery: `exclude: DB.SCHEMA.OBJECT` removes an object from scope
- classifier: `layer: DB.SCHEMA.OBJECT = gold` overrides layer assignment

## 6. LLM usage

All model calls go through `core/llm.complete(ctx, role, messages, ...)`:
- SQL: `SELECT SNOWFLAKE.CORTEX.COMPLETE(?, PARSE_JSON(?)::ARRAY, PARSE_JSON(?)::OBJECT)` (bound parameters).
- Roles map to models in `config/models.yaml` (`planner`, `evaluator`, `translator`); each role tries its model
  then fallbacks, 2 attempts each.
- Every call (success or error) is logged to observability with tokens, latency, retries.
- If `use_llm` is off or there is no connection, `LlmUnavailable` is raised and the calling agent falls back
  (plan: deterministic draft; codegen: slots stay `__PENDING_n__` and STR002 reports them).

| Role | Called by | Purpose |
|---|---|---|
| `translator` | cd_codegen | fill `__PENDING_n__` slots: non-SQL procedures, scripting procedures, SQL sqlglot cannot convert, complex MERGE projections |
| `planner` | md_plan | enrich the deterministic plan draft; rejected if a required section is missing |
| `evaluator` | md_plan | return JSON issues about the plan |

### Prompt assembly (`core/prompts.py`)

```
system : P1 role instruction + HARD CONSTRAINTS (verbatim from reviewers)
user   : P2 task + REVIEWER FEEDBACK (accepted issues, rejected issues, notes) + P3 CONTEXT (truncated to budget)
```

Only context is truncated. `post_check()` deterministically verifies `must contain: X` / `must not contain: X`
constraints on the model output; violations become `HC-*` error issues.

Translator guard-rails: the system prompt allows only Databricks SQL / PySpark functions (no PostgreSQL, SQL
Server, MySQL or Oracle functions) and tells the model that Databricks has no sleep function, so `SYSTEM$WAIT`
is dropped with a comment (notebook statements already run in order). A trailing `;` is stripped from SQL
slots because the scaffold adds it. Mistakes that still get through are caught by the judges: DET002
(Snowflake leftovers), DET003 (functions from other databases, e.g. `pg_sleep`), LIN003 (a `CALL` lost).

## 7. Persistence

| What | Where | Notes |
|---|---|---|
| Run state / checkpoints | `.state/runs.sqlite` (LangGraph `SqliteSaver`) | survives restarts; authoritative |
| Run list (status, error, slice) | `run_registry` table in the same SQLite file | used by the UI selectors |
| Artifacts per iteration | memory, or `ARTIFACTS` table if an app schema is configured | |
| Observability (LLM calls with Snowflake query id, node timings) | `obs_llm_calls` / `obs_nodes` in `.state/runs.sqlite` (`SqliteObs`); copied to `OBS_LLM_CALLS` / `OBS_NODES` if an app schema is configured | survives restarts; writes never break a run |
| Billed credits per call | `billed_credits` column in `obs_llm_calls`, filled from `ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY` by query id | Financial page → Refresh billed credits |
| Price per credit, budget, calibrated rates | `.state/settings.json` | edited on the Financial page |
| Audit copy of run state | `RUN_CHECKPOINTS` table (optional) | written after each drive |

Snowflake tables come from `setup.sql` and are enabled by entering `APP_DB.APP_SCHEMA` in the sidebar.

Failures: an exception in a node leaves that node pending in the checkpoint; the error is stored in the
registry and the run shows `failed`. `Runner.retry()` invokes with `None`, and LangGraph re-runs only the
failed node.

## 8. Concurrency

`core/concurrency.map_ordered(pool, fn, items)` runs work in named thread pools under a global cap of 16:
`translate` 4 (Cortex slot translation), `evaluate` 2, `introspect` 4 (GET_DDL), `probe` 12 (UI check of which
databases the role can use). Results are merged by input index.

## 9. Connection and security

- `core/db.py`: `ConnectorDb` (Snowflake connector, `paramstyle=qmark`), or `SnowparkDb` if a Snowpark session
  is active. All values are bound; identifiers in `SHOW ... IN` / `USE ...` statements are quoted by `qident()`.
- Connection choice (`resolve_connection_name`): sidebar picker → `$SNOWFLAKE_DEFAULT_CONNECTION_NAME` →
  `default_connection_name` in `~/.snowflake/config.toml` → the only connection in `connections.toml`.
  Authentication (Okta SSO, key-pair, password, PAT) is whatever that named connection defines.
- Each browser session has its own connection. The sidebar can change the **role** (`USE ROLE`, limited to
  `CURRENT_AVAILABLE_ROLES()`), **secondary roles** (`USE SECONDARY ROLES ALL|NONE`) and **warehouse**
  (`USE WAREHOUSE`). Everything after that (database lists, GET_DDL, Cortex calls) runs with that role.
- Database listing hides databases the role can see in `SHOW DATABASES` but cannot use (no USAGE); access
  errors are shown as messages, never as crashes.
- The app reads DDL only. The only data queries are optional parity profiles (`COUNT/MIN/MAX`) when enabled.

## 10. Progress reporting

- `Runner.on_step(stage, node)` fires before every LangGraph node; `RunContext.progress(msg)` carries detail
  from inside agents (e.g. `GET_DDL 4/10: TABLE ORDERS`, `Cortex translated 2/3: <node>`).
- `master.node_order()` gives the node sequence used for the percentage; `master.step_text()` gives the
  plain-language description shown in the UI.
- Progress callbacks can run on worker threads; the UI attaches the Streamlit script context before drawing.
  Display failures never affect a run.

## 11. Cost tracking

- **Recording:** every Cortex call stores its Snowflake **query id** (`ConnectorDb.query_with_id` → `cursor.sfqid`) in
  `obs_llm_calls`. The connection sets `QUERY_TAG = 'sf2dbx_migration_app'`, so billing views can isolate the app.
- **Estimated credits** (`core/analytics.estimate_credits`): `tokens_in × input_rate + tokens_out × output_rate`, per
  model, in AI credits per 1M tokens, from `config/pricing.yaml` with overrides in `.state/settings.json`.
- **Billed credits** (`analytics.fetch_billed`): exact `CREDITS` from
  `SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY`, matched by query id (last 30 days, lags by hours),
  written back with `SqliteObs.set_billed`.
- **Calibration** (`analytics.calibrate`): least-squares fit of the two rates per model from billed rows. On this
  account `claude-4-sonnet` fits exactly 1.8 / 9.0 credits per 1M input / output tokens.
- **Run statistics** (`analytics.run_summary`): per-run objects, MAPPED/HYBRID/AI, slots, findings, revisions,
  files, duration, calls, tokens and credits, used by the History and Financial pages.
- **Scope:** Cortex COMPLETE credits only (no warehouse compute, no Cortex Code, no Databricks). Currency is an
  optional user-entered USD-per-credit price.

## 12. Compared with the DOMO → Databricks blueprint

Kept: 7 agents in order; gen → eval → HITL factory; single LLM entry; role → model registry; shared schemas
(`Verdict`, `Issue` with addressable `ref`, `EvalResult`, `HumanFeedback`, `GenResult`); `CONTRACTS_VERSION`;
LangGraph park/resume; nodes return deltas; merge-by-index; named pools with a global ceiling of 16;
deterministic judges; MAPPED / HYBRID / AI origins with mapper registry and `__PENDING_n__` slots;
stream + task + MERGE collapsed into one `apply_changes`.

Differences:

| Blueprint | This app |
|---|---|
| Postgres live checkpointer + Delta audit | SQLite checkpointer + optional Snowflake audit table |
| Prompt layers P1 human > P2 run-level user prompt > P3 evaluator > P4 base, stored per agent | no run-level user prompt; human and evaluator feedback share one layer; hard constraints in the system prompt |
| AI generation/review in standardization, validation, classifier | those agents are deterministic |
| Validation and classifier launch Databricks Jobs (Jobs-as-a-tool) | `DatabricksRunner` stub only; nothing executes on Databricks |
| PIPE + COPY INTO → Auto Loader in V1 | not implemented |
| ETL master, stage injection, sub-phase UI labels, per-agent notebooks | not implemented |
| AI slot fan-out as graph fan-out | thread pool inside the codegen node, merged by index |
