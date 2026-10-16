"""Live smoke (read-only): SHOW + GET_DDL on a real schema, then all 7 agents with auto-approve.

  .venv\\Scripts\\python scripts\\live_smoke.py --connection NAME --database DB --schema SCH [--per-type 2] [--llm]
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import master  # noqa: E402
from agents.connection.agent import list_objects  # noqa: E402
from core.db import connect  # noqa: E402
from core.schemas import HumanFeedback  # noqa: E402
from core.state import RunStatus, SliceSpec  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--connection")
ap.add_argument("--database", required=True)
ap.add_argument("--schema", required=True)
ap.add_argument("--per-type", type=int, default=2)
ap.add_argument("--llm", action="store_true")
a = ap.parse_args()

db = connect(a.connection)
objs = list_objects(db, a.database, a.schema)
print("listed:", dict(collections.Counter(o["type"] for o in objs)))
pick, seen = [], collections.Counter()
for o in objs:
    if seen[o["type"]] < a.per_type:
        pick.append(o)
        seen[o["type"]] += 1
print("picked:", [(o["type"], o["name"]) for o in pick])

b = master.Backend(db=db)
st = master.new_run(SliceSpec(database=a.database, schema=a.schema, objects=pick), use_llm=a.llm)
st = master.run(b, st, progress=lambda m: print("  ..", m))
while st.status == RunStatus.AWAITING_HUMAN:
    st = master.resume(b, st, HumanFeedback(accepted=True))
print("status:", st.status, st.error[:1500])
if st.status == RunStatus.DONE:
    std = st.outputs["standardization"]
    print("parsed objects:", len(std["objects"]), "| standardization issues:", [i["message"][:120] for i in std["issues"]])
    cg = st.outputs["cd_codegen"]
    print("origins:", cg["meta"]["origins"], "| slots:", cg["meta"]["slots_filled"], "/", cg["meta"]["slots_total"])
    print("judges:", st.outputs["validation"]["report"]["counts"])
    for i in st.outputs["validation"]["report"]["issues"]:
        if i["severity"] == "error":
            print("  [error]", i["id"], i["ref"], i["message"][:200])
    print("files:", sorted(st.outputs["classifier"]["files"]))
