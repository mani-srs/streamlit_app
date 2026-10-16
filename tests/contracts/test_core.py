"""Contract tests for core/: schemas, reducers, concurrency, prompts, LLM path, graph park/resume."""
import random
import time

import pytest

from core import CONTRACTS_VERSION, llm
from core.concurrency import map_ordered
from core.graph import RunContext, Runner
from core.nodes import build_gen_eval_hitl
from core.persistence import MemoryObs, MemoryStore
from core.prompts import PromptParts, assemble, post_check, strip_fences
from core.schemas import ContractViolation, EvalResult, GenResult, HumanFeedback, Issue, Severity
from core.state import RunState, RunStatus, merge_by_index


def test_contracts_version():
    assert CONTRACTS_VERSION == "1.1.0"


def test_issue_ref_must_be_addressable():
    Issue("X-1", Severity.WARN, "node:DB.S.T", "ok")
    Issue("X-2", "error", "line:a.py:3", "ok")
    with pytest.raises(ContractViolation):
        Issue("X-3", Severity.WARN, "somewhere", "bad ref")


def test_eval_confidence_bounds_and_roundtrip():
    e = EvalResult("revise", 0.5, (Issue("A", "warn", "file:x", "m"),), "s")
    assert EvalResult.from_dict(e.to_dict()) == e
    with pytest.raises(ContractViolation):
        EvalResult("pass", 1.5)


def test_merge_by_index():
    assert merge_by_index(3, [(2, "c"), (0, "a"), (1, "b")]) == ["a", "b", "c"]
    with pytest.raises(ContractViolation):
        merge_by_index(2, [(0, "a"), (0, "b")])
    with pytest.raises(ContractViolation):
        merge_by_index(2, [(0, "a")])
    with pytest.raises(ContractViolation):
        merge_by_index(1, [(3, "x")])
    assert merge_by_index(2, [(1, "b")], fill=lambda i: None) == [None, "b"]


def test_map_ordered_keeps_input_order_under_jitter():
    def slow(x):
        time.sleep(random.random() / 50)
        return x * 10
    assert map_ordered("translate", slow, list(range(20))) == [x * 10 for x in range(20)]


def test_map_ordered_on_error():
    def boom(x):
        if x == 2:
            raise ValueError("no")
        return x
    assert map_ordered("translate", boom, [0, 1, 2, 3], on_error=lambda i, e: f"err{i}") == [0, 1, "err2", 3]


def test_prompt_priority_and_truncation():
    p = PromptParts(system="SYS", task="TASK", context="C" * 5000, feedback=["fix A"], hard_constraints=["must not contain: DROP"])
    sys_msg, user = assemble(p, budget=1000)
    assert "HARD CONSTRAINTS" in sys_msg["content"] and "must not contain: DROP" in sys_msg["content"]
    u = user["content"]
    assert u.index("TASK") < u.index("REVIEWER FEEDBACK") < u.index("CONTEXT")
    assert "fix A" in u and "[context truncated]" in u  # only P3 is truncated


def test_post_check():
    assert post_check("DROP TABLE x", ["must not contain: drop table"])
    assert post_check("select 1", ["must contain: CLUSTER BY"])
    assert not post_check("CLUSTER BY (a)", ["must contain: cluster by", "be nice"])
    assert strip_fences("```python\nx = 1\n```") == "x = 1"


class _Ctx:
    def __init__(self, enabled=True):
        self.db, self.obs, self.run_id, self.llm_enabled = object(), MemoryObs(), "r1", enabled


def test_llm_single_path_logs_and_falls_back():
    seen = []

    def backend(db, model, messages, options):
        seen.append(model)
        if model == "claude-4-sonnet":
            raise RuntimeError("model down")
        return '{"a": 1}', 3, 4

    llm.set_backend(backend)
    try:
        ctx = _Ctx()
        r = llm.complete(ctx, "evaluator", [{"role": "user", "content": "x"}], json_mode=True)
        assert r.model == "llama3.1-70b" and r.parsed == {"a": 1} and r.retries == 2
        assert seen == ["claude-4-sonnet", "claude-4-sonnet", "llama3.1-70b"]
        statuses = [c["status"] for c in ctx.obs.llm_calls("r1")]
        assert statuses == ["error", "error", "ok"]
        with pytest.raises(llm.LlmUnavailable):
            llm.complete(_Ctx(enabled=False), "evaluator", [])
    finally:
        llm.set_backend(None)


