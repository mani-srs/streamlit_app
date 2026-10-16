# User guide

## 1. Start the app

```powershell
cd C:\Users\MS114310\streamlit_app
.\.venv\Scripts\streamlit run streamlit_app.py                       # http://localhost:8501
```

The app uses a named connection from `~/.snowflake/connections.toml`. If there is only one, it is picked
automatically; otherwise choose it in the sidebar (or set `$env:SNOWFLAKE_DEFAULT_CONNECTION_NAME` first).
The first Snowflake action may open a browser for SSO. If the connection fails, the sidebar says
**No Snowflake connection** and only *Paste DDL* / *Sample slice* modes are available (no Cortex).

### Sidebar

- **Snowflake connection**: named connections from `connections.toml`. Changing it reconnects. When connecting
  fails, **Connection error** shows the exact message and **Retry connection** tries again.
- **Role**: roles granted to your user (`CURRENT_AVAILABLE_ROLES()`); switching runs `USE ROLE` for your session only.
  Everything afterwards (database lists, `GET_DDL`, Cortex calls) uses that role.
- **Also use all my secondary roles**: `USE SECONDARY ROLES ALL`, so privileges from all your roles apply together.
- **Warehouse**: used for metadata queries, Cortex calls and parity profiling. A role without an active warehouse shows a warning.
- **App state schema** (optional): `APP_DB.APP_SCHEMA` where `setup.sql` was run. When set and valid,
  artifacts, observability and an audit copy of each run are also written to Snowflake. Run state is
  always checkpointed locally in `.state/runs.sqlite`.
- **Runs stored in**: shows where runs are persisted.

## 2. New run

1. Choose a **Source**:
   - **Live Snowflake schema** – pick database and schema, then choose objects. By default only databases the
     current role can actually use are listed (**Only show databases my role can use**); databases you can see but
     not use show a lock message with the missing privilege instead of an error:
     - **Object types** chips and **Filter by name** (case-insensitive, regex allowed) narrow the list.
     - **Select all (n)** / **Clear all** act on the rows currently shown; ticks are kept while you filter.
     - Schemas with more than 50 objects start with nothing ticked; above 100 selected you get a warning to split the run.
   - **Paste DDL** – paste `GET_DDL` output or hand-written `CREATE` statements separated by `;`, and give a
     default database/schema for unqualified names.
   - **Sample slice** – the bundled `SALES_DB.CORE` example covering every supported object type.
2. **Options**:
   - *Target Unity Catalog* – UC catalog for all objects (default: lower-case Snowflake database name).
   - *Use Cortex* – off = zero model calls (AI slots stay unfilled and are reported).
   - *Profile Snowflake tables now* – runs `COUNT/MIN/MAX` on selected source tables for the parity manifest (live mode only; costs warehouse time).
   - *Review gates* – switch the human gate per agent on/off.
3. **Start run**. While it runs you see:
   - a **progress bar** ("Step n of 7: <step>") that pauses at review gates,
   - the **current action** in plain words (e.g. "Converting objects to Databricks code"),
   - **live detail** such as "GET_DDL 4/10: TABLE ORDERS" or "Cortex translated 2/3: ...",
   - an expandable **Activity log** with every step.
   The app then opens the Review gate (if a gate was reached) or the Run monitor.

## 3. Run monitor

- Pick any run (runs persist across app restarts).
- Seven stage cards: done / awaiting review / failed / aborted / pending, the gate flag and iteration count.
- **Open review gate** when awaiting review; **Retry stage** on failure (re-runs only the failed step);
  **View outputs** when done.
- **Lineage** graph from discovery (dashed nodes = referenced but outside the slice).
- **Stage timings** from observability.

## 4. Review gate

Shows the stage, verdict (`PASS` / `REVISE` / `FAIL`), confidence and iteration, then:

| Stage | Artifact shown |
|---|---|
| Discover lineage | object table with upstreams, detected CDC patterns |
| Migration plan | rendered plan and markdown source |
| Generate code | MAPPED/HYBRID/AI counts, AI slots filled, every generated file |
| Medallion layers | layer assignment table and the layer files |

**Evaluator issues** table: tick **Fix** (the agent must address it) or **Ignore** (the agent must not change it).
Errors and warnings are pre-ticked as Fix.

**Notes for the agent**: free-form guidance for the next iteration. Special directives:
- `exclude: DB.SCHEMA.OBJECT` (discovery) – drop an object from scope
- `layer: DB.SCHEMA.OBJECT = gold` (classifier) – force a layer

**Hard constraints** (one per line): placed at the top of the model prompt. Lines of the form
`must contain: X` or `must not contain: X` are also checked automatically on the output; a violation
appears as an `HC-*` error.

Buttons:
- **Approve and continue** – accept this iteration; the run moves on.
- **Request revision** – regenerate with your feedback (needs at least one ticked issue, note or constraint).
- **Abort run** – stop the run.

## 5. Outputs

- Metrics: files, objects, MAPPED/HYBRID/AI split, judge errors and warnings.
- **Download bundle (.zip)**.
- Tabs: *Files* (by folder), *Plan*, *Layers*, *Judges* (filter by severity; rule table with finding counts).

### Bundle layout

