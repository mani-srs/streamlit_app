import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from core import llm  # noqa: E402

SAMPLE = open(os.path.join(ROOT, "samples", "sample_slice.sql"), encoding="utf-8").read()


class FakeDb:
    """Stands in for Snowflake: every query goes through the mocked LLM backend only."""

    def query(self, sql, params=None):
        raise AssertionError(f"unexpected SQL in contract tests: {sql[:80]}")


@pytest.fixture
def mock_llm():
    """Mocked complete(): records calls, answers by role/prompt."""
    calls = []

    def backend(db, model, messages, options):
        calls.append({"model": model, "messages": messages})
        system, user = messages[0]["content"], messages[-1]["content"]
        if "Return ONLY JSON" in system:
            return '{"verdict":"pass","confidence":0.9,"summary":"ok","issues":[]}', 10, 5
        if "migration plan" in system:
            return user.split("CONTEXT:\n", 1)[1], 10, 5  # echo the draft (keeps required sections)
        return 'rs = spark.sql(f"SELECT COUNT(*) FROM {TBL}")\ndbutils.notebook.exit(str(rs.collect()[0][0]))', 10, 5

    llm.set_backend(backend)
    yield calls
    llm.set_backend(None)
