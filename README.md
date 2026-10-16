# Snowflake → Databricks Migration App

A local Streamlit application that converts a slice of a Snowflake schema (tables, views, dynamic
tables, streams, tasks, stored procedures) into a deployable Databricks bundle: Unity Catalog DDL,
Lakeflow Declarative Pipelines (DLT) Python split into bronze/silver/gold, notebooks, Databricks Job
JSON, and parity-check SQL.

Seven agents run in a fixed order on **LangGraph**. Deterministic converters do most of the work;
**Snowflake Cortex** (`SNOWFLAKE.CORTEX.COMPLETE`) is used only where rules cannot convert the
source (non-SQL procedures, unparseable SQL) and to write/review the migration plan. Four review gates
let a human approve, revise or abort before the run continues.

```
Snowflake schema ──► 1 Connect ─► 2 Normalize ─► 3 Discover* ─► 4 Plan* ─► 5 Codegen* ─► 6 Validate ─► 7 Classify* ──► bundle.zip
                                                      * = human review gate (default)
```

## Quick start

```powershell
# 1. Python 3.11+ virtual environment
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

# 2. Snowflake connection: any named connection in ~/.snowflake/connections.toml (SSO, key-pair, password...).
#    Picked automatically if it is the only one; otherwise choose it in the sidebar, or set:
#    $env:SNOWFLAKE_DEFAULT_CONNECTION_NAME = "<connection name>"

# 3. Run the app  ->  http://localhost:8501
.\.venv\Scripts\streamlit run streamlit_app.py
```

No Snowflake connection? The app still runs in **Paste DDL** / **Sample slice** mode, without Cortex.

Try it without the UI:

```powershell
.\.venv\Scripts\python scripts\local_dryrun.py                    # sample slice, zero LLM calls
.\.venv\Scripts\python scripts\local_dryrun.py --llm --connection SRSDISTRIBUTION-SE58001 --out out\bundle
.\.venv\Scripts\python -m pytest tests -q                         # contract + UI tests
```

## Documentation

| Document | Read it when you want to... |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | run a migration, use the review gates, understand and deploy the output bundle |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | understand the LangGraph design, state, HITL, LLM usage and persistence |
| [docs/CONVERSION_REFERENCE.md](docs/CONVERSION_REFERENCE.md) | know exactly what each Snowflake object/type/function becomes, and what is not supported |
| [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | add a mapper, judge or agent; the contracts and golden rules; testing |

## Folder map

| Folder | Contents | README |
|---|---|---|
| `streamlit_app.py` | App entry point (navigation + sidebar) | – |
| `app_pages/`, `ui/` | The seven UI pages (incl. History and Financial) and shared UI runtime | [app_pages/README.md](app_pages/README.md) |
| `agents/` | The 7 agents + `master.py` (order, HITL defaults, backend) | [agents/README.md](agents/README.md) |
| `core/` | Frozen contracts: LangGraph runner, node factory, state, LLM wrapper, prompts, persistence | [core/README.md](core/README.md) |
| `converters/` | Deterministic Snowflake parsing and conversion (DDL parser, sqlglot dialect, types, cron, lineage) | [converters/README.md](converters/README.md) |
| `judges/` | Deterministic quality gates (never call a model) | [judges/README.md](judges/README.md) |
| `config/` | Model registry, medallion rules and Cortex pricing | [config/README.md](config/README.md) |
| `samples/` | `sample_slice.sql` (every supported object type) and `task_refresh_dpc_uat.sql` (task with a `BEGIN … END` body of CALLs) | – |
| `scripts/` | Dry run and live smoke scripts | [scripts/README.md](scripts/README.md) |
| `tests/` | Contract tests (mocked LLM) and headless UI test | [tests/README.md](tests/README.md) |
| `setup.sql` | Optional Snowflake tables for artifacts, observability and a run-state audit copy | – |
| `.state/` | Local LangGraph checkpoints (`runs.sqlite`), created on first run, git-ignored | – |

## Key facts

- **Orchestration:** LangGraph `StateGraph`, SQLite checkpointer, `interrupt()` / `Command(resume=...)` for review.
- **LLM:** only through `core/llm.py` → `SNOWFLAKE.CORTEX.COMPLETE`; default `claude-4-sonnet`, fallback `llama3.1-70b`. Toggle off per run for zero model calls.
- **Auth:** the app never stores credentials; it uses your named Snowflake connection (picked automatically when there is only one).
- **Role and warehouse:** switchable in the sidebar per browser session (`USE ROLE`, secondary roles, `USE WAREHOUSE`); only databases the role can use are listed.
- **Progress:** a step-by-step progress bar with live detail (GET_DDL per object, Cortex per AI slot) while a run executes.
- **Generic:** no account, database or role names in code; any Snowflake account where the connecting role can read the source DDL.
- **Quality gates:** 17 deterministic rules (structure, determinism, semantics, lineage, policy, Snowflake specifics) run on every iteration; see [judges/README.md](judges/README.md).
- **History and cost:** a History page for every run and a Financial page with estimated and Snowflake-billed Cortex credits (matched per call by query id), optional USD price and monthly budget.
- **Not executed on Databricks:** generated code is validated statically; the Databricks side of parity is emitted as SQL for you to run.
