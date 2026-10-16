# app_pages/ and ui/ — Streamlit UI

Entry point: `streamlit_app.py` (page config, `st.navigation`, sidebar). Pages are plain scripts; shared
logic lives in `ui/runtime.py`.

| Page | File | Purpose |
|---|---|---|
| New run | `app_pages/new_run.py` | choose source (live schema / paste DDL / sample), pick objects (type chips, name filter, **Select all / Clear all**), options, review gates, start |
| Run monitor | `app_pages/monitor.py` | stage cards, status, retry failed stage, lineage graph, stage timings |
| Review gate | `app_pages/review.py` | artifact viewer, evaluator issues (Fix / Ignore), notes, hard constraints, approve / revise / abort |
| Outputs | `app_pages/outputs.py` | metrics, zip download, files by folder, plan, layers, judge findings |
| History | `app_pages/history.py` | every run: filters, summary metrics, runs per day, rules-vs-AI split, objects by type, top findings, runs table (CSV), open a run in other pages |
| Financial | `app_pages/financial.py` | Cortex cost: price/budget settings, estimated vs billed credits, per run/object/slot, trend, by step/role/model; Billing tab (refresh billed credits, calibrate rates, Snowflake billing view); all calls (CSV) |
| Observability | `app_pages/observability.py` | Cortex calls, tokens, latency, node durations |

`ui/stats.py` loads what History and Financial share: `calls_frame()` (calls + estimated credits),
`runs_frame()` (one `analytics.run_summary` per run), `settings()`, `rates()`, `date_filter()`, `money()`.

Sidebar (`streamlit_app.py`): **Snowflake connection** picker, **Role**, **Also use all my secondary roles**,
**Warehouse**, optional **App state schema**, connection status, and on failure a **Connection error** expander
with **Retry connection**.

## ui/runtime.py

| Function | Purpose |
|---|---|
| `init_state()` | session-state defaults (`run_id`, `app_schema`, ...) |
| `connection_name()` | sidebar choice, else `core.db.resolve_connection_name()` (env var → `config.toml` default → the only connection in `connections.toml`) |
| `get_db()` | `core.db.connect(connection_name())` → `ConnectorDb`, cached per browser session; `None` + `db_error` on failure |
| `reconnect()` | drop the cached connection (after changing connection or pressing Retry) |
| `session_info(db)` | current role, warehouse, secondary-roles flag, available roles (`CURRENT_AVAILABLE_ROLES()`), warehouses; cached until changed |
| `apply_session(db, role=, warehouse=, secondary_all=)` | `USE ROLE` / `USE WAREHOUSE` / `USE SECONDARY ROLES ALL|NONE` on this session's connection; clears object selections |
| `context_key(db)` | cache key (connection + role + secondary roles) for role-dependent lists |
| `usable_databases(db, names)` | databases the role can actually use (parallel `SHOW SCHEMAS ... LIMIT 1` probes, pool `probe`) |
| `backend()` | builds a `master.Backend` with LangGraph checkpoints in `.state/runs.sqlite`; adds Snowflake tables if a valid app schema is entered |
| `drive(b, state, feedback=None)` | runs or resumes the graph with a **progress bar** ("Step n of 7"), the current action in plain words, live detail lines and an **Activity log** |
| `status_badge()`, `lang_for()`, `persistence_label()` | display helpers |

Notes:
- A run executes inside the button's request; the browser waits while a stage runs. Review gates are where
  a run can be left and continued later (even after restarting the app).
- Progress updates can come from worker threads (parallel GET_DDL, Cortex fan-out); `drive()` attaches the
  Streamlit script context to the calling thread before updating the page.
- Role-dependent `st.cache_data` functions take the context key as a normal argument: Streamlit does not
  hash arguments whose names start with `_`, so such a key would be ignored.
- Tests set `SNOWFLAKE_HOME` to an empty folder and `session_state["state_path"]` to a temporary file, so they
  never sign in to Snowflake or touch `.state/`.
- Theme: `.streamlit/config.toml` (Snowflake colours).
