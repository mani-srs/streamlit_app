# Agent 4 — md_plan

Writes the migration plan. **Review gate on**; up to 2 automatic iterations when the gate is off.

- **gen**:
  1. `skeleton()` builds a deterministic draft by running the codegen mapper dry (`map_all`) to predict each
     object's target and origin, plus `risk_register()` (regex rules in `RISKS` over the DDL).
  2. If Cortex is on, the **planner** role rewrites/enriches the draft. The result is accepted only if it keeps
     all six required sections; otherwise the deterministic draft is kept (`meta.planner_rejected`).
  3. Reviewer notes and hard constraints are passed as prompt feedback; hard constraints are post-checked.
- **eval**: required sections present (error), every in-scope object mentioned (warn), hard-constraint
  violations (error), plus the **evaluator** role's JSON issues when Cortex is on (failures are recorded as info, never blocking).

Required sections: `1. Scope`, `2. Object mapping`, `3. Risk register`, `4. Cutover order`, `5. Validation`, `6. Open questions`.

Output: `{"plan_md", "risks", "source": "deterministic" | "planner"}`. Artifact: `plan.md` (also in the bundle).
