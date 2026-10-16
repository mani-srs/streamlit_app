"""Contract tests for converters, mappers, judges and the full 7-agent pipeline (mocked LLM)."""
import json
import os

from agents import master
from converters.cron import to_quartz
from converters.ddl_parser import parse_ddl, split_statements
from converters.dialect import NameMapper, TranspileError, snowflake_residue, transpile
from converters.graph_model import build_graph
from converters.types import map_type
from core.schemas import HumanFeedback
from core.state import RunStatus, SliceSpec
from judges import rules
from tests.contracts.conftest import ROOT, SAMPLE, FakeDb

M = NameMapper("SALES_DB", "CORE")


def objs():
    return [parse_ddl(s, "SALES_DB", "CORE") for s in split_statements(SAMPLE)]


def test_parser_covers_all_types():
    types = sorted({o["type"] for o in objs()})
    assert types == ["DYNAMIC_TABLE", "PROCEDURE", "STREAM", "TABLE", "TASK", "VIEW"]
    by = {o["id"]: o for o in objs()}
    t = by["SALES_DB.CORE.RAW_ORDERS"]
    assert [c["name"] for c in t["columns"]][:3] == ["ORDER_ID", "CUSTOMER_ID", "ORDER_TS"]
    assert t["change_tracking"] and t["columns"][0]["nullable"] is False
    assert by["SALES_DB.CORE.DT_ORDER_ENRICHED"]["target_lag"] == "15 minutes"
    assert by["SALES_DB.CORE.T_MERGE_ORDERS"]["after"] == ["SALES_DB.CORE.T_ROOT"]
    p = by["SALES_DB.CORE.SP_REFRESH_STATS"]
    assert p["language"] == "SQL" and "'REPLAY'" in p["body"] and p["args"][0]["name"] == "DAYS"
    assert by["SALES_DB.CORE.SP_JS_AUDIT"]["language"] == "JAVASCRIPT"


def test_quoted_identifiers_keep_case():
    o = parse_ddl('create table "Db"."Sch"."MixedCase" ("colA" int, b varchar)')
    assert o["id"] == "Db.Sch.MixedCase" and [c["name"] for c in o["columns"]] == ["colA", "B"]


def test_transpile_known_constructs_deterministically():
    sql = "select iff(a>1,'x','y'), nvl(b,0), div0(c,d) from sales_db.core.t qualify row_number() over (partition by a order by b)=1"
    out1, out2 = transpile(sql, M).sql, transpile(sql, M).sql
    assert out1 == out2
    assert "IF(" in out1 and "COALESCE(" in out1 and "sales_db.core.t" in out1 and not snowflake_residue(out1)


def test_transpile_refuses_snowflake_only_residue():
    try:
        transpile("select metadata$action from s", M)
        raise AssertionError("expected TranspileError")
    except TranspileError:
        pass


def test_type_map_and_cron():
    assert map_type("NUMBER(12,2)") == ("DECIMAL(12,2)", None)
    assert map_type("INT")[0] == "DECIMAL(38,0)"
    assert map_type("TIMESTAMP_TZ")[1]
    assert map_type("GEOGRAPHY")[0] == "STRING"
    assert to_quartz("USING CRON 0 5 * * 1-5 UTC")[0]["quartz_cron_expression"] == "0 0 5 ? * MON-FRI"
    assert to_quartz("30 MINUTE")[0]["quartz_cron_expression"] == "0 0/30 * * * ?"


def test_cdc_pattern_detected():
    g = build_graph(objs(), M)
    [p] = g["patterns"]
    assert p["stream"] == "SALES_DB.CORE.RAW_ORDERS_STRM" and p["target"] == "SALES_DB.CORE.ORDERS"
    assert p["keys"] == ["ORDER_ID"] and p["has_delete"] and p["complete"]
    assert not any(n["id"].endswith(".SET") for n in g["nodes"])  # MERGE ... UPDATE SET is not a table


