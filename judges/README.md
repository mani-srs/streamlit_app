# judges/ — deterministic quality gates

`rules.py` contains hard-coded checks. **They never call a model.** `run_all(bundle) -> list[Issue]` is used
by the codegen evaluator (every iteration) and by the validation agent (final report).

Input bundle: `objects`, `graph`, `fragments`, `files`, `targets` (Snowflake id → Databricks name),
`det_diff` (node ids whose MAPPED output changed on re-run).

| Rule | Severity | Checks |
|---|---|---|
| STR001 | error | every in-scope object has a `NODE: <id> \|` marker |
| STR002 | error | no unresolved `__PENDING_n__` slots |
| STR003 | error / warn | Python compiles, JSON parses / SQL parses as Databricks SQL |
| DET001 | error | MAPPED output reproducible |
| DET002 | error | no Snowflake-only constructs in code lines |
| DET003 | error | no functions from other databases that Databricks lacks (model mistakes such as `pg_sleep`) |
| SEM001 | error | source columns / view aliases present in generated code |
| SEM004 | warn | lossy type conversions (declared by mappers) |
| SEM005 | warn | time-zone / session-sensitive functions in source |
| LIN001 | warn | consumers reference their in-scope upstreams |
| LIN002 | error | no writes into DLT-managed tables from tasks/procedures |
| LIN003 | error | every procedure CALLed in the source is still invoked in the output |
| POL001 | error | no role switches, privileged roles, destructive DDL, grants, credentials, Snowflake warehouse names |
| SNF001/2/3 | info / warn | TARGET_LAG, task WHEN, schedule conversion notes |
| SRC001 | error | source definition visible (e.g. procedure body) |

Mapper-declared findings: a mapper appends `"RULE|message"` to `Fragment.notes`; `run_all()` turns each into an
`Issue` (`INFO`/`SNF001` → info, `SRC001` → error, others → warn).

Comment lines (`--`, `#`) are ignored by DET002 and POL001, so node markers and explanations don't trigger them.
