# tests/

```powershell
.\.venv\Scripts\python -m pytest tests -q
```

No Snowflake connection is needed: the LLM is mocked, and the UI test points `SNOWFLAKE_HOME` at an empty
folder so it never finds a connection or opens SSO. 35 tests.

| File | Covers |
|---|---|
| `contracts/conftest.py` | `mock_llm` fixture (`core.llm.set_backend`), `FakeDb`, the sample DDL, `ROOT` |
| `contracts/test_core.py` | contracts version, `Issue` ref rules, `EvalResult` bounds, merge-by-index, ordered pools, prompt priority/truncation, hard-constraint post-check, LLM fallback + logging; **LangGraph runner**: park/resume without regeneration, nodes don't mutate input, checkpoints survive a new runner (SQLite), abort, auto-revise limit, failure + retry of only the failed node |
| `contracts/test_pipeline.py` | DDL parser on all types, quoted identifiers, transpile determinism and refusal, type map, cron, CDC detection, judges catching violations (incl. DET003 without false positives on valid Databricks functions), **`BEGIN ... END` task bodies kept whole, `SYSTEM$WAIT` not a dependency, LIN003 on lost `CALL`s** (`samples/task_refresh_dpc_uat.sql`), full pipeline with no LLM (zero calls, MAPPED coverage, 3 layer files, no LIN003), full pipeline with mocked LLM (slots filled, all 3 roles used), reviewer feedback reaching the translator, empty procedure bodies, `exclude:` and `layer:` directives |
| `contracts/test_analytics.py` | rate calibration recovers the billed 1.8 / 9.0 rates from real billing rows, estimates match billing to 1e-9, settings/overrides, `SqliteObs` survives a restart and matches billed credits, every Cortex call records its query id, run summary figures |
| `ui/test_app_flow.py` | headless `AppTest`: sample run through every review gate and every page (incl. History and Financial) with no exceptions; History and Financial also render with no data |

When you change behaviour, add a test here; when you change a `core/` contract, bump `CONTRACTS_VERSION`
and its assertion in `test_core.py`.