def test_judges_catch_violations():
    files = {"a.py": "x = (\n", "b.sql": "SELECT IFF(a, 1, 2) FROM t", "c.sql": "USE ROLE ACCOUNTADMIN;\nSELECT 1",
             "d.py": "__PENDING_3__", "e.json": "{bad", "f.sql": "SELECT pg_sleep(60)"}
    got = {i.id.split("-")[0] for i in rules.run_all({"files": files})}
    assert {"STR002", "STR003", "DET002", "DET003", "POL001"} <= got
    ok = {"g.sql": "SELECT len(a), datepart('YEAR', d), charindex('x', s), decode(k, 1, 'a', 'b')",
          "h.py": "import time\ntime.sleep(5)\nspark.sql('select isnull(a)')"}
    assert not [i for i in rules.run_all({"files": ok}) if i.id.startswith("DET003")]


def test_scripting_block_is_one_statement_and_calls_are_kept():
    ddl = open(os.path.join(ROOT, "samples", "task_refresh_dpc_uat.sql"), encoding="utf-8").read()
    [stmt] = split_statements(ddl)
    task = parse_ddl(stmt)
    assert task["body"].count("CALL") + task["body"].count("call") == 5 and task["body"].rstrip().endswith("END")
    g = build_graph([task], NameMapper("DV_REPLICATION_DPC_UAT", "TOOLS"))
    calls = sorted(e["dst"].split(".")[-1] for e in g["edges"] if e["kind"] == "calls")
    assert calls == ["RECREATE_VIEWS", "SP_CLONE_DV_REPLICATION", "SP_CLONE_UPDATE_PERMISSIONS"]  # SYSTEM$WAIT excluded
    assert split_statements("select 1; begin transaction; select 2; commit;") == \
        ["select 1", "begin transaction", "select 2", "commit"]
    frag = [{"node_id": task["id"], "kind": "notebook", "code": "CALL x.y.sp_clone_dv_replication();", "job_task": None}]
    got = rules.run_all({"objects": [task], "graph": g, "fragments": frag, "files": {}})
    assert sorted(i.message.split(".")[-1].split(" ")[0] for i in got if i.id.startswith("LIN003")) == \
        ["RECREATE_VIEWS", "SP_CLONE_UPDATE_PERMISSIONS"]


def _run(llm_on: bool):
    backend = master.Backend(db=FakeDb() if llm_on else None)
    st = master.new_run(SliceSpec(database="SALES_DB", schema="CORE", pasted_ddl=SAMPLE), use_llm=llm_on)
    st = master.run(backend, st)
    gates = []
    while st.status == RunStatus.AWAITING_HUMAN:
        gates.append(st.pending["stage"])
        st = master.resume(backend, st, HumanFeedback(accepted=True))
    assert st.status == RunStatus.DONE, st.error
    return st, backend, gates


def test_dry_run_zero_llm_calls_and_known_constructs_mapped():
    st, backend, gates = _run(llm_on=False)
    assert gates == ["discovery", "md_plan", "cd_codegen", "classifier"]
    assert backend.obs.llm_calls(st.run_id) == []
    frags = st.outputs["cd_codegen"]["fragments"]
    origin = {f["node_id"]: f["origin"] for f in frags}
    assert origin["SALES_DB.CORE.SP_JS_AUDIT"] == "AI"
    assert all(v == "MAPPED" for k, v in origin.items() if k not in ("SALES_DB.CORE.SP_JS_AUDIT", "jobs"))
    files = st.outputs["classifier"]["files"]
    assert {f"pipeline/{l}.py" for l in ("bronze", "silver", "gold")} <= set(files)
    assert "pipeline/_all_nodes.py" not in files
    assert "dlt.apply_changes(" in files["pipeline/silver.py"]
    counts = st.outputs["validation"]["report"]["counts"]
    assert counts.get("STR001", 0) == 0 and counts.get("DET001", 0) == 0 and counts.get("DET002", 0) == 0
    assert counts.get("STR002") == 1  # the JS procedure slot, unfilled without a model
    assert counts.get("LIN003", 0) == 0  # T_REFRESH_STATS calls SP_REFRESH_STATS via its notebook
    json.loads(files["jobs/core_t_root.json"])