# ---------------------------------------------------------------- LangGraph park / resume
def _runner(counter, verdict="pass", state_path=None, fail_first=False):
    store, obs = MemoryStore(), MemoryObs()

    def gen(ctx, state, fbs):
        counter["gen"] += 1
        return GenResult(artifact=f"v{len(fbs)}:" + ";".join(f.freeform for f in fbs))

    def ev(ctx, state, g):
        counter["eval"] += 1
        return EvalResult(verdict, 0.9, (Issue("I1", "warn", "file:x", "m"),) if verdict != "pass" else ())

    def last(ctx, state):
        counter["last"] = counter.get("last", 0) + 1
        if fail_first and counter["last"] == 1:
            raise RuntimeError("transient")
        return {"done": state.outputs["s1"]}

    stages = [("s0", lambda c, s: {"ok": 1}),
              ("s1", build_gen_eval_hitl("s1", gen, ev, finalize=lambda c, s, g, e: {"final": g.artifact}, max_iters=3)),
              ("s2", last)]
    factory = lambda st, stage: RunContext(db=None, store=store, obs=obs, run_id=st.run_id, state=st, stage=stage)
    return Runner(stages, factory, obs, state_path=state_path)


def _state(hitl=True):
    st = RunState()
    st.hitl = {"s1": hitl}
    return st


def test_interrupt_parks_and_resume_is_idempotent():
    counter = {"gen": 0, "eval": 0}
    r = _runner(counter)
    st = r.start(_state())
    assert st.status == RunStatus.AWAITING_HUMAN and st.pending["key"] == "s1:iter0"
    assert r.start(st).status == RunStatus.AWAITING_HUMAN  # re-driving a parked run must not regenerate
    assert counter == {"gen": 1, "eval": 1}
    st = r.resume(st.run_id, HumanFeedback(freeform="use bigint"))
    assert st.status == RunStatus.AWAITING_HUMAN and st.pending["key"] == "s1:iter1"
    assert st.pending["artifact"] == "v1:use bigint"
    st = r.resume(st.run_id, HumanFeedback(accepted=True))
    assert st.status == RunStatus.DONE
    assert st.outputs["s2"] == {"done": {"final": "v1:use bigint"}}
    assert counter == {"gen": 2, "eval": 2, "last": 1}  # resume re-ran only the review node


def test_nodes_return_deltas_and_never_mutate_input():
    counter = {"gen": 0, "eval": 0}
    r = _runner(counter)
    st = _state()
    before = st.to_json()
    r.start(st)
    assert st.to_json() == before


def test_checkpoints_survive_a_new_runner(tmp_path):
    path = str(tmp_path / "runs.sqlite")
    counter = {"gen": 0, "eval": 0}
    st = _runner(counter, state_path=path).start(_state())
    r2 = _runner(counter, state_path=path)  # simulates an app restart
    assert [x["run_id"] for x in r2.list_runs()] == [st.run_id]
    assert r2.load(st.run_id).status == RunStatus.AWAITING_HUMAN
    assert r2.resume(st.run_id, HumanFeedback(accepted=True)).status == RunStatus.DONE
    assert counter["gen"] == 1


def test_resume_requires_pending():
    r = _runner({"gen": 0, "eval": 0}, verdict="pass")
    st = r.start(_state(hitl=False))
    assert st.status == RunStatus.DONE
    with pytest.raises(ContractViolation):
        r.resume(st.run_id, HumanFeedback(accepted=True))


def test_abort():
    r = _runner({"gen": 0, "eval": 0})
    st = r.start(_state())
    st = r.resume(st.run_id, HumanFeedback(abort=True, freeform="wrong slice"))
    assert st.status == RunStatus.ABORTED and "wrong slice" in st.error and "s2" not in st.outputs


def test_auto_loop_without_hitl_stops_at_max_iters():
    counter = {"gen": 0, "eval": 0}
    st = _runner(counter, verdict="revise").start(_state(hitl=False))
    assert st.status == RunStatus.DONE and counter["gen"] == 3


def test_stage_failure_is_recorded_and_retryable():
    counter = {"gen": 0, "eval": 0}
    r = _runner(counter, fail_first=True)
    st = r.start(_state(hitl=False))
    assert st.status == RunStatus.FAILED and "transient" in st.error and st.current_stage == "s2"
    st = r.retry(st.run_id)
    assert st.status == RunStatus.DONE and counter["gen"] == 1  # only the failed node re-ran
