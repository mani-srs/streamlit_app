"""Contract tests for the durable call log, query-id capture and cost analytics."""
import os

from agents import master
from core import analytics, llm
from core.persistence import SqliteObs
from core.schemas import HumanFeedback
from core.state import RunStatus, SliceSpec
from tests.contracts.conftest import SAMPLE, FakeDb

# real billed rows from CORTEX_AI_FUNCTIONS_USAGE_HISTORY (claude-4-sonnet, this account, 2026-09-28)
BILLED = [
    {"model": "claude-4-sonnet", "tokens_in": 1829, "tokens_out": 691, "credits": 0.0095112},
    {"model": "claude-4-sonnet", "tokens_in": 2130, "tokens_out": 902, "credits": 0.011952},
    {"model": "claude-4-sonnet", "tokens_in": 667, "tokens_out": 2029, "credits": 0.0194616},
    {"model": "claude-4-sonnet", "tokens_in": 1936, "tokens_out": 668, "credits": 0.0094968},
]


def test_calibration_recovers_billed_rates_and_estimate_matches_billing():
    fit = analytics.calibrate(BILLED)["claude-4-sonnet"]
    assert (fit["input_per_m"], fit["output_per_m"]) == (1.8, 9.0) and fit["max_abs_error"] < 1e-9
    rates = analytics.load_rates({})
    for r in BILLED:
        assert abs(analytics.estimate_credits(r["model"], r["tokens_in"], r["tokens_out"], rates) - r["credits"]) < 1e-9
    assert analytics.estimate_credits("unknown-model", 100, 100, rates) is None
    assert analytics.calibrate(BILLED[:1]) == {}  # one row cannot separate input from output rate


def test_settings_roundtrip_and_rate_override(tmp_path):
    sp = str(tmp_path / "runs.sqlite")
    assert analytics.load_settings(sp) == {}
    analytics.save_settings(sp, {"usd_per_credit": 3.0, "rates_override": {"llama3.1-70b": {"input_per_m": 1, "output_per_m": 2}}})
    s = analytics.load_settings(sp)
    rates = analytics.load_rates(s)
    assert s["usd_per_credit"] == 3.0 and rates["llama3.1-70b"]["output_per_m"] == 2
    assert rates["claude-4-sonnet"]["input_per_m"] == 1.8


def test_sqlite_obs_is_durable_and_matches_billing(tmp_path):
    path = str(tmp_path / "runs.sqlite")
    obs = SqliteObs(path)
    obs.llm_call({"run_id": "r1", "stage": "md_plan", "role": "planner", "model": "claude-4-sonnet",
                  "tokens_in": 1829, "tokens_out": 691, "status": "ok", "query_id": "q1"})
    obs.llm_call({"run_id": "r1", "stage": "md_plan", "role": "planner", "model": "claude-4-sonnet", "status": "error",
                  "error": "boom"})
    obs.node({"run_id": "r1", "stage": "md_plan", "node": "md_plan__gen", "status": "done", "duration_ms": 5})
    again = SqliteObs(path)  # simulates an app restart
    assert [c["query_id"] for c in again.llm_calls("r1")] == ["q1", None]
    assert len(again.nodes("r1")) == 1
    assert again.unbilled_query_ids() == ["q1"]
    assert again.set_billed({"q1": 0.0095112}) == 1
    assert again.unbilled_query_ids() == [] and again.llm_calls("r1")[0]["billed_credits"] == 0.0095112


def test_every_cortex_call_records_its_query_id(tmp_path):
    def backend(db, model, messages, options):
        system = messages[0]["content"]
        if "Return ONLY JSON" in system:
            return '{"verdict":"pass","confidence":0.9,"summary":"ok","issues":[]}', 10, 5, "qid-eval"
        if "migration plan" in system:
            return messages[-1]["content"].split("CONTEXT:\n", 1)[1], 10, 5, "qid-plan"
        return 'x = spark.sql("select 1")\ndbutils.notebook.exit("1")', 7, 3, "qid-tr"

    llm.set_backend(backend)
    try:
        b = master.Backend(db=FakeDb(), state_path=str(tmp_path / "runs.sqlite"))
        st = master.new_run(SliceSpec(database="SALES_DB", schema="CORE", pasted_ddl=SAMPLE), use_llm=True)
        st = master.run(b, st)
        while st.status == RunStatus.AWAITING_HUMAN:
            st = master.resume(b, st, HumanFeedback(accepted=True))
    finally:
        llm.set_backend(None)
    calls = b.obs.llm_calls(st.run_id)
    assert sorted(c["query_id"] for c in calls) == ["qid-eval", "qid-plan", "qid-tr"]
    # the call log lives in the state file next to the LangGraph checkpoints
    assert os.path.exists(str(tmp_path / "runs.sqlite"))

    rates = analytics.load_rates({})
    s = analytics.run_summary(st, analytics.enrich_calls(calls, rates))
    assert s["status"] == "done" and s["objects"] == 12 and s["llm_calls"] == 3
    assert s["mapped"] == 11 and s["ai"] == 1 and s["slots_filled"] == 1
    assert abs(s["llm_est_credits"] - (27 * 1.8 + 13 * 9.0) / 1e6) < 1e-12
    assert s["types"]["TABLE"] == 3 and s["files"] > 10