def test_mocked_llm_fills_slots_and_passes_structure(mock_llm):
    st, backend, _ = _run(llm_on=True)
    counts = st.outputs["validation"]["report"]["counts"]
    assert counts.get("STR002", 0) == 0 and counts.get("STR003", 0) == 0 and counts.get("STR001", 0) == 0
    assert "spark.sql" in st.outputs["classifier"]["files"]["notebooks/proc_sp_js_audit.py"]
    roles = sorted({c["role"] for c in backend.obs.llm_calls(st.run_id)})
    assert roles == ["evaluator", "planner", "translator"]
    assert all(c["status"] == "ok" for c in backend.obs.llm_calls(st.run_id))


def test_reviewer_feedback_reaches_translator(mock_llm):
    backend = master.Backend(db=FakeDb())
    st = master.new_run(SliceSpec(database="SALES_DB", schema="CORE", pasted_ddl=SAMPLE), use_llm=True)
    st = master.run(backend, st)
    while st.status == RunStatus.AWAITING_HUMAN and st.pending["stage"] != "cd_codegen":
        st = master.resume(backend, st, HumanFeedback(accepted=True))
    n_before = len(mock_llm)
    st = master.resume(backend, st, HumanFeedback(freeform="use DataFrame API not spark.sql",
                                                  hard_constraints=("must not contain: collect()",)))
    assert st.pending["stage"] == "cd_codegen" and st.pending["iteration"] == 1
    tr = [c for c in mock_llm[n_before:] if "Translate the following" in c["messages"][-1]["content"]]
    assert tr, "translator not re-invoked on revision"
    assert "use DataFrame API" in tr[-1]["messages"][-1]["content"]
    assert "must not contain: collect()" in tr[-1]["messages"][0]["content"]
    # mock ignores the constraint -> post-check must surface it as an error issue
    assert any(i["id"].startswith("HC-") for i in st.pending["eval"]["issues"])


def test_empty_procedure_body_is_an_error_not_an_ai_slot():
    ddl = ("CREATE OR REPLACE PROCEDURE DB.S.P()\nRETURNS TABLE (\"A\" NUMBER(38,0))\nLANGUAGE SQL\n"
           "COMMENT='user-defined procedure'\nEXECUTE AS OWNER\nAS '';")
    backend = master.Backend(db=None)
    st = master.new_run(SliceSpec(database="DB", schema="S", pasted_ddl=ddl), use_llm=False)
    st.hitl = {k: False for k in st.hitl}
    st = master.run(backend, st)
    assert st.status == RunStatus.DONE, st.error
    assert st.outputs["cd_codegen"]["slots"] == []
    assert st.outputs["validation"]["report"]["counts"].get("SRC001") == 1


def test_discovery_exclude_and_classifier_override():
    backend = master.Backend(db=None)
    st = master.new_run(SliceSpec(database="SALES_DB", schema="CORE", pasted_ddl=SAMPLE), use_llm=False)
    st = master.run(backend, st)
    st = master.resume(backend, st, HumanFeedback(freeform="exclude: SALES_DB.CORE.SP_JS_AUDIT"))
    st = master.resume(backend, st, HumanFeedback(accepted=True))  # discovery iter1
    while st.status == RunStatus.AWAITING_HUMAN and st.pending["stage"] != "classifier":
        st = master.resume(backend, st, HumanFeedback(accepted=True))
    st = master.resume(backend, st, HumanFeedback(freeform="layer: SALES_DB.CORE.DT_ORDER_ENRICHED = gold"))
    st = master.resume(backend, st, HumanFeedback(accepted=True))
    assert st.status == RunStatus.DONE
    files = st.outputs["classifier"]["files"]
    assert "notebooks/proc_sp_js_audit.py" not in files
    assert "dt_order_enriched" in files["pipeline/gold.py"] and "dt_order_enriched" not in files["pipeline/silver.py"]
    assert st.outputs["classifier"]["assignments"]["SALES_DB.CORE.DT_ORDER_ENRICHED"]["reason"] == "reviewer override"
