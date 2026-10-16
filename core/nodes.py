"""generate -> evaluate -> HITL, as three LangGraph nodes per stage (built once, reused by every agent).

Agent callbacks (unchanged contract):
  gen(ctx, state, feedback: list[HumanFeedback]) -> GenResult
  evaluate(ctx, state, gen: GenResult) -> EvalResult
  save(ctx, gen, iteration) -> None          (persist artifact files)
  finalize(ctx, state, gen, eval) -> output  (JSON-able stage output)

Each step works on a copy of the stage scratch and returns a delta {"scratch": {stage: ...}, ...}.
review step, HITL on : interrupt(); accept -> finalize; revise -> feedback appended, iteration+1 (router -> gen)
review step, HITL off: finalize unless eval says revise and iterations remain (eval issues fed back).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable

from core.graph import RunContext
from core.schemas import EvalResult, GenResult, HumanFeedback, Issue
from core.state import RunState, RunStatus


def feedback_notes(fbs: list[HumanFeedback], issues_by_iter: dict[int, list[dict]]) -> tuple[list[str], list[str]]:
    """Turn reviewer feedback into prompt notes + hard constraints (P4)."""
    notes, hard = [], []
    for it, fb in enumerate(fbs):
        issues = {i["id"]: i for i in issues_by_iter.get(it, [])}
        for iid in fb.accepted_issue_ids:
            i = issues.get(iid)
            if i:
                notes.append(f"Fix {i['ref']}: {i['message']}" + (f" (suggested: {i['suggested_fix']})" if i.get("suggested_fix") else ""))
        for iid in fb.rejected_issue_ids:
            i = issues.get(iid)
            if i:
                notes.append(f"Do NOT change for issue at {i['ref']} ({i['message']}); reviewer rejected it.")
        if fb.freeform.strip():
            notes.append(fb.freeform.strip())
        hard.extend(fb.hard_constraints)
    return notes, list(dict.fromkeys(hard))


@dataclass
class ReviewableStage:
    stage: str
    gen: Callable
    evaluate: Callable
    finalize: Callable
    save: Callable | None = None
    max_iters: int = 3

    def _sc(self, state: RunState) -> dict:
        sc = copy.deepcopy(state.scratch.get(self.stage, {}))
        sc.setdefault("iteration", 0)
        sc.setdefault("feedback", [])
        sc.setdefault("issues_by_iter", {})
        return sc

    @staticmethod
    def _gen_of(sc: dict) -> GenResult:
        g = sc["gen"]
        return GenResult(artifact=g["artifact"], node_keys=tuple(g["node_keys"]), meta=g["meta"])

    def gen_step(self, ctx: RunContext, state: RunState) -> dict:
        sc = self._sc(state)
        it = sc["iteration"]
        g: GenResult = self.gen(ctx, state, [HumanFeedback.from_dict(f) for f in sc["feedback"]])
        if self.save:
            self.save(ctx, g, it)
        sc["gen"] = g.to_dict()
        return {"scratch": {self.stage: sc}}

    def eval_step(self, ctx: RunContext, state: RunState) -> dict:
        sc = self._sc(state)
        e: EvalResult = self.evaluate(ctx, state, self._gen_of(sc))
        sc["eval"] = e.to_dict()
        sc["issues_by_iter"][str(sc["iteration"])] = [i.to_dict() for i in e.issues]
        return {"scratch": {self.stage: sc}}

    def review_step(self, ctx: RunContext, state: RunState) -> dict:
        sc = self._sc(state)
        it = sc["iteration"]
        g, e = self._gen_of(sc), EvalResult.from_dict(sc["eval"])
        done = lambda: {"outputs": {self.stage: self.finalize(ctx, state, g, e)}, "completed": [self.stage],
                        "scratch": {self.stage: sc}}
        if state.hitl.get(self.stage, False):
            fb = ctx.interrupt(f"iter{it}", {"iteration": it, "artifact": g.artifact, "eval": e.to_dict(), "meta": g.meta})
            if fb.abort:
                return {"status": RunStatus.ABORTED, "error": fb.freeform or f"reviewer aborted at {self.stage}"}
            if fb.accepted:
                return done()
            sc["feedback"].append(fb.to_dict())
        else:
            if e.verdict.value != "revise" or it + 1 >= self.max_iters:
                return done()
            auto = HumanFeedback(accepted_issue_ids=tuple(i.id for i in e.issues if i.severity.value != "info"))
            sc["feedback"].append(auto.to_dict())
        sc["iteration"] = it + 1
        return {"scratch": {self.stage: sc}}


def build_gen_eval_hitl(stage: str, gen: Callable, evaluate: Callable, finalize: Callable,
                        save: Callable | None = None, max_iters: int = 3) -> ReviewableStage:
    return ReviewableStage(stage, gen, evaluate, finalize, save, max_iters)


def issues_for(state: RunState, stage: str) -> dict[int, list[dict]]:
    return {int(k): v for k, v in state.scratch.get(stage, {}).get("issues_by_iter", {}).items()}


def verdict_from(issues: list[Issue], confidence: float = 0.9, summary: str = "") -> EvalResult:
    sev = {i.severity.value for i in issues}
    v = "fail" if "error" in sev else ("revise" if "warn" in sev else "pass")
    return EvalResult(verdict=v, confidence=confidence, issues=tuple(issues), summary=summary)
