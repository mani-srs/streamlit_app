# scripts/

Command-line helpers. Run from the project root with the virtual environment's Python.

| Script | What it does |
|---|---|
| `local_dryrun.py` | Runs `samples/sample_slice.sql` (or `--ddl <file>`) through all 7 agents, auto-approving every gate; prints gate verdicts, files, judge counts and LLM call count |
| `live_smoke.py` | Read-only live check: lists a real schema, picks up to `--per-type N` objects per type, runs all agents with auto-approve |

```powershell
# no Snowflake, no model calls
.\.venv\Scripts\python scripts\local_dryrun.py --out out\bundle

# your own DDL file, e.g. the task sample with a BEGIN ... END body
.\.venv\Scripts\python scripts\local_dryrun.py --ddl samples\task_refresh_dpc_uat.sql --out out\task

# with Cortex (uses the named connection; may open SSO). --connection is optional when
# connections.toml has a single connection
.\.venv\Scripts\python scripts\local_dryrun.py --llm --connection SRSDISTRIBUTION-SE58001 --catalog main --out out\bundle

# live extract from a real schema (SHOW + GET_DDL only; add --llm to enable Cortex)
.\.venv\Scripts\python scripts\live_smoke.py --connection SRSDISTRIBUTION-SE58001 --database DATAWAREHOUSE_DEV --schema HLSG_DW_CONSUMPTION --per-type 3
```

Both scripts use an in-memory checkpointer, so their runs don't appear in the app. They run with the
connection's default role; the role switcher exists only in the UI.
