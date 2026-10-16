"""Headless UI test: sample slice through every page and every review gate (no Snowflake connection)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["SNOWFLAKE_DEFAULT_CONNECTION_NAME"] = "__no_such_connection__"
import tempfile as _tf  # noqa: E402
os.environ["SNOWFLAKE_HOME"] = _tf.mkdtemp()  # no connections.toml: the test never signs in to Snowflake

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(ROOT, "streamlit_app.py")


def fail_on_exc(at, where):
    if at.exception:
        for e in at.exception:
            print(f"EXCEPTION on {where}:", e.message, "\n", "\n".join(e.stack_trace[-8:]))
        raise SystemExit(1)


def test_ui_flow(tmp_path=None):
    import tempfile
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["state_path"] = os.path.join(str(tmp_path or tempfile.mkdtemp()), "runs.sqlite")
    at.run()
    fail_on_exc(at, "new_run")
    assert at.session_state.db is None
    at.segmented_control(key="source_mode").set_value("Sample slice").run()
    fail_on_exc(at, "sample mode")
    start = next(b for b in at.button if b.label == "Start run")
    start.click().run()
    fail_on_exc(at, "start run")
    rid = at.session_state.run_id
    assert rid, "run not created"
    backend = at.session_state.memory_backend
    for gate in range(8):
        st_ = backend.checkpoints.load(rid)
        if st_.status != "awaiting_human":
            break
        at.switch_page("app_pages/review.py").run()
        fail_on_exc(at, f"review gate {st_.pending['stage']}")
        print("gate:", st_.pending["stage"], "| issues table rows:", len(at.get("arrow_data_frame")) or "-")
        approve = next(b for b in at.button if b.label == "Approve and continue")
        approve.click().run()
        fail_on_exc(at, f"approve {st_.pending['stage']}")
    final = backend.checkpoints.load(rid)
    assert final.status == "done", (final.status, final.error)
    for page in ("app_pages/monitor.py", "app_pages/outputs.py", "app_pages/history.py", "app_pages/financial.py",
                 "app_pages/observability.py", "app_pages/review.py"):
        at.switch_page(page).run()
        fail_on_exc(at, page)
        print("page ok:", page)
    at.switch_page("app_pages/history.py").run()
    assert any(m.label == "Runs" and str(m.value) == "1" for m in at.metric), "History should list the run"
    print("UI flow OK, run", rid, "files:", len(final.outputs["classifier"]["files"]))


def test_history_and_financial_render_without_data():
    import tempfile
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["state_path"] = os.path.join(tempfile.mkdtemp(), "runs.sqlite")
    at.run()
    for page in ("app_pages/history.py", "app_pages/financial.py"):
        at.switch_page(page).run()
        fail_on_exc(at, page)
    assert any("No LLM calls recorded yet" in i.value for i in at.info)


if __name__ == "__main__":
    test_ui_flow()