```
README.md                         deploy order, placeholders, object map
plan.md                           migration plan + risk register
ddl/01_tables.sql                 UC tables
ddl/02_views.sql                  UC views
pipeline/bronze.py|silver.py|gold.py   DLT sources, one per medallion layer
pipeline/pipeline_settings.json   DLT pipeline definition (serverless, the three layer files)
jobs/<schema>_<root_task>.json    one Job per Snowflake task DAG (created PAUSED)
jobs/pipeline_refresh.json        refresh schedule replacing dynamic-table TARGET_LAG
notebooks/proc_*.sql|.py          converted procedures
notebooks/task_*.sql              task bodies that are plain SQL
validation/judges_report.json     all quality-gate findings
validation/parity_manifest.json   parity queries (+ Snowflake results if profiled)
validation/parity_snowflake.sql   run on Snowflake
validation/parity_databricks.sql  run on Databricks, then compare
```

### Deploying the bundle on Databricks

1. Run `ddl/01_tables.sql` on a SQL warehouse (and backfill data – not generated).
2. Import `pipeline/*.py` into the workspace; create the pipeline from `pipeline/pipeline_settings.json`; full refresh.
3. Run `ddl/02_views.sql`.
4. Create jobs from `jobs/*.json`; they are PAUSED.
5. Run the two parity SQL files and compare row counts, non-null counts and min/max per column.
6. Unpause jobs.

Replace placeholders first: `${NOTEBOOK_ROOT}` (workspace folder of the imported bundle), `${PIPELINE_ID}`
(created pipeline), `${SQL_WAREHOUSE_ID}` (warehouse for SQL notebooks).

## 6. History

Every run on this machine (kept in `.state/runs.sqlite`, so it survives restarts).

- **Filters:** date range, status, slice text.
- **Summary:** runs, completion rate, objects, share converted by rules (no AI), AI slots filled, check errors,
  revisions, estimated AI cost.
- **Charts:** runs per day by status, how objects were converted (rules / rules + AI / AI), objects by type,
  most frequent check findings.
- **Runs table:** one row per run with objects, MAPPED/HYBRID/AI, slots, errors/warnings, revisions, files,
  duration, LLM calls, tokens, estimated and billed credits. **Download as CSV**. Select a row for details and
  **Open in** Run monitor / Review gate / Outputs / Financial.

## 7. Financial

Cortex AI cost of the app's LLM calls. Snowflake bills Cortex in **AI credits**.

- **Settings:** price per AI credit (USD, from your contract, optional) and a monthly credit budget (optional).
  Saved in `.state/settings.json`.
- **Cost overview:** calls, tokens in/out, **estimated** credits (tokens × per-model rate, instant), **billed**
  credits (exact, once matched), cost per run / per object / per AI slot, budget progress for the month,
  daily trend, cost by step and role, by model, and the most expensive runs.
- **Billing (Snowflake) tab:**
  - **Refresh billed credits:** looks up each recorded call's Snowflake query id in
    `SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY` and stores the exact credits. That view lags by a
    few hours, so recent calls match later.
  - **Calibrate rates from billing:** fits credits per 1M input/output tokens per model from your billed calls
    and uses them for estimates (for `claude-4-sonnet` this account bills 1.8 in / 9.0 out).
  - **Snowflake billing view:** "This app" (calls carrying the app's query tag) or "All my Cortex AI functions"
    (also calls made before tagging), per day and model, last 30 days.
  - Needs `SNOWFLAKE.USAGE_VIEWER` (or IMPORTED PRIVILEGES on the SNOWFLAKE database).
- **All calls tab:** every call with its query id, tokens, latency, estimated and billed credits; CSV download.
- **Not included:** warehouse compute for those queries, metadata queries, Cortex Code usage while building the
  app, and any Databricks cost.

## 8. Observability

Per run or all runs: Cortex calls (count, errors, tokens in/out, median latency, tokens by stage/role/model)
and node durations by status. Calls and node timings are stored in `.state/runs.sqlite` and survive restarts.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Sidebar: **No Snowflake connection** | Open **Connection error** in the sidebar for the exact message, fix it, press **Retry connection**. |
| `Default connection with name 'default' cannot be found` | More than one connection and none chosen: pick one in the sidebar, or set `default_connection_name` in `~/.snowflake/config.toml`. |
| `Insufficient privileges to operate on database ...` | The role can see but not use that database. Keep **Only show databases my role can use** on, switch **Role**, or turn on **Also use all my secondary roles**. |
| No warehouse warning / Cortex calls fail | The current role has no active warehouse: choose one under **Warehouse**. |
| `SRC001 procedure body is empty` | `GET_DDL` shows procedure bodies only to the owning role. Switch to that role and re-extract. |
| `STR002 unresolved slot` | Cortex was off or failed. Enable *Use Cortex* and request a revision, or fill the code by hand. |
| `DET003 not a Databricks function` | The model used a function from another database (e.g. `pg_sleep`). Request a revision (tick the issue as Fix) or edit by hand. |
| `LIN003 source calls ... but the generated code never invokes it` | A procedure call was lost in conversion. Request a revision, or add the call manually. |
| `LIN002 writes to ... DLT-managed table` | A procedure/task writes into a table the pipeline now owns. Redesign that logic inside the pipeline. |
| `DISC-EXT` warnings | An object outside the slice is referenced. Add it to the slice or make sure it exists in Databricks. |
| App schema not usable | `setup.sql` not run in that schema, or the role cannot read `RUN_CHECKPOINTS`. |
| Repeated SSO pop-ups / `keyring` warning in the terminal | Install `snowflake-connector-python[secure-local-storage]` to cache the token. |
| `pyarrow` UserWarning in the terminal | Reinstall from `requirements.txt` (pins `pyarrow<24`, which the connector requires on Python 3.14). |
| Port 8501 already in use | The app is already running; use it, or start another with `--server.port 8502`. |
